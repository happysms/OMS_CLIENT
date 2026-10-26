"""Small parquet fixtures exercise formula safety, timestamps and alignment."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.analysis import history as service
from oms_client.analysis.formulas import FUNCTION_NAMES, formula_ids
from oms_client.analysis.sources import SOURCE_IDS
from oms_client.market_data import parquet
from oms_client.market_data.validation import QueryTimeout
from oms_client.market_data import export as export_history


class DataServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "DASHBOARD_DATA_ROOT": self.temp.name,
                "DASHBOARD_COLLECTOR_DATA_ROOT": str(Path(self.temp.name) / "collector"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)
        self.write("bithumb", "CT", [0, 60, 120, 180, 240], [10, 20, 30, 40, 50])
        self.write("okx", "CT", [0, 60, 180, 240], [2, 4, 8, 10])

    def write(self, exchange, ticker, seconds, mids):
        path = Path(self.temp.name) / exchange / "ob" / ticker / "2026/10/25/00.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(
            {
                "desktopTime": [self.origin + s * 1000 for s in seconds],
                "b1_p": [m - 0.5 for m in mids],
                "a1_p": [m + 0.5 for m in mids],
            }
        )
        frame.to_parquet(path, index=False)

    def request(self, **changes):
        payload = {
            "legs": [
                {"id": "A", "exchange": "bithumb", "ticker": "CT"},
                {"id": "B", "exchange": "okx", "ticker": "CT"},
            ],
            "start": "2026-10-25T09:00:00",
            "end": "2026-10-25T09:05:00",
            "interval": "1min",
            "formula": "A / B",
            "ewm_span": 2,
        }
        payload.update(changes)
        return service.analyze(payload)

    def test_mid_formula_and_missing_bucket_are_real(self):
        result = self.request()
        self.assertEqual(result["times"], [self.origin + s * 1000 for s in [0, 60, 180, 240]])
        self.assertEqual(result["legs"][0]["mid"], [10, 20, 40, 50])
        self.assertEqual(result["result"], [5, 5, 5, 5])
        self.assertEqual(result["meta"]["rows_read"], 9)
        self.assertEqual(result["stats"]["count"], 4)
        self.assertTrue(result["meta"]["warnings"])

    def test_downsample_does_not_change_statistics_or_ema(self):
        complete = self.request(formula="A", max_points=6000)
        sampled = self.request(formula="A", max_points=2)
        self.assertEqual(sampled["stats"], complete["stats"])
        self.assertEqual(sampled["result"], [10, 50])
        self.assertAlmostEqual(sampled["ewm"][-1], complete["ewm"][-1])
        self.assertEqual(sampled["meta"]["aligned_points"], 5)
        self.assertEqual(sampled["meta"]["returned_points"], 2)

    def test_zero_division_is_json_null(self):
        result = self.request(formula="A / (B - B)")
        self.assertEqual(result["result"], [None] * 4)
        self.assertEqual(result["stats"]["count"], 0)
        self.assertIsNone(result["stats"]["mean"])
        json.dumps(result, allow_nan=False)

    def test_bucket_means_use_all_quotes(self):
        self.write("bithumb", "CT", [0, 10, 20, 60], [10, 20, 60, 40])
        result = self.request(
            legs=[{"id": "A", "exchange": "bithumb", "ticker": "CT"}], formula="A"
        )
        self.assertEqual(result["times"], [self.origin, self.origin + 60_000])
        self.assertEqual(result["result"], [30, 40])
        self.assertEqual(result["legs"][0]["bid"], [29.5, 39.5])

    def test_unsafe_and_unsupported_formula_rejected_before_loading(self):
        for formula in [
            "__import__('os').system('true')",
            "A.__class__",
            "A ** 2",
            "A[0]",
            "C / A",
            "True",
            "A // B",
            "1e999",
        ]:
            with self.subTest(formula=formula), self.assertRaises(service.DataError):
                self.request(formula=formula)

    def test_formula_precedence_and_negative_literals(self):
        result = self.request(formula="(A + -2 * B) / B")
        self.assertEqual(result["result"], [3, 3, 3, 3])

    def test_kst_and_utc_requests_are_equivalent(self):
        korean = self.request()
        utc = self.request(start="2026-10-25T00:00:00Z", end="2026-10-25T00:05:00Z")
        self.assertEqual(korean["times"], utc["times"])
        self.assertEqual(korean["result"], utc["result"])

    def test_invalid_inputs_and_missing_data_have_friendly_errors(self):
        changes = [
            {"end": "2026-10-25T09:00:00"},
            {"end": "2026-11-26T00:00:00"},
            {"start": "NaT"},
            {"interval": "15s"},
            {"max_points": 6001},
            {"ewm_span": 0},
            {"legs": [{"id": "A", "exchange": "../okx", "ticker": "CT"}]},
            {"legs": [{"id": [], "exchange": "okx", "ticker": "CT"}]},
            {"start": "2026-10-24T09:00:00", "end": "2026-10-24T10:00:00"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(service.DataError):
                self.request(**change)

    def test_bounds_read_actual_timestamps(self):
        result = parquet.bounds("bithumb", "CT")
        self.assertEqual(result["start"], "2026-10-25T09:00:00+09:00")
        self.assertEqual(result["end"], "2026-10-25T09:04:00+09:00")

    def test_export_keeps_real_bid_ask_and_source_interval(self):
        result = parquet.export_leg(
            "bithumb", "CT", "2026-10-25T09:00:00", "2026-10-25T09:05:00", "1min"
        )
        self.assertEqual(result["mid"], [10, 20, 30, 40, 50])
        self.assertEqual(result["currency"], "KRW")
        self.assertEqual(result["meta"]["interval"], "1min")
        self.assertNotIn("id", result)

    def test_export_retains_more_than_chart_point_limit(self):
        self.write("bithumb", "CT", [i * 60 for i in range(7001)], [i + 1 for i in range(7001)])
        result = parquet.export_leg(
            "bithumb", "CT", "2026-10-25T09:00:00", "2026-10-30T09:00:00", "1min"
        )
        self.assertEqual(len(result["times"]), 7001)
        self.assertEqual(result["mid"][6000], 6001)

    def test_portable_export_files_match_catalog_and_source(self):
        target = Path(self.temp.name) / "bundle"
        with patch("builtins.print"):
            catalog = export_history.export(
                target, days=1, tickers=["CT"], exchanges=["bithumb", "okx", "upbit"]
            )
        loaded_catalog = json.loads((target / "catalog.json").read_text())
        self.assertEqual(loaded_catalog, catalog)
        self.assertEqual(len(catalog["series"]), 2)
        series = json.loads((target / "bithumb-CT.json").read_text())
        self.assertEqual(series["bid"], [9.5, 19.5, 29.5, 39.5, 49.5])
        self.assertEqual(series["ask"], [10.5, 20.5, 30.5, 40.5, 50.5])
        self.assertEqual(series["source"], "parquet")
        self.assertEqual(catalog["series"]["bithumb:CT"]["count"], len(series["times"]))
        self.assertFalse(list(target.glob("*.tmp")))

    def test_catalog_ignores_non_orderbook_directories(self):
        (Path(self.temp.name) / "other" / "tick" / "BTC").mkdir(parents=True)
        result = service.catalog()
        self.assertEqual(set(result["exchanges"]), {"bithumb", "okx"})
        self.assertEqual(result["defaults"]["legs"][0]["ticker"], "CT")

    def test_all_twenty_six_sources_compose_with_one_read_for_aliases(self):
        legs = [
            {"id": identifier, "exchange": "bithumb", "ticker": "CT"} for identifier in SOURCE_IDS
        ]
        with patch.object(parquet, "read_buckets", wraps=parquet.read_buckets) as reader:
            result = self.request(
                legs=legs, formula=" + ".join(SOURCE_IDS), max_points=2, include_full_data=True
            )
        reader.assert_called_once()
        self.assertEqual(result["result"], [260, 1300])
        self.assertEqual(result["full"]["result"], [260, 520, 780, 1040, 1300])
        self.assertEqual([leg["id"] for leg in result["full"]["legs"]], list(SOURCE_IDS))
        self.assertEqual(result["meta"]["rows_read"], 5)
        self.assertEqual(formula_ids("EMA(E / Z, 2) - ABS(F - Y)"), ["E", "F", "Y", "Z"])
        catalog = service.catalog()
        self.assertEqual(catalog["source_ids"], list(SOURCE_IDS))
        self.assertEqual(catalog["limits"]["max_sources"], 26)
        self.assertEqual(catalog["limits"]["max_total_source_buckets"], 1_000_000)

    def test_nonconsecutive_added_ids_mix_price_and_funding_without_reconverting_units(self):
        path = Path(self.temp.name) / "binance/funding/BTC/2026/10/25/00.parquet"
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                "desktopTime": [self.origin + s * 1000 for s in [0, 60, 180, 240]],
                "serverTime": [self.origin + s * 1000 - 10 for s in [0, 60, 180, 240]],
                "fundingRate": [0, -0.0001, 0.0002, 0.0003],
                "nextFundingTime": [60, 0, -1, 120],
            }
        ).to_parquet(path, index=False)
        result = self.request(
            legs=[
                {"id": "A", "exchange": "bithumb", "ticker": "CT"},
                {"id": "E", "exchange": "binance", "ticker": "BTC", "data_type": "funding"},
                {"id": "Z", "exchange": "okx", "ticker": "CT"},
            ],
            formula="(A + Z) * E / 100",
            include_full_data=True,
        )
        self.assertEqual([leg["id"] for leg in result["legs"]], ["A", "E", "Z"])
        self.assertEqual(result["legs"][1]["values"], [0, -0.01, 0.02, 0.03])
        self.assertEqual(result["legs"][1]["funding_rate"], [0, -0.0001, 0.0002, 0.0003])
        self.assertEqual(result["times"], [self.origin + s * 1000 for s in [0, 60, 180, 240]])
        for actual, expected in zip(result["result"], [0, -0.0024, 0.0096, 0.018]):
            self.assertAlmostEqual(actual, expected)

    def test_added_source_bounds_and_total_output_budget_fail_before_loading(self):
        invalid = [
            [
                {"id": identifier, "exchange": "bithumb", "ticker": "CT"}
                for identifier in [*SOURCE_IDS, "AA"]
            ],
            [{"id": "E"}, {"id": "E"}],
            [{"id": "AA"}],
            [{"id": "e"}],
            [{"id": "Ａ"}],
        ]
        with patch.object(parquet, "read_buckets") as reader:
            for legs in invalid:
                with self.subTest(legs=legs), self.assertRaises(service.DataError):
                    self.request(legs=legs, formula="E")
            legs = [
                {"id": identifier, "exchange": "bithumb", "ticker": "CT"}
                for identifier in SOURCE_IDS
            ]
            with self.assertRaisesRegex(service.DataError, "소스 수 × 시간 구간 수"):
                self.request(
                    legs=legs,
                    formula="+".join(SOURCE_IDS),
                    start="2026-10-25T09:00",
                    end="2026-11-25T09:00",
                )
            with self.assertRaises(service.DataError):
                self.request(legs=legs[:5], formula="Z")
            reader.assert_not_called()
        # Unused controls do not consume the output budget or force alignment.
        result = self.request(
            legs=legs, formula="Z", start="2026-10-25T09:00", end="2026-11-25T09:00"
        )
        self.assertEqual([leg["id"] for leg in result["legs"]], ["Z"])
        self.assertEqual(result["meta"]["rows_read"], 5)

    def test_unused_sources_are_not_validated_loaded_or_aligned(self):
        request = self.request(
            formula="A",
            legs=[
                {"id": "A", "exchange": "bithumb", "ticker": "CT"},
                {"id": "B", "exchange": "no-such-exchange", "ticker": "../private"},
            ],
        )
        self.assertEqual(request["result"], [10, 20, 30, 40, 50])
        self.assertEqual([leg["id"] for leg in request["legs"]], ["A"])
        self.assertEqual(request["meta"]["rows_read"], 5)

    def test_moving_average_ema_differences_returns_zscore_and_abs(self):
        cases = {
            "MA(A, 2)": [None, 15, 25, 35, 45],
            "DIFF(A, 2)": [None, None, 20, 20, 20],
            "RET(A, 1)": [None, 100, 50, 100 / 3, 25],
            "ZSCORE(A, 3)": [None, None, 1, 1, 1],
            "ABS(A - 30)": [20, 10, 0, 10, 20],
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                actual = self.request(formula=formula)["result"]
                for got, wanted in zip(actual, expected):
                    if wanted is None:
                        self.assertIsNone(got)
                    else:
                        self.assertAlmostEqual(got, wanted)
        expected = pd.Series([10, 20, 30, 40, 50]).ewm(span=2, adjust=True).mean().tolist()
        for actual, wanted in zip(self.request(formula="EMA(A, 2)")["result"], expected):
            self.assertAlmostEqual(actual, wanted)

    def test_functions_count_common_samples_without_filling_time_gaps(self):
        result = self.request(formula="MA(A + 0 * B, 2)")
        self.assertEqual(result["result"], [None, 15, 30, 45])
        self.assertEqual(result["times"], [self.origin + s * 1000 for s in [0, 60, 180, 240]])
        self.assertEqual(result["meta"]["window_basis"], "aligned_samples")
        self.assertEqual(result["meta"]["undefined_points"], 1)
        self.assertIn("공통 호가", " ".join(result["meta"]["warnings"]))

    def test_nested_functions_and_zero_variance_have_defined_semantics(self):
        result = self.request(formula="RET(MA(A, 2), 1)")
        self.assertEqual(result["result"][:2], [None, None])
        self.assertAlmostEqual(result["result"][2], (25 / 15 - 1) * 100)
        result = self.request(formula="ZSCORE(A / B, 2)")
        self.assertEqual(result["result"], [None, None, None, None])
        json.dumps(result, allow_nan=False)

    def test_function_arguments_reject_executable_and_unbounded_expressions(self):
        formulas = [
            "MA(A, 0)",
            "MA(A, -1)",
            "MA(A, 1.5)",
            "MA(A, True)",
            "MA(A, 1000001)",
            "MA(A, B)",
            "MA(A, 1 + 1)",
            "MA(A, window=2)",
            "EMA(A)",
            "DIFF(A, 1, 2)",
            "SUM(A, 2)",
            "A.mean()",
            "ABS(A, 2)",
            "getattr(A, 'mean')()",
            "42",
        ]
        for formula in formulas:
            with self.subTest(formula=formula), patch.object(parquet, "read_buckets") as loader:
                with self.assertRaises(service.DataError):
                    self.request(formula=formula)
                loader.assert_not_called()
        self.assertEqual(formula_ids("EMA(A / B, 60) - ABS(C - D)"), ["A", "B", "C", "D"])

    def test_full_data_export_survives_sampling_and_function_warmup(self):
        result = self.request(formula="MA(A, 2)", max_points=2, include_full_data=True)
        self.assertEqual(len(result["times"]), 2)
        self.assertEqual(len(result["full"]["times"]), 5)
        self.assertEqual(result["full"]["result"], [None, 15, 25, 35, 45])
        self.assertEqual(result["full"]["legs"][0]["bid"], [9.5, 19.5, 29.5, 39.5, 49.5])
        self.assertEqual(result["stats"]["count"], 4)
        json.dumps(result, allow_nan=False)

    def test_chart_sampling_keeps_global_spikes_and_all_sources_synchronous(self):
        self.write(
            "bithumb", "CT", [i * 60 for i in range(20)], [1 if i != 7 else 500 for i in range(20)]
        )
        result = self.request(formula="A", end="2026-10-25T09:20:00", max_points=4)
        self.assertEqual(result["result"][0], 1)
        self.assertEqual(result["result"][-1], 1)
        self.assertIn(500, result["result"])
        self.assertEqual(len(result["times"]), 4)
        self.assertEqual(len(result["legs"][0]["mid"]), 4)
        self.assertEqual(result["stats"]["count"], 20)

    def test_case_insensitive_colon_symbols_use_canonical_paths(self):
        self.write("hyperliquid", "xyz:EWY", [0, 60], [100, 101])
        result = self.request(
            formula="A", legs=[{"id": "A", "exchange": "hyperliquid", "ticker": "XYZ:EWY"}]
        )
        self.assertEqual(result["legs"][0]["ticker"], "xyz:EWY")
        self.assertEqual(result["result"], [100, 101])

    def test_summary_statistics_and_source_correlations_use_full_data(self):
        result = self.request(formula="A", max_points=2)
        self.assertEqual(result["stats"]["median"], 30)
        self.assertEqual(result["stats"]["q05"], 12)
        self.assertEqual(result["stats"]["q95"], 48)
        self.assertEqual(result["stats"]["positive_rate"], 1)
        self.assertEqual(result["stats"]["latest_minus_mean"], 20)
        self.assertAlmostEqual(
            result["stats"]["latest_zscore"], 20 / pd.Series([10, 20, 30, 40, 50]).std()
        )
        self.assertEqual(result["stats"]["latest_time"], self.origin + 240000)
        self.assertAlmostEqual(self.request()["correlations"]["A"]["B"], 1)

    def test_resolution_and_period_limits_reject_before_reading(self):
        with patch.object(parquet, "read_buckets") as loader:
            with self.assertRaisesRegex(service.DataError, "간격"):
                self.request(formula="A", interval="1s", end="2026-10-28T09:00:00")
            with self.assertRaisesRegex(service.DataError, "31일"):
                self.request(formula="A", end="2026-11-26T09:00:00")
            loader.assert_not_called()
        result = self.request(formula="A", end="2026-11-25T09:00:00")
        self.assertEqual(result["stats"]["count"], 5)

    def test_catalog_function_contract_and_symlink_escape_protection(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(self.temp.name) / "bithumb" / "ob" / "ESCAPE"
            target.symlink_to(Path(outside), target_is_directory=True)
            self.assertNotIn("ESCAPE", service.catalog()["exchanges"]["bithumb"])
            with self.assertRaises(service.DataError):
                parquet.bounds("bithumb", "ESCAPE")
        catalog = service.catalog()
        self.assertEqual(set(catalog["intervals"]), set(service.INTERVALS))
        self.assertEqual({item["name"] for item in catalog["functions"]}, FUNCTION_NAMES)
        self.assertEqual(catalog["limits"]["max_range_days"], 31)

    def test_query_deadline_interrupts_real_work_and_config_is_validated(self):
        with patch.dict(os.environ, {"DASHBOARD_QUERY_TIMEOUT_SECONDS": "0.02"}):
            con = parquet.connection()
            try:
                with self.assertRaises(QueryTimeout), parquet.query_deadline(con):
                    con.execute("SELECT sum(i) FROM range(1000000000000) t(i)").fetchone()
            finally:
                con.close()
        for changes in [{"DASHBOARD_THREADS": "0"}, {"DASHBOARD_MEMORY_LIMIT": "256MB'; SELECT 1"}]:
            with (
                self.subTest(changes=changes),
                patch.dict(os.environ, changes),
                self.assertRaises(service.DataError),
            ):
                parquet.connection()


if __name__ == "__main__":
    unittest.main()
