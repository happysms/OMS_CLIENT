from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.collectors.usdt_orderbook import UsdtOrderbookCollector, write_status_file
from oms_client.market_data.quotes import parse_quotes
from oms_client.storage.orderbook_samples import (
    CollectorAlreadyRunning, OrderbookSampleStore, SpoolFull, default_state_dir, parquet_path,
)
from oms_client.paths import ROOT


NOW = 1_792_915_291_123


class FakeFeed:
    def __init__(self, legs, on_quote, on_status):
        self.legs, self.on_quote, self.on_status = legs, on_quote, on_status
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = OrderbookSampleStore(self.root / "market", self.root / "state")
        self.collector = UsdtOrderbookCollector(self.store, feed_factory=FakeFeed, clock=lambda: NOW / 1000)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def quote(self, exchange="bithumb", stamp=NOW, received=None, **changes):
        quote = {"exchange": exchange, "ticker": "USDT", "ts": stamp, "receivedAt": stamp if received is None else received,
                 "bid": 1342, "ask": 1343, "bid_size": 925511.1407, "ask_size": 0}
        quote.update(changes)
        self.collector.on_quote(quote)
        self.collector.on_status({"exchange": exchange, "state": "connected"})

    def test_default_state_dir_follows_dashboard_state_root(self):
        with patch.dict(os.environ, {"DASHBOARD_STATE_DIR": str(self.root / "dashboard-state")}):
            self.assertEqual(default_state_dir(), self.root / "dashboard-state" / "collectors" / "usdt")
        with patch.dict(os.environ, {"DASHBOARD_STATE_DIR": ""}):
            self.assertEqual(default_state_dir(), ROOT / ".state" / "collectors" / "usdt")

    def test_optional_parser_sizes_preserve_price_only_consumers(self):
        for exchange, stamp in (("bithumb", NOW * 1000 + 123), ("upbit", NOW)):
            message = {"type": "orderbook", "code": "KRW-USDT", "timestamp": stamp,
                       "orderbook_units": [{"bid_price": 1342, "ask_price": 1343,
                                            "bid_size": "925511.1407", "ask_size": "0"}]}
            quote = parse_quotes(exchange, message)[0]
            self.assertEqual(quote["ts"], NOW)
            self.assertEqual(quote["bid_size"], 925511.1407)
            self.assertEqual(quote["ask_size"], 0)
            message["orderbook_units"][0]["bid_size"] = "nan"
            message["orderbook_units"][0].pop("ask_size")
            quote = parse_quotes(exchange, message)[0]
            self.assertEqual(quote["bid"], 1342)
            self.assertNotIn("bid_size", quote)
            self.assertNotIn("ask_size", quote)

    def test_grid_once_per_second_and_no_delayed_backfill(self):
        self.quote()
        self.quote("upbit")
        rows = self.collector.sample_once(NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["desktopTime"], NOW)
        self.assertEqual(rows[0]["sampledAt"], NOW)
        self.assertEqual(rows[0]["sampleTime"], NOW // 1000 * 1000)
        self.assertEqual(self.collector.sample_once(NOW + 300), [])
        self.assertEqual(len(self.collector.sample_once(NOW + 5300)), 2)
        self.assertEqual(self.collector.missed_slots, 4)
        self.assertEqual(self.store.pending_rows, 4)
        self.assertEqual({row["sampleTime"] for row in self.store.pending()}, {NOW // 1000 * 1000, (NOW + 5300) // 1000 * 1000})
        # A backwards clock never rewrites prior samples or invents new rows.
        self.assertEqual(self.collector.sample_once(NOW - 1000), [])

    def test_quantity_is_usdt_and_prices_are_same_snapshot(self):
        self.quote()
        self.quote(stamp=NOW + 1, bid=1341, ask=1344, bid_size=1.25, ask_size=2.5)
        rows = self.collector.sample_once(NOW + 2)
        self.assertEqual((rows[0]["b1_p"], rows[0]["a1_p"], rows[0]["bid_size"], rows[0]["ask_size"]),
                         (1341, 1344, 1.25, 2.5))
        self.assertEqual(rows[0]["price_unit"], "KRW")
        self.assertEqual(rows[0]["quantity_unit"], "USDT")
        self.assertNotIn("b1_q", rows[0])
        self.assertEqual(rows[0]["quote_age_ms"], 1)

    def test_source_and_receive_freshness_are_both_required(self):
        cases = [({"stamp": NOW - 10_001, "received": NOW}, "stale_quote"),
                 ({"stamp": NOW, "received": NOW - 10_001}, "stale_quote"),
                 ({"stamp": NOW + 5_001, "received": NOW}, "future_quote"),
                 ({"stamp": NOW, "received": NOW + 1}, "future_quote")]
        for quote, reason in cases:
            with self.subTest(reason=reason, quote=quote):
                self.collector.last_slot = None
                self.collector.latest.clear()
                self.quote(**quote)
                self.assertEqual(self.collector.sample_once(NOW), [])
                self.assertGreater(self.collector.exchanges["bithumb"]["skip_reasons"][reason], 0)

    def test_missing_invalid_sizes_and_crossed_quotes_are_skipped(self):
        for changes in ({"bid_size": None}, {"ask_size": float("inf")}, {"bid_size": -1}, {"bid": 1344}):
            with self.subTest(changes=changes):
                self.collector.last_slot = None
                self.quote(**changes)
                self.assertEqual(self.collector.sample_once(NOW), [])
        json.dumps(self.collector.status(), allow_nan=False)

    def test_disconnect_clears_cache_and_reconnect_requires_a_new_quote(self):
        self.quote()
        self.collector.on_status({"exchange": "bithumb", "state": "reconnecting", "message": "https://user:secret@example.invalid"})
        self.assertNotIn("bithumb", self.collector.latest)
        self.collector.on_status({"exchange": "bithumb", "state": "connected"})
        self.assertEqual(self.collector.sample_once(NOW), [])
        self.quote(stamp=NOW + 1000)
        self.assertEqual(len(self.collector.sample_once(NOW + 1000)), 1)
        self.assertNotIn("secret", json.dumps(self.collector.status()))

    def test_regressing_exchange_snapshot_is_not_mixed_with_newer_book(self):
        self.quote()
        self.quote(stamp=NOW - 1, bid=1, ask=2, bid_size=999)
        row = self.collector.sample_once(NOW)[0]
        self.assertEqual(row["b1_p"], 1342)
        self.assertEqual(row["bid_size"], 925511.1407)

    def test_atomic_hourly_publication_and_utc_schema(self):
        self.quote()
        row = self.collector.sample_once(NOW)[0]
        path = parquet_path(self.store.data_root, "bithumb", NOW)
        stamp = datetime.fromtimestamp(NOW / 1000, timezone.utc)
        self.assertTrue(str(path).endswith(stamp.strftime("bithumb/ob_1s/USDT/%Y/%m/%d/%H.parquet")))
        self.assertEqual(self.store.flush(), 1)
        self.assertEqual(self.store.pending_rows, 0)
        stored = pq.read_table(path).to_pylist()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0], {key: value for key, value in row.items() if key != "exchange"})
        self.assertEqual(stored[0]["bid_size"], 925511.1407)
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_publish_failure_keeps_committed_spool_and_previous_file(self):
        self.quote()
        self.collector.sample_once(NOW)
        self.store.flush()
        self.quote(stamp=NOW + 1000)
        self.collector.sample_once(NOW + 1000)
        path = parquet_path(self.store.data_root, "bithumb", NOW)
        before = path.read_bytes()
        with patch("oms_client.storage.orderbook_samples.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.pending_rows, 1)
        self.store.close()
        self.store = OrderbookSampleStore(self.root / "market", self.root / "state")
        self.assertEqual(self.store.pending_rows, 1)
        self.assertEqual(self.store.flush(), 1)
        self.assertEqual(len(pq.read_table(path)), 2)

    def test_crash_after_publish_before_prune_is_deduplicated_on_restart(self):
        self.quote()
        self.collector.sample_once(NOW)
        path = parquet_path(self.store.data_root, "bithumb", NOW)
        self.store._publish(path, self.store.pending())  # Simulate a crash before SQLite deletion.
        self.store.close()
        self.store = OrderbookSampleStore(self.root / "market", self.root / "state")
        self.assertEqual(self.store.pending_rows, 1)
        self.store.flush()
        self.assertEqual(self.store.pending_rows, 0)
        self.assertEqual(len(pq.read_table(path)), 1)

    def test_directory_sync_failure_does_not_prune_the_spool(self):
        self.quote()
        self.collector.sample_once(NOW)
        path = parquet_path(self.store.data_root, "bithumb", NOW)
        from oms_client.storage import orderbook_samples
        real_sync = orderbook_samples.fsync_directory

        def fail_after_replace(directory):
            if path.exists():
                raise OSError("directory sync unavailable")
            real_sync(directory)

        with patch.object(orderbook_samples, "fsync_directory", side_effect=fail_after_replace):
            with self.assertRaises(OSError):
                self.store.flush()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.pending_rows, 1)
        self.store.flush()
        self.assertEqual(len(pq.read_table(path)), 1)
        self.assertEqual(self.store.pending_rows, 0)

    def test_delayed_run_loop_uses_actual_time_without_catching_up(self):
        current = [NOW / 1000]

        class FakeEvent:
            delayed = False

            def is_set(self):
                return False

            def wait(self, seconds):
                current[0] += seconds
                if not self.delayed:
                    current[0] += 4.2  # Simulated scheduler suspension.
                    self.delayed = True

        self.quote()
        self.collector.clock = self.collector.monotonic = lambda: current[0]
        self.collector.stop_event = FakeEvent()
        self.assertEqual(self.collector.run(duration=6.2), 0)
        path = parquet_path(self.store.data_root, "bithumb", NOW)
        rows = pq.read_table(path).to_pylist()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["desktopTime"], NOW)
        self.assertGreaterEqual(rows[1]["desktopTime"], NOW + 5000)
        self.assertEqual(self.collector.missed_slots, 4)
        self.assertTrue(all(row["receivedTime"] <= row["sampledAt"] == row["desktopTime"] for row in rows))

    def test_unique_grid_key_and_spool_limit_preserve_existing_data(self):
        self.quote()
        row = self.collector.sample_once(NOW)[0]
        self.assertEqual(self.store.append([{**row, "desktopTime": NOW + 20, "sampledAt": NOW + 20}]), 0)
        self.store.max_pending = 2
        self.store.append([{**row, "sampleTime": row["sampleTime"] + 1000, "desktopTime": NOW + 1000}])
        with self.assertRaises(SpoolFull):
            self.store.append([{**row, "sampleTime": row["sampleTime"] + 2000, "desktopTime": NOW + 2000}])
        self.assertEqual(self.store.pending_rows, 2)
        self.assertEqual(self.store.counters()["samples_committed"], 2)

    def test_data_root_lock_blocks_second_writer_with_different_state_dir(self):
        with self.assertRaises(CollectorAlreadyRunning):
            OrderbookSampleStore(self.root / "market", self.root / "other-state")
        self.store.close()
        self.store = OrderbookSampleStore(self.root / "market", self.root / "other-state")

    def test_state_dir_cannot_be_shared_or_rebound_to_another_dataset(self):
        with self.assertRaises(CollectorAlreadyRunning):
            OrderbookSampleStore(self.root / "other-market", self.root / "state")
        self.store.close()
        with self.assertRaisesRegex(ValueError, "another data root"):
            OrderbookSampleStore(self.root / "other-market", self.root / "state")
        self.store = OrderbookSampleStore(self.root / "market", self.root / "state")

    def test_status_heartbeat_is_atomic_and_stops_cleanly(self):
        self.quote()
        self.collector.sample_once(NOW)
        self.collector.write_status()
        path = self.store.state_dir / "status.json"
        status = json.loads(path.read_text())
        self.assertEqual(status["heartbeat_ms"], NOW)
        self.assertEqual(status["collector_id"], "usdt")
        self.assertEqual(status["pending_rows"], 1)
        self.assertEqual(status["exchanges"]["bithumb"]["last_quote"]["bid_size"], 925511.1407)
        before = path.read_bytes()
        with patch("oms_client.collectors.usdt_orderbook.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                write_status_file(path, {"broken": True})
        self.assertEqual(path.read_bytes(), before)
        self.collector.stop_event.set()
        self.assertEqual(self.collector.run(), 0)
        self.assertTrue(self.collector.feed.stopped)
        self.assertEqual(self.store.pending_rows, 0)
        self.assertEqual(json.loads(path.read_text())["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
