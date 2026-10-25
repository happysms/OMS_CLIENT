import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.market_data import parquet
from oms_client.market_data.pricing import normalize_price_options, select_quote_price
from oms_client.market_data.validation import DataError


class QuotePricingTests(unittest.TestCase):
    def quote(self):
        return {"bids": [[100, 1], [99, 1], [98, 1], [97, 1], [96, 1]],
                "asks": [[101, 1], [102, 1], [103, 1], [104, 1], [105, 1]]}

    def test_options_default_and_invalid_target_depth_are_validated(self):
        self.assertEqual(normalize_price_options({}), {"price_basis": "mid", "book_level": 1, "liquidity_amount": None})
        for options in ({"price_basis": "unknown"}, {"book_level": 2}, {"price_basis": "bid", "book_level": 6},
                        {"price_basis": "bid", "book_level": True}, {"price_basis": "buy_vwap"},
                        *({"price_basis": "sell_vwap", "liquidity_amount": value} for value in (0, -1, True, float("inf"), "nan"))):
            with self.subTest(options=options), self.assertRaises(DataError):
                normalize_price_options(options)
        options = normalize_price_options({"price_basis": "buy_vwap", "book_level": 5, "liquidity_amount": "100"})
        self.assertEqual(options, {"price_basis": "buy_vwap", "book_level": 1, "liquidity_amount": 100.0})

    def test_mid_and_nth_prices_do_not_replace_raw_best_quote(self):
        quote = self.quote()
        self.assertEqual(select_quote_price(quote, {})["value"], 100.5)
        self.assertEqual(select_quote_price(quote, {"price_basis": "bid", "book_level": 5})["value"], 96)
        self.assertEqual(select_quote_price(quote, {"price_basis": "ask", "book_level": 3})["value"], 103)
        self.assertEqual(quote["bids"][0], [100, 1])

    def test_buy_and_sell_fill_quote_currency_and_report_actual_base_quantity(self):
        for basis, first, second in (("buy_vwap", 101, 102), ("sell_vwap", 100, 99)):
            with self.subTest(basis=basis):
                result = select_quote_price(self.quote(), {"price_basis": basis, "liquidity_amount": 150})
                expected_quantity = 1 + (150 - first) / second
                self.assertAlmostEqual(result["value"], 150 / expected_quantity)
                self.assertAlmostEqual(result["liquidity_filled_quantity"], expected_quantity)
                self.assertEqual(result["liquidity_levels_used"], 2)
                self.assertEqual(result["liquidity_available_notional"], 515 if basis == "buy_vwap" else 490)

    def test_native_best_level_fallback_and_insufficient_liquidity_have_no_partial_price(self):
        quote = {"bid": 100, "ask": 101, "bid_size": 1.5, "ask_size": 2}
        result = select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 202})
        self.assertEqual(result["value"], 101)
        result = select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 203})
        self.assertIsNone(result["value"])
        self.assertEqual(result["liquidity_filled_quantity"], 2)
        self.assertEqual(result["reason"], "insufficient_liquidity")
        self.assertIsNone(select_quote_price(quote, {"price_basis": "bid", "book_level": 2})["value"])

    def test_a_missing_invalid_or_reversed_level_stops_deeper_fills(self):
        for level in ([None, None], [102, float("nan")], [100, 10], [0, 0]):
            with self.subTest(level=level):
                quote = self.quote()
                quote["asks"][1] = level
                small = select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 100})
                large = select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 150})
                self.assertEqual(small["value"], 101)  # Unused bad depth does not invalidate a complete fill.
                self.assertEqual(small["liquidity_available_notional"], 101)
                self.assertIsNone(large["value"])
                self.assertEqual(large["liquidity_levels_used"], 1)

    def test_only_five_levels_are_used_and_overflow_is_an_undefined_price(self):
        quote = self.quote()
        quote["asks"].append([106, 1_000_000])
        self.assertIsNone(select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 516})["value"])
        quote = {"bids": [[1, 1]], "asks": [[1e308, 1], [1.1e308, 1]]}
        self.assertIsNone(select_quote_price(quote, {"price_basis": "buy_vwap", "liquidity_amount": 1})["value"])


class HistoricalPricingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(os.environ, {"DASHBOARD_DATA_ROOT": str(self.root / "legacy"),
                                             "DASHBOARD_COLLECTOR_DATA_ROOT": str(self.root / "market")})
        environment.start()
        self.addCleanup(environment.stop)
        self.start = pd.Timestamp("2026-10-25T00:00:00Z")
        self.origin = self.start.value // 1_000_000

    def fixture(self, native=False):
        rows = {"desktopTime": [self.origin + 100, self.origin + 1100],
                "b1_p": [100, 200], "a1_p": [101, 201],
                "b2_p": [99, 199], "a2_p": [102, 202]}
        if native:
            rows.update(bid_size=[1, .25], ask_size=[1, .25], bid_size_2=[1, .25], ask_size_2=[1, .25],
                        price_unit=["KRW"] * 2, quantity_unit=["USDT"] * 2,
                        sample_interval_ms=[1000] * 2, market=["KRW-USDT"] * 2)
        else:
            rows.update(b1_q=[100, 50], a1_q=[101, 50], b2_q=[99, 49], a2_q=[102, 50])
        return pd.DataFrame(rows)

    def write(self, frame, exchange="bithumb", native=False, hour="00"):
        directory = self.root / ("market" if native else "legacy") / exchange / ("ob_1s" if native else "ob") / "USDT" / "2026/10/25"
        directory.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(directory / f"{hour}.parquet", index=False)

    def read(self, exchange="bithumb", **options):
        return parquet.read_buckets(exchange, "USDT", self.start, self.start + pd.Timedelta(hours=2), 60_000, **options)

    def test_default_value_equals_mid_and_nth_value_preserves_best_bid_ask(self):
        self.write(self.fixture())
        frame, rows = self.read()
        self.assertEqual(frame["value"].tolist(), frame["mid"].tolist())
        self.assertEqual(rows, 2)
        frame, _ = self.read(price_basis="ask", book_level=2, price_mode="last")
        self.assertEqual(frame["value"].tolist(), [202])
        self.assertEqual((frame.iloc[0]["bid"], frame.iloc[0]["ask"], frame.iloc[0]["mid"]), (200, 201, 200.5))
        self.assertEqual(frame.attrs["book_level"], 2)

    def test_legacy_per_level_notional_is_divided_by_each_own_price(self):
        for exchange in ("bithumb", "upbit", "binance", "okx"):
            with self.subTest(exchange=exchange):
                self.write(self.fixture().iloc[:1], exchange)
                frame, _ = self.read(exchange, price_basis="buy_vwap", liquidity_amount=150)
                self.assertAlmostEqual(frame.iloc[0]["value"], 150 / (101 / 101 + 49 / 102))
                self.assertEqual(frame.iloc[0]["liquidity_available_notional"], 203)
                self.assertEqual(frame.attrs["quantity_basis"], "base_reconstructed_from_quote_notional")
                self.assertEqual(frame.attrs["liquidity_currency"], "KRW" if exchange in {"bithumb", "upbit"} else "USDT")

    def test_each_snapshot_is_priced_before_mean_and_final_insufficient_row_stays_null(self):
        self.write(self.fixture())
        mean, rows = self.read(price_basis="buy_vwap", liquidity_amount=150)
        expected = 150 / (1 + 49 / 102)
        self.assertAlmostEqual(mean.iloc[0]["value"], expected)
        self.assertEqual(rows, 2)
        self.assertEqual((mean.attrs["pricing_valid_rows"], mean.attrs["pricing_missing_rows"]), (1, 1))
        last, _ = self.read(price_basis="buy_vwap", liquidity_amount=150, price_mode="last")
        self.assertTrue(pd.isna(last.iloc[0]["value"]))
        self.assertEqual(last.iloc[0]["quote_time"], self.origin + 1100)
        self.assertEqual(last.iloc[0]["ask"], 201)
        self.assertEqual(last.iloc[0]["liquidity_available_notional"], 100)

    def test_native_quantities_match_pure_engine_and_do_not_get_reinterpreted_as_notional(self):
        data = self.fixture(native=True)
        self.write(data, native=True)
        frame, _ = self.read(price_basis="sell_vwap", liquidity_amount=150)
        pure = select_quote_price({"bids": [[100, 1], [99, 1]], "asks": [[101, 1], [102, 1]]},
                                  {"price_basis": "sell_vwap", "liquidity_amount": 150})
        self.assertAlmostEqual(frame.iloc[0]["value"], pure["value"])
        self.assertAlmostEqual(frame.iloc[0]["liquidity_filled_quantity"], (pure["liquidity_filled_quantity"] + .5) / 2)
        self.assertEqual(frame.attrs["quantity_basis"], "native_base_quantity")
        self.assertEqual(frame.attrs["quantity_unit"], "USDT")

    def test_old_native_l1_and_new_deeper_files_union_without_inventing_depth(self):
        old = self.fixture(native=True).iloc[:1].drop(columns=["b2_p", "a2_p", "bid_size_2", "ask_size_2"])
        self.write(old, native=True)
        new = self.fixture(native=True).iloc[:1].copy()
        new["desktopTime"] += 3_600_000
        self.write(new, native=True, hour="01")
        frame, _ = self.read(price_basis="ask", book_level=2)
        self.assertTrue(pd.isna(frame.iloc[0]["value"]))
        self.assertEqual(frame.iloc[1]["value"], 102)
        self.assertEqual(frame.attrs["depth_limit"], 2)
        small, _ = self.read(price_basis="buy_vwap", liquidity_amount=100)
        self.assertEqual(small["value"].tolist(), [101, 101])
        large, _ = self.read(price_basis="buy_vwap", liquidity_amount=150)
        self.assertTrue(pd.isna(large.iloc[0]["value"]))
        self.assertTrue(math.isfinite(large.iloc[1]["value"]))

    def test_gaps_reverse_and_bad_quantities_never_allow_a_fill_through_them(self):
        for changes in ({"a2_p": None}, {"a2_p": 100}, {"a2_q": -1}, {"a2_q": float("nan")}):
            with self.subTest(changes=changes):
                data = self.fixture().iloc[:1].copy()
                for name, value in changes.items():
                    data[name] = value
                data["a3_p"], data["a3_q"] = 103, 10000
                self.write(data)
                small, _ = self.read(price_basis="buy_vwap", liquidity_amount=100)
                self.assertEqual(small.iloc[0]["value"], 101)
                large, _ = self.read(price_basis="buy_vwap", liquidity_amount=150)
                self.assertTrue(pd.isna(large.iloc[0]["value"]))
                self.assertEqual(large.iloc[0]["liquidity_available_notional"], 101)

    def test_unknown_quantity_units_and_absent_requested_depth_are_explicit_errors(self):
        self.write(self.fixture(), "hyperliquid")
        frame, _ = self.read("hyperliquid", price_basis="bid", book_level=2)
        self.assertEqual(frame.iloc[0]["value"], 149)
        with self.assertRaisesRegex(DataError, "단위"):
            self.read("hyperliquid", price_basis="buy_vwap", liquidity_amount=100)
        self.write(self.fixture().drop(columns=["b2_p", "a2_p", "b2_q", "a2_q"]))
        with self.assertRaisesRegex(DataError, "깊이"):
            self.read(price_basis="bid", book_level=2)
        self.write(self.fixture().drop(columns=["b1_q", "a1_q", "b2_q", "a2_q"]))
        with self.assertRaisesRegex(DataError, "잔량"):
            self.read(price_basis="buy_vwap", liquidity_amount=100)


if __name__ == "__main__":
    unittest.main()
