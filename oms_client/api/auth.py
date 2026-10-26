"""Login, session protection, CSRF, origin checks and private response headers."""

from collections import OrderedDict, deque
import hashlib
import hmac
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import jsonify, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash


class TransportAwareSessionInterface(SecureCookieSessionInterface):
    """Keep tunnel cookies HTTPS-only while retaining direct localhost access."""

    def get_cookie_secure(self, app):
        return bool(app.config["SESSION_COOKIE_SECURE"] or request.is_secure)


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


def configure_auth(app, store):
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
    limiter = LoginLimiter()
    app.extensions["login_limiter"] = limiter

    def authenticated():
        return session.get("user") == "admin" and hmac.compare_digest(
            session.get("auth_version", ""), auth_version
        )

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
                return (
                    jsonify(error="로그인 정보가 갱신됐습니다. 새로고침 후 다시 시도해 주세요."),
                    403,
                )

    @app.after_request
    def private_headers(response):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

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
                session.update(
                    user="admin", auth_version=auth_version, csrf_token=secrets.token_urlsafe(32)
                )
                session.permanent = True
                return redirect("/")
        return render_template("login.html", error=error, csrf=token, action="/login"), code

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

    return csrf_token
