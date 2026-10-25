"""Read-only local order-book discovery, DuckDB queries and export series."""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
from pathlib import Path
import re
import threading

import duckdb
import pandas as pd

from ..paths import ROOT
from .validation import (DataError, QueryTimeout, IDENTIFIER, INTERVALS, KST,
                         array, integer, iso_timestamp, parse_date)


def data_root() -> Path:
    return Path(os.environ.get("DASHBOARD_DATA_ROOT", "/data")).resolve()


def collector_data_root() -> Path:
    """Keep application samples separate from the legacy /data retention job."""
    return Path(os.environ.get("DASHBOARD_COLLECTOR_DATA_ROOT", ROOT / "data" / "market")).expanduser().resolve()



def connection():
    threads = integer(os.environ.get("DASHBOARD_THREADS", "2"), "DuckDB 스레드 수", 1, 32)
    memory = os.environ.get("DASHBOARD_MEMORY_LIMIT", "256MB")
    if not re.fullmatch(r"[1-9]\d*(?:\.\d+)?\s*(?:KB|MB|GB|TB)", memory, re.IGNORECASE):
        raise DataError("DASHBOARD_MEMORY_LIMIT는 256MB, 1GB와 같이 지정해 주세요.")
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET threads = ?", [threads])
        con.execute("SET memory_limit = ?", [memory])
    except Exception:
        con.close()
        raise
    return con



