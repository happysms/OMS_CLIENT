from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.storage.state import StateStore
from oms_client.paper_trading.service import PaperTradingService, PublicQuoteProvider


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = StateStore(self.temporary.name)
        self.now = time.time()
        self.bid, self.ask = 99.0, 101.0
        self.age = 0
        self.unavailable = False
        self.paper = PaperTradingService(self.store, self.quote, clock=lambda: self.now,
                                         symbol_validator=lambda exchange, ticker: (exchange, ticker))
        self.paper.reset({"initial_cash": 1000, "fee_bps": 10, "slippage_bps": 0})

    def tearDown(self):
        self.paper.close()
        self.temporary.cleanup()

    def quote(self, exchange, ticker):
        if self.unavailable:
            raise RuntimeError("No fresh quote")
        return {"exchange": exchange, "ticker": ticker, "bid": self.bid, "ask": self.ask,
                "ts": int((self.now - self.age) * 1000), "receivedAt": int(self.now * 1000)}

    def order(self, side, quantity, **extra):
        return self.paper.place_order({"exchange": "bithumb", "ticker": "BTC", "side": side,
                                       "quantity": quantity, **extra})

    def test_buy_then_partial_sell_preserves_cash_and_fee_inclusive_cost_basis(self):
        bought = self.order("buy", 2)
        self.assertAlmostEqual(bought["account"]["cash"], 797.798)
        self.assertAlmostEqual(bought["positions"][0]["avg_price"], 101.101)
        self.assertAlmostEqual(bought["positions"][0]["market_value"], 198)
        self.bid, self.ask = 110.0, 112.0
        sold = self.order("sell", 1)
        self.assertAlmostEqual(sold["account"]["cash"], 907.688)
        self.assertAlmostEqual(sold["account"]["realized_pnl"], 8.789)
        self.assertEqual(sold["positions"][0]["quantity"], 1)
        self.assertEqual(len(sold["orders"]), 2)
        json.dumps(sold, allow_nan=False)

    def test_insufficient_cash_and_overselling_are_atomic(self):
        with self.assertRaises(RuntimeError):
            self.order("buy", 100)
        self.assertEqual(self.paper.snapshot()["orders"], [])
        self.order("buy", 1)
        before = self.paper.snapshot()
        with self.assertRaises(RuntimeError):
            self.order("sell", 2)
        after = self.paper.snapshot()
        self.assertEqual(before["account"]["cash"], after["account"]["cash"])
        self.assertEqual(len(after["orders"]), 1)

    def test_stale_quotes_do_not_execute_and_missing_marks_are_not_fabricated(self):
        self.age = 11
        with self.assertRaises(RuntimeError):
            self.order("buy", 1)
        self.age = 0
        self.order("buy", 1)
        self.unavailable = True
        snapshot = self.paper.snapshot()
        self.assertIsNone(snapshot["equity"])
        self.assertIsNone(snapshot["positions"][0]["mark_price"])
        self.assertEqual(snapshot["quote_status"]["state"], "unavailable")

    def test_idempotent_retry_does_not_repeat_fill(self):
        first = self.order("buy", 1, client_order_id="same-order")
        second = self.order("buy", 1, client_order_id="same-order")
        self.assertEqual(first["order"]["id"], second["order"]["id"])
        self.assertEqual(second["account"]["cash"], first["account"]["cash"])
        self.assertEqual(len(second["orders"]), 1)
        with self.assertRaises(ValueError):
            self.order("buy", 2, client_order_id="same-order")

    def test_concurrent_buys_cannot_overspend_account(self):
        self.paper.reset({"initial_cash": 150, "fee_bps": 0, "slippage_bps": 0})
        def attempt(_):
            try:
                self.order("buy", 1)
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, range(2)))
        self.assertEqual(sum(results), 1)
        self.assertAlmostEqual(self.paper.snapshot()["account"]["cash"], 49)

    def test_server_price_only_and_cash_account_currency_constraints(self):
        with self.assertRaises(ValueError):
            self.order("buy", 1, price=.01)
        for quantity in [-1, 0, float("nan"), float("inf"), True]:
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                self.order("buy", quantity)
        with self.assertRaises(ValueError):
            self.paper.place_order({"exchange": [], "ticker": "BTC", "side": "buy", "quantity": 1})
        with self.assertRaises(ValueError):
            self.paper.reset({"quote_currency": "USDT"})
        with self.assertRaises(ValueError):
            self.paper.place_order({"exchange": "okx", "ticker": "BTC", "side": "buy", "quantity": 1})

    def test_ledger_persists_without_reset_on_service_restart(self):
        self.order("buy", 1)
        reopened = PaperTradingService(self.store, self.quote, clock=lambda: self.now,
                                      symbol_validator=lambda exchange, ticker: (exchange, ticker))
        snapshot = reopened.snapshot()
        self.assertEqual(snapshot["positions"][0]["quantity"], 1)
        self.assertEqual(len(snapshot["orders"]), 1)
        self.assertAlmostEqual(snapshot["account"]["cash"], 898.899)
        reopened.close()

    def test_reset_only_changes_paper_tables(self):
        self.store.set_setting("existing_session", "retained")
        self.order("buy", 1)
        reset = self.paper.reset({"initial_cash": 5000})
        self.assertEqual(reset["account"]["cash"], 5000)
        self.assertEqual(reset["positions"], [])
        self.assertEqual(reset["orders"], [])
        self.assertEqual(self.store.setting("existing_session"), "retained")

    def test_explicit_slippage_is_applied_in_both_directions(self):
        self.paper.reset({"initial_cash": 1000, "fee_bps": 0, "slippage_bps": 100})
        self.assertAlmostEqual(self.order("buy", 1)["order"]["price"], 102.01)
        self.assertAlmostEqual(self.order("sell", 1)["order"]["price"], 98.01)
        self.assertEqual(self.paper.snapshot()["positions"], [])


class ProviderTests(unittest.TestCase):
    def test_symbol_subscriptions_are_isolated_and_close_discards_late_callbacks(self):
        instances = []
        class Feed:
            def __init__(self, legs, on_quote, on_status):
                self.legs, self.on_quote, self.on_status = legs, on_quote, on_status
                self.stopped = False
                instances.append(self)
            def start(self):
                now = int(time.time() * 1000)
                for leg in self.legs:
                    self.on_quote({**leg, "bid": 99, "ask": 101, "ts": now, "receivedAt": now})
            def stop(self):
                self.stopped = True
        provider = PublicQuoteProvider(feed_factory=Feed, max_symbols=2)
        try:
            self.assertEqual(provider.get("bithumb", "BTC")["mid"], 100)
            self.assertEqual(provider.get("upbit", "ETH")["mid"], 100)
            self.assertFalse(instances[0].stopped)
            instances[1].on_status({"exchange": "upbit", "state": "error", "message": "invalid symbol"})
            self.assertEqual(provider.get("bithumb", "BTC")["mid"], 100)
            with self.assertRaises(ValueError):
                provider.get("bithumb", "XRP")
        finally:
            provider.close()
        now = int(time.time() * 1000)
        instances[0].on_quote({"exchange": "bithumb", "ticker": "BTC", "bid": 1, "ask": 3, "ts": now, "receivedAt": now})
        self.assertEqual(provider.quotes, {})
        self.assertTrue(instances[0].stopped)
        self.assertTrue(instances[-1].stopped)


if __name__ == "__main__":
    unittest.main()
