"""Run and supervise the dashboard's external HTTPS route.

Quick routes are temporary; named routes require a Cloudflare tunnel token file
and a dashboard route configured to the same loopback origin.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen


from ..paths import ROOT
SCRIPT = ROOT / "routing.py"
QUICK_URL = re.compile(r"https://([a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com)(?![a-zA-Z0-9._:-])")


def origin_url(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("포트는 1~65535 사이 정수여야 합니다.")
    return f"http://127.0.0.1:{port}"


def extract_url(line):
    match = QUICK_URL.search(line)
    return match.group(0) if match else None


def state_directory():
    path = Path(os.environ.get("DASHBOARD_STATE_DIR", str(ROOT / ".state"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_state(directory, state):
    temporary = directory / f".routing-{os.getpid()}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, directory / "routing.json")
    finally:
        temporary.unlink(missing_ok=True)


def read_state(directory):
    path = directory / "routing.json"
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("라우팅 상태 파일을 확인하세요.")
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("state")
        return state
    except (ValueError, OSError) as exc:
        raise ValueError("라우팅 상태 파일을 읽지 못했습니다.") from exc


def process_identity(pid):
    """Inspect only our own routing manager; never match names or port owners."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return None
        argv = [item.decode() for item in (proc / "cmdline").read_bytes().split(b"\0") if item]
        if len(argv) < 3 or not {"quick", "named"}.intersection(argv[2:]):
            return None
        script = Path(argv[1])
        if not script.is_absolute():
            script = Path(os.readlink(proc / "cwd")) / script
        if script.resolve() != SCRIPT:
            return None
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "start_ticks": fields[19]}
    except (OSError, ValueError, UnicodeError, IndexError):
        return None


def is_manager(state):
    identity = process_identity(state.get("manager_pid"))
    return bool(identity and identity["start_ticks"] == state.get("manager_start_ticks"))


def status(directory):
    state = read_state(directory)
    active = is_manager(state)
    return {**state, "active": active, "url": state.get("url") if active else None,
            "status": state.get("status", "starting") if active else "stopped"}


def stop(directory):
    state = read_state(directory)
    if not is_manager(state):
        print("실행 중인 이 프로젝트의 라우팅 프로세스가 없습니다.")
        return
    pid = state["manager_pid"]
    descriptor = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            descriptor = os.pidfd_open(pid)
            if not is_manager(state):
                raise ValueError("프로세스가 변경되어 종료하지 않았습니다.")
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        else:
            if not is_manager(state):
                raise ValueError("프로세스가 변경되어 종료하지 않았습니다.")
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)
    deadline = time.monotonic() + 15
    while is_manager(state) and time.monotonic() < deadline:
        time.sleep(0.1)
    if is_manager(state):
        raise ValueError("종료 요청을 보냈지만 아직 실행 중입니다. 서비스 상태를 확인하세요.")
    print("이 프로젝트의 외부 라우팅을 종료했습니다.")


def make_logger(directory):
    logger = logging.Logger(f"midprice.routing.{os.getpid()}")
    handler = RotatingFileHandler(directory / "routing.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    os.chmod(directory / "routing.log", 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def redact(line, token=""):
    if token:
        line = line.replace(token, "[redacted]")
    line = re.sub(r"(?i)(token(?:\s*[:=]\s*|\s+))[^\s,;]+", r"\1[redacted]", line)
    return line.rstrip()[:4096]


def command(mode, port, binary):
    base = [str(binary), "tunnel", "--no-autoupdate", "--protocol", "http2", "--metrics", "127.0.0.1:0"]
    token = ""
    if mode == "quick":
        return [*base, "--url", origin_url(port)], token
    token_path = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN_FILE")
    if not token_path:
        raise ValueError("고정 터널은 CLOUDFLARE_TUNNEL_TOKEN_FILE을 설정하세요.")
    path = Path(token_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("터널 토큰 파일을 확인하세요.")
    if path.stat().st_mode & 0o077:
        raise ValueError("터널 토큰 파일 권한을 0600으로 설정하세요.")
    token = path.read_text().strip()
    if not token or "\n" in token:
        raise ValueError("터널 토큰 파일에 토큰 한 줄을 저장하세요.")
    return [*base, "run", "--token-file", str(path)], token


def run(mode, port):
    directory = state_directory()
    lock_file = directory / "routing.lock"
    with lock_file.open("a+") as lock:
        os.chmod(lock_file, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("이 프로젝트의 외부 라우팅이 이미 실행 중입니다.") from exc
        try:
            with urlopen(origin_url(port) + "/api/health", timeout=3) as response:
                health = json.load(response)
            if (not isinstance(health, dict) or health.get("service") not in ("oms-client", "midprice-lab")
                    or health.get("ok") is not True):
                raise ValueError("origin")
        except (OSError, ValueError, URLError) as exc:
            raise ValueError("먼저 로컬 대시보드 서버를 실행하세요.") from exc
        binary = Path(os.environ.get("DASHBOARD_CLOUDFLARED", str(ROOT / ".tools" / "cloudflared"))).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(".tools/cloudflared 실행 파일을 설치하세요.")
        args, token = command(mode, port, binary)
        identity = process_identity(os.getpid())
        if identity is None:
            raise ValueError("routing.py 파일 경로로 실행하세요.")
        logger = make_logger(directory)
        stopping = threading.Event()
        previous = {sig: signal.signal(sig, lambda _sig, _frame: stopping.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
        state = {"mode": mode, "origin": origin_url(port), "status": "starting", "url": None,
                 "manager_pid": os.getpid(), "manager_start_ticks": identity["start_ticks"],
                 "started_at": datetime.now(timezone.utc).isoformat()}
        child = None
        reader = None
        messages = queue.Queue(maxsize=128)
        try:
            child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", errors="replace", start_new_session=True)
            state["child_pid"] = child.pid
            write_state(directory, state)
            def receive():
                for line in iter(lambda: child.stdout.readline(4096), ""):
                    try:
                        messages.put_nowait(line)
                    except queue.Full:
                        pass
            reader = threading.Thread(target=receive, name="routing-output", daemon=True)
            reader.start()
            print("외부 HTTPS 라우팅을 연결하고 있습니다.", flush=True)
            while not stopping.is_set():
                try:
                    line = messages.get(timeout=0.2)
                except queue.Empty:
                    if child.poll() is not None:
                        break
                    continue
                logger.info("%s", redact(line, token))
                url = extract_url(line)
                if url and state.get("url") != url:
                    state.update(url=url, status="connecting")
                    write_state(directory, state)
                    print(url, flush=True)
                if "Registered tunnel connection" in line:
                    state["status"] = "connected"
                    write_state(directory, state)
            if child.poll() not in (None, 0) and not stopping.is_set():
                raise ValueError("터널 연결이 종료됐습니다. .state/routing.log를 확인하세요.")
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if reader:
                reader.join(timeout=1)
            if child is not None and child.stdout:
                child.stdout.close()
            state.update(status="stopped", last_url=state.get("url"), url=None,
                         stopped_at=datetime.now(timezone.utc).isoformat())
            write_state(directory, state)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            for handler in logger.handlers:
                handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["quick", "named", "status", "stop"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        if args.command == "status":
            print(json.dumps(status(state_directory()), ensure_ascii=False, indent=2))
        elif args.command == "stop":
            stop(state_directory())
        else:
            run(args.command, args.port)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
