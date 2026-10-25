from datetime import datetime, timezone
from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.collectors.usdt_candles import (
    CandleFetchError, MINUTE_MS, PublicMinuteCandleClient, RequestLimitExceeded,
    backfill_exchange, normalize_candle, request_to,
)
from oms_client.storage.candle_samples import MinuteCandleStore, candle_path
from oms_client.storage.orderbook_samples import CollectorAlreadyRunning, OrderbookSampleStore

END = int(datetime(2026, 10, 25, 8, 17, tzinfo=timezone.utc).timestamp() * 1000)


def raw_candle(opened, **changes):
    row = {"market": "KRW-USDT", "unit": 1,
           "candle_date_time_utc": datetime.fromtimestamp(opened / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
           "opening_price": 1343, "high_price": 1344, "low_price": 1342, "trade_price": 1343,
           "candle_acc_trade_volume": 14977.42471398, "candle_acc_trade_price": 20103654.31853714,
           "timestamp": opened + 54193}
    row.update(changes)
    return row


class FakePageClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.cursors = []

    def fetch_page(self, exchange, cursor):
        self.cursors.append((exchange, cursor))
        return next(self.pages)


class FakeHttp:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "market"
        self.store = MinuteCandleStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_official_exchange_specific_exclusive_to_format(self):
        self.assertEqual(request_to("bithumb", END), "2026-10-25T17:17:00")
        self.assertEqual(request_to("upbit", END), "2026-10-25T08:17:00Z")
        http, clock = FakeHttp([[], []]), FakeClock()
        client = PublicMinuteCandleClient(opener=http, monotonic=clock.now, sleep=clock.sleep)
        client.fetch_page("bithumb", END)
        client.fetch_page("upbit", END)
        for index, exchange in enumerate(("bithumb", "upbit")):
            query = parse_qs(urlsplit(http.requests[index][0].full_url).query)
            self.assertEqual(query, {"market": ["KRW-USDT"], "count": ["200"], "to": [request_to(exchange, END)]})

    def test_close_availability_and_actual_quantity_units(self):
        opened = END - MINUTE_MS
        row = normalize_candle(raw_candle(opened), END + 1000)
        self.assertEqual(row["candleTime"], opened)
        self.assertEqual(row["availableTime"], END)  # Not the timestamp of the final trade.
        self.assertEqual(row["volume"], 14977.42471398)
        self.assertEqual(row["trade_value"], 20103654.31853714)
        self.assertEqual(row["quantity_unit"], "USDT")
        self.assertEqual(row["price_unit"], "KRW")
        self.assertEqual(row["source"], "rest_1m")
        self.assertNotIn("bid_size", row)

    def test_descending_pagination_deduplicates_and_leaves_no_trade_gaps_empty(self):
        client = FakePageClient([
            [raw_candle(END - MINUTE_MS), raw_candle(END - 2 * MINUTE_MS), raw_candle(END - 2 * MINUTE_MS)],
            [raw_candle(END - 2 * MINUTE_MS), raw_candle(END - 5 * MINUTE_MS), raw_candle(END - 6 * MINUTE_MS)],
        ])
        result = backfill_exchange("bithumb", END - 6 * MINUTE_MS, END, client, self.store, clock=lambda: END / 1000)
        self.assertEqual(client.cursors, [("bithumb", END), ("bithumb", END - 2 * MINUTE_MS)])
        self.assertEqual(result["candles_inserted"], 4)
        rows = pq.read_table(candle_path(self.root, "bithumb", END - MINUTE_MS)).to_pylist()
        self.assertEqual([row["candleTime"] for row in rows], [END - step * MINUTE_MS for step in (6, 5, 2, 1)])
        self.assertEqual(len(rows), 4)  # Minutes 3 and 4 are not fabricated.

    def test_current_and_out_of_window_candles_are_excluded(self):
        client = FakePageClient([[raw_candle(END), raw_candle(END - MINUTE_MS), raw_candle(END - 3 * MINUTE_MS)]])
        result = backfill_exchange("upbit", END - 2 * MINUTE_MS, END, client, self.store, clock=lambda: (END + 1000) / 1000)
        self.assertEqual(result["candles_inserted"], 1)
        rows = pq.read_table(candle_path(self.root, "upbit", END - MINUTE_MS)).to_pylist()
        self.assertEqual([row["candleTime"] for row in rows], [END - MINUTE_MS])

    def test_no_progress_is_fatal_and_empty_history_terminates(self):
        with self.assertRaisesRegex(CandleFetchError, "did not move"):
            backfill_exchange("upbit", END - MINUTE_MS, END, FakePageClient([[raw_candle(END)]]), self.store)
        result = backfill_exchange("upbit", END - MINUTE_MS, END, FakePageClient([[]]), self.store)
        self.assertEqual(result["candles_received"], 0)
        self.assertEqual(result["pages"], 1)

    def test_invalid_market_interval_ohlc_and_nonfinite_values_fail(self):
        for changes in ({"market": "KRW-BTC"}, {"unit": 5}, {"high_price": 1340}, {"low_price": 1344},
                        {"trade_price": float("nan")}, {"opening_price": 0}, {"candle_acc_trade_volume": -1},
                        {"candle_date_time_utc": "2026-10-25T08:16:00.000001"}):
            with self.subTest(changes=changes), self.assertRaises(CandleFetchError):
                normalize_candle(raw_candle(END - MINUTE_MS, **changes), END)

    def test_http_200_error_object_is_not_treated_as_an_empty_page(self):
        http = FakeHttp([{"error": {"name": 400, "message": "Invalid parameter"}}])
        client = PublicMinuteCandleClient(opener=http)
        with self.assertRaisesRegex(CandleFetchError, "valid candle page"):
            client.fetch_page("bithumb", END)

    def test_global_rate_limit_and_429_backoff_are_bounded(self):
        headers = Message()
        headers["Retry-After"] = "2"
        limited = HTTPError("https://api.upbit.com", 429, "slow", headers, io.BytesIO())
        clock = FakeClock()
        http = FakeHttp([limited, [], []])
        client = PublicMinuteCandleClient(opener=http, monotonic=clock.now, sleep=clock.sleep)
        client.fetch_page("upbit", END)
        self.assertGreaterEqual(clock.value, 2)
        before = clock.value
        client.fetch_page("bithumb", END)
        self.assertGreaterEqual(clock.value - before, 1 / 3 - 1e-9)
        self.assertEqual(client.requests, 3)

    def test_request_cap_counts_retries_and_never_logs_network_credentials(self):
        clock = FakeClock()
        http = FakeHttp([URLError("https://user:secret@example.invalid"), []])
        client = PublicMinuteCandleClient(opener=http, monotonic=clock.now, sleep=clock.sleep, max_requests=1)
        with self.assertRaises(RequestLimitExceeded):
            client.fetch_page("upbit", END)
        self.assertEqual(len(http.requests), 1)
        http = FakeHttp([URLError("https://user:secret@example.invalid")])
        client = PublicMinuteCandleClient(opener=http, max_retries=0)
        with self.assertRaises(CandleFetchError) as error:
            client.fetch_page("upbit", END)
        self.assertNotIn("secret", str(error.exception))

    def test_idempotent_daily_merge_preserves_existing_closed_candle_and_fetched_at(self):
        row = normalize_candle(raw_candle(END - MINUTE_MS), END)
        self.assertEqual(self.store.merge("upbit", [row, row]), 1)
        path = candle_path(self.root, "upbit", row["candleTime"])
        before = path.read_bytes()
        self.assertEqual(self.store.merge("upbit", [{**row, "close": 1344, "fetchedAt": END + MINUTE_MS}]), 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(str(path).endswith("upbit/candles_1m/USDT/2026/10/25.parquet"))

    def test_atomic_replace_failure_preserves_existing_day_and_retry_is_safe(self):
        first = normalize_candle(raw_candle(END - 2 * MINUTE_MS), END)
        second = normalize_candle(raw_candle(END - MINUTE_MS), END)
        self.store.merge("upbit", [first])
        path = candle_path(self.root, "upbit", first["candleTime"])
        before = path.read_bytes()
        with patch("oms_client.storage.candle_samples.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.store.merge("upbit", [second])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(path.parent.glob("*.tmp")))
        self.assertEqual(self.store.merge("upbit", [second]), 1)
        self.assertEqual(len(pq.read_table(path)), 2)

    def test_retry_after_directory_sync_failure_finishes_publication_without_duplicate(self):
        row = normalize_candle(raw_candle(END - MINUTE_MS), END)
        with patch("oms_client.storage.candle_samples.fsync_directory", side_effect=OSError("sync unavailable")):
            with self.assertRaises(OSError):
                self.store.merge("upbit", [row])
        path = candle_path(self.root, "upbit", row["candleTime"])
        self.assertTrue(path.exists())
        from oms_client.storage import candle_samples
        with patch.object(candle_samples, "fsync_directory", wraps=candle_samples.fsync_directory) as synchronize:
            self.assertEqual(self.store.merge("upbit", [row]), 0)
            synchronize.assert_called_with(path.parent)
        self.assertEqual(len(pq.read_table(path)), 1)

    def test_candle_writer_lock_is_independent_of_one_second_collector(self):
        with self.assertRaises(CollectorAlreadyRunning):
            MinuteCandleStore(self.root)
        with OrderbookSampleStore(self.root, Path(self.temporary.name) / "state"):
            row = normalize_candle(raw_candle(END - MINUTE_MS), END)
            self.assertEqual(self.store.merge("bithumb", [row]), 1)
        self.assertFalse(list(self.root.glob("*/ob_1s/USDT/**/*.parquet")))


if __name__ == "__main__":
    unittest.main()
