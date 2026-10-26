"""Read-only archived funding-rate snapshots, expressed as rates rather than fees.

The collectors store ``fundingRate`` as a ratio and ``nextFundingTime`` as
seconds remaining at the source observation. Neither field is an account's
settled cash payment. Public series expose both the original ratio and percent.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from . import parquet
from .funding_archives import materialize_parquet
from .timestamps import timestamp_delta_sql, unmeasurable_clock_reason
from .validation import DataError, IDENTIFIER, iso_timestamp


def list_symbols() -> dict[str, list[str]]:
    root = parquet.data_root()
    if not root.is_dir():
        return {}
    found = {}
    for exchange in sorted(root.iterdir()):
        directory = exchange / "funding"
        if (
            not IDENTIFIER.fullmatch(exchange.name)
            or not directory.is_dir()
            or not parquet.within_root(directory, root)
        ):
            continue
        symbols = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_dir()
            and IDENTIFIER.fullmatch(path.name)
            and parquet.within_root(path, directory)
        )
        if symbols:
            found[exchange.name] = symbols
    return found


def validate_symbol(exchange, ticker, available=None) -> tuple[str, str]:
    if not isinstance(exchange, str) or not isinstance(ticker, str):
        raise DataError("펀딩비율 거래소와 종목을 선택해 주세요.")
    ticker = ticker.strip()
    available = list_symbols() if available is None else available
    if exchange in available:
        lookup = {symbol.upper(): symbol for symbol in available[exchange]}
        normalized = lookup.get(ticker.upper())
        if normalized is not None:
            return exchange, normalized
    raise DataError(f"선택한 거래소·종목의 펀딩비율 데이터가 없습니다: {exchange} / {ticker}")


def _source_files(directory, pattern=""):
    """List safe hour files once; a plain file supersedes its gzip sibling."""
    found = {}
    for suffix in [".parquet.gz", ".parquet"]:
        for path in directory.glob(f"{pattern}*{suffix}"):
            if path.is_file() and parquet.within_root(path, directory):
                logical = str(path)[:-3] if suffix.endswith(".gz") else str(path)
                found[logical] = path
    return [found[key] for key in sorted(found)]


def bounds(exchange: str, ticker: str) -> dict:
    """Return actual observation timestamp bounds, including gzip archives."""
    exchange, ticker = validate_symbol(exchange, ticker)
    directory = parquet.data_root() / exchange / "funding" / ticker
    files = _source_files(directory, "*/*/*/")
    if not files:
        raise DataError(f"{exchange} {ticker}의 펀딩비율 parquet 파일이 없습니다.")
    con = parquet.connection()
    try:
        start = end = None
        with parquet.query_deadline(con):
            for path in files:
                with materialize_parquet([str(path)]) as readable:
                    start = con.execute(
                        "SELECT min(desktopTime) FROM read_parquet(?)", [readable[0]]
                    ).fetchone()[0]
                if start is not None:
                    break
            for path in reversed(files):
                with materialize_parquet([str(path)]) as readable:
                    end = con.execute(
                        "SELECT max(desktopTime) FROM read_parquet(?)", [readable[0]]
                    ).fetchone()[0]
                if end is not None:
                    break
    except duckdb.Error as exc:
        raise DataError(
            "펀딩비율 파일의 시간 범위를 읽지 못했습니다. parquet 파일을 확인해 주세요."
        ) from exc
    finally:
        con.close()
    if start is None or end is None:
        raise DataError(f"{exchange} {ticker}의 펀딩비율 파일이 비어 있습니다.")
    return {
        "start": iso_timestamp(int(start)),
        "end": iso_timestamp(int(end)),
        "exchange": exchange,
        "ticker": ticker,
        "data_type": "funding",
    }


def files_for_range(
    exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp
) -> list[str]:
    exchange, ticker = validate_symbol(exchange, ticker)
    parent = parquet.data_root() / exchange / "funding" / ticker
    first, last = start.floor("h"), (end - pd.Timedelta(nanoseconds=1)).floor("h")
    files = []
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        directory = parent / day.strftime("%Y/%m/%d")
        if not directory.is_dir() or not parquet.within_root(directory, parent):
            continue
        for path in _source_files(directory):
            if not parquet.within_root(path, parent):
                continue
            hour_name = path.name.removesuffix(".gz").removesuffix(".parquet")
            if hour_name.isdigit() and 0 <= int(hour_name) <= 23:
                hour = day + pd.Timedelta(hours=int(hour_name))
                if not first <= hour <= last:
                    continue
            files.append(str(path))
    return files


def read_buckets(
    exchange: str,
    ticker: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    step: int,
    *,
    price_mode: str = "mean",
):
    """Read last observations or means, retaining last observation metadata.

    ``value`` is in percent; ``funding_rate`` preserves the source ratio.
    Missing buckets are omitted. Zero and negative rates are valid. For means,
    ``source_time``/``server_time``/``next_funding_in_seconds`` describe the last
    observation in that bucket, not a synthetic observation of its mean.
    """
    if not isinstance(price_mode, str) or price_mode not in {"mean", "last"}:
        raise DataError("조회 방식은 마지막 관측값 또는 평균으로 선택해 주세요.")
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError(f"선택한 기간에 {exchange} {ticker} 펀딩비율 파일이 없습니다.")
    con = parquet.connection()
    try:
        with materialize_parquet(paths) as readable, parquet.query_deadline(con):
            # Metadata is optional for older/minimal snapshots. The rate and
            # desktopTime remain required; union_by_name tolerates optional
            # columns that exist in only some of the selected hours.
            columns = {
                row[0]
                for row in con.execute(
                    "DESCRIBE SELECT * FROM read_parquet($paths, union_by_name=true)",
                    {"paths": readable},
                ).fetchall()
            }
            server = (
                'try_cast("serverTime" AS BIGINT)' if "serverTime" in columns else "NULL::BIGINT"
            )
            remaining = (
                'CASE WHEN isfinite(try_cast("nextFundingTime" AS DOUBLE)) '
                'AND abs(try_cast("nextFundingTime" AS DOUBLE)) <= 2678400 '
                'THEN try_cast("nextFundingTime" AS DOUBLE) ELSE NULL END'
                if "nextFundingTime" in columns
                else "NULL::DOUBLE"
            )
            rate = "observation.funding_rate" if price_mode == "last" else "mean_rate"
            delta = timestamp_delta_sql(columns, exchange, 'funding')
            selected_delta = (
                'observation.timestamp_delta_ms' if price_mode == 'last' else 'mean_delta'
            )
            query = f"""
                WITH buckets AS (
                    SELECT CAST(floor(desktopTime / $step) AS BIGINT) * $step AS time,
                           avg(CAST(fundingRate AS DOUBLE)) AS mean_rate,
                           avg({delta}) AS mean_delta,
                           arg_max(struct_pack(
                               funding_rate := CAST(fundingRate AS DOUBLE),
                               source_time := CAST(desktopTime AS BIGINT),
                               server_time := {server},
                               timestamp_delta_ms := {delta},
                               next_funding_in_seconds := {remaining}
                           ), desktopTime) AS observation,
                           count(*) AS source_rows
                    FROM read_parquet($paths, union_by_name=true)
                    WHERE desktopTime >= $start AND desktopTime < $end
                      AND isfinite(CAST(fundingRate AS DOUBLE))
                      AND isfinite(CAST(fundingRate AS DOUBLE) * 100.0)
                    GROUP BY time
                )
                SELECT time, {rate} AS funding_rate, {rate} * 100.0 AS value,
                       source_rows, observation.source_time AS source_time,
                       observation.server_time AS server_time,
                       {selected_delta} AS timestamp_delta_ms,
                       observation.next_funding_in_seconds AS next_funding_in_seconds
                FROM buckets ORDER BY time
            """
            frame = con.execute(
                query,
                {
                    "paths": readable,
                    "start": int(start.value // 1_000_000),
                    "end": int(end.value // 1_000_000),
                    "step": step,
                },
            ).df()
    except duckdb.Error as exc:
        raise DataError(
            f"{exchange} {ticker} 펀딩비율 파일을 읽지 못했습니다. parquet 스키마를 확인해 주세요."
        ) from exc
    finally:
        con.close()
    if frame.empty:
        raise DataError(f"선택한 기간에 {exchange} {ticker}의 유효한 펀딩비율 관측값이 없습니다.")
    frame.attrs['timestamp_delta_unavailable_reason'] = unmeasurable_clock_reason(
        exchange, 'funding'
    )
    return frame.set_index("time"), int(frame["source_rows"].sum())
