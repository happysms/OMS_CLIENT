import base64
import gzip
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from oms_client.market_data import file_browser as browser
from oms_client.market_data.validation import DataError


class FileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.legacy, self.collector = self.directory / "legacy", self.directory / "collector"
        environment = patch.dict(
            os.environ,
            {
                "DASHBOARD_DATA_ROOT": str(self.legacy),
                "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.collector),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(
        self,
        exchange="bithumb",
        kind="ob",
        ticker="BTC",
        suffix=".parquet",
        day="25",
        hour="07",
        table=None,
    ):
        root = self.collector if kind in {"ob_1s", "candles_1m"} else self.legacy
        path = root / exchange / kind / ticker / "2026" / "10"
        path = path / f"{day}{suffix}" if kind == "candles_1m" else path / day / f"{hour}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        table = (
            table
            if table is not None
            else pa.table(
                {
                    "desktopTime": pa.array([1792911600010, 1792911600020], pa.int64()),
                    "b1_p": pa.array([100.5, 101.5], pa.float64()),
                    "a1_p": pa.array([101.5, 102.5], pa.float64()),
                    "raw_string": ["value", "value2"],
                    "raw_flag": [True, False],
                }
            )
        )
        if suffix.endswith(".gz"):
            buffer = pa.BufferOutputStream()
            pq.write_table(table, buffer)
            path.write_bytes(gzip.compress(buffer.getvalue().to_pybytes()))
        else:
            pq.write_table(table, path)
        return path

    def identifier(self, path):
        root = self.collector if path.is_relative_to(self.collector) else self.legacy
        return browser._encode(path.relative_to(root).parts)

    def test_roots_merge_exchanges_and_expose_only_whitelisted_kinds(self):
        self.write()
        self.write(kind="ob_1s", ticker="USDT")
        self.write(kind="candles_1m", ticker="USDT")
        self.write("okx", "funding", "BTC")
        (self.legacy / "bithumb" / "private").mkdir()
        (self.legacy / "error.csv").write_text("hidden")
        (self.collector / ".usdt-orderbook.lock").write_text("hidden")
        root = browser.list_files()
        self.assertEqual([item["name"] for item in root["items"]], ["bithumb", "okx"])
        self.assertTrue(all(item["kind"] == "directory" for item in root["items"]))
        self.assertNotIn(str(self.legacy), json.dumps(root))
        merged = browser.list_files(root["items"][0]["id"])
        self.assertEqual({item["name"] for item in merged["items"]}, {"ob", "ob_1s", "candles_1m"})
        sources = {item["name"]: item["source"] for item in merged["items"]}
        self.assertEqual(sources, {"ob": "legacy", "ob_1s": "collector", "candles_1m": "collector"})

    def test_ticker_search_pagination_and_case_insensitive_colon_symbols(self):
        for ticker in ("ABC", "xyz:AAOI", "BTC", "Xyz:EWY"):
            self.write("hyperliquid", ticker=ticker)
        parent = browser._encode(("hyperliquid", "ob"))
        first = browser.list_files(parent, limit=2)
        second = browser.list_files(parent, offset=first["next_offset"], limit=2)
        self.assertEqual(first["total"], 4)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len({item["id"] for item in [*first["items"], *second["items"]]}), 4)
        searched = browser.list_files(parent, q="XYZ:")
        self.assertEqual({item["ticker"] for item in searched["items"]}, {"xyz:AAOI", "Xyz:EWY"})
        self.assertEqual(browser.list_files(q="does-not-filter-exchanges")["total"], 1)
        self.assertEqual(
            browser.list_files(browser._encode(("hyperliquid", "ob", "BTC")), q="no-match")[
                "total"
            ],
            1,
        )
        self.assertEqual(browser.list_files(parent, offset=100)["items"], [])

    def test_ids_reject_arbitrary_roots_traversal_hidden_names_invalid_dates_and_wrong_depth(self):
        self.write()
        invalid = [
            ("/data", "bithumb", "ob"),
            ("bithumb", "private"),
            ("bithumb", "ob", ".."),
            ("bithumb", "ob", ".env"),
            ("bithumb", "ob", "BTC\\secret"),
            ("bithumb", "ob", "BTC", "2026", "13"),
            ("bithumb", "ob", "BTC", "2026", "02", "31"),
            ("bithumb", "ob", "BTC", "2026", "10", "25", "24.parquet"),
            ("bithumb", "candles_1m", "USDT", "2026", "10", "25", "00.parquet"),
        ]
        for parts in invalid:
            with self.subTest(parts=parts), self.assertRaises(DataError):
                browser.list_files(browser._encode(parts))
        for identifier in (
            "invalid!",
            "..",
            "A" * 2000,
            base64.urlsafe_b64encode(json.dumps({"root": "/etc"}).encode()).decode().rstrip("="),
        ):
            with self.subTest(identifier=identifier[:30]), self.assertRaises(DataError):
                browser.list_files(identifier)

    def test_page_limit_offset_and_search_bounds_are_enforced(self):
        for options in (
            {"limit": 101},
            {"limit": 0},
            {"limit": True},
            {"offset": -1},
            {"offset": 1_000_001},
            {"offset": "1.5"},
            {"q": "a" * 81},
        ):
            with self.subTest(options=options), self.assertRaises(DataError):
                browser.list_files(**options)

    def test_only_valid_partition_files_are_listed(self):
        path = self.write()
        compressed = self.write(suffix=".parquet.gz")
        for name in (
            ".env",
            "07.parquet.tmp",
            "script.py",
            "keys.parquet",
            "07.sqlite",
            "error.csv",
            "24.parquet",
        ):
            (path.parent / name).write_text("hidden")
        result = browser.list_files(self.identifier(path.parent))
        self.assertEqual({item["name"] for item in result["items"]}, {path.name, compressed.name})
        self.assertEqual({item["format"] for item in result["items"]}, {"parquet", "parquet.gz"})
        self.assertTrue(
            all(item["bytes"] > 0 and not item["expandable"] for item in result["items"])
        )

    def test_symlink_files_directories_and_direct_info_requests_cannot_escape(self):
        path = self.write()
        outside = self.directory / "outside"
        outside.mkdir()
        target = outside / "08.parquet"
        target.write_bytes(path.read_bytes())
        symlink_file = path.parent / target.name
        symlink_file.symlink_to(target)
        symlink_ticker = self.legacy / "bithumb" / "ob" / "ESCAPE"
        symlink_ticker.symlink_to(outside, target_is_directory=True)
        files = browser.list_files(self.identifier(path.parent))
        self.assertEqual([item["name"] for item in files["items"]], [path.name])
        tickers = browser.list_files(browser._encode(("bithumb", "ob")))
        self.assertEqual([item["name"] for item in tickers["items"]], ["BTC"])
        with self.assertRaises(DataError):
            browser.file_info(self.identifier(symlink_file))
        with self.assertRaises(DataError):
            browser.list_files(browser._encode(("bithumb", "ob", "ESCAPE")))

    def test_footer_returns_row_count_raw_schema_stats_and_utc_hour_query_without_data_scan(self):
        path = self.write()
        with (
            patch.object(pq, "read_table", side_effect=AssertionError("full scan forbidden")),
            patch.object(pq.ParquetFile, "read", side_effect=AssertionError("full scan forbidden")),
        ):
            result = browser.file_info(self.identifier(path))
        self.assertEqual(result["rows"], 2)
        columns = {field["name"]: field["type"] for field in result["columns"]}
        self.assertEqual(
            columns,
            {
                "desktopTime": "int64",
                "b1_p": "double",
                "a1_p": "double",
                "raw_string": "string",
                "raw_flag": "bool",
            },
        )
        self.assertEqual(
            result["time_stats"]["desktopTime"], {"min_ms": 1792911600010, "max_ms": 1792911600020}
        )
        self.assertEqual(
            result["query"],
            {
                "data_type": "price",
                "exchange": "bithumb",
                "ticker": "BTC",
                "start": "2026-10-25T07:00:00Z",
                "end": "2026-10-25T08:00:00Z",
                "interval": "1min",
            },
        )
        self.assertEqual(result["relative_path"], "bithumb/ob/BTC/2026/10/25/07.parquet")

    def test_collector_price_alias_and_legacy_precedence_do_not_query_another_source(self):
        legacy = self.write(ticker="USDT")
        collected = self.write(kind="ob_1s", ticker="USDT")
        old = browser.file_info(self.identifier(legacy))
        new = browser.file_info(self.identifier(collected))
        self.assertIsNone(old["query"])
        self.assertIn("우선", old["message"])
        self.assertEqual(new["query"]["data_type"], "price")
        self.assertEqual(new["source"], "collector")

    def test_daily_candle_query_uses_close_availability_and_preserves_day_partition_label(self):
        path = self.write(kind="candles_1m", ticker="USDT")
        result = browser.file_info(self.identifier(path))
        self.assertEqual(result["name"], "25.parquet")
        self.assertEqual(
            result["query"],
            {
                "data_type": "candle",
                "exchange": "bithumb",
                "ticker": "USDT",
                "start": "2026-10-25T00:01:00Z",
                "end": "2026-10-26T00:01:00Z",
                "interval": "1min",
            },
        )
        month = browser.list_files(self.identifier(path.parent))
        self.assertEqual(month["items"][0]["kind"], "file")

    def test_empty_files_have_schema_but_are_not_offered_as_queryable_data(self):
        path = self.write(
            table=pa.table(
                {
                    "desktopTime": pa.array([], pa.int64()),
                    "b1_p": pa.array([], pa.float64()),
                    "a1_p": pa.array([], pa.float64()),
                }
            )
        )
        result = browser.file_info(self.identifier(path))
        self.assertEqual(result["rows"], 0)
        self.assertEqual(len(result["columns"]), 3)
        self.assertIsNone(result["query"])
        self.assertIn("행이 없습니다", result["message"])

    def test_funding_gzip_metadata_is_bounded_and_next_funding_seconds_are_not_timestamps(self):
        path = self.write(
            "okx",
            "funding",
            suffix=".parquet.gz",
            table=pa.table(
                {
                    "desktopTime": [1792911600010],
                    "fundingRate": [-0.0001],
                    "nextFundingTime": [3600],
                }
            ),
        )
        result = browser.file_info(self.identifier(path))
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["format"], "parquet.gz")
        self.assertEqual(result["query"]["data_type"], "funding")
        self.assertNotIn("nextFundingTime", result["time_stats"])
        with patch.object(browser, "_GZIP_METADATA_BYTES", 128):
            limited = browser.file_info(self.identifier(path))
        self.assertIsNone(limited["rows"])
        self.assertEqual(limited["columns"], [])
        self.assertEqual(limited["bytes"], path.stat().st_size)
        self.assertIn("용량 제한", limited["message"])

    def test_tick_files_offer_exact_hour_query_for_plain_and_compressed_sources(self):
        for suffix, hour in ((".parquet", "07"), (".parquet.gz", "08")):
            with self.subTest(suffix=suffix):
                path = self.write(
                    "upbit",
                    "tick",
                    "BTC",
                    suffix=suffix,
                    hour=hour,
                    table=pa.table(
                        {
                            "desktopTime": [1792911600010],
                            "serverTime": [1792911600000],
                            "price": [100.5],
                            "qty": [-0.125],
                        }
                    ),
                )
                result = browser.file_info(self.identifier(path))
                self.assertEqual(result["rows"], 1)
                self.assertEqual(
                    result["query"],
                    {
                        "data_type": "tick",
                        "exchange": "upbit",
                        "ticker": "BTC",
                        "start": f"2026-10-25T{hour}:00:00Z",
                        "end": f"2026-10-25T{int(hour) + 1:02d}:00:00Z",
                        "interval": "1s",
                    },
                )
                self.assertEqual(result["data_kind"], "tick")
                self.assertEqual(result["ticker"], "BTC")

    def test_shadowed_compressed_tick_cannot_silently_query_a_different_file(self):
        plain = self.write("binance", "tick")
        compressed = self.write("binance", "tick", suffix=".parquet.gz")
        result = browser.file_info(self.identifier(compressed))
        self.assertIsNone(result["query"])
        self.assertIn("일반 Parquet", result["message"])
        self.assertEqual(browser.file_info(self.identifier(plain))["query"]["data_type"], "tick")

    def test_unsupported_gzip_fx_and_corrupt_metadata_degrade_to_stat_and_message(self):
        for kind, suffix in (("ob", ".parquet.gz"), ("fx", ".parquet")):
            with self.subTest(kind=kind):
                result = browser.file_info(self.identifier(self.write(kind=kind, suffix=suffix)))
                self.assertIsNone(result["query"])
                self.assertTrue(result["message"])
        broken = self.write("upbit")
        broken.write_bytes(b"not-a-parquet-file")
        result = browser.file_info(self.identifier(broken))
        self.assertIsNone(result["rows"])
        self.assertEqual(result["columns"], [])
        self.assertEqual(result["bytes"], 18)
        self.assertIn("메타데이터", result["message"])
        self.assertEqual(browser.list_files(self.identifier(broken.parent))["total"], 1)

    def test_http_routes_use_existing_login_and_validate_bad_ids(self):
        from oms_client.api import create_app
        from oms_client.api.routes import files

        app = create_app(
            {
                "TESTING": True,
                "STATE_DIR": str(self.directory / "state"),
                "PASSWORD": "file-browser-test",
            }
        )
        self.addCleanup(app.extensions["close_services"])
        if not any(rule.rule == "/api/data/files" for rule in app.url_map.iter_rules()):
            files.register(app)
        client = app.test_client()
        self.assertEqual(client.get("/api/data/files").status_code, 401)
        self.assertEqual(client.get("/api/data/files/info?node=invalid!").status_code, 401)
        login = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.get_data(as_text=True))[1]
        self.assertEqual(
            client.post(
                "/login", data={"csrf_token": token, "password": "file-browser-test"}
            ).status_code,
            302,
        )
        path = self.write()
        self.assertEqual(client.get("/api/data/files").status_code, 200)
        response = client.get("/api/data/files/info", query_string={"node": self.identifier(path)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["rows"], 2)
        self.assertEqual(client.get("/api/data/files?limit=101").status_code, 400)
        self.assertEqual(client.get("/api/data/files/info?node=invalid!").status_code, 400)


if __name__ == "__main__":
    unittest.main()
