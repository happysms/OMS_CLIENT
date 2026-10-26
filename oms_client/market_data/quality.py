"""Read-only timestamp quality: signed desktopTime - serverTime, in ms."""
from __future__ import annotations

import math
from pathlib import Path

import duckdb
import pandas as pd

from . import parquet
from .funding_archives import materialize_parquet
from .timestamps import MIN_TIMESTAMP_MS, MAX_TIMESTAMP_MS, unmeasurable_clock_reason
from .validation import DataError, IDENTIFIER, INTERVALS, integer, parse_date

KINDS = {"ob": "호가", "tick": "체결", "funding": "펀딩비율", "ob_1s": "1초 호가"}
MAX_FILES = 1024
MAX_RAW_ROWS = 5_000_000
MAX_BUCKETS = 250_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024


def _safe(path, root, *, file=False):
    try:
        cursor = root
        for part in path.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                return False
        return (path.is_file() if file else path.is_dir()) and parquet.within_root(path, root)
    except (OSError, RuntimeError, ValueError):
        return False


def _sources(kind):
    root = parquet.collector_data_root() if kind == "ob_1s" else parquet.data_root()
    found = {}
    try:
        exchanges = sorted(root.iterdir())
    except OSError:
        return found
    for exchange in exchanges:
        if not IDENTIFIER.fullmatch(exchange.name) or not _safe(exchange / kind, root):
            continue
        if kind == "ob_1s" and exchange.name not in {"bithumb", "upbit"}:
            continue
        try:
            symbols = {path.name: path for path in sorted((exchange / kind).iterdir())
                       if IDENTIFIER.fullmatch(path.name) and _safe(path, root)
                       and (kind != "ob_1s" or path.name.upper() == "USDT")}
        except OSError:
            continue
        if symbols:
            found[exchange.name] = symbols
    return found


def catalog():
    return {"kinds": [{"id": kind, "label": label, "live_supported": True} for kind, label in KINDS.items()],
            "exchanges": {kind: {exchange: list(symbols) for exchange, symbols in _sources(kind).items()}
                          for kind in KINDS}}


def _files(directory: Path, start, end, root):
    first, last = start.floor("h"), (end - pd.Timedelta(nanoseconds=1)).floor("h")
    found = {}
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        folder = directory / day.strftime("%Y/%m/%d")
        if not _safe(folder, root):
            continue
        for suffix in (".parquet.gz", ".parquet"):
            for path in sorted(folder.glob(f"*{suffix}")):
                hour = path.name.removesuffix(".gz").removesuffix(".parquet")
                if not hour.isdigit() or len(hour) != 2 or not 0 <= int(hour) <= 23:
                    continue
                if not first <= day + pd.Timedelta(hours=int(hour)) <= last or not _safe(path, root, file=True):
                    continue
                logical = str(path).removesuffix(".gz")
                found[logical] = str(path)  # Prefer plain Parquet over a gzip sibling.
    return [found[key] for key in sorted(found)]


def _valid(column):
    return f"(isfinite({column}) AND {column} >= {MIN_TIMESTAMP_MS} AND {column} < {MAX_TIMESTAMP_MS})"


def _empty_stats():
    return {"rows": 0, "valid_pairs": 0, "invalid_server_rows": 0, "unplaced_desktop_rows": 0,
            "negative_count": 0, "above_threshold_count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None,
            "min_ms": None, "max_ms": None, "latest_ms": None, "latest_desktop_ms": None, "latest_server_ms": None}


def _series(exchange, ticker):
    return {"exchange": exchange, "ticker": ticker, "status": "no_data", "times": [],
            **{name: [] for name in ("mean_ms", "p95_ms", "max_ms", "min_ms", "count", "negative_count", "above_threshold_count")},
            "stats": _empty_stats(), "time_basis": "desktopTime", "source": "parquet"}


