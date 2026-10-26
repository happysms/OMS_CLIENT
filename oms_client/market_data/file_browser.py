"""Browse only declared market-data layouts and read bounded Parquet footers."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import pyarrow.parquet as pq

from . import parquet
from .funding_archives import materialize_parquet
from .validation import DataError, IDENTIFIER, integer

LEGACY_KINDS = {"ob", "tick", "funding", "fx"}
COLLECTOR_KINDS = {"ob_1s", "candles_1m"}
KINDS = LEGACY_KINDS | COLLECTOR_KINDS
KIND_LABELS = {"ob": "호가", "tick": "체결", "funding": "펀딩비율", "fx": "환율",
               "ob_1s": "1초 호가", "candles_1m": "과거 1분봉"}
EXCHANGE_LABELS = {"bithumb": "빗썸", "upbit": "업비트", "binance": "바이낸스", "okx": "OKX",
                   "hyperliquid": "Hyperliquid", "ls": "LS증권", "fx": "환율"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
_FILE = re.compile(r"^(\d{2})\.parquet(\.gz)?$")
_GZIP_METADATA_BYTES = 32 * 1024 * 1024
_FOOTER_STRING_BYTES = 8 * 1024 * 1024
_TIME_FIELDS = {"desktopTime", "serverTime", "sampledAt", "receivedTime", "candleTime", "availableTime"}


def _encode(parts) -> str:
    return base64.urlsafe_b64encode(json.dumps([1, *parts], separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(identifier) -> tuple[str, ...]:
    if identifier is None or identifier == "":
        return ()
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise DataError("데이터 파일 식별자가 올바르지 않습니다.")
    try:
        decoded = json.loads(base64.b64decode(identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True))
        if not isinstance(decoded, list) or not decoded or decoded[0] != 1 or isinstance(decoded[0], bool):
            raise ValueError("version")
        parts = tuple(decoded[1:])
        if not all(isinstance(part, str) for part in parts) or _encode(parts) != identifier:
            raise ValueError("encoding")
        _validate_parts(parts)
        return parts
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        raise DataError("데이터 파일 식별자가 올바르지 않습니다.") from None


def _validate_parts(parts):
    if len(parts) > 7:
        raise ValueError("depth")
    if parts and not IDENTIFIER.fullmatch(parts[0]):
        raise ValueError("exchange")
    if len(parts) < 2:
        return
    if parts[1] not in KINDS:
        raise ValueError("kind")
    if len(parts) > 2 and not IDENTIFIER.fullmatch(parts[2]):
        raise ValueError("ticker")
    if len(parts) > 3 and not re.fullmatch(r"[1-9]\d{3}", parts[3]):
        raise ValueError("year")
    if len(parts) > 4 and not re.fullmatch(r"0[1-9]|1[0-2]", parts[4]):
        raise ValueError("month")
    if parts[1] == "candles_1m":
        if len(parts) > 6:
            raise ValueError("candle depth")
        if len(parts) == 6:
            match = _FILE.fullmatch(parts[5])
            if not match:
                raise ValueError("candle file")
            datetime(int(parts[3]), int(parts[4]), int(match[1]))
    else:
        if len(parts) > 5:
            if not re.fullmatch(r"0[1-9]|[12]\d|3[01]", parts[5]):
                raise ValueError("day")
            datetime(int(parts[3]), int(parts[4]), int(parts[5]))
        if len(parts) == 7:
            match = _FILE.fullmatch(parts[6])
            if not match or not 0 <= int(match[1]) <= 23:
                raise ValueError("hour file")


def _root(kind):
    return parquet.collector_data_root() if kind in COLLECTOR_KINDS else parquet.data_root()


def _is_file(parts):
    return len(parts) >= 2 and len(parts) == (6 if parts[1] == "candles_1m" else 7)


def _safe_path(path: Path, root: Path, *, is_file=False):
    try:
        relative = path.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        return (path.is_file() if is_file else path.is_dir()) and parquet.within_root(path, root)
    except (OSError, RuntimeError, ValueError):
        return False


def _resolve(parts):
    if len(parts) < 2:
        return None
    root = _root(parts[1])
    path = root.joinpath(*parts)
    if not _safe_path(path, root, is_file=_is_file(parts)):
        raise DataError("데이터 파일 또는 폴더가 없거나 접근할 수 없습니다.")
    return path


def _label(parts):
    if not parts:
        return "데이터 저장소"
    if len(parts) == 1:
        return EXCHANGE_LABELS.get(parts[0], parts[0])
    if len(parts) == 2:
        return KIND_LABELS[parts[1]]
    return parts[-1]


def _source(parts):
    return "collector" if len(parts) > 1 and parts[1] in COLLECTOR_KINDS else "legacy" if len(parts) > 1 else None


def _node(parts):
    return {"id": _encode(parts), "name": parts[-1] if parts else "data", "label": _label(parts)}


def _children(parts):
    if not parts:
        exchanges = set()
        for root, kinds in ((parquet.data_root(), LEGACY_KINDS), (parquet.collector_data_root(), COLLECTOR_KINDS)):
            try:
                for path in root.iterdir():
                    if (IDENTIFIER.fullmatch(path.name) and _safe_path(path, root)
                            and any(_safe_path(path / kind, root) for kind in kinds)):
                        exchanges.add(path.name)
            except OSError:
                continue
        return [(exchange,) for exchange in sorted(exchanges)]
    if len(parts) == 1:
        result = [(parts[0], kind) for kind in sorted(KINDS) if _safe_path(_root(kind) / parts[0] / kind, _root(kind))]
        if not result:
            raise DataError("선택한 거래소의 데이터 폴더가 없습니다.")
        return result
    path = _resolve(parts)
    if _is_file(parts):
        raise DataError("파일 목록은 폴더를 선택해 조회해 주세요.")
    result = []
    try:
        for child in path.iterdir():
            candidate = (*parts, child.name)
            try:
                _validate_parts(candidate)
            except (ValueError, TypeError):
                continue
            if _safe_path(child, _root(parts[1]), is_file=_is_file(candidate)):
                result.append(candidate)
    except OSError:
        raise DataError("선택한 데이터 폴더를 읽을 수 없습니다.") from None
    return sorted(result, key=lambda entry: tuple(part.casefold() for part in entry))


def _modified(stat_result):
    return datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _entry(parts):
    is_file = _is_file(parts)
    item = {**_node(parts), "kind": "file" if is_file else "directory", "expandable": not is_file,
            "exchange": parts[0], "data_kind": parts[1] if len(parts) > 1 else None}
    if len(parts) > 1:
        item["source"] = _source(parts)
    if len(parts) > 2:
        item["ticker"] = parts[2]
    if is_file:
        info = _resolve(parts).stat()
        item.update(format="parquet.gz" if parts[-1].endswith(".gz") else "parquet",
                    bytes=info.st_size, modified_at=_modified(info))
    return item


def list_files(node=None, *, offset=0, limit=100, q=None):
    parts = _decode(node)
    offset = integer(offset, "파일 목록 시작 위치", 0, 1_000_000)
    limit = integer(limit, "파일 목록 개수", 1, 100)
    if q is not None and (not isinstance(q, str) or len(q) > 80):
        raise DataError("종목 검색어는 80자 이하로 입력해 주세요.")
    children = _children(parts)
    if len(parts) == 2 and q:
        search = q.strip().casefold()
        children = [child for child in children if search in child[-1].casefold()]
    total = len(children)
    items = []
    for child in children[offset:offset + limit]:
        try:
            items.append(_entry(child))
        except (DataError, OSError):
            continue  # A concurrently replaced/removed file must not fail a directory.
    has_more = offset + limit < total
    return {"node": _node(parts), "items": items, "offset": offset, "total": total,
            "has_more": has_more, "next_offset": offset + limit if has_more else None}


@contextmanager
def _open_source(parts):
    """Walk with openat/O_NOFOLLOW so metadata reads cannot escape via a race."""
    descriptors = []
    try:
        descriptor = os.open(_root(parts[1]), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError("Not a regular file")
        with os.fdopen(descriptor, "rb") as stream:
            yield stream
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_footer(parts):
    with _open_source(parts) as stream:
        if not parts[-1].endswith(".gz"):
            reader = pq.ParquetFile(stream, thrift_string_size_limit=_FOOTER_STRING_BYTES, thrift_container_size_limit=100_000)
            return reader.metadata, reader.schema_arrow
        # Keep the already-open source stable during bounded extraction. The
        # existing helper requires a named .gz source, so copy compressed bytes
        # to a private temporary file, bounded to the same 32 MiB maximum.
        if os.fstat(stream.fileno()).st_size > _GZIP_METADATA_BYTES:
            raise DataError("압축 파일의 메타데이터 읽기 용량 제한을 초과했습니다.")
        with tempfile.TemporaryDirectory(prefix="oms-file-info-") as directory:
            compressed = Path(directory) / "source.parquet.gz"
            with compressed.open("wb") as target:
                copied = 0
                while True:
                    chunk = stream.read(min(1024 * 1024, _GZIP_METADATA_BYTES - copied + 1))
                    if not chunk:
                        break
                    if copied + len(chunk) > _GZIP_METADATA_BYTES:
                        raise DataError("압축 파일의 메타데이터 읽기 용량 제한을 초과했습니다.")
                    target.write(chunk)
                    copied += len(chunk)
            with materialize_parquet([compressed], max_total_bytes=_GZIP_METADATA_BYTES,
                                     max_file_bytes=_GZIP_METADATA_BYTES, max_files=1) as paths:
                reader = pq.ParquetFile(paths[0], thrift_string_size_limit=_FOOTER_STRING_BYTES, thrift_container_size_limit=100_000)
                return reader.metadata, reader.schema_arrow


def _query(parts):
    exchange, kind, ticker = parts[:3]
    if kind == "fx":
        return None, "환율 파일은 현재 데이터 조회 소스로 지원하지 않습니다."
    if parts[-1].endswith(".gz") and kind not in {"funding", "tick"}:
        return None, "이 유형의 압축 파일은 현재 분석 조회에서 지원하지 않습니다."
    if kind in {"ob", "ob_1s"}:
        try:
            selected = parquet._resolve_source(exchange, ticker)
        except DataError:
            return None, "이 호가 파일의 소스는 현재 분석 조회에서 지원하지 않습니다."
        if selected["directory"].resolve() != _root(kind).joinpath(*parts[:3]).resolve():
            return None, "같은 종목의 1초 수집 데이터가 우선 선택되므로 이 기존 파일은 바로 조회할 수 없습니다."
        data_type = "price"
    elif kind == "tick":
        if parts[-1].endswith(".gz") and _safe_path(_root(kind).joinpath(*parts).with_suffix(""), _root(kind), is_file=True):
            return None, "같은 시간의 일반 Parquet 체결 파일이 우선 선택됩니다. 압축 파일 대신 해당 일반 파일을 선택해 주세요."
        data_type = "tick"
    elif kind == "candles_1m":
        if exchange not in {"bithumb", "upbit"} or ticker != "USDT":
            return None, "이 1분봉 파일의 소스는 현재 분석 조회에서 지원하지 않습니다."
        data_type = "candle"
    else:
        data_type = "funding"
    match = _FILE.fullmatch(parts[-1])
    if kind == "candles_1m":
        start = datetime(int(parts[3]), int(parts[4]), int(match[1]), tzinfo=timezone.utc) + timedelta(minutes=1)
        end = start + timedelta(days=1)
    else:
        start = datetime(int(parts[3]), int(parts[4]), int(parts[5]), int(match[1]), tzinfo=timezone.utc)
        end = start + timedelta(hours=1)
    return {"data_type": data_type, "exchange": exchange, "ticker": ticker,
            "start": start.isoformat().replace("+00:00", "Z"), "end": end.isoformat().replace("+00:00", "Z"),
            "interval": "1s" if kind == "tick" else "1min"}, None


def _time_stats(metadata):
    found = {}
    for row_index in range(min(metadata.num_row_groups, 1000)):
        group = metadata.row_group(row_index)
        for column_index in range(group.num_columns):
            column = group.column(column_index)
            if column.path_in_schema not in _TIME_FIELDS:
                continue
            statistics = column.statistics
            if statistics is None or not statistics.has_min_max:
                continue
            low, high = statistics.min, statistics.max
            if isinstance(low, bool) or not isinstance(low, int) or not isinstance(high, int):
                continue
            current = found.setdefault(column.path_in_schema, {"min_ms": low, "max_ms": high})
            current["min_ms"], current["max_ms"] = min(current["min_ms"], low), max(current["max_ms"], high)
    # Partial footer ranges must not be presented as complete bounds.
    return found if metadata.num_row_groups <= 1000 else {}


def file_info(node):
    parts = _decode(node)
    if not _is_file(parts):
        raise DataError("상세 정보를 확인할 데이터 파일을 선택해 주세요.")
    path = _resolve(parts)
    info = path.stat()
    query, unsupported = _query(parts)
    result = {**_node(parts), "exchange": parts[0], "data_kind": parts[1], "ticker": parts[2],
              "source": _source(parts), "relative_path": "/".join(parts),
              "format": "parquet.gz" if parts[-1].endswith(".gz") else "parquet",
              "bytes": info.st_size, "modified_at": _modified(info), "rows": None, "columns": [], "query": query}
    messages = [unsupported] if unsupported else []
    try:
        metadata, schema = _read_footer(parts)
        result.update(rows=metadata.num_rows, columns=[{"name": field.name, "type": str(field.type)} for field in schema],
                      time_stats=_time_stats(metadata))
        if metadata.num_rows == 0:
            result["query"] = None
            messages.append("파일에 저장된 데이터 행이 없습니다.")
    except Exception:
        messages.append("파일 메타데이터를 읽을 수 없거나 읽기 용량 제한을 초과했습니다. 파일 크기와 수정 시각만 표시합니다.")
    if messages:
        result["message"] = " ".join(messages)
    return result
