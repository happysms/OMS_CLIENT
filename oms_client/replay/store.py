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
                CREATE INDEX IF NOT EXISTS fills_time ON fills(run_id, json_extract(report,'$.timestamp'));
                CREATE INDEX IF NOT EXISTS order_events_time ON order_events(run_id, json_extract(report,'$.closed_at'));
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
        identifier = snapshot['id']
        # Serialize immutable reports before taking SQLite's write transaction.
        fill_rows = [(identifier, row['sequence'], json.dumps(row, allow_nan=False)) for row in fills]
        command_rows = [(identifier, row['sequence'], json.dumps(row, allow_nan=False)) for row in commands]
        order_rows = [(identifier, row['sequence'], json.dumps(row, allow_nan=False)) for row in order_events]
        with self.connect() as con:
            existing = con.execute('SELECT metadata FROM accounts WHERE run_id=?', (snapshot['id'],)).fetchone()
            metadata = snapshot.get('virtual_account') or (json.loads(existing[0]) if existing else account_metadata(
                snapshot['id'], snapshot.get('initial_config', snapshot.get('config', {})), int(time.time()*1000)))
            if existing and json.loads(existing[0]) != metadata:
                raise ValueError('이미 생성한 계좌의 초기조건과 식별자는 변경할 수 없습니다.')
            con.execute('INSERT OR IGNORE INTO accounts(run_id,metadata) VALUES (?,?)', (snapshot['id'],json.dumps(metadata)))
            snapshot = dict(snapshot, virtual_account=metadata)
            encoded = json.dumps(snapshot, allow_nan=False)
            con.executemany('INSERT INTO fills VALUES (?,?,?)', fill_rows)
            con.executemany('INSERT INTO commands VALUES (?,?,?)', command_rows)
            con.executemany('INSERT INTO order_events VALUES (?,?,?)', order_rows)
            con.execute('INSERT INTO runs VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,snapshot=excluded.snapshot',
                        (snapshot['id'], time.time(), encoded))
        # Returned only after commit succeeds: the UI can reuse exactly the stored JSON.
        return encoded

    def get(self, identifier):
        return json.loads(self.get_encoded(identifier))

    def get_encoded(self, identifier):
        with self.connect() as con:
            row = con.execute('SELECT snapshot FROM runs WHERE id=?', (identifier,)).fetchone()
        if not row: raise KeyError(identifier)
        return row[0]

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
                      ('id','status','config','virtual_account','virtual_time_ms','fill_count','account','usdt','activity','error','uncertain')},
                     history_sequence=sequence)
                for sequence,raw in rows for snapshot in [json.loads(raw)]]

    def fills(self, identifier, before=None, limit=100):
        with self.connect() as con:
            if con.execute('SELECT 1 FROM runs WHERE id=?', (identifier,)).fetchone() is None:
                raise KeyError(identifier)
            rows = con.execute('SELECT report FROM fills WHERE run_id=? AND sequence<? ORDER BY sequence DESC LIMIT ?',
                               (identifier, before if before is not None else 2**63-1, limit)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def fill_context(self, identifier, sequence):
        """Freeze the fill and neighbouring saved reports at one database checkpoint."""
        if type(sequence) is not int or not 1 <= sequence <= 100000:
            raise ValueError('체결 번호는 1~100000 정수여야 합니다.')
        with self.connect() as con:
            con.execute('BEGIN')
            saved = con.execute('SELECT snapshot FROM runs WHERE id=?', (identifier,)).fetchone()
            target = con.execute('SELECT report FROM fills WHERE run_id=? AND sequence=?', (identifier, sequence)).fetchone()
            if not saved or not target:
                raise KeyError(identifier)
            snapshot, fill = json.loads(saved[0]), json.loads(target[0])
            center = fill['timestamp']
            start, requested_end = max(0, center - 60000), center + 60001
            active = snapshot['status'] in {'loading', 'running', 'paused'}
            end = min(requested_end, snapshot['virtual_time_ms'] + 1) if active else requested_end
            neighbours = con.execute('''SELECT report FROM fills WHERE run_id=? AND sequence!=? AND sequence<=?
                AND json_extract(report,'$.timestamp')>=? AND json_extract(report,'$.timestamp')<?
                ORDER BY abs(json_extract(report,'$.timestamp')-?),sequence LIMIT 1000''',
                (identifier, sequence, snapshot['fill_count'], start, end, center)).fetchall()
            orders = con.execute('''SELECT report FROM order_events WHERE run_id=? AND sequence<=?
                AND json_extract(report,'$.closed_at')>=? AND json_extract(report,'$.closed_at')<?
                ORDER BY abs(json_extract(report,'$.closed_at')-?),sequence LIMIT 201''',
                (identifier, snapshot.get('order_event_count', 0), start, end, center)).fetchall()
        fills = [fill, *[json.loads(row[0]) for row in neighbours[:999]]]
        return dict(snapshot=snapshot, fill=fill, start_ms=start, end_ms=end,
                    requested_end_ms=requested_end, future_hidden=end < requested_end,
                    fills=sorted(fills, key=lambda row: (row['timestamp'], row['sequence'])),
                    fills_truncated=len(neighbours) > 999,
                    order_events=sorted([json.loads(row[0]) for row in orders[:200]],
                                        key=lambda row: (row['closed_at'], row['sequence'])),
                    orders_truncated=len(orders) > 200)
