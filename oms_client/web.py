"""Authenticated, server-side DuckDB analysis API and dashboard.

Set a password once with ``python server.py --init-password``. The HTTP server
binds to 127.0.0.1 unless explicitly configured; external access should use an
HTTPS reverse proxy or an authenticated tunnel.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, send_from_directory, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

from .features.research import data_service
from .store import StateStore, utc_now


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend"
STATIC_FILES = {"app.js", "workspace.js", "charts.js", "styles.css"}
CONFIG_KEYS = {"legs", "formula", "start", "end", "interval", "ewm_span", "max_points", "unit"}
MODULES = [
    {"id": "data", "label": "데이터 확인", "status": "ready"},
    {"id": "analysis", "label": "분석", "status": "ready"},
    {"id": "backtest", "label": "백테스트", "status": "ready"},
    {"id": "paper", "label": "모의투자", "status": "ready"},
    {"id": "oms", "label": "OMS", "status": "planned"},
]


class TransportAwareSessionInterface(SecureCookieSessionInterface):
    """Keep tunnel cookies HTTPS-only while retaining direct localhost access."""

    def get_cookie_secure(self, app):
        return bool(app.config["SESSION_COOKIE_SECURE"] or request.is_secure)


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

LOGIN_TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>로그인 · OMS Client</title>
<style>html{color-scheme:dark;font-family:system-ui,sans-serif;background:#101518;color:#e8edef}body{margin:0;min-height:100vh;display:grid;place-items:center}.login{width:min(360px,calc(100vw - 72px));padding:34px;background:#171e22;border:1px solid #2a3338;border-radius:10px}.brand{color:#5bdfb7;font-size:11px;letter-spacing:2px}h1{font-size:25px;margin:16px 0 10px}p{color:#94a7b1;font-size:12px;line-height:1.8}label{display:block;font-size:12px;color:#b8c9d1;margin:23px 0 8px}input,button{box-sizing:border-box;width:100%;font:inherit;border-radius:5px;padding:12px}input{background:#101518;border:1px solid #3b4b53;color:#e8edef}button{margin-top:22px;background:#5bdfb7;color:#10382b;border:0;cursor:pointer;font-weight:700}.error{color:#f0b1a4;background:#301f1d;padding:10px;border-radius:4px}small{display:block;color:#728995;font-size:10px;margin-top:22px;line-height:1.7}input:focus-visible,button:focus-visible{outline:2px solid #5bdfb7;outline-offset:3px}</style></head>
<body><main class="login"><div class="brand">OMS CLIENT / SERVER</div><h1>분석 공간에 로그인</h1><p>서버의 실제 호가 데이터로 조회하고,<br>나만의 조합을 분석합니다.</p>
{% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
<form method="post" action="{{ action }}"><input type="hidden" name="csrf_token" value="{{ csrf }}"><label for="password">비밀번호</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus><button type="submit">로그인</button></form>
<small>계정: admin · 비밀번호는 서버에서 설정합니다.</small></main></body></html>"""