def _sample_indices(frame, maximum):
    if len(frame) <= maximum:
        return list(range(len(frame)))
    # Preserve the greatest positive spike and the negative extreme even with
    # a tiny chart budget. Full raw quantiles/stats were already calculated.
    priority = [int(frame["max_ms"].argmax()), int(frame["min_ms"].argmin()), len(frame) - 1, 0]
    chosen = []
    for index in priority:
        if index not in chosen:
            chosen.append(index)
        if len(chosen) == maximum:
            return sorted(chosen)
    for slot in range(maximum):
        index = round(slot * (len(frame) - 1) / max(1, maximum - 1))
        if index not in chosen:
            chosen.append(index)
        if len(chosen) == maximum:
            return sorted(chosen)
    for index in range(len(frame)):
        if index not in chosen:
            chosen.append(index)
        if len(chosen) == maximum:
            break
    return sorted(chosen)


def _finite(value):
    return float(value) if value is not None and pd.notna(value) and math.isfinite(float(value)) else None


def _read(exchange, ticker, directory, kind, start, end, step, threshold, maximum, budget):
    result = _series(exchange, ticker)
    root = parquet.collector_data_root() if kind == "ob_1s" else parquet.data_root()
    paths = _files(directory, start, end, root)
    if not paths:
        return result
    if len(paths) > MAX_FILES - budget["files"]:
        raise DataError("요청의 데이터 파일 수가 1,024개 한도를 초과했습니다. 기간·거래소 수를 줄여 주세요.")
    budget["files"] += len(paths)
    remaining_archive_bytes = MAX_ARCHIVE_BYTES - budget["archive_bytes"]
    if remaining_archive_bytes <= 0 and any(path.endswith(".gz") for path in paths):
        raise DataError("요청의 압축 해제 용량 제한을 초과했습니다. 기간·거래소 수를 줄여 주세요.")
    con = parquet.connection()
    try:
        with materialize_parquet(paths, max_files=MAX_FILES,
                                 max_total_bytes=max(1, remaining_archive_bytes),
                                 max_file_bytes=MAX_ARCHIVE_FILE_BYTES) as readable, parquet.query_deadline(con):
            budget["archive_bytes"] += sum(Path(target).stat().st_size for source, target in zip(paths, readable) if source.endswith(".gz"))
            columns = {row[0] for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)", [readable]).fetchall()}
            if not {"desktopTime", "serverTime"}.issubset(columns):
                raise DataError("시간차 측정에 필요한 desktopTime·serverTime 컬럼이 없습니다.")
            received = kind == "ob_1s" and "receivedTime" in columns
            received_sql = 'try_cast("receivedTime" AS DOUBLE)' if received else "NULL::DOUBLE"
            desktop_valid, server_valid = _valid("desktop_ms"), _valid("server_ms")
            con.execute(f"""
                CREATE TEMP TABLE quality_raw AS
                SELECT * FROM (
                    SELECT try_cast("desktopTime" AS DOUBLE) AS desktop_ms,
                           try_cast("serverTime" AS DOUBLE) AS server_ms,
                           {received_sql} AS received_ms
                    FROM read_parquet($paths, union_by_name=true)
                ) WHERE NOT coalesce({desktop_valid}, false)
                        OR (desktop_ms >= $start AND desktop_ms < $end)
                LIMIT $row_limit
            """, {"paths": readable, "start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000), "row_limit": MAX_RAW_ROWS + 1})
            rows, invalid_server, unplaced = con.execute(f"""
                SELECT count(*), count(*) FILTER (WHERE {desktop_valid} AND NOT coalesce({server_valid}, false)),
                       count(*) FILTER (WHERE NOT coalesce({desktop_valid}, false)) FROM quality_raw
            """).fetchone()
            if rows > MAX_RAW_ROWS:
                raise DataError("거래소별 원본 5,000,000행 한도를 초과했습니다. 조회 기간을 줄여 주세요.")
            con.execute(f"""
                CREATE TEMP VIEW quality_valid AS
                SELECT *, desktop_ms - server_ms AS delta_ms,
                       CASE WHEN {_valid('received_ms')} THEN received_ms - server_ms END AS received_delta_ms
                FROM quality_raw WHERE {desktop_valid} AND {server_valid}
            """)
            stats_row = con.execute("""
                SELECT count(*), count(*) FILTER (WHERE delta_ms < 0), count(*) FILTER (WHERE delta_ms > $threshold),
                       avg(delta_ms), quantile_cont(delta_ms, 0.50), quantile_cont(delta_ms, 0.95), min(delta_ms), max(delta_ms),
                       arg_max(struct_pack(value := delta_ms, desktop := desktop_ms, server := server_ms), desktop_ms),
                       avg(received_delta_ms)
                FROM quality_valid
            """, {"threshold": threshold}).fetchone()
            latest = stats_row[8]
            result["stats"].update(rows=int(rows), valid_pairs=int(stats_row[0]), invalid_server_rows=int(invalid_server),
                                   unplaced_desktop_rows=int(unplaced), negative_count=int(stats_row[1]), above_threshold_count=int(stats_row[2]),
                                   **{name: _finite(value) for name, value in zip(("mean_ms", "p50_ms", "p95_ms", "min_ms", "max_ms"), stats_row[3:8])},
                                   latest_ms=_finite(latest["value"]) if latest else None,
                                   latest_desktop_ms=_finite(latest["desktop"]) if latest else None,
                                   latest_server_ms=_finite(latest["server"]) if latest else None)
            if not stats_row[0]:
                if rows:
                    result["error"] = "조회 기간에 유효한 UTC 밀리초 시각 쌍이 없습니다. 잘못된 시각 행 통계를 확인해 주세요."
                return result
            frame = con.execute("""
                SELECT CAST(floor(desktop_ms / $step) AS BIGINT) * $step AS time,
                       avg(delta_ms) AS mean_ms, quantile_cont(delta_ms, 0.95) AS p95_ms,
                       max(delta_ms) AS max_ms, min(delta_ms) AS min_ms, count(*) AS count,
                       count(*) FILTER (WHERE delta_ms < 0) AS negative_count,
                       count(*) FILTER (WHERE delta_ms > $threshold) AS above_threshold_count,
                       avg(received_delta_ms) AS received_mean_ms
                FROM quality_valid GROUP BY time ORDER BY time
            """, {"step": step, "threshold": threshold}).df()
            if len(frame) > MAX_BUCKETS:
                raise DataError("차트 구간 수가 250,000개 한도를 초과했습니다.")
            result.update(status="ok", bucket_count=len(frame))
            frame = frame.iloc[_sample_indices(frame, maximum)]
            result["times"] = [int(value) for value in frame["time"]]
            for field in ("mean_ms", "p95_ms", "max_ms", "min_ms"):
                result[field] = [_finite(value) for value in frame[field]]
            for field in ("count", "negative_count", "above_threshold_count"):
                result[field] = [int(value) for value in frame[field]]
            if received:
                result["received_mean_ms"] = [_finite(value) for value in frame["received_mean_ms"]]
                result["stats"]["received_mean_ms"] = _finite(stats_row[9])
            return result
    except duckdb.Error as error:
        raise DataError("시간차 데이터 파일을 읽지 못했습니다. Parquet 파일과 시간 컬럼 스키마를 확인해 주세요.") from error
    except DataError as error:
        if str(error).startswith("펀딩비"):
            raise DataError(str(error).replace("펀딩비", "시간차", 1)) from None
        raise
    finally:
        con.close()


def query_history(payload):
    if not isinstance(payload, dict):
        raise DataError("시간차 조회 조건을 확인해 주세요.")
    kind = payload.get("data_kind", "ob")
    if not isinstance(kind, str) or kind not in KINDS:
        raise DataError("시간차 데이터는 호가·체결·펀딩·1초 호가 중에서 선택해 주세요.")
    start, end = parse_date(payload.get("start"), "시작 시간"), parse_date(payload.get("end"), "종료 시간")
    if start >= end or end - start > pd.Timedelta(days=7):
        raise DataError("시간차 조회 기간은 0일 초과 7일 이하여야 합니다.")
    interval = payload.get("interval", "1min")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("지원하지 않는 시간차 조회 간격입니다.")
    step = INTERVALS[interval]
    start_ms, end_ms = int(start.value // 1_000_000), int(end.value // 1_000_000)
    if (end_ms - 1) // step - start_ms // step + 1 > MAX_BUCKETS:
        raise DataError("조회 구간이 250,000개를 초과합니다. 기간을 줄이거나 간격을 늘려 주세요.")
    maximum = integer(payload.get("max_points", 2000), "차트 표시 개수", 1, 6000)
    raw_threshold = payload.get("threshold_ms", 1000)
    try:
        threshold = float(raw_threshold)
        if isinstance(raw_threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 86_400_000:
            raise ValueError("range")
    except (TypeError, ValueError, OverflowError):
        raise DataError("시간차 임계값은 0~86,400,000 범위의 유한한 ms 숫자로 입력해 주세요.") from None
    ticker = payload.get("ticker", "")
    if ticker is None:
        ticker = ""
    if not isinstance(ticker, str) or (ticker.strip() and not IDENTIFIER.fullmatch(ticker.strip())):
        raise DataError("시간차 조회 종목이 올바르지 않습니다.")
    ticker = ticker.strip()
    sources = _sources(kind)
    exchanges = payload.get("exchanges")
    automatic_exchanges = exchanges is None
    if automatic_exchanges:
        exchanges = list(sources)[:8]
    if (not isinstance(exchanges, list) or (not exchanges and not automatic_exchanges) or len(exchanges) > 8 or not all(isinstance(ex, str) and IDENTIFIER.fullmatch(ex) for ex in exchanges)
            or len(set(exchanges)) != len(exchanges)):
        raise DataError("거래소는 중복 없이 최대 8개까지 선택해 주세요.")
    warnings = ["desktopTime과 serverTime은 서로 다른 장비의 시계입니다. 시간차는 왕복 지연(RTT)이 아니며 음수는 시계 차이를 포함할 수 있습니다.",
                "타임스탬프는 UTC 2000~2100년의 밀리초 범위만 유효합니다. 단위를 자동 변환하거나 빈 구간을 채우지 않습니다.",
                "체결·펀딩의 갱신 간격은 거래소와 수집 방식에 따라 달라 빈 구간만으로 유실을 단정할 수 없습니다."]
    if automatic_exchanges and len(sources) > 8:
        warnings.append("한 번에 최대 8개 거래소를 조회하므로 앞선 8개 거래소를 선택했습니다.")
    if kind == "ob_1s":
        warnings.append("1초 호가의 desktopTime은 실제 채택 시각이므로 원본 호가 경과 시간과 샘플 대기를 포함합니다. receivedTime-serverTime 평균은 별도 보조 지표입니다.")
        warnings.append("1초 수집기는 10초를 초과한 오래된 호가를 저장하지 않으므로 분포가 해당 TTL로 제한됩니다. 10초 초과값이 없다는 사실은 품질 보증이 아니며 누락 구간도 함께 확인해야 합니다.")
    budget, series = {"files": 0, "archive_bytes": 0}, []
    for exchange in exchanges:
        available = sources.get(exchange, {})
        lookup = {symbol.upper(): symbol for symbol in available}
        selected = lookup.get(ticker.upper()) if ticker else lookup.get("BTC") or next(iter(available), None)
        item = _series(exchange, selected or ticker or None)
        reason = unmeasurable_clock_reason(exchange, kind)
        if reason:
            item["error"] = reason
            warnings.append(f"{exchange}: {reason}")
        elif selected is None:
            item["error"] = "선택한 거래소·종목에 해당 시간차 데이터가 없습니다."
        else:
            try:
                item = _read(exchange, selected, available[selected], kind, start, end, step, threshold, maximum, budget)
                if item["stats"]["unplaced_desktop_rows"]:
                    warnings.append(f"{exchange}: 배치 불가 desktopTime 행은 시각 필터를 적용할 수 없어 선택된 파일 전체에서 집계했습니다.")
                if item.get("bucket_count", 0) > len(item["times"]):
                    warnings.append(f"{exchange}: 차트만 {len(item['times'])}개로 줄였으며 통계는 전체 유효 원본 행으로 계산했습니다.")
            except DataError as error:
                item.update(status="error", error=str(error))
            except (OSError, ValueError):
                item.update(status="error", error="시간차 원본 파일을 읽을 수 없습니다.")
        series.append(item)
    return {"mode": "history", "data_kind": kind, "threshold_ms": threshold, "series": series,
            "meta": {"start": start.isoformat(), "end": end.isoformat(), "interval": interval,
                     "definition": "desktopTime - serverTime", "unit": "ms", "warnings": warnings}}
