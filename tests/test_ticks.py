"""Trade archive browsing preserves signed rows, bounded output, and auth."""
import csv
import gzip
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.api import create_app
from oms_client.market_data import ticks
from oms_client.market_data.validation import DataError


class TickFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(os.environ, {
            "DASHBOARD_DATA_ROOT": str(self.root),
            "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.root / "collector"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)
        self.path = self.write([0, 200, 1000, 1000, 2000], [100, 102, 101, 101, 103], [1, -2, 3, 3, -4])

    def write(self, offsets, prices, quantities, *, exchange="bithumb", ticker="BTC", day="25", hour="00", **extra):
        path = self.root / exchange / "tick" / ticker / f"2026/10/{day}/{hour}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        times = [self.origin + offset for offset in offsets]
        pd.DataFrame({"desktopTime": times, "serverTime": [value - 10 for value in times],
                      "price": prices, "qty": quantities, **extra}).to_parquet(path, index=False)
        return path

    def config(self, **overrides):
        return {"exchange": "bithumb", "ticker": "BTC", "start": "2026-10-25T09:00:00+09:00",
                "end": "2026-10-25T09:01:00+09:00", "interval": "1s", "price_mode": "last", **overrides}


class TickTests(TickFixture):
    def test_last_mean_signed_volume_and_exact_duplicate_preservation(self):
        result = ticks.query(self.config())
        leg = result["legs"][0]
        self.assertEqual(result["times"], [self.origin, self.origin + 1000, self.origin + 2000])
        self.assertEqual(leg["values"], [102, 101, 103])
        self.assertEqual(leg["trade_count"], [2, 2, 1])
        self.assertEqual(leg["volume"], [3, 6, 4])
        self.assertEqual(leg["buy_volume"], [1, 6, 0])
        self.assertEqual(leg["sell_volume"], [2, 0, 4])
        self.assertEqual(result["stats"]["total_volume"], 13)
        self.assertEqual(result["stats"]["count"], 5)
        self.assertEqual(result["stats"]["latest_price"], 103)
        self.assertEqual([row["side"] for row in result["rows"]], ["sell", "buy", "buy", "sell", "buy"])
        self.assertEqual([row["qty"] for row in result["rows"]], [-4, 3, 3, -2, 1])
        self.assertEqual(ticks.query(self.config(price_mode="mean"))["values"], [101, 101, 103])
        self.assertEqual(leg["quantity_unit"], "BTC")
        self.assertEqual(result["rows"][0]["server_time"], self.origin + 1990)
        json.dumps(result, allow_nan=False)

    def test_range_excludes_end_and_does_not_fill_empty_buckets(self):
        result = ticks.query(self.config(start="2026-10-25T09:00:00.200+09:00", end="2026-10-25T09:00:02+09:00"))
        self.assertEqual(result["values"], [102, 101])
        self.assertEqual(result["stats"]["count"], 3)
        self.write([0, 4000], [100, 104], [1, -1])
        self.assertEqual(ticks.query(self.config())["times"], [self.origin, self.origin + 4000])

    def test_chart_sampling_keeps_full_statistics_and_independent_raw_pagination(self):
        result = ticks.query(self.config(max_points=2, row_limit=2))
        self.assertEqual(len(result["times"]), 2)
        self.assertEqual(result["meta"]["aligned_points"], 3)
        self.assertEqual(result["stats"]["count"], 5)
        self.assertEqual(result["stats"]["total_volume"], 13)
        self.assertTrue(result["meta"]["table_has_more"])
        page2 = ticks.query({**result["config"], "offset": 2})
        page3 = ticks.query({**result["config"], "offset": 4})
        self.assertFalse(page3["meta"]["table_has_more"])
        self.assertEqual(len(result["rows"] + page2["rows"] + page3["rows"]), 5)
        self.assertEqual([row["source_time"] for row in result["rows"] + page2["rows"] + page3["rows"]],
                         [self.origin + offset for offset in [2000, 1000, 1000, 200, 0]])
        self.assertEqual(ticks.query(self.config(offset=100))["rows"], [])

    def test_unverified_collector_keeps_raw_quantity_without_guessed_unit_or_side(self):
        self.write([0, 1000], [100, 101], [2, -4], exchange="okx")
        result = ticks.query(self.config(exchange="okx"))
        self.assertIsNone(result["legs"][0]["quantity_unit"])
        self.assertFalse(result["meta"]["direction_available"])
        self.assertIsNone(result["stats"]["buy_volume"])
        self.assertEqual(result["legs"][0]["buy_volume"], [None, None])
        self.assertEqual([row["side"] for row in result["rows"]], ["unknown", "unknown"])
        self.assertEqual([row["qty"] for row in result["rows"]], [-4, 2])

    def test_invalid_numeric_rows_reported_but_csv_retains_all_columns_and_rows(self):
        self.write([0, 1000, 2000, 3000, 4000, 5000], [100, float("nan"), -1, 101, 102, 103],
                   [1, 2, 3, 0, float("inf"), -2], note=["a", "=SUM(1,2)", "c", "d", "e", "f"])
        result = ticks.query(self.config())
        self.assertEqual(result["meta"]["raw_rows"], 6)
        self.assertEqual(result["meta"]["invalid_rows"], 4)
        self.assertEqual(result["stats"]["count"], 2)
        stream, name, size = ticks.csv_file({**result["config"], "offset": 1, "row_limit": 1, "max_points": 2})
        with stream:
            content = stream.read()
        self.assertEqual(len(content), size)
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("BTC-ticks", name)
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 6)
        self.assertIn("note", rows[0])
        self.assertEqual(rows[1]["note"], "'=SUM(1,2)")
        self.assertEqual(rows[-1]["qty"], "-2.0")
        json.dumps(result, allow_nan=False)

    def test_optional_server_time_and_case_preserving_symbol_lookup(self):
        path = self.write([0], [10], [-1], ticker="xyz:ABC")
        pd.DataFrame({"desktopTime": [self.origin], "price": [10], "qty": [-1]}).to_parquet(path, index=False)
        result = ticks.query(self.config(ticker="XYZ:abc"))
        self.assertEqual(result["config"]["ticker"], "xyz:ABC")
        self.assertEqual(result["rows"][0]["server_time"], None)
        self.assertEqual(ticks.bounds("bithumb", "XYZ:abc")["ticker"], "xyz:ABC")

    def test_unverified_or_non_epoch_server_time_is_not_displayed_as_exchange_date(self):
        self.write([0, 1000], [100, 101], [1, -1], serverTime=[123456, 9999999999999999])
        self.assertEqual([row["server_time"] for row in ticks.query(self.config())["rows"]], [None, None])
        self.write([0], [10], [1], exchange="ls", ticker="005930")
        result = ticks.query(self.config(exchange="ls", ticker="005930"))
        self.assertIsNone(result["rows"][0]["server_time"])
        self.assertTrue(any("LS serverTime" in warning for warning in result["meta"]["warnings"]))

    def test_gzip_history_and_plain_preference_never_duplicate_hours_or_mutate_sources(self):
        earlier = self.write([-1000], [99], [1], day="24", hour="23")
        archived = earlier.with_name(earlier.name + ".gz")
        archived.write_bytes(gzip.compress(earlier.read_bytes()))
        earlier.unlink()
        sibling = self.path.with_name(self.path.name + ".gz")
        sibling.write_bytes(gzip.compress(self.path.read_bytes()))
        before = {path: path.read_bytes() for path in [archived, sibling, self.path]}
        result = ticks.query(self.config(start="2026-10-25T08:59:00+09:00"))
        self.assertEqual(result["stats"]["count"], 6)
        self.assertEqual(result["meta"]["source_files"], 2)
        self.assertEqual(ticks.bounds("bithumb", "btc")["start"], "2026-10-25T08:59:59+09:00")
        self.assertEqual(ticks.bounds("bithumb", "btc")["end"], "2026-10-25T09:00:02+09:00")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_symlinks_escape_rejected_and_hour_filter_applies_to_gzip(self):
        with tempfile.TemporaryDirectory() as outside:
            escaped = self.root / "bithumb/tick/ESCAPE"
            escaped.symlink_to(outside, target_is_directory=True)
            self.assertNotIn("ESCAPE", ticks.list_symbols()["bithumb"])
            leaked = self.path.parent / "01.parquet"
            target = Path(outside) / "01.parquet"
            target.write_bytes(self.path.read_bytes())
            leaked.symlink_to(target)
            selected = ticks.files_for_range("bithumb", "BTC", pd.Timestamp("2026-10-25T00:00:00Z"), pd.Timestamp("2026-10-25T02:00:00Z"))
            self.assertEqual(selected, [str(self.path)])
        for override in [{"ticker": "../BTC"}, {"exchange": "../private"}, {"path": "/etc/passwd"},
                         {"data_type": "price"}, {"max_points": 6001}, {"offset": -1}, {"row_limit": True},
                         {"interval": "5ms"}, {"price_mode": "raw"}, {"end": "2026-10-25T08:59:00+09:00"},
                         {"end": "2026-11-26T09:00:00+09:00"}, {"end": "2026-10-29T09:00:00+09:00"}]:
            with self.subTest(override=override), self.assertRaises(DataError):
                ticks.query(self.config(**override))

    def test_missing_invalid_schema_corrupt_archive_and_row_limits_are_friendly_errors(self):
        with self.assertRaisesRegex(DataError, "파일이 없습니다"):
            ticks.query(self.config(start="2026-10-26T09:00:00+09:00", end="2026-10-26T10:00:00+09:00"))
        with patch.object(ticks, "MAX_READ_ROWS", 4), self.assertRaisesRegex(DataError, "최대 4행"):
            ticks.query(self.config())
        with self.assertRaisesRegex(DataError, "최대 4행"):
            ticks.csv_file(self.config(), max_rows=4)
        with self.assertRaisesRegex(DataError, "크기 한도"):
            ticks.csv_file(self.config(), max_bytes=8)
        pd.DataFrame({"desktopTime": [self.origin]}).to_parquet(self.path, index=False)
        with self.assertRaisesRegex(DataError, "컬럼이 필요"):
            ticks.query(self.config())
        archived = self.path.with_name(self.path.name + ".gz")
        archived.write_bytes(b"invalid gzip")
        self.path.unlink()
        with self.assertRaisesRegex(DataError, "체결 틱 압축"):
            ticks.query(self.config())


class TickApiTests(TickFixture):
    def make_client(self):
        state = self.root / "state"
        self.app = create_app({"TESTING": True, "STATE_DIR": str(state), "PASSWORD": "valid-password"})
        self.addCleanup(self.app.extensions["close_services"])
        self.client = self.app.test_client()
        login = self.client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', login)[1]
        self.client.post("/login", data={"csrf_token": token, "password": "valid-password"})
        self.headers = {"X-CSRF-Token": self.client.get("/api/session").json["csrf_token"]}

    def test_tick_catalog_bounds_query_csv_auth_and_csrf(self):
        self.make_client()
        anonymous = self.app.test_client()
        for suffix in ["", "/csv"]:
            self.assertEqual(anonymous.post("/api/data/ticks" + suffix, json=self.config()).status_code, 401)
            self.assertEqual(self.client.post("/api/data/ticks" + suffix, json=self.config()).status_code, 403)
        self.assertEqual(self.client.get("/api/catalog").json["tick_exchanges"], {"bithumb": ["BTC"]})
        bounds = self.client.get("/api/bounds?data_type=tick&exchange=bithumb&ticker=btc")
        self.assertEqual(bounds.status_code, 200)
        self.assertEqual(bounds.json["data_type"], "tick")
        response = self.client.post("/api/data/ticks", json=self.config(row_limit=1), headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(len(response.json["rows"]), 1)
        with self.client.post("/api/data/ticks/csv", json=response.json["config"], headers=self.headers) as response:
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            rows = list(csv.DictReader(io.StringIO(response.get_data().decode("utf-8-sig"))))
            self.assertEqual(len(rows), 5)
            self.assertIn("ticks", response.headers["Content-Disposition"])
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.post("/api/data/ticks", json=self.config(ticker="../private"), headers=self.headers).status_code, 400)

    def test_tick_api_limits_simultaneous_queries(self):
        self.make_client()
        entered, release = threading.Barrier(3), threading.Event()
        original = ticks.query
        outcomes = []

        def wait_query(payload):
            entered.wait(timeout=5)
            release.wait(timeout=5)
            return original(payload)

        with self.client.session_transaction() as session:
            snapshot = dict(session)

        def run():
            with self.app.test_client() as client:
                with client.session_transaction() as session:
                    session.update(snapshot)
                response = client.post("/api/data/ticks", json=self.config(), headers=self.headers)
                outcomes.append(response.status_code)

        with patch.object(ticks, "query", side_effect=wait_query):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                entered.wait(timeout=5)
                self.assertEqual(self.client.post("/api/data/ticks", json=self.config(), headers=self.headers).status_code, 429)
                self.assertEqual(self.client.get("/api/session").status_code, 200)
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=5)
        self.assertEqual(sorted(outcomes), [200, 200])


if __name__ == "__main__":
    unittest.main()
