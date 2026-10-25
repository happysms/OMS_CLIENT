"""Bounded, temporary access to read-only compressed funding Parquet files."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import os
from pathlib import Path
import tempfile
import zlib

from .validation import DataError

_CHUNK_BYTES = 1024 * 1024


@contextmanager
def materialize_parquet(
    paths,
    *,
    max_total_bytes=256 * 1024 * 1024,
    max_file_bytes=32 * 1024 * 1024,
    max_files=1000,
):
    """Yield readable paths in order; remove all extracted files on context exit.

    The caller selects and validates source paths within the data root. Plain
    Parquet paths are returned without copying. Byte limits cover temporary
    archive output, while the file-count limit covers both kinds of source.
    No source file is changed, including when extraction or a consumer fails.
    """
    for value in (max_total_bytes, max_file_bytes, max_files):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DataError("펀딩비 압축 데이터의 읽기 제한이 올바르지 않습니다.")

    selected = []
    for source in paths:
        if len(selected) >= max_files:
            raise DataError("펀딩비 데이터 파일이 너무 많습니다. 조회 기간을 줄여 주세요.")
        try:
            path = Path(source)
        except (TypeError, ValueError) as exc:
            raise DataError("펀딩비 데이터 파일 경로가 올바르지 않습니다.") from exc
        if not (path.name.endswith(".parquet") or path.name.endswith(".parquet.gz")):
            raise DataError("지원하지 않는 펀딩비 데이터 파일 형식입니다.")
        try:
            if not path.is_file():
                raise DataError("펀딩비 데이터 파일을 찾을 수 없습니다.")
        except OSError as exc:
            raise DataError("펀딩비 데이터 파일을 읽을 수 없습니다.") from exc
        selected.append(path)

    if not any(path.name.endswith(".parquet.gz") for path in selected):
        yield [str(path) for path in selected]
        return

    # TemporaryDirectory creates a private 0700 directory. os.open also gives
    # each extracted file 0600 permissions from the moment it is created.
    with tempfile.TemporaryDirectory(prefix="oms-funding-") as directory:
        readable = []
        total_bytes = 0
        for index, source in enumerate(selected):
            if source.name.endswith(".parquet"):
                readable.append(str(source))
                continue
            target = Path(directory) / f"{index:06d}.parquet"
            file_bytes = 0
            try:
                with gzip.open(source, "rb") as compressed:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        while True:
                            remaining = min(max_file_bytes - file_bytes, max_total_bytes - total_bytes)
                            # Reading one byte past the remaining budget also
                            # detects oversized streams when a limit is exact.
                            chunk = compressed.read(min(_CHUNK_BYTES, remaining + 1))
                            if not chunk:
                                break
                            if len(chunk) > remaining:
                                raise DataError("펀딩비 압축 데이터가 읽기 용량 제한을 초과했습니다. 조회 기간을 줄여 주세요.")
                            output.write(chunk)
                            file_bytes += len(chunk)
                            total_bytes += len(chunk)
            except (OSError, EOFError, zlib.error) as exc:
                raise DataError("펀딩비 압축 데이터가 손상되었거나 읽을 수 없습니다.") from exc
            readable.append(str(target))
        yield readable
