"""Final-quote buckets retain an actual bid/ask pair and its source time."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.analysis.history import analyze
from oms_client.market_data import parquet
from oms_client.market_data.validation import DataError


class LastQuoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = patch.dict(os.environ, {"DASHBOARD_DATA_ROOT": self.temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)
        self.path = Path(self.temporary.name) / "bithumb/ob/AXIS/2026/10/25/00.parquet"
        self.path.parent.mkdir(parents=True)
        # Physical file order differs from timestamp order. Later invalid quotes
        # must not replace the final valid pair, and empty minutes stay empty.
        self.frame = pd.DataFrame({
            "desktopTime": [self.origin + value for value in [59_900, 1_000, 59_000, 60_000, 61_000, 119_999, 180_000, 181_000]],
            "b1_p": [324, 323, 324, 326, 0, 327, 329, 330],
            "a1_p": [325, 325, 326, 327, 328, 326, 331, 331],
        })
        self.frame.to_parquet(self.path, index=False)

    def query(self, **changes):
        payload = {
            "legs": [{"id": "A", "exchange": "bithumb", "ticker": "AXIS"}],
            "formula": "A", "start": "2026-10-25T09:00:00+09:00",
            "end": "2026-10-25T09:04:00+09:00", "interval": "1min",
            "ewm_span": 1, "price_mode": "last",
        }
        payload.update(changes)
        return analyze(payload)

    def test_last_keeps_real_integer_quotes_and_half_tick_midpoint(self):
        result = self.query()
        leg = result["legs"][0]
        self.assertEqual(leg["bid"], [324, 326, 330])
        self.assertEqual(leg["ask"], [325, 327, 331])
        self.assertEqual(leg["mid"], [324.5, 326.5, 330.5])
        self.assertEqual(result["result"], leg["mid"])
        self.assertTrue(all(value.is_integer() for value in leg["bid"] + leg["ask"]))
        self.assertEqual(result["meta"]["price_mode"], "last")
        self.assertEqual(result["meta"]["rows_read"], 6)
        json.dumps(result, allow_nan=False)

    def test_timestamp_sort_gap_and_original_receipt_time_are_preserved(self):
        result = self.query()
        self.assertEqual(result["times"], [self.origin + value for value in [0, 60_000, 180_000]])
        self.assertEqual(result["legs"][0]["quote_times"], [self.origin + value for value in [59_900, 60_000, 181_000]])
        frame, count = parquet.read_buckets("bithumb", "AXIS", pd.Timestamp("2026-10-25T00:00:00Z"), pd.Timestamp("2026-10-25T00:04:00Z"), 60_000, price_mode="last")
        self.assertEqual(count, 6)
        self.assertEqual(frame["source_rows"].tolist(), [3, 1, 2])

    def test_partial_bucket_uses_start_inclusive_end_exclusive_and_no_carry(self):
        result = self.query(start="2026-10-25T09:00:59+09:00", end="2026-10-25T09:01:00+09:00")
        self.assertEqual(result["legs"][0]["bid"], [324])
        self.assertEqual(result["legs"][0]["quote_times"], [self.origin + 59_900])
        with self.assertRaises(DataError):
            self.query(start="2026-10-25T09:02:00+09:00", end="2026-10-25T09:03:00+09:00")

    def test_same_timestamp_never_constructs_a_pair_from_different_rows(self):
        self.frame = pd.DataFrame({"desktopTime": [self.origin + 59_900] * 2,
                                   "b1_p": [320, 324], "a1_p": [323, 330]})
        self.frame.to_parquet(self.path, index=False)
        result = self.query()
        leg = result["legs"][0]
        pair = (leg["bid"][0], leg["ask"][0])
        self.assertIn(pair, {(320, 323), (324, 330)})
        self.assertEqual(leg["mid"][0], sum(pair) / 2)
        self.assertEqual(leg["quote_times"], [self.origin + 59_900])

    def test_means_remain_fractional_and_default_loader_call_is_compatible(self):
        with patch.object(parquet, "read_buckets", wraps=parquet.read_buckets) as loader:
            explicit = self.query(price_mode="mean")
            self.assertEqual(loader.call_args.kwargs, {})
        payload = {"legs": [{"id": "A", "exchange": "bithumb", "ticker": "AXIS"}],
                   "formula": "A", "start": "2026-10-25T09:00:00+09:00",
                   "end": "2026-10-25T09:04:00+09:00", "interval": "1min", "ewm_span": 1}
        default = analyze(payload)
        self.assertEqual(default, explicit)
        self.assertEqual(default["meta"]["price_mode"], "mean")
        self.assertAlmostEqual(default["legs"][0]["bid"][0], 971 / 3)
        self.assertAlmostEqual(default["legs"][0]["ask"][0], 976 / 3)
        self.assertNotIn("quote_times", default["legs"][0])

    def test_full_data_preserves_quote_times_when_chart_is_sampled(self):
        result = self.query(max_points=2, include_full_data=True)
        self.assertEqual(result["times"], [self.origin, self.origin + 180_000])
        self.assertEqual(result["legs"][0]["quote_times"], [self.origin + 59_900, self.origin + 181_000])
        self.assertEqual(result["full"]["legs"][0]["quote_times"], [self.origin + value for value in [59_900, 60_000, 181_000]])
        self.assertEqual(result["full"]["legs"][0]["bid"], [324, 326, 330])

    def test_utc_and_kst_queries_select_the_same_snapshot(self):
        korean = self.query()
        utc = self.query(start="2026-10-25T00:00:00Z", end="2026-10-25T00:04:00Z")
        self.assertEqual(korean["times"], utc["times"])
        self.assertEqual(korean["legs"], utc["legs"])

    def test_unknown_modes_are_rejected_before_reading_quotes(self):
        for value in ["median", "LAST", True, None, [], {}]:
            with self.subTest(price_mode=value), patch.object(parquet, "read_buckets") as loader:
                with self.assertRaises(DataError):
                    self.query(price_mode=value)
                loader.assert_not_called()
        with patch.object(parquet, "files_for_range") as selector, self.assertRaises(DataError):
            parquet.read_buckets("bithumb", "AXIS", None, None, 60_000, price_mode="wrong")
        selector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
