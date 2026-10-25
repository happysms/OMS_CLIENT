"""Server boundary tests: authentication, real jobs, full exports and persistence."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.features.research import data_service
from oms_client.web import create_app, _waitress_proxy_options
from waitress.proxy_headers import proxy_headers_middleware


def result_fixture(payload):
    result = {"times": [1000, 3000], "result": [2.0, 4.0], "ewm": [2.0, 3.0], "mean": 3.0,
              "legs": [{"id": "A", "exchange": "bithumb", "ticker": "CT", "currency": "KRW", "mid": [2.0, 4.0], "bid": [1.5, 3.5], "ask": [2.5, 4.5]}],
              "stats": {"count": 3, "mean": 3.0, "std": 1.0, "min": 2.0, "max": 4.0, "latest": 4.0},
              "meta": {"aligned_points": 3, "returned_points": 2, "formula": payload["formula"], "interval": "1s", "warnings": []}}
    result["full"] = {"times": [1000, 2000, 3000], "result": [2.0, 3.0, 4.0], "ewm": [2.0, 2.5, 3.0],
                      "legs": [{"id": "A", "exchange": "bithumb", "ticker": "CT", "currency": "KRW", "mid": [2.0, 3.0, 4.0], "bid": [1.5, 2.5, 3.5], "ask": [2.5, 3.5, 4.5]}]}
    return result


class FakeLive:
    def __init__(self):
        self.items = {}
        self.closed = False

    def start(self, payload):
        self.items["live-1"] = {"id": "live-1", "status": "running", "result": None, "quotes": [], "connections": [], "missing": [], "error": None}
        return "live-1"

    def get(self, live_id):
        return self.items[live_id]

    def stop(self, live_id):
        self.items[live_id]["status"] = "stopped"

    def close(self):
        self.closed = True


class FakePaper:
    def __init__(self):
        self.orders = []

    def snapshot(self):
        return {"account": {"cash": 1000}, "orders": self.orders}

    def place_order(self, payload):
        if payload.get("side") == "invalid":
            raise ValueError("매수 또는 매도를 선택해 주세요.")
        if payload.get("quantity") == 999:
            raise RuntimeError("모의 잔고가 부족합니다.")
        self.orders.append({**payload, "simulation": True})
        return self.orders[-1]

    def reset(self, payload=None):
        self.orders.clear()
        return self.snapshot()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.live = FakeLive()
        self.paper = FakePaper()
        self.calls = []

        def analyze(payload):
            self.calls.append(payload)
            return result_fixture(payload)

        self.config = {"TESTING": True, "STATE_DIR": self.temp.name, "PASSWORD": "valid-password", "ANALYZER": analyze, "LIVE_MANAGER": self.live, "PAPER_SERVICE": self.paper}
        self.app = create_app(self.config)
        self.addCleanup(lambda: self.app.extensions["close_services"]())
        self.client = self.app.test_client()
        self.token = None

    def login(self, password="valid-password"):
        response = self.client.get("/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        response = self.client.post("/login", data={"csrf_token": match[1], "password": password})
        if response.status_code == 302:
            self.token = self.client.get("/api/session").json["csrf_token"]
        return response

    def headers(self):
        return {"X-CSRF-Token": self.token}

    def query(self):
        return {"legs": [{"id": "A", "exchange": "bithumb", "ticker": "CT"}], "formula": "A", "start": "2026-10-25T09:00", "end": "2026-10-25T10:00", "interval": "1s", "unit": "KRW", "max_points": 2}

    def submit(self):
        response = self.client.post("/api/analyses", json=self.query(), headers=self.headers())
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.json["id"]

    def wait(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/analyses/{run_id}")
            if response.json["status"] in {"complete", "failed"}:
                return response.json
            time.sleep(0.01)
        self.fail("worker did not finish")

    def test_authentication_protects_page_api_and_static_data(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/app.js").status_code, 302)
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json["authenticated"])
        self.assertEqual(self.client.get("/api/health").json, {"ok": True, "service": "oms-client"})
        self.assertEqual(self.login().status_code, 302)
        with self.client.get("/") as page:
            self.assertEqual(page.status_code, 200)
        for path in ["/data/catalog.json", "/.openai/hosting.json", "/../server.py", "/server.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_login_csrf_logout_and_cookie_boundary(self):
        self.assertEqual(self.client.post("/login", data={"password": "valid-password"}).status_code, 403)
        self.assertEqual(self.login("wrong-password").status_code, 401)
        self.assertEqual(self.login().status_code, 302)
        session = self.client.get("/api/session").json
        self.assertEqual(session["mode"], "server")
        self.assertEqual(session["user"], "admin")
        response = self.client.post("/logout", json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/logout", json={}, headers=self.headers()).status_code, 200)
        self.assertTrue(self.live.closed)
        self.assertEqual(self.client.get("/api/session").status_code, 401)

    def test_mutations_require_csrf_and_same_origin(self):
        self.login()
        self.assertEqual(self.client.post("/api/analyses", json=self.query()).status_code, 403)
        headers = {**self.headers(), "Origin": "https://untrusted.example"}
        self.assertEqual(self.client.post("/api/analyses", json=self.query(), headers=headers).status_code, 403)
        headers = {**self.headers(), "Origin": "http://localhost"}
        self.assertEqual(self.client.post("/api/analyses", json=self.query(), headers=headers).status_code, 202)

    def test_job_runs_server_side_and_full_csv_is_not_chart_sample(self):
        self.login()
        run_id = self.submit()
        result = self.wait(run_id)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["config"]["unit"], "KRW")
        self.assertNotIn("full", result["result"])
        self.assertEqual(len(result["result"]["times"]), 2)
        self.assertTrue(self.calls[0]["include_full_data"])
        self.assertNotIn("unit", self.calls[0])
        response = self.client.get(f"/api/analyses/{run_id}/csv")
        self.assertEqual(response.status_code, 200)
        text = response.get_data().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(len(rows), 4)
        self.assertEqual([float(row[1]) for row in rows[1:]], [2.0, 3.0, 4.0])
        self.assertTrue(all(row[0].endswith("+09:00") for row in rows[1:]))
        self.assertIn("attachment", response.headers["Content-Disposition"])
        response.close()
        history = self.client.get("/api/history").json["items"]
        self.assertEqual(history[0]["id"], run_id)
        self.assertNotIn("result", history[0])

    def test_failed_query_is_reported_without_server_trace(self):
        self.login()
        def fail(payload):
            raise data_service.DataError("테스트 기간에 호가가 없습니다.")
        self.app.extensions["analysis_jobs"].analyzer = fail
        result = self.wait(self.submit())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "테스트 기간에 호가가 없습니다.")
        self.assertFalse(result["result_available"])

    def test_queue_is_bounded_and_web_remains_responsive(self):
        self.login()
        entered, release = threading.Event(), threading.Event()
        def blocking(payload):
            entered.set()
            release.wait(5)
            return result_fixture(payload)
        jobs = self.app.extensions["analysis_jobs"]
        jobs.analyzer = blocking
        jobs.slots = threading.BoundedSemaphore(1)
        self.addCleanup(release.set)
        run_id = self.submit()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.client.get("/api/session").status_code, 200)
        response = self.client.post("/api/analyses", json=self.query(), headers=self.headers())
        self.assertEqual(response.status_code, 429)
        release.set()
        self.assertEqual(self.wait(run_id)["status"], "complete")

    def test_saved_presets_survive_reopening_state(self):
        self.login()
        payload = {"name": "CT midpoint", "config": self.query()}
        response = self.client.post("/api/presets", json=payload, headers=self.headers())
        self.assertEqual(response.status_code, 201)
        preset_id = response.json["id"]
        from oms_client.store import StateStore
        reopened = StateStore(self.temp.name)
        self.assertEqual(reopened.presets()[0]["config"], self.query())
        payload.update(id=preset_id, name="Renamed")
        self.assertEqual(self.client.post("/api/presets", json=payload, headers=self.headers()).json["name"], "Renamed")
        self.assertEqual(len(self.client.get("/api/presets").json["items"]), 1)
        self.assertEqual(self.client.delete(f"/api/presets/{preset_id}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get("/api/presets").json["items"], [])

    def test_expired_result_keeps_history_but_does_not_claim_csv_exists(self):
        self.login()
        run_id = self.submit()
        self.assertEqual(self.wait(run_id)["status"], "complete")
        jobs = self.app.extensions["analysis_jobs"]
        jobs.ttl = 0
        result = self.client.get(f"/api/analyses/{run_id}").json
        self.assertFalse(result["result_available"])
        self.assertNotIn("result", result)
        self.assertEqual(self.client.get(f"/api/analyses/{run_id}/csv").status_code, 410)
        self.assertEqual(self.client.get("/api/analyses/unknown").status_code, 404)

    def test_live_routes_delegate_to_server_manager(self):
        self.login()
        response = self.client.post("/api/live", json=self.query(), headers=self.headers())
        self.assertEqual(response.status_code, 202)
        live_id = response.json["id"]
        self.assertEqual(self.client.get(f"/api/live/{live_id}").json["status"], "running")
        self.assertEqual(self.client.delete(f"/api/live/{live_id}", headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get(f"/api/live/{live_id}").json["status"], "stopped")
        self.assertEqual(self.client.get("/api/live/unknown").status_code, 404)

    def test_feature_manifest_and_reserved_oms_have_no_live_order_action(self):
        self.assertEqual(self.client.get("/api/modules").status_code, 401)
        self.login()
        modules = self.client.get("/api/modules").json["items"]
        self.assertEqual([item["id"] for item in modules], ["data", "analysis", "backtest", "paper", "oms"])
        self.assertEqual(modules[-1]["status"], "planned")
        self.assertFalse(self.client.get("/api/oms/status").json["live_orders"])
        self.assertEqual(self.client.post("/api/oms/orders", json={}, headers=self.headers()).status_code, 405)

    def test_application_version_requires_auth_and_reports_deployed_revision(self):
        self.assertEqual(self.client.get("/api/version").status_code, 401)
        self.login()
        with patch.dict(os.environ, {"OMS_CLIENT_REVISION": ""}):
            self.assertEqual(self.client.get("/api/version").json["revision"], "development")
        with patch.dict(os.environ, {"OMS_CLIENT_REVISION": "ab12cd3"}):
            version = self.client.get("/api/version").json
            self.assertEqual(version["application"], "OMS_CLIENT")
            self.assertEqual(version["revision"], "ab12cd3")
            self.assertEqual([item["id"] for item in version["modules"]], ["data", "analysis", "backtest", "paper", "oms"])
            self.assertEqual(self.app.test_client().get("/api/health").json,
                             {"ok": True, "service": "oms-client", "revision": "ab12cd3"})

    def test_backtest_uses_shared_jobs_and_exports_ledger(self):
        self.login()
        calls = []
        def backtest(payload):
            calls.append(payload)
            return {"times": [1000], "result": [1.0], "ewm": [1.0], "legs": [],
                    "metrics": {"return_pct": 1},
                    "trades": [{"timestamp": 1000, "side": "buy", "price": 12, "quantity": 2, "fee": 0.1, "reason": "=untrusted"}]}
        self.app.extensions["analysis_jobs"].backtester = backtest
        payload = {"exchange": "bithumb", "ticker": "BTC", "strategy": "buy_hold"}
        response = self.client.post("/api/backtests", json=payload, headers=self.headers())
        self.assertEqual(response.status_code, 202)
        run_id = response.json["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/backtests/{run_id}").json
            if result["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["kind"], "backtest")
        self.assertEqual(calls, [payload])
        self.assertEqual(self.client.get(f"/api/analyses/{run_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/analyses/{run_id}/csv").status_code, 404)
        export = self.client.get(f"/api/backtests/{run_id}/csv")
        self.assertEqual(export.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(export.get_data().decode("utf-8-sig"))))
        export.close()
        self.assertEqual(rows[0]["price"], "12")
        self.assertEqual(rows[0]["reason"], "'=untrusted")
        self.assertEqual(self.client.get("/api/history").json["items"][0]["kind"], "backtest")

    def test_paper_api_requires_csrf_and_returns_simulated_orders(self):
        self.assertEqual(self.client.get("/api/paper").status_code, 401)
        self.login()
        order = {"exchange": "bithumb", "ticker": "BTC", "side": "buy", "quantity": 1}
        self.assertEqual(self.client.post("/api/paper/orders", json=order).status_code, 403)
        self.assertEqual(self.client.post("/api/backtests", json={}).status_code, 403)
        response = self.client.post("/api/paper/orders", json=order, headers=self.headers())
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json["simulation"])
        self.assertEqual(len(self.client.get("/api/paper").json["orders"]), 1)
        self.assertEqual(self.client.post("/api/paper/orders", json={**order, "side": "invalid"}, headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.post("/api/paper/orders", json={**order, "quantity": 999}, headers=self.headers()).status_code, 409)
        for endpoint in ("/api/paper/orders", "/api/paper/reset", "/api/backtests"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.post(endpoint, json=[], headers=self.headers()).status_code, 400)
                self.assertEqual(self.client.post(endpoint, json={"quantity": float("nan")}, headers=self.headers()).status_code, 400)
        response = self.client.post("/api/paper/reset", json={}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["orders"], [])

    def test_login_attempts_are_limited(self):
        for _ in range(8):
            self.assertEqual(self.login("wrong-password").status_code, 401)
        self.assertEqual(self.login("valid-password").status_code, 429)

    def test_rejects_malformed_or_executable_requests_before_queue(self):
        self.login()
        for payload in [[], {}, {**self.query(), "sql": "DROP TABLE x"}, {**self.query(), "formula": "A" * 257}, {**self.query(), "unit": ["KRW"]}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/analyses", json=payload, headers=self.headers()).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_private_headers_and_state_file_permissions(self):
        self.login()
        response = self.client.get("/api/session")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("connect-src 'self'", response.headers["Content-Security-Policy"])
        path = Path(self.temp.name)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual((path / "dashboard.sqlite3").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("valid-password", (path / "dashboard.sqlite3").read_bytes().decode("latin1"))

    def proxy_app(self, **config):
        app = create_app({**self.config, "BEHIND_PROXY": True, **config})
        self.addCleanup(lambda: app.extensions["close_services"]())
        settings = _waitress_proxy_options(True)
        app.wsgi_app = proxy_headers_middleware(
            app.wsgi_app,
            trusted_proxy=settings["trusted_proxy"],
            trusted_proxy_count=settings["trusted_proxy_count"],
            trusted_proxy_headers=settings["trusted_proxy_headers"],
            clear_untrusted=settings["clear_untrusted_proxy_headers"],
        )
        return app

    def test_cookie_secure_follows_transport_and_local_http_remains_usable(self):
        http = self.client.get("/login")
        self.assertNotIn("; Secure", http.headers["Set-Cookie"])
        https_client = self.app.test_client()
        https = https_client.get("/login", base_url="https://localhost")
        self.assertIn("; Secure", https.headers["Set-Cookie"])
        self.assertIn("HttpOnly", https.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", https.headers["Set-Cookie"])
        self.assertNotIn("Domain=", https.headers["Set-Cookie"])
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get("/api/session").status_code, 200)

    def test_forwarded_headers_are_not_implicitly_trusted_by_flask(self):
        self.app.config["BEHIND_PROXY"] = True
        response = self.client.get("/login", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.42"})
        self.assertNotIn("; Secure", response.headers["Set-Cookie"])

    def test_tunnel_proxy_trusts_only_loopback_and_rightmost_client_hop(self):
        app = self.proxy_app()
        peer = {"REMOTE_ADDR": "127.0.0.1"}
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.9, 203.0.113.42"}
        client = app.test_client()
        response = client.get("/login", base_url="http://qa.trycloudflare.com", headers=headers, environ_overrides=peer)
        self.assertIn("; Secure", response.headers["Set-Cookie"])
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]
        # Login over the effective HTTPS scheme to have the test cookie jar send
        # the Secure cookie, just as a browser does through the public endpoint.
        response = client.post("/login", base_url="https://qa.trycloudflare.com", headers=headers,
                               environ_overrides=peer, data={"csrf_token": csrf, "password": "wrong-password"})
        self.assertEqual(response.status_code, 401)
        limiter = app.extensions["login_limiter"]
        self.assertEqual(list(limiter.failures), ["203.0.113.42"])
        untrusted = app.test_client().get("/login", base_url="http://qa.trycloudflare.com", headers=headers,
                                          environ_overrides={"REMOTE_ADDR": "192.0.2.25"})
        self.assertNotIn("; Secure", untrusted.headers["Set-Cookie"])
        self.assertEqual(_waitress_proxy_options(False), {})

    def test_proxy_host_allowlist_and_same_origin_https_mutation(self):
        app = self.proxy_app()
        client = app.test_client()
        base = "https://qa.trycloudflare.com"
        peer = {"REMOTE_ADDR": "127.0.0.1"}
        proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.42"}
        page = client.get("/login", base_url=base, headers=proxy, environ_overrides=peer)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))[1]
        login = client.post("/login", base_url=base, headers=proxy, environ_overrides=peer,
                            data={"csrf_token": csrf, "password": "valid-password"})
        self.assertEqual(login.status_code, 302)
        token = client.get("/api/session", base_url=base, headers=proxy, environ_overrides=peer).json["csrf_token"]
        headers = {**proxy, "X-CSRF-Token": token, "Origin": base}
        response = client.post("/api/analyses", base_url=base, headers=headers, environ_overrides=peer, json=self.query())
        self.assertEqual(response.status_code, 202)
        for origin in ("http://qa.trycloudflare.com", "https://other.trycloudflare.com", "null", "http://["):
            with self.subTest(origin=origin):
                response = client.post("/api/analyses", base_url=base, headers={**headers, "Origin": origin},
                                       environ_overrides=peer, json=self.query())
                self.assertEqual(response.status_code, 403)
        for path in ("/login", "/api/health", "/api/session"):
            with self.subTest(path=path):
                response = app.test_client().get(path, base_url="https://untrusted.example", headers=proxy, environ_overrides=peer)
                self.assertEqual(response.status_code, 400)

    def test_custom_trusted_hosts_override_quick_tunnel_default(self):
        with patch.dict(os.environ, {"DASHBOARD_BEHIND_PROXY": "1", "DASHBOARD_TRUSTED_HOSTS": " localhost,127.0.0.1, analysis.example.test "}):
            app = create_app({**self.config})
        self.addCleanup(lambda: app.extensions["close_services"]())
        self.assertEqual(app.config["TRUSTED_HOSTS"], ["localhost", "127.0.0.1", "analysis.example.test"])
        self.assertEqual(app.test_client().get("/login", base_url="https://analysis.example.test").status_code, 200)
        self.assertEqual(app.test_client().get("/login", base_url="https://qa.trycloudflare.com").status_code, 400)


if __name__ == "__main__":
    unittest.main()
