import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.market_data import quality
from oms_client.market_data.validation import DataError


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy, self.collector = self.root / "legacy", self.root / "collector"
        environment = patch.dict(os.environ, {"DASHBOARD_DATA_ROOT": str(self.legacy),
                                             "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.collector)})
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)

    def write(self, exchange="bithumb", kind="ob", ticker="BTC", frame=None, hour="00", gzip_only=False):
        root = self.collector if kind == "ob_1s" else self.legacy
        path = root / exchange / kind / ticker / "2026/10/25" / f"{hour}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if frame is None:
            frame = pd.DataFrame({"desktopTime": [self.origin + 1000], "serverTime": [self.origin + 975]})
        frame.to_parquet(path, index=False)
        if gzip_only:
            compressed = Path(str(path) + ".gz")
            compressed.write_bytes(gzip.compress(path.read_bytes()))
            path.unlink()
            return compressed
        return path

    def query(self, **changes):
        payload = {"data_kind": "ob", "exchanges": ["bithumb"], "start": "2026-10-25T00:00:00Z",
                   "end": "2026-10-25T00:01:00Z", "interval": "1s", "threshold_ms": 1000, "max_points": 2000}
        payload.update(changes)
        return quality.query_history(payload)

    def test_catalog_preserves_symbol_case_colons_and_does_not_mix_data_kinds(self):
        self.write()
        self.write("bithumb", "tick", "ETH")
        self.write("okx", "funding", "BTC")
        self.write("bithumb", "ob_1s", "USDT")
        self.write("bithumb", "ob_1s", "BTC")
        self.write("hyperliquid", "ob", "xyz:AAOI")
        self.write("unknown", "ob_1s", "USDT")
        self.write("fx", "fx", "fx")
        result = quality.catalog()
        self.assertEqual({item["id"] for item in result["kinds"]}, {"ob", "tick", "funding", "ob_1s"})
        self.assertTrue(all(item["live_supported"] for item in result["kinds"]))
        self.assertEqual(result["exchanges"]["tick"], {"bithumb": ["ETH"]})
        self.assertEqual(result["exchanges"]["ob_1s"], {"bithumb": ["USDT"]})
        self.assertEqual(result["exchanges"]["ob"]["hyperliquid"], ["xyz:AAOI"])

    def test_signed_raw_pairs_invalid_units_and_invalid_desktop_counts_are_separate(self):
        deltas = [-10, 0, 10, 1000, 2000]
        desktop = [self.origin + index * 1000 for index in range(5)]
        server = [stamp - delta for stamp, delta in zip(desktop, deltas)]
        bad_server = [0, None, float("nan"), self.origin / 1000, self.origin * 1000, self.origin * 1_000_000]
        bad_desktop = [0, None, float("nan"), self.origin / 1000, self.origin * 1000, self.origin * 1_000_000]
        frame = pd.DataFrame({"desktopTime": desktop + [self.origin + 10000 + i for i in range(6)] + bad_desktop,
                              "serverTime": server + bad_server + [self.origin] * 6,
                              "b1_p": [-1] * 17, "a1_p": [-2] * 17})
        self.write(frame=frame)
        result = self.query()
        item = result["series"][0]
        stats = item["stats"]
        self.assertEqual(item["status"], "ok")
        self.assertEqual((stats["rows"], stats["valid_pairs"], stats["invalid_server_rows"], stats["unplaced_desktop_rows"]), (17, 5, 6, 6))
        self.assertEqual(stats["negative_count"], 1)
        self.assertEqual(stats["above_threshold_count"], 1)  # Strictly above, not equal.
        for field, expected in (("mean_ms", 600), ("p50_ms", 10), ("p95_ms", 1800), ("min_ms", -10), ("max_ms", 2000)):
            self.assertAlmostEqual(stats[field], expected)
        self.assertEqual(item["min_ms"][0], -10)
        self.assertEqual(stats["latest_ms"], 2000)
        self.assertEqual(stats["latest_desktop_ms"], self.origin + 4000)
        self.assertEqual(stats["latest_server_ms"], self.origin + 2000)
        self.assertTrue(any("선택된 파일 전체" in warning for warning in result["meta"]["warnings"]))
        json.dumps(result, allow_nan=False)

    def test_fractional_ms_and_raw_p95_are_not_replaced_by_means_or_last_prices(self):
        deltas = [0.25, 0.5, 10.75, 100.5, 1000.25]
        desktop = [self.origin + 100 + index for index in range(5)]
        self.write(frame=pd.DataFrame({"desktopTime": desktop, "serverTime": [stamp - delta for stamp, delta in zip(desktop, deltas)]}))
        item = self.query(interval="1min")["series"][0]
        self.assertEqual(item["count"], [5])
        self.assertAlmostEqual(item["mean_ms"][0], sum(deltas) / 5)
        self.assertAlmostEqual(item["p95_ms"][0], 820.3)
        self.assertAlmostEqual(item["stats"]["p95_ms"], 820.3)

    def test_desktop_filter_is_start_inclusive_end_exclusive_without_filling_buckets(self):
        desktop = [self.origin, self.origin + 1000, self.origin + 3000, self.origin + 60000]
        self.write(frame=pd.DataFrame({"desktopTime": desktop, "serverTime": [stamp - 5 for stamp in desktop]}))
        item = self.query(start="2026-10-25T00:00:01Z")["series"][0]
        self.assertEqual(item["times"], [self.origin + 1000, self.origin + 3000])
        self.assertEqual(item["stats"]["rows"], 2)
        self.assertEqual(item["stats"]["mean_ms"], 5)

    def test_default_btc_and_case_insensitive_explicit_symbol_are_canonical_per_exchange(self):
        self.write(ticker="ETH")
        self.write(ticker="BTC")
        self.write("hyperliquid", ticker="xyz:AAOI")
        result = self.query(exchanges=["bithumb", "hyperliquid"], ticker="")
        self.assertEqual([item["ticker"] for item in result["series"]], ["BTC", "xyz:AAOI"])
        result = self.query(exchanges=["hyperliquid"], ticker="XYZ:aaoi")
        self.assertEqual(result["series"][0]["ticker"], "xyz:AAOI")
        self.assertEqual(result["series"][0]["status"], "ok")

    def test_chart_sampling_keeps_global_positive_and_negative_spikes_and_full_raw_statistics(self):
        deltas = [10] * 30
        deltas[11], deltas[21] = 9000, -8000
        desktop = [self.origin + index * 1000 for index in range(30)]
        self.write(frame=pd.DataFrame({"desktopTime": desktop, "serverTime": [stamp - delta for stamp, delta in zip(desktop, deltas)]}))
        full = self.query()["series"][0]
        sampled = self.query(max_points=2)["series"][0]
        self.assertEqual(sampled["stats"], full["stats"])
        self.assertEqual(sampled["times"], [self.origin + 11000, self.origin + 21000])
        self.assertEqual(sampled["max_ms"], [9000, -8000])
        self.assertEqual(sampled["bucket_count"], 30)

    def test_schema_and_corrupt_file_failures_do_not_block_another_exchange(self):
        self.write(frame=pd.DataFrame({"desktopTime": [self.origin]}))
        self.write("upbit")
        broken = self.write("binance")
        broken.write_bytes(b"corrupt")
        result = self.query(exchanges=["bithumb", "binance", "upbit"])
        statuses = {item["exchange"]: item["status"] for item in result["series"]}
        self.assertEqual(statuses, {"bithumb": "error", "binance": "error", "upbit": "ok"})
        self.assertIn("serverTime", result["series"][0]["error"])

    def test_plain_precedes_gzip_and_compressed_only_sources_are_supported(self):
        path = self.write()
        Path(str(path) + ".gz").write_bytes(gzip.compress(path.read_bytes()))
        self.write("upbit", "tick", gzip_only=True)
        item = self.query()["series"][0]
        self.assertEqual(item["stats"]["rows"], 1)
        compressed = self.query(data_kind="tick", exchanges=["upbit"])["series"][0]
        self.assertEqual(compressed["status"], "ok")
        self.assertEqual(compressed["stats"]["mean_ms"], 25)
        with patch.object(quality, "MAX_ARCHIVE_FILE_BYTES", 128):
            limited = self.query(data_kind="tick", exchanges=["upbit"])["series"][0]
        self.assertEqual(limited["status"], "error")
        self.assertIn("용량 제한", limited["error"])

    def test_selected_source_kind_is_isolated_from_orderbook_precedence_and_other_sources(self):
        self.write(ticker="USDT", frame=pd.DataFrame({"desktopTime": [self.origin + 1000], "serverTime": [self.origin + 990]}))
        self.write(kind="ob_1s", ticker="USDT", frame=pd.DataFrame({"desktopTime": [self.origin + 1000], "serverTime": [self.origin + 900], "receivedTime": [self.origin + 920]}))
        self.write(kind="tick", ticker="USDT", frame=pd.DataFrame({"desktopTime": [self.origin + 1000], "serverTime": [self.origin + 500]}))
        legacy = self.query(ticker="USDT")["series"][0]
        collected = self.query(ticker="USDT", data_kind="ob_1s")
        self.assertEqual(legacy["stats"]["mean_ms"], 10)
        self.assertEqual(collected["series"][0]["stats"]["mean_ms"], 100)
        self.assertEqual(collected["series"][0]["received_mean_ms"], [20])
        self.assertTrue(any("10초" in warning for warning in collected["meta"]["warnings"]))

    def test_synthetic_and_unmeasurable_server_clocks_are_not_zero_latency_measurements(self):
        for exchange, kind in (("okx", "funding"), ("hyperliquid", "funding"), ("ls", "ob")):
            with self.subTest(exchange=exchange):
                self.write(exchange, kind, frame=pd.DataFrame({"desktopTime": [self.origin], "serverTime": [self.origin]}))
                result = self.query(exchanges=[exchange], data_kind=kind)["series"][0]
                self.assertEqual(result["status"], "no_data")
                self.assertIsNone(result["stats"]["mean_ms"])
                self.assertIn("측정할 수 없습니다", result["error"])
                self.assertTrue(quality.unmeasurable_clock_reason(exchange, kind))
        self.write("binance", "funding")
        result = self.query(exchanges=["binance"], data_kind="funding")["series"][0]
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(quality.unmeasurable_clock_reason("binance", "funding"))

    def test_symlink_escape_and_unknown_kind_are_not_browsed_or_read(self):
        target = self.write("upbit")
        linked = self.legacy / "bithumb" / "ob" / "BTC"
        linked.parent.mkdir(parents=True)
        linked.symlink_to(target.parents[3], target_is_directory=True)
        self.assertNotIn("bithumb", quality.catalog()["exchanges"]["ob"])
        result = self.query()["series"][0]
        self.assertEqual(result["status"], "no_data")
        with self.assertRaises(DataError):
            self.query(data_kind="../ob")
        with self.assertRaises(DataError):
            self.query(data_kind="fx")

    def test_query_bounds_and_resource_caps_fail_clearly(self):
        self.write()
        for changes in ({"end": "2026-11-02T00:00:00Z"}, {"end": "2026-10-25T00:00:00Z"},
                        {"end": "2026-10-29T00:00:00Z", "interval": "1s"}, {"max_points": 6001},
                        {"threshold_ms": True}, {"threshold_ms": float("inf")}, {"threshold_ms": -1}, {"threshold_ms": 86_400_001},
                        {"exchanges": []}, {"exchanges": ["bithumb", "bithumb"]}, {"ticker": "../BTC"}, {"ticker": 0}):
            with self.subTest(changes=changes), self.assertRaises(DataError):
                self.query(**changes)
        self.write(frame=pd.DataFrame({"desktopTime": [self.origin + i for i in range(3)], "serverTime": [self.origin] * 3}))
        self.write("upbit")
        with patch.object(quality, "MAX_RAW_ROWS", 2):
            result = self.query(exchanges=["bithumb", "upbit"])
        self.assertEqual([item["status"] for item in result["series"]], ["error", "ok"])
        with patch.object(quality, "MAX_FILES", 1):
            result = self.query(exchanges=["bithumb", "upbit"])
        self.assertEqual([item["status"] for item in result["series"]], ["ok", "error"])


if __name__ == "__main__":
    unittest.main()
