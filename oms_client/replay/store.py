"""Atomic replay checkpoints and immutable fill reports, isolated from paper accounts."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


def account_metadata(identifier, config, created_ms):
    """Immutable identity and seed; current balances remain in the run checkpoint."""
    return dict(id='va_'+identifier, name=config.get('account_name') or f"{config.get('ticker', '이전')} 모의계좌 · {identifier[:8]}",
                created_at_ms=created_ms, ticker=config.get('ticker'), venues=config.get('venues', ['bithumb']),
                initial_krw=config.get('initial_krw'), initial_coin=config.get('initial_coin'))


class ReplayStore:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = Path(directory) / 'replays.sqlite3'
        with self.connect() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, updated REAL NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, report TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence));
                CREATE TABLE IF NOT EXISTS commands (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, command TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence));
                CREATE TABLE IF NOT EXISTS order_events (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, report TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence));
            ''')
            for identifier, updated, raw in con.execute('SELECT id,updated,snapshot FROM runs ORDER BY updated,id').fetchall():
                snapshot = json.loads(raw)
                metadata = snapshot.get('virtual_account')
                if metadata is None:
                    metadata = account_metadata(identifier, snapshot.get('initial_config', snapshot.get('config', {})), None)
                    metadata.update(legacy_imported=True, last_saved_at_ms=int(updated*1000))
                snapshot['virtual_account'] = metadata
                con.execute('INSERT OR IGNORE INTO accounts(run_id,metadata) VALUES (?,?)', (identifier,json.dumps(metadata)))
                if snapshot['status'] in {'loading','running','paused'}:
                    snapshot.update(status='interrupted', error='서버가 종료되어 마지막 저장 시점에서 중단되었습니다. 새 실행을 시작하세요.')
                con.execute('UPDATE runs SET snapshot=? WHERE id=?', (json.dumps(snapshot), identifier))

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self, snapshot, fills=(), commands=(), order_events=()):
        with self.connect() as con:
            existing = con.execute('SELECT metadata FROM accounts WHERE run_id=?', (snapshot['id'],)).fetchone()
            metadata = snapshot.get('virtual_account') or (json.loads(existing[0]) if existing else account_metadata(
                snapshot['id'], snapshot.get('initial_config', snapshot.get('config', {})), int(time.time()*1000)))
            if existing and json.loads(existing[0]) != metadata:
                raise ValueError('이미 생성한 계좌의 초기조건과 식별자는 변경할 수 없습니다.')
            con.execute('INSERT OR IGNORE INTO accounts(run_id,metadata) VALUES (?,?)', (snapshot['id'],json.dumps(metadata)))
            snapshot = dict(snapshot, virtual_account=metadata)
            con.executemany('INSERT INTO fills VALUES (?,?,?)',
                            [(snapshot['id'], row['sequence'], json.dumps(row, allow_nan=False)) for row in fills])
            con.executemany('INSERT INTO commands VALUES (?,?,?)',
                            [(snapshot['id'], row['sequence'], json.dumps(row, allow_nan=False)) for row in commands])
            con.executemany('INSERT INTO order_events VALUES (?,?,?)',
                            [(snapshot['id'], row['sequence'], json.dumps(row, allow_nan=False)) for row in order_events])
            con.execute('INSERT INTO runs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,snapshot=excluded.snapshot',
                        (snapshot['id'], time.time(), json.dumps(snapshot, allow_nan=False)))

    def get(self, identifier):
        with self.connect() as con:
            row = con.execute('SELECT snapshot FROM runs WHERE id=?', (identifier,)).fetchone()
        if not row: raise KeyError(identifier)
        return json.loads(row[0])

    def order_events(self, identifier, before, limit=500):
        with self.connect() as con:
            rows=con.execute('SELECT report FROM order_events WHERE run_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                             (identifier,before,limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def recent(self, before=None, limit=20):
        with self.connect() as con:
            rows = con.execute(
                'SELECT a.sequence,r.snapshot FROM accounts a JOIN runs r ON r.id=a.run_id '
                'WHERE a.sequence<? ORDER BY a.sequence DESC LIMIT ?',
                (before if before is not None else 2**63-1, limit)).fetchall()
        return [dict({key: snapshot.get(key) for key in
                      ('id','status','config','virtual_account','virtual_time_ms','fill_count','account','error','uncertain')},
                     history_sequence=sequence)
                for sequence,raw in rows for snapshot in [json.loads(raw)]]

    def fills(self, identifier, before=None, limit=100):
        self.get(identifier)
        with self.connect() as con:
            rows = con.execute('SELECT report FROM fills WHERE run_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                               (identifier, before if before is not None else 2**63-1, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]
