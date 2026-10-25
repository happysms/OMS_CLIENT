import json
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.market_data import validation as ds
from oms_client.analysis.live import LiveAnalysisManager
from oms_client.market_data.quotes import parse_quotes


class FakeFeed:
    instances = []

    def __init__(self, legs, on_quote, on_status):
        self.legs, self.on_quote, self.on_status = legs, on_quote, on_status
        self.stopped = False
        self.__class__.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class LiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.manager = LiveAnalysisManager(feed_factory=FakeFeed, start_reaper=False)
        self.config = {"legs": [{"id": "A", "exchange": "bithumb", "ticker": "BTC"},
                                {"id": "B", "exchange": "okx", "ticker": "BTC"}],
                       "formula": "A / B", "ewm_span": 2}

    def tearDown(self):
        self.manager.close()

    def session(self, config=None):
        identifier = self.manager.start(config or self.config)
        session = self.manager.sessions[identifier]
        session.stop_event.set()  # sample deterministically in the test
        session.thread.join(timeout=1)
        return identifier, session, session.feed

    def quotes(self, session, feed, now, a=200, b=100):
        for leg, mid in zip(session.legs, [a, b]):
            feed.on_quote({**leg, "bid": mid - 1, "ask": mid + 1, "mid": mid,
                           "ts": int(now * 1000), "receivedAt": int(now * 1000)})
            feed.on_status({"exchange": leg["exchange"], "state": "connected", "message": ""})

    def test_normalizes_real_protocols_and_rejects_crossed_quotes(self):
        quote = parse_quotes("bithumb", {"type": "orderbook", "code": "KRW-BTC", "timestamp": 1792900000123456,
                                        "orderbook_units": [{"bid_price": 100, "ask_price": 102}]})[0]
        self.assertEqual(quote["ts"], 1792900000123)
        self.assertEqual(quote["mid"], 101)
        okx = {"arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
               "data": [{"bids": [["100", "1"]], "asks": [["102", "1"]], "ts": "1792900000123"}]}
        self.assertEqual(parse_quotes("okx", okx)[0]["ticker"], "BTC")
        okx["data"][0]["asks"][0][0] = "99"
        self.assertEqual(parse_quotes("okx", okx), [])

    def test_funding_sources_are_never_silently_subscribed_as_prices(self):
        before = len(FakeFeed.instances)
        config = {**self.config, "legs": [{"id": "A", "exchange": "okx", "ticker": "BTC", "data_type": "funding"}], "formula": "A"}
        with self.assertRaisesRegex(ds.DataError, "펀딩"):
            self.manager.start(config)
        self.assertEqual(len(FakeFeed.instances), before)

    def test_server_composes_once_all_sources_are_fresh_and_stops_on_stale(self):
        identifier, session, feed = self.session()
        now = time.time()
        session.clock = lambda: now
        session._tick()
        self.assertIsNone(self.manager.get(identifier)["result"])
        self.quotes(session, feed, now)
        session._tick()
        self.assertEqual(self.manager.get(identifier)["result"]["result"], [2.0])
        session.clock = lambda: now + 11
        session._tick()
        snapshot = self.manager.get(identifier)
        self.assertEqual(len(snapshot["missing"]), 2)
        self.assertEqual(snapshot["result"]["stats"]["count"], 1)

    def test_partial_subscription_does_not_discard_previous_quote(self):
        _, session, feed = self.session()
        now = time.time()
        self.quotes(session, feed, now)
        feed.on_status({"exchange": "bithumb", "state": "connecting", "message": ""})
        self.assertIn("A", session.quotes)
        feed.on_status({"exchange": "bithumb", "state": "reconnecting", "message": ""})
        self.assertNotIn("A", session.quotes)

    def test_live_functions_share_server_formula_engine_and_null_warmup(self):
        config = {**self.config, "formula": "MA(A / B, 2)"}
        identifier, session, feed = self.session(config)
        now = time.time()
        session.clock = lambda: now
        self.quotes(session, feed, now)
        session._tick()
        self.assertEqual(self.manager.get(identifier)["result"]["result"], [None])
        now += 1
        self.quotes(session, feed, now, a=400)
        session._tick()
        self.assertEqual(self.manager.get(identifier)["result"]["result"], [None, 3.0])
        json.dumps(self.manager.get(identifier), allow_nan=False)

    def test_retention_excludes_old_samples_even_across_long_gap(self):
        _, session, feed = self.session()
        now = time.time()
        session.clock = lambda: now
        self.quotes(session, feed, now)
        session._tick()
        now += 3601
        self.quotes(session, feed, now, a=300)
        session._tick()
        self.assertEqual(session.result["result"], [3.0])

    def test_future_quotes_and_regressing_timestamps_are_rejected(self):
        identifier, session, feed = self.session()
        now = time.time()
        session.clock = lambda: now
        self.quotes(session, feed, now + 6)
        session._tick()
        self.assertIsNone(self.manager.get(identifier)["result"])
        previous = session.quotes["A"]
        feed.on_quote({**previous, "ts": previous["ts"] - 1, "mid": 999})
        self.assertEqual(session.quotes["A"]["mid"], previous["mid"])

    def test_expired_result_is_cleared_even_without_new_fresh_quotes(self):
        identifier, session, feed = self.session()
        now = time.time()
        session.clock = lambda: now
        self.quotes(session, feed, now)
        session._tick()
        now += 3601
        session._tick()
        self.assertEqual(len(session.samples), 0)
        self.assertIsNone(self.manager.get(identifier)["result"])

    def test_stopped_session_churn_keeps_total_retention_bounded(self):
        for _ in range(12):
            identifier = self.manager.start(self.config)
            self.manager.stop(identifier)
        self.assertLessEqual(len(self.manager.sessions), 8)

    def test_rejects_unsupported_sources_and_unsafe_formulas(self):
        with self.assertRaises(ds.DataError):
            self.manager.start({**self.config, "formula": "__import__('os')"})
        with self.assertRaises(ds.DataError):
            self.manager.start({**self.config, "legs": [{"id": "A", "exchange": "ls", "ticker": "BTC"}], "formula": "A"})
        with self.assertRaises(ds.DataError):
            self.manager.start({**self.config, "formula": "MA(A, 4000)"})
        with self.assertRaises(ds.DataError):
            self.manager.start({**self.config, "legs": [{"id": [], "exchange": "okx", "ticker": "BTC"}]})

    def test_bounded_sessions_and_stop_cleanup(self):
        first = self.manager.start(self.config)
        self.manager.start(self.config)
        with self.assertRaises(ds.DataError):
            self.manager.start(self.config)
        self.manager.stop(first)
        self.assertEqual(self.manager.get(first)["status"], "stopped")
        self.assertTrue(self.manager.sessions[first].feed.stopped)
        self.manager.close()
        with self.assertRaises(KeyError):
            self.manager.get(first)


if __name__ == "__main__":
    unittest.main()