def _query_config(payload):
    if not isinstance(payload, dict):
        raise data_service.DataError("분석 조건은 JSON 객체로 보내 주세요.")
    unknown = set(payload) - CONFIG_KEYS
    if unknown:
        raise data_service.DataError("지원하지 않는 분석 조건입니다. 화면의 조회 설정을 확인해 주세요.")
    if not isinstance(payload.get("legs"), list) or not 1 <= len(payload["legs"]) <= 4:
        raise data_service.DataError("가격 소스를 1~4개 선택해 주세요.")
    if not isinstance(payload.get("formula"), str) or not 1 <= len(payload["formula"]) <= 256:
        raise data_service.DataError("조합식을 256자 이내로 입력해 주세요.")
    # Copy and validate JSON numbers before handing work to a background thread.
    try:
        copied = json.loads(json.dumps(payload, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise data_service.DataError("분석 조건에 유효하지 않은 값이 있습니다.") from exc
    if "unit" in copied and (not isinstance(copied["unit"], str) or len(copied["unit"]) > 20):
        raise data_service.DataError("표시 단위를 확인해 주세요.")
    return copied


def _json_object(payload):
    if not isinstance(payload, dict):
        raise data_service.DataError("요청은 JSON 객체로 보내 주세요.")
    try:
        return json.loads(json.dumps(payload, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise data_service.DataError("요청에 유효하지 않은 값이 있습니다.") from exc


def _csv_value(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _write_trades_csv(path, result):
    trades = result.get("trades", [])
    fields = list(dict.fromkeys(key for trade in trades for key in trade))
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            os.chmod(temporary, 0o600)
            writer = csv.writer(stream)
            writer.writerow(fields or ["time", "side", "price", "quantity", "fee", "pnl"])
            for trade in trades:
                writer.writerow([_csv_value(trade.get(field)) for field in fields])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_csv(path: Path, result: dict, unit: str):
    complete = result.get("full") or result
    # Labels may be supplied through the API. Keep spreadsheet applications from
    # interpreting a display label as a formula; numeric result columns stay numeric.
    if unit.lstrip().startswith(("=", "+", "-", "@")):
        unit = "'" + unit
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            os.chmod(temporary, 0o600)
            writer = csv.writer(stream)
            writer.writerow(["time_kst", "result", "ema", "unit", *[name for leg in complete["legs"] for name in (f"{leg['id']}_{leg['exchange']}_{leg['ticker']}_mid", f"{leg['id']}_bid", f"{leg['id']}_ask")]])
            for index, timestamp in enumerate(complete["times"]):
                label = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=9))).isoformat()
                writer.writerow([label, complete["result"][index], complete["ewm"][index], unit, *[value for leg in complete["legs"] for value in (leg["mid"][index], leg["bid"][index], leg["ask"][index])]])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class AnalysisJobs:
    def __init__(self, store, analyzer, *, backtester=None, workers=1, max_pending=8, max_cached=16, ttl=3600, max_csv_bytes=256 * 1024 * 1024):
        self.store, self.analyzer = store, analyzer
        self.backtester = backtester
        self.executor = ThreadPoolExecutor(max_workers=max(1, min(2, workers)), thread_name_prefix="analysis")
        self.slots = threading.BoundedSemaphore(max_pending)
        self.jobs = OrderedDict()
        self.lock = threading.RLock()
        self.max_cached, self.ttl, self.max_csv_bytes = max_cached, ttl, max_csv_bytes
        self.directory = store.directory / "results"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.closed = False
        # Result files are only a cache. Avoid retaining orphaned exports after restart.
        for path in self.directory.glob("*.csv"):
            if path.is_file() and not path.is_symlink():
                path.unlink()
        for path in self.directory.glob("*.tmp"):
            if path.is_file() and not path.is_symlink():
                path.unlink()

    @staticmethod
    def metadata(job):
        return {key: value for key, value in job.items() if key not in {"result", "future", "csv_path", "finished_monotonic"}}

    def _prune(self):
        now = time.monotonic()
        completed = [(key, job) for key, job in self.jobs.items() if job["status"] in {"complete", "failed"}]
        byte_count = sum(job.get("csv_bytes", 0) for _, job in completed)
        for key, job in completed:
            expired = now - job.get("finished_monotonic", now) >= self.ttl
            if expired or len(completed) > self.max_cached or byte_count > self.max_csv_bytes:
                path = job.get("csv_path")
                if path:
                    Path(path).unlink(missing_ok=True)
                byte_count -= job.get("csv_bytes", 0)
                self.jobs.pop(key, None)
                completed = [(k, j) for k, j in completed if k != key]

    def submit(self, config, *, kind="analysis"):
        if kind not in {"analysis", "backtest"}:
            raise ValueError("지원하지 않는 실행 종류입니다.")
        if self.closed or not self.slots.acquire(blocking=False):
            raise OverflowError("조회 요청이 많습니다. 현재 분석이 끝난 뒤 다시 실행해 주세요.")
        run_id = secrets.token_hex(16)
        now = utc_now()
        job = {"id": run_id, "kind": kind, "status": "queued", "config": config, "created_at": now, "started_at": None, "completed_at": None, "error": None, "result_available": False, "progress": 0}
        with self.lock:
            self._prune()
            self.jobs[run_id] = job
            self.store.save_run(self.metadata(job))
        try:
            future = self.executor.submit(self._run, run_id)
            with self.lock:
                if run_id in self.jobs:
                    self.jobs[run_id]["future"] = future
        except Exception:
            self.slots.release()
            with self.lock:
                self.jobs.pop(run_id, None)
            raise
        return {"id": run_id, "status": "queued"}

    def _run(self, run_id):
        try:
            with self.lock:
                job = self.jobs[run_id]
                job.update(status="running", started_at=utc_now(), progress=0.1)
                self.store.save_run(self.metadata(job))
            config = job["config"]
            if job["kind"] == "backtest":
                backtester = self.backtester
                if backtester is None:
                    from .features.backtesting import run_backtest
                    backtester = run_backtest
                result = backtester(config)
            else:
                requested = {key: value for key, value in config.items() if key != "unit"}
                requested["include_full_data"] = True
                result = self.analyzer(requested)
            csv_path = self.directory / f"{run_id}.csv"
            if job["kind"] == "backtest":
                _write_trades_csv(csv_path, result)
            else:
                _write_csv(csv_path, result, config.get("unit", ""))
            result.pop("full", None)
            with self.lock:
                job.update(status="complete", completed_at=utc_now(), result=result, result_available=True, progress=1, csv_path=str(csv_path), csv_bytes=csv_path.stat().st_size, finished_monotonic=time.monotonic())
                self.store.save_run(self.metadata(job))
                self._prune()
        except Exception as exc:
            message = str(exc) if isinstance(exc, (data_service.DataError, ValueError)) else "분석 중 오류가 발생했습니다. 서버 로그를 확인해 주세요."
            if not isinstance(exc, (data_service.DataError, ValueError)):
                logging.getLogger(__name__).exception("Analysis job failed: %s", run_id)
            with self.lock:
                job = self.jobs.get(run_id)
                if job:
                    job.update(status="failed", completed_at=utc_now(), error=message, result_available=False, progress=1, finished_monotonic=time.monotonic())
                    self.store.save_run(self.metadata(job))
                    self._prune()
        finally:
            self.slots.release()

    def get(self, run_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(run_id)
            if job:
                metadata = self.metadata(job)
                if job.get("result") is not None:
                    metadata["result"] = job["result"]
                return metadata
        metadata = self.store.run(run_id)
        if metadata:
            metadata["result_available"] = False
            if metadata["status"] in {"queued", "running"}:
                metadata.update(status="failed", error="서버가 재시작되어 분석이 중단됐습니다. 같은 조건으로 다시 실행해 주세요.")
            return metadata
        raise KeyError(run_id)

    def csv(self, run_id):
        with self.lock:
            self._prune()
            job = self.jobs.get(run_id)
            if job and job["status"] == "complete" and job.get("csv_path"):
                path = Path(job["csv_path"])
                if path.is_file():
                    return path
        if self.store.run(run_id):
            raise ValueError("결과 보관 시간이 지났거나 분석이 끝나지 않았습니다. 다시 분석한 뒤 내보내 주세요.")
        raise KeyError(run_id)

    def history(self):
        rows = self.store.history()
        with self.lock:
            self._prune()
            available = {key for key, job in self.jobs.items() if job.get("result") is not None}
        for row in rows:
            row["result_available"] = row["id"] in available
            if row["status"] in {"queued", "running"} and row["id"] not in self.jobs:
                row.update(status="failed", error="서버가 재시작되어 분석이 중단됐습니다.")
        return rows

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=False)


class LoginLimiter:
    def __init__(self, attempts=8, window=600):
        self.attempts, self.window = attempts, window
        self.failures = OrderedDict()
        self.lock = threading.Lock()

    def blocked(self, address):
        with self.lock:
            queue = self.failures.get(address, deque())
            now = time.monotonic()
            while queue and queue[0] < now - self.window:
                queue.popleft()
            return len(queue) >= self.attempts

    def fail(self, address):
        with self.lock:
            self.failures.setdefault(address, deque()).append(time.monotonic())
            self.failures.move_to_end(address)
            while len(self.failures) > 1024:
                self.failures.popitem(last=False)

    def clear(self, address):
        with self.lock:
            self.failures.pop(address, None)


def create_app(config=None):
    app = Flask(__name__, static_folder=None)
    app.session_interface = TransportAwareSessionInterface()
    trusted_hosts = os.environ.get("DASHBOARD_TRUSTED_HOSTS", "")
    app.config.update(
        STATE_DIR=os.environ.get("DASHBOARD_STATE_DIR", str(ROOT / ".state")),
        PASSWORD=os.environ.get("DASHBOARD_PASSWORD"),
        PASSWORD_HASH=os.environ.get("DASHBOARD_PASSWORD_HASH"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_NAME="midprice_session",
        SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_SECURE_COOKIES") == "1",
        BEHIND_PROXY=os.environ.get("DASHBOARD_BEHIND_PROXY") == "1",
        TRUSTED_HOSTS=[host.strip() for host in trusted_hosts.split(",") if host.strip()] or None,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=64 * 1024,
        JOB_WORKERS=1, JOB_MAX_PENDING=8, JOB_MAX_CACHED=16, JOB_TTL=3600,
        JOB_MAX_CSV_BYTES=256 * 1024 * 1024,
    )
    if config:
        app.config.update(config)
    if app.config["BEHIND_PROXY"] and app.config["TRUSTED_HOSTS"] is None:
        app.config["TRUSTED_HOSTS"] = ["localhost", "127.0.0.1", ".trycloudflare.com"]
    store = StateStore(app.config["STATE_DIR"])
    password_hash = app.config.get("PASSWORD_HASH")
    if app.config.get("PASSWORD"):
        if len(app.config["PASSWORD"]) < 8:
            raise RuntimeError("대시보드 비밀번호는 8자 이상으로 설정해 주세요.")
        password_hash = generate_password_hash(app.config["PASSWORD"])
    if password_hash:
        store.set_setting("password_hash", password_hash)
    password_hash = password_hash or store.setting("password_hash")
    if not password_hash:
        raise RuntimeError("먼저 server.py --init-password로 로그인 비밀번호를 설정해 주세요.")
    app.config["PASSWORD"] = None
    app.config["SECRET_KEY"] = app.config.get("SECRET_KEY") or store.session_secret()
    auth_version = hashlib.sha256(password_hash.encode()).hexdigest()
    jobs = AnalysisJobs(store, app.config.get("ANALYZER", data_service.analyze), backtester=app.config.get("BACKTESTER"), workers=app.config["JOB_WORKERS"], max_pending=app.config["JOB_MAX_PENDING"], max_cached=app.config["JOB_MAX_CACHED"], ttl=app.config["JOB_TTL"], max_csv_bytes=app.config["JOB_MAX_CSV_BYTES"])
    limiter = LoginLimiter()
    app.extensions.update(state_store=store, analysis_jobs=jobs, login_limiter=limiter)
    if app.config.get("LIVE_MANAGER") is not None:
        app.extensions["live_manager"] = app.config["LIVE_MANAGER"]
    if app.config.get("PAPER_SERVICE") is not None:
        app.extensions["paper_service"] = app.config["PAPER_SERVICE"]
    live_lock = threading.Lock()
    paper_lock = threading.Lock()

    def paper_service():
        with paper_lock:
            if "paper_service" not in app.extensions:
                from .features.paper import PaperTradingService
                app.extensions["paper_service"] = PaperTradingService(store)
            return app.extensions["paper_service"]

    def live_manager():
        with live_lock:
            if "live_manager" not in app.extensions:
                try:
                    from .features.research.live_service import LiveAnalysisManager
                except ImportError:
                    abort(503, description="실시간 분석 모듈이 아직 준비되지 않았습니다.")
                app.extensions["live_manager"] = LiveAnalysisManager()
            return app.extensions["live_manager"]

    def authenticated():
        return session.get("user") == "admin" and hmac.compare_digest(session.get("auth_version", ""), auth_version)

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    def valid_csrf():
        candidate = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        return bool(candidate and expected and hmac.compare_digest(candidate, expected))

    @app.before_request
    def require_authentication():
        # Host rejection must not be hidden by an unauthenticated redirect.
        _ = request.host
        if request.endpoint in {"login", "health"}:
            return None
        if not authenticated():
            if request.path.startswith("/api/"):
                return jsonify(authenticated=False, error="로그인이 필요합니다."), 401
            return redirect("/login")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin:
                try:
                    source = urlsplit(origin)
                    valid_origin = source.scheme == request.scheme and source.netloc == request.host
                except ValueError:
                    valid_origin = False
                if not valid_origin:
                    return jsonify(error="같은 사이트에서 다시 요청해 주세요."), 403
            if not valid_csrf():
                return jsonify(error="로그인 정보가 갱신됐습니다. 새로고침 후 다시 시도해 주세요."), 403

    @app.after_request
    def private_headers(response):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.errorhandler(data_service.DataError)
    def data_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="조회 설정이 너무 큽니다."), 413

    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(503)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error=error.description), error.code
        return error.get_response()

    @app.get("/api/health")
    def health():
        result = {"ok": True, "service": "oms-client"}
        if os.environ.get("OMS_CLIENT_REVISION"):
            result["revision"] = os.environ["OMS_CLIENT_REVISION"]
        return jsonify(result)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if authenticated():
            return redirect("/")
        error, code = None, 200
        token = csrf_token()
        if request.method == "POST":
            address = request.remote_addr or "unknown"
            if not valid_csrf():
                error, code = "로그인 화면을 새로고침한 뒤 다시 시도해 주세요.", 403
            elif limiter.blocked(address):
                error, code = "로그인 시도가 많습니다. 10분 뒤 다시 시도해 주세요.", 429
            elif not check_password_hash(password_hash, request.form.get("password", "")[:4096]):
                limiter.fail(address)
                error, code = "비밀번호를 확인해 주세요.", 401
            else:
                limiter.clear(address)
                session.clear()
                session.update(user="admin", auth_version=auth_version, csrf_token=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect("/")
        return render_template_string(LOGIN_TEMPLATE, error=error, csrf=token, action="/login"), code

    @app.post("/logout")
    def logout():
        manager = app.extensions.get("live_manager")
        if manager:
            manager.close()
            app.extensions.pop("live_manager", None)
        session.clear()
        if request.is_json or request.headers.get("X-CSRF-Token"):
            return jsonify(ok=True)
        return redirect("/login")

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/<path:filename>")
    def static_file(filename):
        if filename not in STATIC_FILES:
            abort(404)
        return send_from_directory(STATIC, filename)

    @app.get("/api/session")
    def session_information():
        return jsonify(authenticated=True, user="admin", csrf_token=csrf_token(), mode="server", capabilities={"server_analysis": True, "live": True, "saved_presets": True, "backtest": True, "paper": True, "live_orders": False})

    @app.get("/api/version")
    def version_information():
        return jsonify(application="OMS_CLIENT", revision=os.environ.get("OMS_CLIENT_REVISION") or "development", modules=MODULES)

    @app.get("/api/modules")
    def modules():
        return jsonify(items=MODULES)

    @app.get("/api/oms/status")
    def oms_status():
        return jsonify(status="planned", connected=False, live_orders=False,
                       message="OMS 연결과 실주문 제어는 이후 모듈로 추가됩니다.")

    @app.get("/api/catalog")
    def catalog():
        result = data_service.catalog()
        result["mode"] = "server"
        return jsonify(result)

    @app.get("/api/bounds")
    def bounds():
        return jsonify(data_service.bounds(request.args.get("exchange"), request.args.get("ticker")))

    @app.post("/api/analyses")
    def create_analysis():
        config = _query_config(request.get_json(silent=True))
        try:
            return jsonify(jobs.submit(config)), 202
        except OverflowError as error:
            return jsonify(error=str(error)), 429

    @app.post("/api/backtests")
    def create_backtest():
        config = _json_object(request.get_json(silent=True))
        try:
            return jsonify(jobs.submit(config, kind="backtest")), 202
        except OverflowError as error:
            return jsonify(error=str(error)), 429

    @app.get("/api/backtests/<run_id>")
    def get_backtest(run_id):
        try:
            result = jobs.get(run_id)
            if result.get("kind", "analysis") != "backtest":
                raise KeyError(run_id)
            return jsonify(result)
        except KeyError:
            return jsonify(error="백테스트 기록을 찾지 못했습니다."), 404

    @app.get("/api/backtests/<run_id>/csv")
    def export_backtest(run_id):
        try:
            if jobs.get(run_id).get("kind", "analysis") != "backtest":
                raise KeyError(run_id)
            return send_file(jobs.csv(run_id), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"backtest-trades-{run_id}.csv")
        except KeyError:
            return jsonify(error="백테스트 기록을 찾지 못했습니다."), 404
        except (ValueError, FileNotFoundError):
            return jsonify(error="결과 보관 시간이 지났거나 백테스트가 끝나지 않았습니다. 다시 실행한 뒤 내보내 주세요."), 410

    @app.get("/api/paper")
    def paper_snapshot():
        try:
            return jsonify(paper_service().snapshot())
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/orders")
    def paper_order():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().place_order(payload)), 201
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.post("/api/paper/reset")
    def paper_reset():
        payload = _json_object(request.get_json(silent=True))
        try:
            return jsonify(paper_service().reset(payload)), 200
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except RuntimeError as error:
            return jsonify(error=str(error)), 409

    @app.get("/api/analyses/<run_id>")
    def get_analysis(run_id):
        try:
            result = jobs.get(run_id)
            if result.get("kind", "analysis") != "analysis":
                raise KeyError(run_id)
            return jsonify(result)
        except KeyError:
            return jsonify(error="분석 기록을 찾지 못했습니다."), 404

    @app.get("/api/analyses/<run_id>/csv")
    def export_analysis(run_id):
        try:
            if jobs.get(run_id).get("kind", "analysis") != "analysis":
                raise KeyError(run_id)
            return send_file(jobs.csv(run_id), mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"midprice-{run_id}.csv")
        except KeyError:
            return jsonify(error="분석 기록을 찾지 못했습니다."), 404
        except ValueError as error:
            return jsonify(error=str(error)), 410
        except FileNotFoundError:
            return jsonify(error="결과 보관 시간이 지났습니다. 다시 분석한 뒤 내보내 주세요."), 410

    @app.get("/api/history")
    def history():
        return jsonify(items=jobs.history())

    @app.route("/api/presets", methods=["GET", "POST"])
    def presets():
        if request.method == "GET":
            return jsonify(items=store.presets())
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not 1 <= len(payload["name"].strip()) <= 80:
            return jsonify(error="조합 이름을 1~80자로 입력해 주세요."), 400
        if payload.get("id") is not None and (not isinstance(payload["id"], str) or not re.fullmatch(r"[a-f0-9]{24}", payload["id"])):
            return jsonify(error="저장한 조합 ID를 확인해 주세요."), 400
        config = _query_config(payload.get("config"))
        try:
            return jsonify(store.save_preset(payload["name"].strip(), config, payload.get("id"))), 201
        except KeyError:
            return jsonify(error="저장한 조합을 찾지 못했습니다."), 404
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @app.delete("/api/presets/<preset_id>")
    def delete_preset(preset_id):
        try:
            store.delete_preset(preset_id)
            return jsonify(ok=True)
        except KeyError:
            return jsonify(error="저장한 조합을 찾지 못했습니다."), 404

    @app.post("/api/live")
    def create_live():
        config = _query_config(request.get_json(silent=True))
        return jsonify(id=live_manager().start(config), status="running"), 202

    @app.get("/api/live/<live_id>")
    def get_live(live_id):
        try:
            return jsonify(live_manager().get(live_id))
        except KeyError:
            return jsonify(error="실시간 분석을 찾지 못했습니다."), 404

    @app.delete("/api/live/<live_id>")
    def stop_live(live_id):
        try:
            live_manager().stop(live_id)
            return jsonify(ok=True)
        except KeyError:
            return jsonify(error="실시간 분석을 찾지 못했습니다."), 404

    def close_services():
        manager = app.extensions.get("live_manager")
        if manager:
            manager.close()
        paper = app.extensions.get("paper_service")
        if paper and hasattr(paper, "close"):
            paper.close()
        jobs.close()

    app.extensions["close_services"] = close_services
    return app


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


if __name__ == "__main__":
    main()
