"""Run the authenticated OMS Client HTTP server or initialize its password."""
import argparse
import getpass
import os
import signal
import sys

from werkzeug.security import generate_password_hash

from .api import create_app
from .paths import ROOT
from .storage.state import StateStore

def _waitress_proxy_options(behind_proxy):
    """Only the local tunnel may supply transport and visitor address headers."""
    if not behind_proxy:
        return {}
    return {
        "trusted_proxy": "127.0.0.1",
        "trusted_proxy_count": 1,
        "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"},
        "clear_untrusted_proxy_headers": True,
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8765")))
    parser.add_argument("--behind-proxy", action="store_true", default=os.environ.get("DASHBOARD_BEHIND_PROXY") == "1", help="로컬 터널의 클라이언트 IP·HTTPS 전달 헤더만 신뢰")
    parser.add_argument("--init-password", action="store_true", help="로그인 비밀번호를 해시로 저장한 뒤 종료")
    parser.add_argument("--password-stdin", action="store_true", help="--init-password와 함께 비밀번호를 stdin 한 줄에서 읽기")
    args = parser.parse_args()
    try:
        if args.init_password:
            if args.password_stdin and sys.stdin.isatty():
                print("Ready for password on stdin (input hidden).", flush=True)
                password = getpass.getpass("")
            elif args.password_stdin:
                password = sys.stdin.readline(4097).rstrip("\r\n")
            else:
                password = getpass.getpass("New dashboard password: ")
            if not 8 <= len(password) <= 4096:
                raise RuntimeError("비밀번호는 8~4,096자로 설정해 주세요.")
            if not args.password_stdin and password != getpass.getpass("Confirm password: "):
                raise RuntimeError("비밀번호가 일치하지 않습니다.")
            store = StateStore(os.environ.get("DASHBOARD_STATE_DIR", str(ROOT / ".state")))
            store.set_setting("password_hash", generate_password_hash(password))
            print("Dashboard password configured.")
            return
        if args.password_stdin:
            raise RuntimeError("--password-stdin은 --init-password와 함께 사용해 주세요.")
        app = create_app({"BEHIND_PROXY": args.behind_proxy})
        try:
            try:
                from waitress import serve
            except ImportError:
                raise RuntimeError("서버 실행에는 waitress가 필요합니다. requirements.txt의 패키지를 설치해 주세요.")
            else:
                def terminate(_signum, _frame):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, terminate)
                serve(app, host=args.host, port=args.port, threads=8, channel_timeout=120,
                      **_waitress_proxy_options(app.config["BEHIND_PROXY"]))
        finally:
            app.extensions["close_services"]()
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
