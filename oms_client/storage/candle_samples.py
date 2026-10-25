"""Atomic, idempotent UTC-day storage for public historical 1-minute candles."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from oms_client.storage.orderbook_samples import (
    CollectorAlreadyRunning, EXCHANGES, default_data_root, durable_mkdir, fsync_directory,
)

CANDLE_SCHEMA = pa.schema([
    ("candleTime", pa.int64()), ("availableTime", pa.int64()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("volume", pa.float64()), ("trade_value", pa.float64()), ("fetchedAt", pa.int64()),
    ("market", pa.string()), ("price_unit", pa.string()), ("quantity_unit", pa.string()),
    ("candle_interval_ms", pa.int64()), ("source", pa.string()),
])


def candle_path(data_root: Path, exchange: str, candle_time: int) -> Path:
    if exchange not in EXCHANGES:
        raise ValueError("Unsupported exchange")
    stamp = datetime.fromtimestamp(candle_time / 1000, timezone.utc)
    return data_root / exchange / "candles_1m" / "USDT" / stamp.strftime("%Y/%m/%d.parquet")


class MinuteCandleStore:
    """Existing closed candles win on reruns, including their fetchedAt provenance."""

    def __init__(self, data_root=None):
        self.data_root = Path(data_root or default_data_root()).expanduser().resolve()
        durable_mkdir(self.data_root)
        # Deliberately independent of the continuously running 1-second book
        # collector. Only another candle backfill is excluded by this lock.
        self._lock = (self.data_root / ".usdt-candles-1m.lock").open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise CollectorAlreadyRunning("A USDT candle backfill already writes this data root") from None

    def merge(self, exchange: str, candles: list[dict]) -> int:
        groups = defaultdict(list)
        for candle in candles:
            groups[candle_path(self.data_root, exchange, candle["candleTime"])].append(candle)
        inserted = 0
        for path, incoming in sorted(groups.items()):
            existing = pq.read_table(path).select(CANDLE_SCHEMA.names).cast(CANDLE_SCHEMA).to_pylist() if path.exists() else []
            by_time = {row["candleTime"]: row for row in existing}
            before = len(by_time)
            for row in incoming:
                by_time.setdefault(row["candleTime"], {name: row[name] for name in CANDLE_SCHEMA.names})
            added = len(by_time) - before
            if not added:
                # Also make a retry after replace-but-failed-directory-fsync
                # durable, even when every incoming candle is already present.
                fsync_directory(path.parent)
                continue
            table = pa.Table.from_pylist([by_time[key] for key in sorted(by_time)], schema=CANDLE_SCHEMA)
            durable_mkdir(path.parent)
            temporary = None
            try:
                descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
                os.close(descriptor)
                pq.write_table(table, temporary, compression="zstd")
                with open(temporary, "rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                fsync_directory(path.parent)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            inserted += added
        return inserted

    def close(self):
        if not self._lock.closed:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
