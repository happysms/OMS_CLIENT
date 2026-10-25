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

from .validation import (DataError, QueryTimeout, IDENTIFIER, INTERVALS, KST,
                         array, integer, iso_timestamp, parse_date)


def data_root() -> Path:
    return Path(os.environ.get("DASHBOARD_DATA_ROOT", "/data")).resolve()



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



def list_symbols() -> dict[str, list[str]]:
    root = data_root()
    if not root.is_dir():
        return {}
    found = {}
    for exchange in sorted(root.iterdir()):
        ob = exchange / "ob"
        if not IDENTIFIER.fullmatch(exchange.name) or not ob.is_dir() or not within_root(ob, root):
            continue
        symbols = sorted(p.name for p in ob.iterdir()
                         if p.is_dir() and IDENTIFIER.fullmatch(p.name) and within_root(p, ob))
        if symbols:
            found[exchange.name] = symbols
    return found



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
    directory = data_root() / exchange / "ob" / ticker
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
    files = []
    last = (end - pd.Timedelta(nanoseconds=1)).floor("h")
    first = start.floor("h")
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        directory = data_root() / exchange / "ob" / ticker / day.strftime("%Y/%m/%d")
        parent = data_root() / exchange / "ob" / ticker
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



def read_buckets(exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp, step: int):
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError(f"선택한 기간에 {exchange} {ticker} 호가 파일이 없습니다.")
    con = connection()
    try:
        with query_deadline(con):
            frame = con.execute(
                """
                SELECT CAST(floor(desktopTime / $step) AS BIGINT) * $step AS time,
                       avg(CAST(b1_p AS DOUBLE)) AS bid,
                       avg(CAST(a1_p AS DOUBLE)) AS ask,
                       avg(CAST(b1_p AS DOUBLE) / 2.0 + CAST(a1_p AS DOUBLE) / 2.0) AS mid,
                       count(*) AS source_rows
                FROM read_parquet($paths)
                WHERE desktopTime >= $start AND desktopTime < $end
                  AND isfinite(CAST(b1_p AS DOUBLE)) AND isfinite(CAST(a1_p AS DOUBLE))
                  AND b1_p > 0 AND a1_p >= b1_p
                GROUP BY time ORDER BY time
                """,
                {"paths": paths, "start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000), "step": step},
            ).df()
    except duckdb.Error as exc:
        raise DataError(f"{exchange} {ticker} 호가 파일을 읽지 못했습니다. parquet 스키마를 확인해 주세요.") from exc
    finally:
        con.close()
    if frame.empty:
        raise DataError(f"선택한 기간에 {exchange} {ticker}의 유효한 매수·매도 호가가 없습니다.")
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
    return {
        "exchange": exchange, "ticker": ticker,
        "currency": "KRW" if exchange in {"bithumb", "upbit", "ls"} else "USDT",
        "times": [int(value) for value in frame.index],
        **{column: array(frame[column]) for column in ["mid", "bid", "ask"]},
        "meta": {"start": start_dt.tz_convert(KST).isoformat(), "end": end_dt.tz_convert(KST).isoformat(),
                 "interval": interval, "source": "parquet", "rows_read": rows,
                 "aligned_points": len(frame), "returned_points": len(frame), "warnings": []},
    }
