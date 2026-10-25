"""Small, private SQLite state for the analysis server (never market data)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.database = self.directory / "dashboard.sqlite3"
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
        os.chmod(self.database, 0o600)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def setting(self, key, default=None):
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self.connection() as connection:
            connection.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def session_secret(self):
        # INSERT OR IGNORE also makes concurrent startup select a single secret.
        with self.connection() as connection:
            connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('session_secret',?)", (secrets.token_hex(32),))
            return connection.execute("SELECT value FROM settings WHERE key='session_secret'").fetchone()["value"]

    @staticmethod
    def _preset(row):
        return {"id": row["id"], "name": row["name"], "config": json.loads(row["config"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def presets(self):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM presets ORDER BY updated_at DESC,id").fetchall()
        return [self._preset(row) for row in rows]

    def save_preset(self, name, config, preset_id=None):
        now = utc_now()
        with self.connection() as connection:
            if preset_id:
                row = connection.execute("SELECT created_at FROM presets WHERE id=?", (preset_id,)).fetchone()
                if not row:
                    raise KeyError(preset_id)
                created = row["created_at"]
            else:
                if connection.execute("SELECT count(*) FROM presets").fetchone()[0] >= 100:
                    raise ValueError("저장한 조합은 최대 100개입니다. 사용하지 않는 조합을 삭제해 주세요.")
                preset_id, created = secrets.token_hex(12), now
            connection.execute("INSERT INTO presets(id,name,config,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,config=excluded.config,updated_at=excluded.updated_at", (preset_id, name, json.dumps(config, ensure_ascii=False, allow_nan=False), created, now))
        return {"id": preset_id, "name": name, "config": config, "created_at": created, "updated_at": now}

    def delete_preset(self, preset_id):
        with self.connection() as connection:
            if not connection.execute("DELETE FROM presets WHERE id=?", (preset_id,)).rowcount:
                raise KeyError(preset_id)

    def save_run(self, metadata):
        with self.connection() as connection:
            connection.execute("INSERT INTO runs(id,metadata,created_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata", (metadata["id"], json.dumps(metadata, ensure_ascii=False, allow_nan=False), metadata["created_at"]))
            connection.execute("DELETE FROM runs WHERE id IN (SELECT id FROM runs ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET 500)")

    def run(self, run_id):
        with self.connection() as connection:
            row = connection.execute("SELECT metadata FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row["metadata"]) if row else None

    def history(self, limit=50):
        with self.connection() as connection:
            rows = connection.execute("SELECT metadata FROM runs ORDER BY created_at DESC,id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["metadata"]) for row in rows]
