"""Bounded, read-only Parquet-to-CSV conversion independent of chart sampling."""
from __future__ import annotations

import csv
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
import re
import tempfile

import duckdb
import pandas as pd

from . import parquet
from .validation import DataError, IDENTIFIER, INTERVALS, KST, MAX_BUCKETS, MAX_RANGE_DAYS, parse_date

MAX_ROWS = 1_000_000
MAX_BYTES = 256 * 1024 * 1024
AGGREGATED_COLUMNS = ["time_kst", "exchange", "ticker", "currency", "mid", "bid", "ask", "spread", "source_rows"]


def normalize_config(payload):
    if not isinstance(payload, dict) or set(payload) - {"exchange", "ticker", "start", "end", "mode", "interval"}:
        raise DataError("CSV 조건에는 거래소·종목·기간·내보내기 방식만 지정해 주세요.")
    values = {}
    for key, label in [("exchange", "거래소"), ("ticker", "종목")]:
        value = payload.get(key)
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value.strip()):
            raise DataError(f"CSV {label}을 확인해 주세요.")
        values[key] = value.strip()
    start, end = parse_date(payload.get("start"), "시작 시간"), parse_date(payload.get("end"), "종료 시간")
    if start >= end or end - start > pd.Timedelta(days=MAX_RANGE_DAYS):
        raise DataError("CSV 내보내기 기간은 0일 초과 31일 이하여야 합니다.")
    mode, interval = payload.get("mode", "raw"), payload.get("interval", "1min")
    if not isinstance(mode, str) or mode not in {"raw", "aggregated"}:
        raise DataError("CSV 내보내기 방식은 원본 또는 평균 집계로 선택해 주세요.")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("CSV 집계 간격을 확인해 주세요.")
    if mode == "aggregated" and math.ceil((end - start).total_seconds() * 1000 / INTERVALS[interval]) > MAX_BUCKETS:
        raise DataError("CSV 집계는 최대 250,000구간입니다. 기간을 줄이거나 간격을 늘려 주세요.")
    return {**values, "start": start.tz_convert(KST).isoformat(), "end": end.tz_convert(KST).isoformat(), "mode": mode, "interval": interval}


def filename(config):
    ticker = re.sub(r"[^A-Za-z0-9_.-]", "_", config["ticker"])
    start = parse_date(config["start"], "시작 시간").tz_convert(KST).strftime("%Y%m%d-%H%M%S")
    end = parse_date(config["end"], "종료 시간").tz_convert(KST).strftime("%Y%m%d-%H%M%S")
    return f"oms-{config['exchange']}-{ticker}-{config['mode']}-{start}-{end}.csv"


def _cell(value):
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, (datetime, date)):
        value = value.isoformat()
    # Numeric negative prices/quantities remain numbers. Only strings can be formulas.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        value = "'" + value
    return value


class _LimitedCsvStream:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.bytes = stream, maximum, 0

    def write(self, value):
        encoded = value.encode("utf-8")
        if self.bytes + len(encoded) > self.maximum:
            raise DataError("CSV 파일 크기 한도를 초과했습니다. 기간을 줄여 다시 내보내 주세요.")
        self.stream.write(encoded)
        self.bytes += len(encoded)


def export_csv(config, path: Path, *, max_rows=MAX_ROWS, max_bytes=MAX_BYTES):
    """Write one complete CSV atomically, or fail without publishing partial data."""
    config = normalize_config(config)
    exchange, ticker = parquet.validate_symbol(config["exchange"], config["ticker"])
    start, end = parse_date(config["start"], "시작 시간"), parse_date(config["end"], "종료 시간")
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".csv-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            sink = _LimitedCsvStream(stream, max_bytes)
            sink.write("\ufeff")
            writer = csv.writer(sink)
            if config["mode"] == "raw":
                result = _write_raw(writer, exchange, ticker, start, end, max_rows, path.parent)
            else:
                result = _write_aggregated(writer, exchange, ticker, start, end, config["interval"], max_rows)
            size = sink.bytes
        os.replace(temporary, path)
        temporary = None
        return {**result, "bytes": size, "filename": filename(config)}
    except duckdb.Error as exc:
        raise DataError("CSV를 만들지 못했습니다. 원본 Parquet 스키마나 조회 기간을 확인해 주세요.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_raw(writer, exchange, ticker, start, end, max_rows, directory):
    paths = parquet.files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError("선택한 기간에 내보낼 원본 Parquet 파일이 없습니다.")
    con = parquet.connection()
    scratch = tempfile.TemporaryDirectory(prefix=".csv-sort-", dir=directory)
    parameters = {"paths": paths, "start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000)}
    try:
        con.execute("SET temp_directory = ?", [scratch.name])
        con.execute("SET max_temp_directory_size = ?", ["512MB"])
        with parquet.query_deadline(con):
            # Bound the full sort before reading all columns. Never silently truncate.
            count = con.execute("SELECT count(*) FROM read_parquet($paths, union_by_name=true) WHERE desktopTime >= $start AND desktopTime < $end", parameters).fetchone()[0]
            if not count:
                raise DataError("선택한 기간에 내보낼 원본 행이 없습니다.")
            if count > max_rows:
                raise DataError(f"원본 CSV는 최대 {max_rows:,}행입니다. 기간을 줄여 주세요.")
            cursor = con.execute("SELECT * FROM read_parquet($paths, union_by_name=true) WHERE desktopTime >= $start AND desktopTime < $end ORDER BY desktopTime LIMIT $limit", {**parameters, "limit": max_rows + 1})
            columns = [column[0] for column in cursor.description]
            writer.writerow([_cell(column) for column in columns])
            rows = 0
            while batch := cursor.fetchmany(2048):
                for row in batch:
                    rows += 1
                    if rows > max_rows:
                        raise DataError(f"원본 CSV는 최대 {max_rows:,}행입니다. 기간을 줄여 주세요.")
                    writer.writerow([_cell(value) for value in row])
        return {"rows": rows, "columns": columns, "source_rows": rows}
    finally:
        con.close()
        scratch.cleanup()


def _write_aggregated(writer, exchange, ticker, start, end, interval, max_rows):
    frame, source_rows = parquet.read_buckets(exchange, ticker, start, end, INTERVALS[interval])
    if len(frame) > min(MAX_BUCKETS, max_rows):
        raise DataError("CSV 집계 행 한도를 초과했습니다. 기간을 줄이거나 간격을 늘려 주세요.")
    currency = "KRW" if exchange in {"bithumb", "upbit", "ls"} else "USDT"
    has_sizes = frame.attrs.get("quantity_unit") == "USDT" and {"bid_size", "ask_size"}.issubset(frame.columns)
    columns = AGGREGATED_COLUMNS.copy()
    if has_sizes:
        columns.extend(["bid_size", "ask_size", "quantity_unit", "source_kind", "sample_interval_ms"])
    writer.writerow(columns)
    for row in frame.itertuples():
        timestamp = pd.Timestamp(row.Index, unit="ms", tz="UTC").tz_convert(KST).isoformat()
        values = [timestamp, exchange, ticker, currency, row.mid, row.bid, row.ask, row.ask - row.bid, row.source_rows]
        if has_sizes:
            values.extend([row.bid_size, row.ask_size, frame.attrs["quantity_unit"], frame.attrs["source_kind"], frame.attrs["sample_interval_ms"]])
        writer.writerow(values)
    return {"rows": len(frame), "columns": columns, "source_rows": source_rows}
