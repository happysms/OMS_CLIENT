"""Funding snapshots preserve ratios, percent units and missing observations."""
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.history import analyze, catalog
from oms_client.market_data import funding, parquet
from oms_client.market_data.validation import DataError


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = patch.dict(os.environ, {"DASHBOARD_DATA_ROOT": self.temporary.name, "DASHBOARD_COLLECTOR_DATA_ROOT": str(Path(self.temporary.name) / "collector")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temporary.name)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)
        self.path = self.write_funding("binance", "BTC", [0, 10, 60, 180], [-0.0001, 0.0002, 0, 0.0003])
        price_path = self.root / "binance/ob/BTC/2026/10/25/00.parquet"
        price_path.parent.mkdir(parents=True)
        pd.DataFrame({"desktopTime": [self.origin + second * 1000 for second in [0, 60, 120, 180, 240]],
                      "b1_p": [99, 100, 101, 102, 103], "a1_p": [101, 102, 103, 104, 105]}).to_parquet(price_path, index=False)

    def write_funding(self, exchange, ticker, seconds, rates, **columns):
        path = self.root / exchange / "funding" / ticker / "2026/10/25/00.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        times = [self.origin + second * 1000 for second in seconds]
        pd.DataFrame({"desktopTime": times, "serverTime": [time - 50 for time in times],
                      "fundingRate": rates, "nextFundingTime": [3600 - second for second in seconds],
                      **columns}).to_parquet(path, index=False)
        return path

    def query(self, **changes):
        payload = {"legs": [{"id": "A", "exchange": "binance", "ticker": "BTC", "data_type": "funding"}],
                   "formula": "A", "start": "2026-10-25T09:00:00+09:00", "end": "2026-10-25T09:05:00+09:00",
                   "interval": "1min", "price_mode": "last", "ewm_span": 1}
        payload.update(changes)
        return analyze(payload)

    def test_last_snapshot_percent_ratio_and_zero_are_distinct_from_prices(self):
        result = self.query()
        leg = result["legs"][0]
        self.assertEqual(leg["data_type"], "funding")
        self.assertEqual(leg["unit"], "%")
        self.assertEqual(leg["funding_rate"], [0.0002, 0, 0.0003])
        for actual, expected in zip(leg["values"], [0.02, 0, 0.03]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(result["result"], leg["values"])
        self.assertEqual(leg["source_times"], [self.origin + second * 1000 for second in [10, 60, 180]])
        self.assertEqual(leg["server_times"], [time - 50 for time in leg["source_times"]])
        self.assertEqual(leg["next_funding_in_seconds"], [3590, 3540, 3420])
        self.assertFalse(set(leg) & {"bid", "ask", "mid", "currency", "quote_times"})
        self.assertEqual(result["meta"]["rows_read"], 4)
        json.dumps(result, allow_nan=False)

    def test_mean_is_a_ratio_mean_with_last_observation_metadata(self):
        result = self.query(price_mode="mean")
        leg = result["legs"][0]
        self.assertAlmostEqual(leg["funding_rate"][0], 0.00005)
        self.assertAlmostEqual(leg["values"][0], 0.005)
        self.assertEqual(leg["source_times"][0], self.origin + 10_000)
        self.assertEqual(leg["next_funding_in_seconds"][0], 3590)
        self.assertIn("마지막 원본 관측값", " ".join(result["meta"]["warnings"]))

    def test_negative_rates_are_valid_and_invalid_numeric_rates_are_excluded(self):
        self.write_funding("binance", "BTC", [0, 60, 120, 180, 240], [-0.0001, 0, float("inf"), float("nan"), -0.0002])
        result = self.query()
        self.assertEqual(result["times"], [self.origin + second * 1000 for second in [0, 60, 240]])
        self.assertEqual(result["result"], [-0.01, 0, -0.02])
        self.assertEqual(result["meta"]["rows_read"], 3)
        self.assertEqual(result["stats"]["min"], -0.02)

    def test_price_and_funding_same_symbol_have_separate_loads_and_common_buckets(self):
        result = self.query(legs=[{"id": "A", "exchange": "binance", "ticker": "BTC"},
                                  {"id": "B", "exchange": "binance", "ticker": "BTC", "data_type": "funding"}],
                            formula="A * B / 100")
        self.assertEqual(result["times"], [self.origin + second * 1000 for second in [0, 60, 180]])
        self.assertEqual(result["legs"][0]["mid"], [100, 101, 103])
        self.assertEqual(result["legs"][1]["funding_rate"], [0.0002, 0, 0.0003])
        for actual, expected in zip(result["result"], [0.02, 0, 0.0309]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(result["meta"]["rows_read"], 9)
        self.assertIn("이전 값으로 채우지 않습니다", " ".join(result["meta"]["warnings"]))

    def test_funding_only_symbol_and_case_insensitive_colon_symbol(self):
        self.write_funding("hyperliquid", "xyz:ONLY", [0, 60], [-0.0001, 0.0002])
        result = self.query(legs=[{"id": "A", "exchange": "hyperliquid", "ticker": "XYZ:ONLY", "data_type": "funding"}])
        self.assertEqual(result["legs"][0]["ticker"], "xyz:ONLY")
        self.assertEqual(result["result"], [-0.01, 0.02])
        info = catalog()
        self.assertIn("xyz:ONLY", info["funding_exchanges"]["hyperliquid"])
        self.assertNotIn("hyperliquid", info["exchanges"])
        self.assertEqual({item["id"] for item in info["source_types"]}, {"price", "funding", "candle"})

    def test_full_export_keeps_unsampled_rates_and_metadata(self):
        result = self.query(max_points=2, include_full_data=True)
        self.assertEqual(len(result["times"]), 2)
        full = result["full"]["legs"][0]
        self.assertEqual(full["funding_rate"], [0.0002, 0, 0.0003])
        self.assertEqual(len(full["source_times"]), 3)
        self.assertEqual(full["values"], result["full"]["result"])
        json.dumps(result, allow_nan=False)

    def test_only_common_observations_and_functions_no_forward_fill(self):
        self.write_funding("okx", "BTC", [0, 180], [0.0001, 0.0002])
        result = self.query(legs=[{"id": "A", "exchange": "binance", "ticker": "BTC", "data_type": "funding"},
                                  {"id": "B", "exchange": "okx", "ticker": "BTC", "data_type": "funding"}],
                            formula="MA(A - B, 2)")
        self.assertEqual(result["times"], [self.origin, self.origin + 180_000])
        self.assertIsNone(result["result"][0])
        self.assertAlmostEqual(result["result"][1], 0.01)
        self.assertEqual(result["meta"]["window_basis"], "aligned_samples")

    def test_bounds_actual_timestamps_partial_buckets_and_no_data_are_explicit(self):
        self.assertEqual(funding.bounds("binance", "btc"), {
            "exchange": "binance", "ticker": "BTC", "data_type": "funding",
            "start": "2026-10-25T09:00:00+09:00", "end": "2026-10-25T09:03:00+09:00"})
        result = self.query(start="2026-10-25T00:00:01Z", end="2026-10-25T00:00:11Z")
        self.assertEqual(result["legs"][0]["funding_rate"], [0.0002])
        with self.assertRaisesRegex(DataError, "유효한 펀딩비율"):
            self.query(start="2026-10-25T00:02:00Z", end="2026-10-25T00:03:00Z")
        with self.assertRaisesRegex(DataError, "펀딩비율 파일이 없습니다"):
            self.query(start="2026-10-26T00:00:00Z", end="2026-10-26T01:00:00Z")

    def test_metadata_optional_and_remaining_seconds_never_expose_epoch_as_seconds(self):
        pd.DataFrame({"desktopTime": [self.origin], "fundingRate": [0]}).to_parquet(self.path, index=False)
        leg = self.query()["legs"][0]
        self.assertEqual(leg["server_times"], [None])
        self.assertEqual(leg["next_funding_in_seconds"], [None])
        self.write_funding("binance", "BTC", [0, 60, 120], [0, 0, 0],
                           nextFundingTime=[self.origin + 3600000, -1, float("inf")])
        leg = self.query()["legs"][0]
        self.assertEqual(leg["next_funding_in_seconds"], [None, -1, None])

    def test_unknown_types_and_ranges_reject_before_loading_but_unused_legs_stay_unused(self):
        for value in ["fees", None, True, [], {}]:
            with self.subTest(data_type=value), patch.object(funding, "read_buckets") as loader:
                with self.assertRaisesRegex(DataError, "데이터 종류"):
                    self.query(legs=[{"id": "A", "exchange": "binance", "ticker": "BTC", "data_type": value}])
                loader.assert_not_called()
        result = self.query(legs=[{"id": "A", "exchange": "binance", "ticker": "BTC", "data_type": "funding"},
                                  {"id": "B", "exchange": "missing", "ticker": "../secret", "data_type": "unknown"}])
        self.assertEqual(len(result["legs"]), 1)
        for changes in [{"end": "2026-11-26T09:00:00+09:00"},
                        {"end": "2026-10-29T09:00:00+09:00", "interval": "1s"}]:
            with self.subTest(changes=changes), patch.object(funding, "read_buckets") as loader:
                with self.assertRaises(DataError):
                    self.query(**changes)
                loader.assert_not_called()

    def test_symlink_escape_and_unknown_symbols_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            escape = self.root / "binance/funding/ESCAPE"
            escape.symlink_to(outside, target_is_directory=True)
            self.assertNotIn("ESCAPE", funding.list_symbols()["binance"])
            with self.assertRaises(DataError):
                funding.bounds("binance", "ESCAPE")
            leaked_hour = self.path.parent / "01.parquet"
            external_file = Path(outside) / "01.parquet"
            pd.read_parquet(self.path).to_parquet(external_file, index=False)
            leaked_hour.symlink_to(external_file)
            paths = funding.files_for_range("binance", "BTC", pd.Timestamp("2026-10-25T00:00:00Z"), pd.Timestamp("2026-10-25T02:00:00Z"))
            self.assertEqual(paths, [str(self.path)])
        with self.assertRaises(DataError):
            funding.files_for_range("../binance", "BTC", pd.Timestamp("2026-10-25T00:00:00Z"), pd.Timestamp("2026-10-25T01:00:00Z"))

    def test_gzip_history_bounds_and_plain_file_preference(self):
        older = self.root / "binance/funding/BTC/2026/10/24/23.parquet"
        older.parent.mkdir(parents=True)
        pd.DataFrame({"desktopTime": [self.origin - 60_000], "serverTime": [self.origin - 60_050],
                      "fundingRate": [-0.0005], "nextFundingTime": [60]}).to_parquet(older, index=False)
        compressed = older.with_name(older.name + ".gz")
        compressed.write_bytes(gzip.compress(older.read_bytes()))
        older.unlink()
        # A stale compressed sibling must never double-count a plain hour.
        sibling = self.path.with_name(self.path.name + ".gz")
        sibling.write_bytes(gzip.compress(self.path.read_bytes()))
        before = {path: path.read_bytes() for path in [compressed, sibling, self.path]}
        self.assertEqual(funding.bounds("binance", "BTC")["start"], "2026-10-25T08:59:00+09:00")
        result = self.query(start="2026-10-25T08:59:00+09:00")
        self.assertEqual(result["meta"]["rows_read"], 5)
        self.assertEqual(result["legs"][0]["funding_rate"], [-0.0005, 0.0002, 0, 0.0003])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        # Compressed-hour filtering must parse 23.parquet.gz as hour 23.
        self.assertEqual(funding.files_for_range("binance", "BTC", pd.Timestamp("2026-10-24T20:00:00Z"),
                                                pd.Timestamp("2026-10-24T21:00:00Z")), [])

    def test_gzip_corruption_is_a_friendly_source_error(self):
        compressed = self.path.with_name(self.path.name + ".gz")
        compressed.write_bytes(b"not a gzip stream")
        self.path.unlink()
        with self.assertRaises(DataError):
            self.query()
        with self.assertRaises(DataError):
            funding.bounds("binance", "BTC")


if __name__ == "__main__":
    unittest.main()
