"""Collect Bithumb/Upbit KRW-USDT top-of-book snapshots once per second."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

from oms_client.market_data.quotes import PublicQuoteFeed
from oms_client.storage.orderbook_samples import (
    CollectorAlreadyRunning,
    DEPTH_COLUMNS,
    EXCHANGES,
    OrderbookSampleStore,
    SpoolFull,
    default_data_root,
    default_state_dir,
    fsync_directory,
)

LEGS = [
    {"id": "A", "exchange": "bithumb", "ticker": "USDT"},
    {"id": "B", "exchange": "upbit", "ticker": "USDT"},
]
SAMPLE_INTERVAL_MS = 1000
MAX_QUOTE_AGE_MS = 10_000
MAX_FUTURE_MS = 5_000


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def write_status_file(path: Path, status: dict) -> None:
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".status.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(status, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class UsdtOrderbookCollector:
    def __init__(
        self,
        store: OrderbookSampleStore,
        *,
        feed_factory=PublicQuoteFeed,
        clock=time.time,
        monotonic=time.monotonic,
        flush_seconds=5,
        max_quote_age_ms=MAX_QUOTE_AGE_MS,
    ):
        if not math.isfinite(flush_seconds) or flush_seconds <= 0:
            raise ValueError("flush_seconds must be positive")
        self.store, self.clock, self.monotonic = store, clock, monotonic
        self.flush_seconds, self.max_quote_age_ms = flush_seconds, max_quote_age_ms
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.feed = feed_factory(LEGS, self.on_quote, self.on_status)
        self.started_at_ms = int(clock() * 1000)
        self.started_monotonic = monotonic()
        self.state = "starting"
        self.latest = {}
        self.last_slot = None
        self.last_sample_ms = None
        self.last_flush_ms = None
        self.errors = Counter()
        self.last_error = None
        self.missed_slots = 0
        self.exchanges = {
            exchange: {
                "state": "starting",
                "connected": False,
                "last_quote": None,
                "last_sample_ms": None,
                "rows_committed": 0,
                "quotes_received": 0,
                "skipped_samples": 0,
                "skip_reasons": Counter(),
            }
            for exchange in EXCHANGES
        }

    def on_quote(self, quote):
        exchange = quote.get("exchange")
        if exchange not in EXCHANGES or quote.get("ticker") != "USDT":
            return
        normalized = {
            name: _finite(quote.get(source))
            for name, source in (
                ("bid", "bid"),
                ("ask", "ask"),
                ("bid_size", "bid_size"),
                ("ask_size", "ask_size"),
                ("serverTime", "ts"),
                ("receivedTime", "receivedAt"),
            )
        }
        for side, price_key, size_key in (('bids', 'bid', 'bid_size'), ('asks', 'ask', 'ask_size')):
            normalized[side] = [[normalized[price_key], normalized[size_key]]]
            source_book = quote.get(side)
            if not isinstance(source_book, list) or not source_book:
                continue
            try:
                if _finite(source_book[0][0]) != normalized[price_key]:
                    continue
            except (TypeError, IndexError):
                continue
            for item in source_book[1:5]:
                try:
                    price, size = _finite(item[0]), _finite(item[1])
                    previous = normalized[side][-1][0]
                    if (
                        price is None
                        or size is None
                        or price <= 0
                        or size < 0
                        or previous is None
                        or (side == 'bids' and price > previous)
                        or (side == 'asks' and price < previous)
                    ):
                        break
                    normalized[side].append([price, size])
                except (TypeError, IndexError):
                    break
        with self.lock:
            previous = self.latest.get(exchange)
            if (
                previous
                and normalized["serverTime"] is not None
                and previous["serverTime"] is not None
            ):
                if normalized["serverTime"] < previous["serverTime"]:
                    return
            self.latest[exchange] = normalized
            state = self.exchanges[exchange]
            state["last_quote"] = normalized
            state["quotes_received"] += 1

    def on_status(self, status):
        exchange, state = status.get("exchange"), status.get("state")
        if exchange not in EXCHANGES:
            return
        with self.lock:
            record = self.exchanges[exchange]
            record["state"] = (
                state
                if state in {"connecting", "connected", "reconnecting", "error", "stopped"}
                else "error"
            )
            record["connected"] = state == "connected"
            if state != "connected":
                self.latest.pop(exchange, None)
            if state in {"error", "reconnecting"}:
                self.errors["connection"] += 1
                # Never copy arbitrary network/proxy exception messages to disk.
                self.last_error = f"{exchange}: public quote connection unavailable"

    def _sample(self, exchange, now_ms, slot):
        if not self.exchanges[exchange]["connected"]:
            return None, "disconnected"
        quote = self.latest.get(exchange)
        if quote is None:
            return None, "missing_quote"
        bid, ask = quote["bid"], quote["ask"]
        bid_size, ask_size = quote["bid_size"], quote["ask_size"]
        if bid is None or ask is None or bid <= 0 or ask < bid:
            return None, "invalid_price"
        if bid_size is None or ask_size is None or bid_size < 0 or ask_size < 0:
            return None, "missing_or_invalid_size"
        source, received = quote["serverTime"], quote["receivedTime"]
        if source is None or received is None or source <= 0 or received <= 0:
            return None, "invalid_timestamp"
        age, receive_age = now_ms - source, now_ms - received
        # Only the exchange clock may be skewed. A local message received
        # after this observation cannot belong to the sampled snapshot.
        if age < -MAX_FUTURE_MS or receive_age < 0:
            return None, "future_quote"
        if age > self.max_quote_age_ms or receive_age > self.max_quote_age_ms:
            return None, "stale_quote"
        depth = dict.fromkeys(DEPTH_COLUMNS)
        for side, prefix, size_key in (('bids', 'b', 'bid_size'), ('asks', 'a', 'ask_size')):
            for level, (price, size) in enumerate(quote[side][1:5], start=2):
                depth[f'{prefix}{level}_p'] = price
                depth[f'{size_key}_{level}'] = size
        return {
            "exchange": exchange,
            "sampleTime": slot,
            **depth,
            # desktopTime is the actual observation time. The separate grid
            # key must not make a late snapshot appear earlier in backtests.
            "desktopTime": now_ms,
            "sampledAt": now_ms,
            "serverTime": int(source),
            "receivedTime": int(received),
            "b1_p": bid,
            "a1_p": ask,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "quote_age_ms": int(age),
            "price_unit": "KRW",
            "quantity_unit": "USDT",
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "market": "KRW-USDT",
        }, None

    def sample_once(self, now_ms=None) -> list[dict]:
        rows = []
        with self.lock:
            # Capture time while holding the quote lock. Otherwise a newer
            # callback could slip in between the clock read and the snapshot.
            now_ms = int(self.clock() * 1000) if now_ms is None else int(now_ms)
            slot = now_ms // SAMPLE_INTERVAL_MS * SAMPLE_INTERVAL_MS
            if self.last_slot is not None and slot <= self.last_slot:
                return []
            if self.last_slot is not None:
                self.missed_slots += max(0, (slot - self.last_slot) // SAMPLE_INTERVAL_MS - 1)
            self.last_slot = slot
            for exchange in EXCHANGES:
                row, reason = self._sample(exchange, now_ms, slot)
                if row is not None:
                    rows.append(row)
                else:
                    record = self.exchanges[exchange]
                    record["skipped_samples"] += 1
                    record["skip_reasons"][reason] += 1
        self.store.append(rows)
        if rows:
            self.last_sample_ms = now_ms
            with self.lock:
                for row in rows:
                    record = self.exchanges[row["exchange"]]
                    record["rows_committed"] += 1
                    record["last_sample_ms"] = now_ms
        return rows

    def status(self) -> dict:
        now_ms = int(self.clock() * 1000)
        with self.lock:
            exchanges = {
                exchange: {**record, "skip_reasons": dict(record["skip_reasons"])}
                for exchange, record in self.exchanges.items()
            }
            for record in exchanges.values():
                quote = record["last_quote"]
                record["quote_age_ms"] = (
                    int(now_ms - quote["serverTime"]) if quote and quote["serverTime"] else None
                )
            errors, last_error = dict(self.errors), self.last_error
        counts = self.store.counters()
        return {
            "version": 1,
            "collector_id": "usdt",
            "market": "KRW-USDT",
            "state": self.state,
            "price_unit": "KRW",
            "quantity_unit": "USDT",
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "max_quote_age_ms": self.max_quote_age_ms,
            "heartbeat_ms": now_ms,
            "started_at_ms": self.started_at_ms,
            "uptime_seconds": round(max(0, self.monotonic() - self.started_monotonic), 3),
            "data_root": str(self.store.data_root),
            "state_dir": str(self.store.state_dir),
            "last_sample_ms": self.last_sample_ms,
            "last_flush_ms": self.last_flush_ms,
            "rows_committed": counts.get("samples_committed", 0),
            "rows_flushed": counts.get("samples_flushed", 0),
            "pending_rows": self.store.pending_rows,
            "missed_slots": self.missed_slots,
            "errors": errors,
            "last_error": last_error,
            "exchanges": exchanges,
        }

    def write_status(self):
        write_status_file(self.store.state_dir / "status.json", self.status())

    def _error(self, category, error):
        # Type names help identify disk/schema issues without leaking network
        # URLs, credentials, SQL values or complete tick payloads into logs.
        with self.lock:
            self.errors[category] += 1
            self.last_error = f"{category}: {type(error).__name__}"

    def flush(self) -> bool:
        try:
            self.store.flush()
            self.last_flush_ms = int(self.clock() * 1000)
            return True
        except Exception as error:
            self._error("flush", error)
            return False

    def run(self, duration=None) -> int:
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            raise ValueError("duration must be positive")
        deadline = None if duration is None else self.monotonic() + duration
        next_flush = self.monotonic()
        exit_code = 0
        self.state = "running"
        try:
            self.feed.start()
            while not self.stop_event.is_set():
                now_monotonic = self.monotonic()
                if deadline is not None and now_monotonic >= deadline:
                    break
                self.sample_once()
                if now_monotonic >= next_flush:
                    self.flush()
                    try:
                        self.write_status()
                    except Exception as error:
                        self._error("status", error)
                    next_flush = now_monotonic + self.flush_seconds
                # Wait for the next *current* UTC second, never replay slots
                # skipped by a slow flush, scheduling delay or suspended host.
                wait = max(0.001, (math.floor(self.clock()) + 1) - self.clock())
                if deadline is not None:
                    wait = min(wait, max(0, deadline - self.monotonic()))
                self.stop_event.wait(min(wait, 1))
        except Exception as error:
            self._error("collector", error)
            self.state = "error"
            exit_code = 1
        finally:
            try:
                self.feed.stop()
            except Exception as error:
                self._error("shutdown", error)
                exit_code = 1
            if not self.flush():
                exit_code = 1
            self.state = "error" if exit_code else "stopped"
            with self.lock:
                for record in self.exchanges.values():
                    record["connected"] = False
                    record["state"] = "stopped"
                self.latest.clear()
            try:
                self.write_status()
            except Exception as error:
                self._error("status", error)
                exit_code = 1
        return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--duration", type=float, help="Stop after this many seconds (smoke run)")
    parser.add_argument("--flush-seconds", type=float, default=5)
    parser.add_argument("--max-pending", type=int, default=36_000)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            print(
                json.dumps(
                    json.loads((args.state_dir / "status.json").read_text(encoding="utf-8")),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        except FileNotFoundError:
            print("Collector status is not available yet.", file=sys.stderr)
            return 1
    if not math.isfinite(args.flush_seconds) or args.flush_seconds <= 0:
        parser.error("--flush-seconds must be positive")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("--duration must be positive")
    try:
        with OrderbookSampleStore(args.data_root, args.state_dir, args.max_pending) as store:
            collector = UsdtOrderbookCollector(store, flush_seconds=args.flush_seconds)
            previous = {}
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[number] = signal.signal(number, lambda *_: collector.stop_event.set())
            try:
                return collector.run(args.duration)
            finally:
                for number, handler in previous.items():
                    signal.signal(number, handler)
    except (CollectorAlreadyRunning, SpoolFull, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Collector failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
