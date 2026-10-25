"""Trading decisions, execution costs and complete-series risk measurements."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.backtesting.engine import run_backtest
from oms_client.market_data.validation import DataError


STEP = 60_000


def fixture(mids, spread=0):
    leg = {"id": "A", "exchange": "bithumb", "ticker": "BTC", "currency": "KRW",
           "mid": list(mids), "bid": [value - spread for value in mids],
           "ask": [value + spread for value in mids]}
    return {"full": {"times": [i * STEP for i in range(len(mids))], "legs": [leg]},
            "meta": {"source": "parquet", "rows_read": len(mids), "warnings": []}}


class BacktestTests(unittest.TestCase):
    def request(self, mids, *, source=None, **changes):
        payload = {"exchange": "bithumb", "ticker": "BTC", "start": "1970-01-01T00:00:00Z",
                   "end": "1970-01-02T00:00:00Z", "interval": "1min", "strategy": "ma_cross",
                   "fast_window": 2, "slow_window": 3, "initial_cash": 1000,
                   "fee_bps": 0, "slippage_bps": 0, "close_on_end": False}
        payload.update(changes)
        self.calls = []
        def provider(config):
            self.calls.append(config)
            return source if source is not None else fixture(mids)
        return run_backtest(payload, data_provider=provider)

    def test_cross_signals_use_previous_completed_bucket_and_cross_down_exits(self):
        result = self.request([3, 2, 1, 2, 3, 4, 3, 2, 1, 2, 3])
        trades = result["trades"]
        self.assertEqual([trade["side"] for trade in trades], ["buy", "sell"])
        self.assertEqual([trade["timestamp"] for trade in trades], [6 * STEP, 9 * STEP])
        self.assertEqual([trade["price"] for trade in trades], [4, 1])
        self.assertEqual(result["metrics"]["closed_trades"], 1)
        self.assertEqual(result["metrics"]["realized_pnl"], -750)
        self.assertEqual(result["metrics"]["win_rate_pct"], 0)
        self.assertEqual(self.calls[0]["formula"], "A")
        self.assertTrue(self.calls[0]["include_full_data"])

    def test_current_execution_bucket_cannot_change_its_entry_signal(self):
        before = self.request([1, 2, 3, 4], fast_window=1, slow_window=2)
        changed = self.request([1, 2, .1, 4], fast_window=1, slow_window=2)
        self.assertEqual(before["trades"][0]["timestamp"], 3 * STEP)
        self.assertEqual(changed["trades"][0]["timestamp"], 3 * STEP)
        self.assertEqual(before["trades"][0]["price"], 3)
        self.assertEqual(changed["trades"][0]["price"], .1)
        warmup = self.request([1, 2, 3], fast_window=2, slow_window=3)
        self.assertEqual(warmup["trades"], [])
        self.assertEqual(warmup["metrics"]["final_equity"], 1000)

    def test_buy_hold_is_cash_funded_with_spread_slippage_and_two_sided_fees(self):
        source = fixture([100] * 4, spread=1)
        result = self.request([], source=source, strategy="buy_hold", fee_bps=10,
                              slippage_bps=20, close_on_end=True)
        buy, sell = result["trades"]
        quantity = 1000 / (101 * 1.002 * 1.001)
        final = quantity * 99 * .998 * .999
        self.assertAlmostEqual(buy["quantity"], quantity)
        self.assertAlmostEqual(result["metrics"]["final_equity"], final)
        self.assertAlmostEqual(result["metrics"]["realized_pnl"], final - 1000)
        self.assertEqual(sell["reason"], "end_of_test")
        self.assertGreater(result["metrics"]["fees_paid"], 0)
        self.assertTrue(all(trade["quantity"] > 0 and trade["cash"] >= 0 for trade in result["trades"]))
        self.assertEqual(result["equity"]["values"], result["equity"]["benchmark"])
        self.assertEqual(result["metrics"]["trades"], 2)
        json.dumps(result, allow_nan=False)

    def test_drawdown_and_unrealized_loss_use_whole_curve_before_downsampling(self):
        full = self.request([100, 100, 50, 80], strategy="buy_hold", max_points=6000)
        sampled = self.request([100, 100, 50, 80], strategy="buy_hold", max_points=2)
        self.assertEqual(full["metrics"]["max_drawdown_pct"], 50)
        self.assertAlmostEqual(full["metrics"]["return_pct"], -20)
        self.assertEqual(full["metrics"]["realized_pnl"], 0)
        self.assertEqual(sampled["metrics"], full["metrics"])
        self.assertEqual(sampled["stats"], full["stats"])
        self.assertEqual(sampled["trades"], full["trades"])
        self.assertEqual(len(sampled["times"]), 2)
        self.assertNotIn("full", sampled)
        self.assertEqual(sampled["equity"]["times"], sampled["times"])
        self.assertEqual(sampled["meta"]["aligned_points"], 4)

    def test_partial_end_bucket_is_excluded_and_times_are_bucket_end(self):
        result = self.request([100, 100, 100, 100], strategy="buy_hold",
                              end="1970-01-01T00:03:30Z", close_on_end=True)
        self.assertEqual(result["times"], [STEP, 2 * STEP, 3 * STEP])
        self.assertEqual(result["trades"][-1]["timestamp"], 3 * STEP)
        self.assertEqual(result["meta"]["timestamp_basis"], "bucket_end")

    def test_execution_ledger_limit_stops_job_with_actionable_error(self):
        with patch("oms_client.backtesting.engine.MAX_TRADES", 1):
            with self.assertRaisesRegex(DataError, "기간을 줄이거나"):
                self.request([100] * 4, strategy="buy_hold", close_on_end=True)

    def test_invalid_configuration_is_rejected_before_data_is_read(self):
        changes = [{"strategy": "unknown"}, {"strategy": []}, {"fee_bps": -1}, {"fee_bps": float("nan")},
                   {"slippage_bps": float("inf")}, {"initial_cash": 0}, {"initial_cash": True},
                   {"slow_window": 2}, {"fast_window": 1.5}, {"start": "NaT"},
                   {"end": "1970-01-01T00:00:00Z"}, {"end": "1971-01-01T00:00:00Z"},
                   {"interval": "15s"}, {"max_points": 6001}, {"close_on_end": 1}, {"quantity": 3},
                   {"exchange": "../private"}, {"ticker": "../../secret"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(DataError):
                self.request([1, 2, 3], **change)
            self.assertEqual(self.calls, [])

    def test_malformed_prices_times_and_feed_lengths_are_rejected(self):
        original = fixture([100, 100, 100])
        bad = []
        for invalid in [float("nan"), float("inf"), 0, -1, True]:
            source = copy.deepcopy(original)
            source["full"]["legs"][0]["bid"][1] = invalid
            bad.append(source)
        for times in [[0, 0, STEP], [STEP, 0, 2 * STEP], [0, float("nan"), 2 * STEP]]:
            source = copy.deepcopy(original)
            source["full"]["times"] = times
            bad.append(source)
        source = copy.deepcopy(original)
        source["full"]["legs"][0]["ask"].pop()
        bad.append(source)
        source = copy.deepcopy(original)
        source["full"]["legs"][0]["bid"][1] = 101
        bad.append(source)
        for source in bad:
            with self.subTest(source=source), self.assertRaises(DataError):
                self.request([], source=source)


if __name__ == "__main__":
    unittest.main()
