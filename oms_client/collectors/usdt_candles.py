"""Backfill completed Bithumb/Upbit KRW-USDT candles through public REST APIs."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from oms_client.storage.candle_samples import MinuteCandleStore
from oms_client.storage.orderbook_samples import (
    CollectorAlreadyRunning,
    EXCHANGES,
    default_data_root,
)

MINUTE_MS = 60_000
ENDPOINTS = {exchange: f"https://api.{exchange}.com/v1/candles/minutes/1" for exchange in EXCHANGES}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
KST = timezone(timedelta(hours=9))


class CandleFetchError(RuntimeError):
    pass


class RequestLimitExceeded(CandleFetchError):
    pass


def request_to(exchange: str, utc_ms: int) -> str:
    stamp = datetime.fromtimestamp(utc_ms / 1000, timezone.utc)
    # Bithumb's official API uses KST without a timezone suffix. Supplying Z
    # or an offset can produce an HTTP-200 error object rather than a candle.
    if exchange == "bithumb":
        return stamp.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S")
    if exchange == "upbit":
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError("Unsupported exchange")


def normalize_candle(item: dict, fetched_at: int) -> dict:
    try:
        if not isinstance(item, dict) or item.get("market") != "KRW-USDT" or item.get("unit") != 1:
            raise ValueError("Wrong market or candle interval")
        stamp = datetime.fromisoformat(item["candle_date_time_utc"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        opened = int(stamp.timestamp() * 1000)
        if opened <= 0 or opened % MINUTE_MS or stamp.second or stamp.microsecond:
            raise ValueError("Candle is not aligned to a UTC minute")
        values = {
            name: float(item[source])
            for name, source in (
                ("open", "opening_price"),
                ("high", "high_price"),
                ("low", "low_price"),
                ("close", "trade_price"),
                ("volume", "candle_acc_trade_volume"),
                ("trade_value", "candle_acc_trade_price"),
            )
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("Non-finite candle value")
        if min(values[name] for name in ("open", "high", "low", "close")) <= 0:
            raise ValueError("Non-positive candle price")
        if values["high"] < max(values["open"], values["close"], values["low"]) or values[
            "low"
        ] > min(values["open"], values["close"]):
            raise ValueError("Invalid OHLC range")
        if values["volume"] < 0 or values["trade_value"] < 0:
            raise ValueError("Negative candle volume")
        return {
            "candleTime": opened,
            "availableTime": opened + MINUTE_MS,
            **values,
            "fetchedAt": int(fetched_at),
            "market": "KRW-USDT",
            "price_unit": "KRW",
            "quantity_unit": "USDT",
            "candle_interval_ms": MINUTE_MS,
            "source": "rest_1m",
        }
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        raise CandleFetchError("Exchange returned an invalid KRW-USDT 1-minute candle") from None


class PublicMinuteCandleClient:
    def __init__(
        self,
        *,
        opener=urlopen,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
        max_requests=2000,
        max_retries=4,
        requests_per_second=3,
    ):
        if not 1 <= max_requests <= 20_000 or not 0 <= max_retries <= 8:
            raise ValueError("Invalid request/retry limit")
        if not math.isfinite(requests_per_second) or not 0 < requests_per_second <= 3:
            raise ValueError("Request rate must be greater than 0 and no more than 3 per second")
        self.opener, self.clock, self.monotonic, self.sleep = opener, clock, monotonic, sleep
        self.max_requests, self.max_retries = max_requests, max_retries
        self.request_interval = 1 / requests_per_second
        self.requests = 0
        self.last_request = None

    def _wait_for_slot(self):
        if self.requests >= self.max_requests:
            raise RequestLimitExceeded(
                f"REST request limit ({self.max_requests}) reached; completed pages are preserved"
            )
        if self.last_request is not None:
            self.sleep(max(0, self.request_interval - (self.monotonic() - self.last_request)))
        self.last_request = self.monotonic()
        self.requests += 1

    def _retry_delay(self, error, attempt):
        delay = min(30, 2**attempt)
        retry_after = (
            error.headers.get("Retry-After")
            if isinstance(error, HTTPError) and error.headers
            else None
        )
        if retry_after:
            try:
                requested = float(retry_after)
            except ValueError:
                try:
                    requested = parsedate_to_datetime(retry_after).timestamp() - self.clock()
                except (TypeError, ValueError, OverflowError):
                    requested = 0
            if math.isfinite(requested):
                delay = max(delay, min(60, max(0, requested)))
        return delay

    def fetch_page(self, exchange: str, to_ms: int) -> list[dict]:
        if exchange not in EXCHANGES:
            raise ValueError("Unsupported exchange")
        query = urlencode({"market": "KRW-USDT", "count": 200, "to": request_to(exchange, to_ms)})
        request = Request(
            f"{ENDPOINTS[exchange]}?{query}",
            headers={"Accept": "application/json", "User-Agent": "OMS-Client-USDT-Backfill/1.0"},
        )
        for attempt in range(self.max_retries + 1):
            self._wait_for_slot()
            try:
                with self.opener(request, timeout=15) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CandleFetchError(f"{exchange}: REST response exceeded size limit")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise CandleFetchError(f"{exchange}: REST returned invalid JSON") from None
                if not isinstance(data, list) or len(data) > 200:
                    raise CandleFetchError(f"{exchange}: REST did not return a valid candle page")
                return data
            except HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code <= 599
                delay = self._retry_delay(error, attempt)
                error.close()
                if not retryable or attempt >= self.max_retries:
                    raise CandleFetchError(f"{exchange}: REST HTTP {error.code}") from None
                self.sleep(delay)
            except (URLError, TimeoutError, OSError) as error:
                if attempt >= self.max_retries:
                    raise CandleFetchError(
                        f"{exchange}: REST network failure ({type(error).__name__})"
                    ) from None
                self.sleep(self._retry_delay(error, attempt))
        raise CandleFetchError(f"{exchange}: REST retry limit reached")


def backfill_exchange(
    exchange: str, start_ms: int, end_ms: int, client, store, *, clock=time.time
) -> dict:
    """Fetch descending exclusive pages. No trade/no candle intervals stay empty."""
    if exchange not in EXCHANGES or start_ms >= end_ms or end_ms % MINUTE_MS:
        raise ValueError("Invalid exchange or candle window")
    cursor, received, inserted, pages = end_ms, 0, 0, 0
    earliest = latest = None
    while cursor > start_ms:
        page = client.fetch_page(exchange, cursor)
        pages += 1
        if not page:
            break
        fetched_at = int(clock() * 1000)
        normalized = [normalize_candle(item, fetched_at) for item in page]
        oldest = min(row["candleTime"] for row in normalized)
        if oldest >= cursor:
            raise CandleFetchError(f"{exchange}: REST pagination did not move backwards")
        by_time = {
            row["candleTime"]: row
            for row in normalized
            if start_ms <= row["candleTime"] < cursor
            and row["availableTime"] <= end_ms
            and row["availableTime"] <= fetched_at
        }
        selected = [by_time[key] for key in sorted(by_time)]
        if selected:
            inserted += store.merge(exchange, selected)
            received += len(selected)
            earliest = (
                selected[0]["candleTime"]
                if earliest is None
                else min(earliest, selected[0]["candleTime"])
            )
            latest = (
                selected[-1]["candleTime"]
                if latest is None
                else max(latest, selected[-1]["candleTime"])
            )
        cursor = oldest
    return {
        "exchange": exchange,
        "pages": pages,
        "candles_received": received,
        "candles_inserted": inserted,
        "earliest_candle_ms": earliest,
        "latest_candle_ms": latest,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=float,
        default=30,
        help="Completed recent days; fractional days are useful for smoke runs",
    )
    parser.add_argument("--exchange", choices=(*EXCHANGES, "all"), default="all")
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument(
        "--max-requests",
        type=int,
        default=2000,
        help="Total HTTP attempts across exchanges, including retries (max 20000)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.days) or not 0 < args.days <= 365:
        parser.error("--days must be greater than 0 and no more than 365")
    if not 1 <= args.max_requests <= 20_000:
        parser.error("--max-requests must be between 1 and 20000")
    end_ms = int(time.time() * 1000) // MINUTE_MS * MINUTE_MS
    start_ms = end_ms - max(1, int(args.days * 1440)) * MINUTE_MS
    client = PublicMinuteCandleClient(max_requests=args.max_requests)
    try:
        with MinuteCandleStore(args.data_root) as store:
            results = [
                backfill_exchange(exchange, start_ms, end_ms, client, store)
                for exchange in (EXCHANGES if args.exchange == "all" else (args.exchange,))
            ]
        print(
            json.dumps(
                {
                    "source": "rest_1m",
                    "market": "KRW-USDT",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "requests": client.requests,
                    "data_root": str(Path(args.data_root).expanduser().resolve()),
                    "exchanges": results,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (CandleFetchError, CollectorAlreadyRunning) as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Candle backfill failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
