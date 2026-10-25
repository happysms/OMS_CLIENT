"""Durable spool and atomic hourly Parquet publication for 1-second books."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from oms_client.paths import ROOT

EXCHANGES = ("bithumb", "upbit")
SAMPLE_SCHEMA = pa.schema([
    ("sampleTime", pa.int64()), ("desktopTime", pa.int64()),
    ("serverTime", pa.int64()), ("receivedTime", pa.int64()), ("sampledAt", pa.int64()),
    ("b1_p", pa.float64()), ("a1_p", pa.float64()),
    ("bid_size", pa.float64()), ("ask_size", pa.float64()), ("quote_age_ms", pa.int64()),
    ("price_unit", pa.string()), ("quantity_unit", pa.string()),
    ("sample_interval_ms", pa.int64()), ("market", pa.string()),
])
COLUMNS = SAMPLE_SCHEMA.names


def default_data_root() -> Path:
    return Path(os.environ.get("DASHBOARD_COLLECTOR_DATA_ROOT", ROOT / "data" / "market")).expanduser().resolve()


def default_state_dir() -> Path:
    state_root = Path(os.environ.get("DASHBOARD_STATE_DIR") or ROOT / ".state").expanduser().resolve()
    return state_root / "collectors" / "usdt"


class CollectorAlreadyRunning(RuntimeError):
    pass


class SpoolFull(RuntimeError):
    pass


def fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(directory: Path, mode=0o755) -> None:
    """Persist new directory entries all the way to an existing ancestor."""
    missing, cursor = [], directory
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True, mode=mode)
    for created in missing:  # leaf first, then each newly created parent
        fsync_directory(created)
    if missing:
        fsync_directory(missing[-1].parent)


def parquet_path(data_root: Path, exchange: str, timestamp_ms: int) -> Path:
    if exchange not in EXCHANGES:
        raise ValueError("Unsupported exchange")
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    return data_root / exchange / "ob_1s" / "USDT" / stamp.strftime("%Y/%m/%d/%H.parquet")


class OrderbookSampleStore:
    """Commit samples before publishing; replaying a publish is idempotent.

    The data-root lock protects the complete dataset even when two processes
    are accidentally configured with different SQLite state directories.
    """

    def __init__(self, data_root=None, state_dir=None, max_pending=36_000):
        self.data_root = Path(data_root or default_data_root()).expanduser().resolve()
        self.state_dir = Path(state_dir or default_state_dir()).expanduser().resolve()
        self.max_pending = int(max_pending)
        if self.max_pending < 2:
            raise ValueError("max_pending must be at least 2")
        durable_mkdir(self.data_root)
        durable_mkdir(self.state_dir, mode=0o700)
        self._lock_file = (self.data_root / ".usdt-orderbook.lock").open("a+")
        self._state_lock_file = None
        self.connection = None
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise CollectorAlreadyRunning("A USDT collector already writes this data root") from None
        try:
            self._state_lock_file = (self.state_dir / ".usdt-collector.lock").open("a+")
            try:
                fcntl.flock(self._state_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CollectorAlreadyRunning("A USDT collector already uses this state directory") from None
            self.connection = sqlite3.connect(self.state_dir / "samples.sqlite3", timeout=10)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            definitions = []
            for field in SAMPLE_SCHEMA:
                sql_type = "TEXT" if pa.types.is_string(field.type) else "REAL" if pa.types.is_floating(field.type) else "INTEGER"
                definitions.append(f'"{field.name}" {sql_type} NOT NULL')
            self.connection.execute("CREATE TABLE IF NOT EXISTS samples (exchange TEXT NOT NULL, " +
                                    ", ".join(definitions) + ", PRIMARY KEY(exchange, sampleTime))")
            self.connection.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            binding = self.connection.execute("SELECT value FROM settings WHERE name='data_root'").fetchone()
            if binding is not None and binding[0] != str(self.data_root):
                raise ValueError("This state directory belongs to another data root; use a separate --state-dir")
            self.connection.execute("INSERT OR IGNORE INTO settings(name,value) VALUES ('data_root',?)", (str(self.data_root),))
            self.connection.commit()
            fsync_directory(self.state_dir)
        except Exception:
            self.close()
            raise

    @property
    def pending_rows(self) -> int:
        return self.connection.execute("SELECT count(*) FROM samples").fetchone()[0]

    def counters(self) -> dict:
        return dict(self.connection.execute("SELECT name, value FROM counters"))

    def _increment(self, name, value):
        self.connection.execute("INSERT INTO counters(name, value) VALUES (?, ?) "
                                "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, value))

    def append(self, samples: list[dict]) -> int:
        if not samples:
            return 0
        for sample in samples:
            if sample.get("exchange") not in EXCHANGES:
                raise ValueError("Unsupported exchange")
        columns = ["exchange", *COLUMNS]
        names = ",".join(f'"{name}"' for name in columns)
        values = [tuple(sample[name] for name in columns) for sample in samples]
        before = self.connection.total_changes
        try:
            self.connection.executemany(f"INSERT OR IGNORE INTO samples ({names}) VALUES ({','.join('?' for _ in columns)})", values)
            inserted = self.connection.total_changes - before
            if self.pending_rows > self.max_pending:
                raise SpoolFull(f"Pending samples exceeded {self.max_pending} rows; existing samples are preserved")
            self._increment("samples_committed", inserted)
            self.connection.commit()
            return inserted
        except Exception:
            self.connection.rollback()
            raise

    def pending(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM samples ORDER BY sampleTime, exchange")]

    def _publish(self, path: Path, samples: list[dict]) -> None:
        by_slot = {}
        if path.exists():
            existing = pq.read_table(path, schema=SAMPLE_SCHEMA).to_pylist()
            by_slot.update((row["sampleTime"], row) for row in existing)
        by_slot.update((row["sampleTime"], {name: row[name] for name in COLUMNS}) for row in samples)
        table = pa.Table.from_pylist([by_slot[key] for key in sorted(by_slot)], schema=SAMPLE_SCHEMA)
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

    def flush(self) -> int:
        groups = defaultdict(list)
        for sample in self.pending():
            groups[parquet_path(self.data_root, sample["exchange"], sample["desktopTime"])].append(sample)
        flushed = 0
        for path, samples in sorted(groups.items()):
            self._publish(path, samples)
            # A crash before this commit leaves rows in the spool. Replaying
            # them replaces the same sampleTime, never appends duplicates.
            with self.connection:
                self.connection.executemany("DELETE FROM samples WHERE exchange=? AND sampleTime=?",
                                            [(row["exchange"], row["sampleTime"]) for row in samples])
                self._increment("samples_flushed", len(samples))
            flushed += len(samples)
        return flushed

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if not self._lock_file.closed:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
        if self._state_lock_file is not None and not self._state_lock_file.closed:
            fcntl.flock(self._state_lock_file, fcntl.LOCK_UN)
            self._state_lock_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
