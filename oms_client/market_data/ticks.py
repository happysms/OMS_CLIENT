"""Read-only signed trade ticks, with bounded charts and paginated raw rows.

The archive has no trade identifiers. Equal records are preserved, and sorting
ties are resolved by their recorded values rather than inventing trade order.
Only Bithumb/Upbit direction and quantity units are verified by local collectors.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
import tempfile

import duckdb
import pandas as pd

from . import parquet
from .csv_export import MAX_BYTES, MAX_ROWS, _cell, _LimitedCsvStream
from .funding_archives import materialize_parquet
from .validation import (
    DataError,
    IDENTIFIER,
    INTERVALS,
    KST,
    MAX_BUCKETS,
    MAX_RANGE_DAYS,
    array,
    integer,
    iso_timestamp,
    number,
    parse_date,
)

MAX_READ_ROWS = 5_000_000
MAX_CHART_POINTS = 6000
VERIFIED_DIRECTIONS = frozenset({"bithumb", "upbit"})


@contextmanager
def _materialize(paths):
    try:
        with materialize_parquet(paths) as readable:
            yield readable
    except DataError as exc:
        raise DataError(str(exc).replace("펀딩비", "체결 틱")) from None


def list_symbols() -> dict[str, list[str]]:
    root = parquet.data_root()
    if not root.is_dir():
        return {}
    result = {}
    for exchange in sorted(root.iterdir()):
        directory = exchange / "tick"
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
            result[exchange.name] = symbols
    return result


def validate_symbol(exchange, ticker) -> tuple[str, str]:
    if (
        not isinstance(exchange, str)
        or not IDENTIFIER.fullmatch(exchange)
        or not isinstance(ticker, str)
        or not IDENTIFIER.fullmatch(ticker.strip())
    ):
        raise DataError("체결 틱 거래소와 종목을 선택해 주세요.")
    symbols = list_symbols().get(exchange, [])
    matched = next((symbol for symbol in symbols if symbol.upper() == ticker.strip().upper()), None)
    if matched is None:
        raise DataError(f"선택한 거래소·종목의 체결 틱 데이터가 없습니다: {exchange} / {ticker}")
    return exchange, matched


def _hour_files(directory):
    """Prefer an uncompressed hour over its gzip sibling, never count both."""
    found = {}
    for suffix in (".parquet.gz", ".parquet"):
        for path in directory.glob("*" + suffix):
            if path.is_file() and parquet.within_root(path, directory):
                name = path.name.removesuffix(".gz").removesuffix(".parquet")
                if name.isdigit() and 0 <= int(name) <= 23:
                    found[name] = path
    return [found[name] for name in sorted(found, key=int)]


def files_for_range(exchange, ticker, start, end) -> list[str]:
    exchange, ticker = validate_symbol(exchange, ticker)
    parent = parquet.data_root() / exchange / "tick" / ticker
    first, last = start.floor("h"), (end - pd.Timedelta(nanoseconds=1)).floor("h")
    files = []
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        directory = parent / day.strftime("%Y/%m/%d")
        if not directory.is_dir() or not parquet.within_root(directory, parent):
            continue
        for path in _hour_files(directory):
            hour = day + pd.Timedelta(
                hours=int(path.name.removesuffix(".gz").removesuffix(".parquet"))
            )
            if first <= hour <= last:
                files.append(str(path))
    return files


def bounds(exchange, ticker) -> dict:
    exchange, ticker = validate_symbol(exchange, ticker)
    parent = parquet.data_root() / exchange / "tick" / ticker
    directories = sorted(
        path for path in parent.glob("*/*/*") if path.is_dir() and parquet.within_root(path, parent)
    )
    files = [path for directory in directories for path in _hour_files(directory)]
    if not files:
        raise DataError(f"{exchange} {ticker}의 체결 틱 parquet 파일이 없습니다.")
    con = parquet.connection()
    try:
        start = end = None
        with parquet.query_deadline(con):
            for path in files:
                with _materialize([str(path)]) as readable:
                    start = con.execute(
                        'SELECT min(try_cast("desktopTime" AS BIGINT)) FROM read_parquet(?)',
                        [readable[0]],
                    ).fetchone()[0]
                if start is not None:
                    break
            for path in reversed(files):
                with _materialize([str(path)]) as readable:
                    end = con.execute(
                        'SELECT max(try_cast("desktopTime" AS BIGINT)) FROM read_parquet(?)',
                        [readable[0]],
                    ).fetchone()[0]
                if end is not None:
                    break
    except duckdb.Error as exc:
        raise DataError(
            "체결 틱 파일의 시간 범위를 읽지 못했습니다. parquet 파일을 확인해 주세요."
        ) from exc
    finally:
        con.close()
    if start is None or end is None:
        raise DataError(f"{exchange} {ticker}의 체결 틱 파일이 비어 있습니다.")
    try:
        return {
            "exchange": exchange,
            "ticker": ticker,
            "data_type": "tick",
            "start": iso_timestamp(int(start)),
            "end": iso_timestamp(int(end)),
        }
    except (ValueError, OverflowError) as exc:
        raise DataError("체결 틱 파일의 기록 시각이 올바르지 않습니다.") from exc


def normalize_config(payload) -> dict:
    allowed = {
        "exchange",
        "ticker",
        "start",
        "end",
        "interval",
        "price_mode",
        "row_limit",
        "offset",
        "max_points",
        "data_type",
    }
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise DataError("체결 틱 조회에는 거래소·종목·기간·간격·조회 방식만 지정해 주세요.")
    if payload.get("data_type", "tick") != "tick":
        raise DataError("체결 틱 데이터 종류를 선택해 주세요.")
    for key in ("exchange", "ticker"):
        value = payload.get(key)
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value.strip()):
            raise DataError("체결 틱 거래소와 종목을 선택해 주세요.")
    start, end = parse_date(payload.get("start"), "시작 시간"), parse_date(
        payload.get("end"), "종료 시간"
    )
    if start >= end or end - start > pd.Timedelta(days=MAX_RANGE_DAYS):
        raise DataError("체결 틱 조회 기간은 0일 초과 31일 이하여야 합니다.")
    interval, mode = payload.get("interval", "1s"), payload.get("price_mode", "last")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("체결 틱 조회 간격을 확인해 주세요.")
    if not isinstance(mode, str) or mode not in {"last", "mean"}:
        raise DataError("체결가 조회 방식은 마지막 체결가 또는 평균으로 선택해 주세요.")
    step = INTERVALS[interval]
    buckets = (end.value - 1) // (step * 1_000_000) - start.value // (step * 1_000_000) + 1
    if buckets > MAX_BUCKETS:
        raise DataError(
            "체결 틱 차트는 최대 250,000구간입니다. 기간을 줄이거나 간격을 늘려 주세요."
        )
    return {
        "exchange": payload["exchange"].strip(),
        "ticker": payload["ticker"].strip(),
        "data_type": "tick",
        "start": start.tz_convert(KST).isoformat(),
        "end": end.tz_convert(KST).isoformat(),
        "interval": interval,
        "price_mode": mode,
        "row_limit": integer(payload.get("row_limit", 100), "체결 내역 표시 수", 1, 1000),
        "offset": integer(payload.get("offset", 0), "체결 내역 시작 위치", 0, MAX_READ_ROWS),
        "max_points": integer(
            payload.get("max_points", MAX_CHART_POINTS), "차트 표본 수", 2, MAX_CHART_POINTS
        ),
    }


def _prepare(con, readable, start, end):
    columns = {
        row[0]
        for row in con.execute(
            "DESCRIBE SELECT * FROM read_parquet($paths, union_by_name=true)", {"paths": readable}
        ).fetchall()
    }
    if not {"desktopTime", "price", "qty"}.issubset(columns):
        raise DataError("체결 틱 파일에 desktopTime·price·qty 컬럼이 필요합니다.")
    server = (
        'CASE WHEN try_cast("serverTime" AS BIGINT) >= 946684800000 '
        'AND try_cast("serverTime" AS BIGINT) < 4102444800000 '
        'THEN try_cast("serverTime" AS BIGINT) ELSE NULL END'
        if "serverTime" in columns
        else "NULL::BIGINT"
    )
    # Create views through a relation to keep all file paths and user values bound.
    con.from_parquet(readable, union_by_name=True).create_view("tick_source")
    con.execute(
        f'''CREATE TEMP VIEW tick_normalized AS SELECT
        try_cast("desktopTime" AS BIGINT) AS source_time,
        {server} AS server_time, try_cast(price AS DOUBLE) AS price,
        try_cast(qty AS DOUBLE) AS qty FROM tick_source'''
    )
    parameters = {"start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000)}
    raw_count = con.execute(
        "SELECT count(*) FROM tick_normalized WHERE source_time >= $start AND source_time < $end",
        parameters,
    ).fetchone()[0]
    if not raw_count:
        raise DataError("선택한 기간에 체결 틱이 없습니다.")
    if raw_count > MAX_READ_ROWS:
        raise DataError(f"체결 틱 조회는 최대 {MAX_READ_ROWS:,}행입니다. 기간을 줄여 주세요.")
    return parameters, int(raw_count)


_VALID = "source_time >= $start AND source_time < $end AND isfinite(price) AND price > 0 AND isfinite(qty) AND qty != 0"
_ORDER = "source_time DESC, server_time DESC NULLS LAST, price DESC, qty DESC"


def query(payload) -> dict:
    config = normalize_config(payload)
    exchange, ticker = validate_symbol(config["exchange"], config["ticker"])
    config.update(exchange=exchange, ticker=ticker)
    start, end = parse_date(config["start"], "시작 시간"), parse_date(config["end"], "종료 시간")
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError("선택한 기간에 체결 틱 파일이 없습니다.")
    con = parquet.connection()
    try:
        with (
            _materialize(paths) as readable,
            tempfile.TemporaryDirectory(prefix="oms-tick-sort-") as scratch,
            parquet.query_deadline(con),
        ):
            con.execute("SET temp_directory = ?", [scratch])
            con.execute("SET max_temp_directory_size = ?", ["512MB"])
            parameters, raw_count = _prepare(con, readable, start, end)
            stats_row = con.execute(
                f"""SELECT count(*), min(price), max(price), sum(abs(qty)),
                sum(CASE WHEN qty > 0 THEN qty ELSE 0 END),
                sum(CASE WHEN qty < 0 THEN -qty ELSE 0 END),
                arg_max(price, struct_pack(t := source_time, s := coalesce(server_time, 0), p := price, q := qty)),
                max(source_time) FROM tick_normalized WHERE {_VALID}""",
                parameters,
            ).fetchone()
            count = int(stats_row[0])
            if not count:
                raise DataError("선택한 기간에 유효한 가격·수량의 체결 틱이 없습니다.")
            value = "last_tick.price" if config["price_mode"] == "last" else "mean_price"
            frame = con.execute(
                f"""WITH buckets AS (
                SELECT CAST(floor(source_time / $step) AS BIGINT) * $step AS time,
                    count(*) AS trade_count, avg(price) AS mean_price, sum(abs(qty)) AS volume,
                    sum(CASE WHEN qty > 0 THEN qty ELSE 0 END) AS buy_volume,
                    sum(CASE WHEN qty < 0 THEN -qty ELSE 0 END) AS sell_volume,
                    arg_max(struct_pack(price := price, source_time := source_time),
                        struct_pack(t := source_time, s := coalesce(server_time, 0), p := price, q := qty)) AS last_tick
                FROM tick_normalized WHERE {_VALID} GROUP BY time)
                SELECT time, {value} AS value, last_tick.source_time AS source_time,
                    trade_count, volume, buy_volume, sell_volume FROM buckets ORDER BY time""",
                {**parameters, "step": INTERVALS[config["interval"]]},
            ).df()
            raw_rows = con.execute(
                f"""SELECT source_time, server_time, price, qty
                FROM tick_normalized WHERE {_VALID} ORDER BY {_ORDER}
                LIMIT $limit OFFSET $offset""",
                {**parameters, "limit": config["row_limit"], "offset": config["offset"]},
            ).fetchall()
    except duckdb.Error as exc:
        raise DataError(
            "체결 틱 파일을 읽지 못했습니다. parquet 스키마나 조회 기간을 확인해 주세요."
        ) from exc
    finally:
        con.close()
    direction = exchange in VERIFIED_DIRECTIONS
    quantity_unit = ticker if direction else None
    currency = (
        "KRW"
        if exchange in {"bithumb", "upbit", "ls"}
        else "USDT" if exchange in {"binance", "okx"} else None
    )
    warnings = [
        "차트는 선택한 간격의 체결가를 집계하고 내역은 원본 체결을 최신순으로 표시합니다. 체결 없는 구간은 채우지 않습니다.",
        "원본에 체결 ID가 없어 같은 값의 행을 중복으로 제거하지 않습니다. 같은 기록 시각의 실제 선후관계는 확정할 수 없습니다.",
    ]
    if not direction:
        warnings.append(
            "이 거래소 수집기의 수량 단위와 부호 의미는 검증되지 않았습니다. 원본 부호 수량을 보존하며 매수·매도 방향을 추정하지 않습니다."
        )
    if exchange == "ls":
        warnings.append(
            "LS serverTime은 날짜 없는 시분초 또는 수집기 생성 시각이므로 거래소 체결 시각으로 표시하지 않습니다. 원본 CSV에서 원래 값을 확인할 수 있습니다."
        )
    if raw_count != count:
        warnings.append(
            f"가격·수량이 유효하지 않은 {raw_count - count:,}행은 차트·내역에서 제외했습니다. 원본 CSV에는 보존합니다."
        )
    points = len(frame)
    if points > config["max_points"]:
        # Include both ends, applying the same selection to every chart series.
        indices = [
            round(index * (points - 1) / (config["max_points"] - 1))
            for index in range(config["max_points"])
        ]
        frame = frame.iloc[indices]
        warnings.append(
            f"차트는 전체 {points:,}개 집계 구간 중 {len(frame):,}개를 표시합니다. 통계와 원본 내역은 전체 조회 구간 기준입니다."
        )
    leg = {
        "id": "A",
        "data_type": "tick",
        "exchange": exchange,
        "ticker": ticker,
        "currency": currency,
        "quantity_unit": quantity_unit,
        "values": array(frame["value"]),
        "source_times": array(frame["source_time"]),
        "trade_count": array(frame["trade_count"]),
        "volume": array(frame["volume"]),
        "buy_volume": array(frame["buy_volume"]) if direction else [None] * len(frame),
        "sell_volume": array(frame["sell_volume"]) if direction else [None] * len(frame),
    }
    rows = [
        {
            "source_time": int(row[0]),
            "server_time": int(row[1]) if row[1] is not None and exchange != "ls" else None,
            "price": number(row[2]),
            "qty": number(row[3]),
            "quantity": number(abs(row[3])),
            "side": ("buy" if row[3] > 0 else "sell") if direction else "unknown",
        }
        for row in raw_rows
    ]
    return {
        "config": config,
        "times": [int(value) for value in frame["time"]],
        "values": leg["values"],
        "result": leg["values"],
        "legs": [leg],
        "rows": rows,
        "stats": {
            "count": count,
            "min": number(stats_row[1]),
            "max": number(stats_row[2]),
            "total_volume": number(stats_row[3]),
            "buy_volume": number(stats_row[4]) if direction else None,
            "sell_volume": number(stats_row[5]) if direction else None,
            "latest_price": number(stats_row[6]),
            "latest_source_time": int(stats_row[7]),
        },
        "meta": {
            "data_type": "tick",
            "rows_read": count,
            "raw_rows": raw_count,
            "invalid_rows": raw_count - count,
            "aligned_points": points,
            "shown_points": len(frame),
            "returned_points": len(frame),
            "interval": config["interval"],
            "price_mode": config["price_mode"],
            "clock_basis": "desktopTime",
            "direction_available": direction,
            "quantity_unit": quantity_unit,
            "source_files": len(paths),
            "table_offset": config["offset"],
            "table_limit": config["row_limit"],
            "table_returned": len(rows),
            "table_has_more": config["offset"] + len(rows) < count,
            "table_truncated": len(rows) < count,
            "warnings": warnings,
        },
    }


def csv_file(payload, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES):
    """Return a private open temporary file containing every raw source column.

    Limits fail before a download becomes available. The caller closes the file
    when its response finishes. TemporaryFile is unlinked by the operating system.
    """
    config = normalize_config(payload)
    exchange, ticker = validate_symbol(config["exchange"], config["ticker"])
    start, end = parse_date(config["start"], "시작 시간"), parse_date(config["end"], "종료 시간")
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError("선택한 기간에 체결 틱 파일이 없습니다.")
    con, output = parquet.connection(), tempfile.TemporaryFile(mode="w+b", prefix="oms-ticks-")
    try:
        with (
            _materialize(paths) as readable,
            tempfile.TemporaryDirectory(prefix="oms-tick-csv-sort-") as scratch,
            parquet.query_deadline(con),
        ):
            con.execute("SET temp_directory = ?", [scratch])
            con.execute("SET max_temp_directory_size = ?", ["512MB"])
            parameters, count = _prepare(con, readable, start, end)
            if count > max_rows:
                raise DataError(
                    f"체결 틱 원본 CSV는 최대 {max_rows:,}행입니다. 기간을 줄여 주세요."
                )
            # The view preserves unioned raw columns, including malformed or
            # optional values that are omitted from chart prices.
            cursor = con.execute(
                '''SELECT * FROM tick_source
                WHERE try_cast("desktopTime" AS BIGINT) >= $start
                  AND try_cast("desktopTime" AS BIGINT) < $end
                ORDER BY try_cast("desktopTime" AS BIGINT),
                         try_cast(price AS DOUBLE), try_cast(qty AS DOUBLE)''',
                parameters,
            )
            sink = _LimitedCsvStream(output, max_bytes)
            sink.write("\ufeff")
            writer = csv.writer(sink)
            writer.writerow([_cell(column[0]) for column in cursor.description])
            written = 0
            while batch := cursor.fetchmany(2048):
                for row in batch:
                    written += 1
                    if written > max_rows:
                        raise DataError(
                            f"체결 틱 원본 CSV는 최대 {max_rows:,}행입니다. 기간을 줄여 주세요."
                        )
                    writer.writerow([_cell(value) for value in row])
        output.seek(0)
        safe_ticker = "".join(
            character if character.isalnum() or character in "_.-" else "_" for character in ticker
        )
        name = f"oms-{exchange}-{safe_ticker}-ticks-{start.tz_convert(KST):%Y%m%d-%H%M%S}-{end.tz_convert(KST):%Y%m%d-%H%M%S}.csv"
        return output, name, sink.bytes
    except duckdb.Error as exc:
        output.close()
        raise DataError(
            "체결 틱 CSV를 만들지 못했습니다. parquet 파일과 조회 기간을 확인해 주세요."
        ) from exc
    except BaseException:
        output.close()
        raise
    finally:
        con.close()
