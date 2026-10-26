"""Dedicated USDT snapshots expose real quantities without retyping legacy data."""

import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.history import analyze, catalog
from oms_client.market_data import csv_export, parquet
from oms_client.market_data.validation import DataError


class UsdtDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.market = self.root / "collector"
        self.legacy = self.root / "legacy"
        environment = patch.dict(
            os.environ,
            {
                "DASHBOARD_DATA_ROOT": str(self.legacy),
                "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.market),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = int(pd.Timestamp("2026-10-25T00:00:00Z").timestamp() * 1000)

    def rows(self, **changes):
        data = {
            "sampleTime": [self.origin, self.origin + 1000, self.origin + 2000],
            "desktopTime": [self.origin + 25, self.origin + 1025, self.origin + 2025],
            "sampledAt": [self.origin + 25, self.origin + 1025, self.origin + 2025],
            "serverTime": [self.origin - 100, self.origin + 900, self.origin + 1900],
            "receivedTime": [self.origin + 10, self.origin + 1010, self.origin + 2010],
            "b1_p": [1342, 1343, 1344],
            "a1_p": [1343, 1344, 1345],
            "bid_size": [0.125, 1.5, 23.456789],
            "ask_size": [40.25, 2.5, 0],
            "quote_age_ms": [125, 125, 125],
            "price_unit": ["KRW"] * 3,
            "quantity_unit": ["USDT"] * 3,
            "sample_interval_ms": [1000] * 3,
            "market": ["KRW-USDT"] * 3,
        }
        data.update(changes)
        return pd.DataFrame(data)

    def write(self, exchange="bithumb", ticker="USDT", frame=None, *, legacy=False):
        path = (
            (self.legacy if legacy else self.market)
            / exchange
            / ("ob" if legacy else "ob_1s")
            / ticker
            / "2026/10/25/00.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        (self.rows() if frame is None else frame).to_parquet(path, index=False)
        return path

    def query(self, **changes):
        config = {
            "legs": [{"id": "A", "exchange": "bithumb", "ticker": "usdt"}],
            "formula": "A",
            "start": "2026-10-25T00:00:00Z",
            "end": "2026-10-25T00:00:03Z",
            "interval": "1s",
            "price_mode": "last",
            "ewm_span": 1,
        }
        config.update(changes)
        return analyze(config)

    def export(self, mode="raw", **changes):
        config = {
            "exchange": "bithumb",
            "ticker": "usdt",
            "start": "2026-10-25T00:00:00Z",
            "end": "2026-10-25T00:00:03Z",
            "mode": mode,
            "interval": "10s",
        }
        config.update(changes)
        target = self.root / "output.csv"
        result = csv_export.export_csv(config, target)
        with target.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        return result, rows

    def test_collector_only_discovery_bounds_and_canonical_query(self):
        self.write()
        self.write("upbit")
        self.write("unknown")
        self.write("bithumb", "BTC")
        self.assertEqual(parquet.list_symbols(), {"bithumb": ["USDT"], "upbit": ["USDT"]})
        self.assertEqual(catalog()["exchanges"], parquet.list_symbols())
        self.assertEqual(parquet.validate_symbol("bithumb", "usdt"), ("bithumb", "USDT"))
        self.assertEqual(
            parquet.bounds("bithumb", "usdt")["start"], "2026-10-25T09:00:00.025000+09:00"
        )
        self.assertEqual(parquet.bounds("upbit", "USDT")["end"], "2026-10-25T09:00:02.025000+09:00")
        result = self.query()
        leg = result["legs"][0]
        self.assertEqual(leg["ticker"], "USDT")
        self.assertEqual(leg["bid_size"], [0.125, 1.5, 23.456789])
        self.assertEqual(leg["ask_size"], [40.25, 2.5, 0])
        self.assertEqual(leg["quote_times"], [self.origin + delta for delta in [25, 1025, 2025]])
        self.assertEqual(leg["quantity_unit"], "USDT")
        self.assertEqual(leg["source_kind"], "orderbook_1s")
        self.assertEqual(leg["sample_interval_ms"], 1000)
        self.assertEqual(leg["quote_time_basis"], "sampled_at")
        json.dumps(result, allow_nan=False)

    def test_last_preserves_price_and_sizes_from_one_complete_row(self):
        frame = self.rows(desktopTime=[self.origin + 1500, self.origin + 500, self.origin + 1500])
        self.write(frame=frame)
        leg = self.query(interval="10s")["legs"][0]
        complete = (leg["bid"][0], leg["ask"][0], leg["bid_size"][0], leg["ask_size"][0])
        self.assertIn(complete, {(1342, 1343, 0.125, 40.25), (1344, 1345, 23.456789, 0)})
        self.assertEqual(leg["quote_times"], [self.origin + 1500])

    def test_mean_averages_only_the_same_valid_price_and_size_rows(self):
        self.write(frame=self.rows(bid_size=[0, float("nan"), 6]))
        result = self.query(interval="10s", price_mode="mean")
        leg = result["legs"][0]
        self.assertEqual(leg["bid"], [1343])
        self.assertEqual(leg["ask"], [1344])
        self.assertEqual(leg["bid_size"], [3])
        self.assertEqual(leg["ask_size"], [20.125])
        self.assertEqual(result["meta"]["rows_read"], 2)
        self.assertIn("수량", " ".join(result["meta"]["warnings"]))
        self.assertNotIn("quote_times", leg)
        for field, value in [
            ("bid_size", -1),
            ("ask_size", float("inf")),
            ("quantity_unit", "KRW"),
            ("price_unit", "USDT"),
            ("sample_interval_ms", 2000),
            ("market", "USDT-BTC"),
        ]:
            with self.subTest(field=field):
                self.write(frame=self.rows(**{field: [value] * 3}))
                with self.assertRaises(DataError):
                    self.query()

    def test_query_uses_actual_adoption_time_without_grid_lookahead(self):
        self.write()
        with self.assertRaises(DataError):
            self.query(end="2026-10-25T00:00:00.020Z")
        result = self.query(start="2026-10-25T00:00:00.025Z", end="2026-10-25T00:00:01.025Z")
        self.assertEqual(result["legs"][0]["bid"], [1342])
        self.assertEqual(result["legs"][0]["quote_times"], [self.origin + 25])

    def test_sampling_and_full_result_keep_quantities_aligned(self):
        self.write()
        result = self.query(max_points=2, include_full_data=True)
        self.assertEqual(result["legs"][0]["bid_size"], [0.125, 23.456789])
        self.assertEqual(result["full"]["legs"][0]["bid_size"], [0.125, 1.5, 23.456789])
        self.assertEqual(result["full"]["legs"][0]["ask_size"], [40.25, 2.5, 0])

    def test_both_exchanges_can_be_combined_for_analysis(self):
        self.write()
        self.write("upbit", frame=self.rows(b1_p=[1341] * 3, a1_p=[1342] * 3))
        result = self.query(
            legs=[
                {"id": "A", "exchange": "bithumb", "ticker": "USDT"},
                {"id": "B", "exchange": "upbit", "ticker": "USDT"},
            ],
            formula="A - B",
        )
        self.assertEqual(result["result"], [1, 2, 3])
        self.assertEqual([leg["quantity_unit"] for leg in result["legs"]], ["USDT", "USDT"])

    def test_raw_csv_keeps_all_clocks_prices_and_true_fractional_quantities(self):
        self.write()
        result, rows = self.export()
        for column in [
            "sampleTime",
            "sampledAt",
            "desktopTime",
            "serverTime",
            "receivedTime",
            "quote_age_ms",
            "bid_size",
            "ask_size",
            "quantity_unit",
        ]:
            self.assertIn(column, result["columns"])
        self.assertEqual(float(rows[0]["bid_size"]), 0.125)
        self.assertEqual(int(rows[0]["desktopTime"]), self.origin + 25)
        self.assertEqual(int(rows[0]["sampleTime"]), self.origin)
        self.assertEqual(rows[0]["quantity_unit"], "USDT")
        self.assertNotIn("b1_q", result["columns"])

    def test_aggregated_csv_includes_average_quantities_and_units(self):
        self.write()
        result, rows = self.export("aggregated")
        self.assertEqual(result["rows"], 1)
        self.assertEqual(float(rows[0]["bid"]), 1343)
        self.assertAlmostEqual(float(rows[0]["bid_size"]), (0.125 + 1.5 + 23.456789) / 3)
        self.assertEqual(rows[0]["quantity_unit"], "USDT")
        self.assertEqual(rows[0]["sample_interval_ms"], "1000")
        self.assertEqual(rows[0]["source_kind"], "orderbook_1s")

    def test_supplemental_usdt_precedence_and_legacy_notional_is_never_a_size(self):
        frame = self.rows().drop(columns=["bid_size", "ask_size"])
        frame["b1_q"], frame["a1_q"] = 100000, 200000
        frame["b1_p"], frame["a1_p"] = 100, 101
        self.write(ticker="usdt", frame=frame, legacy=True)
        self.write(ticker="BTC", frame=frame, legacy=True)
        self.write()
        self.assertEqual(parquet.list_symbols()["bithumb"], ["BTC", "USDT"])
        self.assertEqual(self.query()["legs"][0]["bid"], [1342, 1343, 1344])
        legacy = self.query(legs=[{"id": "A", "exchange": "bithumb", "ticker": "BTC"}])["legs"][0]
        self.assertNotIn("bid_size", legacy)
        self.assertNotIn("quantity_unit", legacy)
        result, _ = self.export("aggregated", ticker="BTC")
        self.assertEqual(result["columns"], csv_export.AGGREGATED_COLUMNS)

    def test_explicit_collector_root_selects_only_that_store(self):
        self.write()
        empty = self.root / "alternative"
        with patch.dict(os.environ, {"DASHBOARD_COLLECTOR_DATA_ROOT": str(empty)}):
            self.assertEqual(parquet.collector_data_root(), empty)
            self.assertEqual(parquet.list_symbols(), {})
            with self.assertRaises(DataError):
                self.query()

    def test_symbol_and_hour_symlinks_cannot_escape_allowed_roots(self):
        path = self.write()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "00.parquet").write_bytes(path.read_bytes())
        evil = self.market / "upbit/ob_1s/USDT"
        evil.parent.mkdir(parents=True)
        evil.symlink_to(outside, target_is_directory=True)
        self.assertNotIn("upbit", parquet.list_symbols())
        path.unlink()
        path.symlink_to(outside / "00.parquet")
        self.assertEqual(
            parquet.files_for_range(
                "bithumb",
                "USDT",
                pd.Timestamp("2026-10-25T00:00:00Z"),
                pd.Timestamp("2026-10-25T00:01:00Z"),
            ),
            [],
        )
        with self.assertRaises(DataError):
            parquet.bounds("bithumb", "USDT")

    def test_unknown_or_traversing_source_is_rejected(self):
        self.write()
        for exchange, ticker in [
            ("../bithumb", "USDT"),
            ("bithumb", "../../USDT"),
            ("bithumb", "NOPE"),
        ]:
            with self.subTest(exchange=exchange, ticker=ticker), self.assertRaises(DataError):
                parquet.files_for_range(
                    exchange,
                    ticker,
                    pd.Timestamp("2026-10-25T00:00:00Z"),
                    pd.Timestamp("2026-10-25T00:01:00Z"),
                )


if __name__ == "__main__":
    unittest.main()