@contextmanager
def query_deadline(con):
    try:
        seconds = float(os.environ.get("DASHBOARD_QUERY_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(seconds) or not 0.01 <= seconds <= 3600:
            raise ValueError("range")
    except (TypeError, ValueError) as exc:
        raise DataError("DASHBOARD_QUERY_TIMEOUT_SECONDS는 0.01~3600초로 지정해 주세요.") from exc
    timed_out = threading.Event()

    def interrupt():
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
        if timed_out.is_set():
            raise QueryTimeout("조회 제한 시간을 초과했습니다. 기간을 줄이거나 조회 간격을 늘려 주세요.")
    except duckdb.InterruptException as exc:
        if timed_out.is_set():
            raise QueryTimeout("조회 제한 시간을 초과했습니다. 기간을 줄이거나 조회 간격을 늘려 주세요.") from exc
        raise
    finally:
        timer.cancel()
        timer.join()



def within_root(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, RuntimeError):
        return False



def _source_directories() -> dict:
    found = {}
    # Dedicated 1-second USDT snapshots take precedence over legacy USDT data.
    # Their size columns contain token quantities; legacy *_q means notional KRW.
    for root, layout, kind in [(data_root(), "ob", "legacy"),
                               (collector_data_root(), "ob_1s", "orderbook_1s")]:
        if not root.is_dir():
            continue
        for exchange in sorted(root.iterdir()):
            ob = exchange / layout
            if (not IDENTIFIER.fullmatch(exchange.name) or not ob.is_dir()
                    or not within_root(ob, root)):
                continue
            if kind == "orderbook_1s" and exchange.name not in {"bithumb", "upbit"}:
                continue
            for symbol in sorted(ob.iterdir()):
                if (not symbol.is_dir() or not IDENTIFIER.fullmatch(symbol.name)
                        or not within_root(symbol, ob)):
                    continue
                if kind == "orderbook_1s" and symbol.name.upper() != "USDT":
                    continue
                found.setdefault(exchange.name, {})[symbol.name.upper()] = {
                    "ticker": symbol.name, "directory": symbol, "source_kind": kind,
                }
    return found


def list_symbols() -> dict[str, list[str]]:
    return {exchange: sorted(source["ticker"] for source in symbols.values())
            for exchange, symbols in sorted(_source_directories().items())}


def _resolve_source(exchange, ticker) -> dict:
    if not isinstance(exchange, str) or not isinstance(ticker, str):
        raise DataError("거래소와 종목을 선택해 주세요.")
    source = _source_directories().get(exchange, {}).get(ticker.strip().upper())
    if source is None:
        raise DataError(f"선택한 거래소·종목의 호가 데이터가 없습니다: {exchange} / {ticker}")
    return source



def validate_symbol(exchange, ticker, available=None) -> tuple[str, str]:
    if not isinstance(exchange, str) or not isinstance(ticker, str):
        raise DataError("거래소와 종목을 선택해 주세요.")
    ticker = ticker.strip()
    available = list_symbols() if available is None else available
    if exchange not in available:
        raise DataError(f"선택한 거래소·종목의 호가 데이터가 없습니다: {exchange} / {ticker}")
    lookup = {symbol.upper(): symbol for symbol in available[exchange]}
    normalized = lookup.get(ticker.upper())
    if normalized is None:
        raise DataError(f"선택한 거래소·종목의 호가 데이터가 없습니다: {exchange} / {ticker}")
    return exchange, normalized



def bounds(exchange: str, ticker: str) -> dict:
    """Return actual timestamp bounds from the first and last available files."""
    exchange, ticker = validate_symbol(exchange, ticker)
    directory = _resolve_source(exchange, ticker)["directory"]
    files = sorted(p for p in directory.glob("*/*/*/*.parquet") if within_root(p, directory))
    if not files:
        raise DataError(f"{exchange} {ticker}의 parquet 파일이 없습니다.")
    con = connection()
    try:
        start = end = None
        with query_deadline(con):
            for path in files:
                start = con.execute("SELECT min(desktopTime) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                if start is not None:
                    break
            for path in reversed(files):
                end = con.execute("SELECT max(desktopTime) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                if end is not None:
                    break
    except duckdb.Error as exc:
        raise DataError("호가 파일의 시간 범위를 읽지 못했습니다. parquet 파일을 확인해 주세요.") from exc
    finally:
        con.close()
    if start is None or end is None:
        raise DataError(f"{exchange} {ticker}의 호가 파일이 비어 있습니다.")
    return {"start": iso_timestamp(int(start)), "end": iso_timestamp(int(end)), "exchange": exchange, "ticker": ticker}



def files_for_range(exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    parent = _resolve_source(exchange, ticker)["directory"]
    files = []
    last = (end - pd.Timedelta(nanoseconds=1)).floor("h")
    first = start.floor("h")
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        directory = parent / day.strftime("%Y/%m/%d")
        if not directory.is_dir() or not within_root(directory, parent):
            continue
        for path in sorted(directory.glob("*.parquet")):
            if not within_root(path, parent):
                continue
            if path.stem.isdigit() and 0 <= int(path.stem) <= 23:
                hour = day + pd.Timedelta(hours=int(path.stem))
                if not first <= hour <= last:
                    continue
            files.append(str(path))
    return files



def read_buckets(exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp, step: int, *, price_mode: str = "mean"):
    """Read interval means or one complete final source quote per interval."""
    if not isinstance(price_mode, str) or price_mode not in {"mean", "last"}:
        raise DataError("가격 조회 방식은 마지막 호가 또는 평균으로 선택해 주세요.")
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError(f"선택한 기간에 {exchange} {ticker} 호가 파일이 없습니다.")
    source = _resolve_source(exchange, ticker)
    has_sizes = source["source_kind"] == "orderbook_1s"
    # Fixed SQL fragments depend only on our declared source schema, never input.
    size_struct = ", bid_size := CAST(bid_size AS DOUBLE), ask_size := CAST(ask_size AS DOUBLE)" if has_sizes else ""
    size_last = ", quote.bid_size AS bid_size, quote.ask_size AS ask_size" if has_sizes else ""
    size_mean = ", avg(CAST(bid_size AS DOUBLE)) AS bid_size, avg(CAST(ask_size AS DOUBLE)) AS ask_size" if has_sizes else ""
    size_filter = """
        AND isfinite(CAST(bid_size AS DOUBLE)) AND isfinite(CAST(ask_size AS DOUBLE))
        AND bid_size >= 0 AND ask_size >= 0
        AND quantity_unit = 'USDT' AND price_unit = 'KRW'
        AND sample_interval_ms = 1000 AND market = 'KRW-USDT'
    """ if has_sizes else ""
    con = connection()
    try:
        with query_deadline(con):
            if price_mode == "last":
                query = f"""
                WITH buckets AS (
                    SELECT CAST(floor(desktopTime / $step) AS BIGINT) * $step AS time,
                           arg_max(struct_pack(
                               bid := CAST(b1_p AS DOUBLE),
                               ask := CAST(a1_p AS DOUBLE),
                               quote_time := CAST(desktopTime AS BIGINT){size_struct}
                           ), desktopTime) AS quote,
                           count(*) AS source_rows
                    FROM read_parquet($paths)
                    WHERE desktopTime >= $start AND desktopTime < $end
                      AND isfinite(CAST(b1_p AS DOUBLE)) AND isfinite(CAST(a1_p AS DOUBLE))
                      AND b1_p > 0 AND a1_p >= b1_p
                      {size_filter}
                    GROUP BY time
                )
                SELECT time, quote.bid AS bid, quote.ask AS ask,
                       quote.bid / 2.0 + quote.ask / 2.0 AS mid,
                       source_rows, quote.quote_time AS quote_time{size_last}
                FROM buckets ORDER BY time
                """
            else:
                query = f"""
                SELECT CAST(floor(desktopTime / $step) AS BIGINT) * $step AS time,
                       avg(CAST(b1_p AS DOUBLE)) AS bid,
                       avg(CAST(a1_p AS DOUBLE)) AS ask,
                       avg(CAST(b1_p AS DOUBLE) / 2.0 + CAST(a1_p AS DOUBLE) / 2.0) AS mid,
                       count(*) AS source_rows{size_mean}
                FROM read_parquet($paths)
                WHERE desktopTime >= $start AND desktopTime < $end
                  AND isfinite(CAST(b1_p AS DOUBLE)) AND isfinite(CAST(a1_p AS DOUBLE))
                  AND b1_p > 0 AND a1_p >= b1_p
                  {size_filter}
                GROUP BY time ORDER BY time
                """
            frame = con.execute(
                query,
                {"paths": paths, "start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000), "step": step},
            ).df()
    except duckdb.Error as exc:
        raise DataError(f"{exchange} {ticker} 호가 파일을 읽지 못했습니다. parquet 스키마를 확인해 주세요.") from exc
    finally:
        con.close()
    if frame.empty:
        raise DataError(f"선택한 기간에 {exchange} {ticker}의 유효한 매수·매도 호가가 없습니다.")
    if has_sizes:
        frame.attrs.update(source_kind="orderbook_1s", sample_interval_ms=1000,
                           quantity_unit="USDT", price_unit="KRW", quote_time_basis="sampled_at")
    return frame.set_index("time"), int(frame["source_rows"].sum())



def export_leg(exchange: str, ticker: str, start: str, end: str, interval: str = "5min") -> dict:
    """Return a compact historical series for a portable static snapshot.

    Unlike chart responses, exports retain every interval, including 10,080+
    one-minute buckets for seven days. No prices are carried into empty buckets.
    """
    exchange, ticker = validate_symbol(exchange, ticker)
    start_dt, end_dt = parse_date(start, "시작 시간"), parse_date(end, "종료 시간")
    if start_dt >= end_dt or end_dt - start_dt > pd.Timedelta(days=7):
        raise DataError("내보내기 기간은 0일 초과 7일 이하여야 합니다.")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("지원하지 않는 내보내기 간격입니다.")
    frame, rows = read_buckets(exchange, ticker, start_dt, end_dt, INTERVALS[interval])
    sizes = {}
    if frame.attrs.get("quantity_unit") == "USDT":
        sizes = {**frame.attrs, **{column: array(frame[column]) for column in ["bid_size", "ask_size"]}}
    return {
        "exchange": exchange, "ticker": ticker,
        "currency": "KRW" if exchange in {"bithumb", "upbit", "ls"} else "USDT",
        "times": [int(value) for value in frame.index],
        **{column: array(frame[column]) for column in ["mid", "bid", "ask"]},
        **sizes,
        "meta": {"start": start_dt.tz_convert(KST).isoformat(), "end": end_dt.tz_convert(KST).isoformat(),
                 "interval": interval, "source": "parquet", "rows_read": rows,
                 "aligned_points": len(frame), "returned_points": len(frame), "warnings": []},
    }
