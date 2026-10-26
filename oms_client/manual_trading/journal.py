"""Durable intent ledger: persist before dispatch, and never replay a submitted intent."""

from contextlib import contextmanager
import json
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import time
from .models import OrderReport

TERMINAL = {'filled', 'completed', 'cancelled', 'rejected'}


class OrderJournal:

    def __init__(self, directory):
        self.path = Path(directory) / 'manual_trading' / 'orders.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute(
                'CREATE TABLE IF NOT EXISTS orders(key_id TEXT NOT NULL, client_id TEXT NOT NULL, body TEXT NOT NULL, report TEXT NOT NULL, PRIMARY KEY(key_id,client_id))'
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def get(self, key_id, client_id) -> OrderReport:
        with self.connection() as con:
            row = con.execute(
                'SELECT report FROM orders WHERE key_id=? AND client_id=?', (key_id, client_id)
            ).fetchone()
        if not row:
            raise ValueError('이 창에서 등록한 주문이 아닙니다.')
        return json.loads(row[0])

    def list(self, key_id=None) -> list[OrderReport]:
        with self.connection() as con:
            rows = con.execute(
                "SELECT report FROM orders WHERE (? IS NULL OR key_id=?) AND (json_extract(report,'$.status') NOT IN ('filled','completed','cancelled','rejected') OR rowid IN (SELECT rowid FROM orders ORDER BY rowid DESC LIMIT 100)) ORDER BY rowid DESC",
                (key_id, key_id),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def has_open(self, key_id):
        with self.connection() as con:
            return bool(
                con.execute(
                    "SELECT 1 FROM orders WHERE key_id=? AND json_extract(report,'$.status') NOT IN ('filled','completed','cancelled','rejected') LIMIT 1",
                    (key_id,),
                ).fetchone()
            )

    def reserve(self, key_id, body: OrderReport) -> tuple[OrderReport, bool]:
        encoded = json.dumps(body, sort_keys=True)
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            previous = con.execute(
                'SELECT body,report FROM orders WHERE key_id=? AND client_id=?',
                (key_id, body['client_order_id']),
            ).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError('같은 주문 요청 ID의 가격·수량을 바꿀 수 없습니다.')
                return (json.loads(previous[1]), False)
            unresolved = con.execute(
                "SELECT 1 FROM orders WHERE key_id=? AND json_extract(report,'$.status') IN ('sending','unknown') LIMIT 1",
                (key_id,),
            ).fetchone()
            if unresolved:
                raise ValueError(
                    '접수 여부가 미확인인 주문이 있습니다. 해당 주문의 상태를 먼저 조회하세요.'
                )
            report = dict(
                body,
                key_id=key_id,
                status='sending',
                created_ms=int(time.time() * 1000),
                updated_ms=int(time.time() * 1000),
                filled=None,
                remaining=body['quantity'],
            )
            con.execute(
                'INSERT INTO orders VALUES (?,?,?,?)',
                (key_id, body['client_order_id'], encoded, json.dumps(report)),
            )
        return (report, True)

    def update(self, key_id, client_id, changes: OrderReport) -> OrderReport:
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute(
                'SELECT report FROM orders WHERE key_id=? AND client_id=?', (key_id, client_id)
            ).fetchone()
            if not row:
                raise ValueError('저장된 주문이 없습니다.')
            previous = json.loads(row[0])
            if previous.get('filled') is not None and changes.get('filled') is not None:
                if Decimal(changes['filled']) < Decimal(previous['filled']):
                    raise ValueError(
                        '이전보다 작은 누적 체결 응답은 반영하지 않았습니다. 다시 상태 조회하세요.'
                    )
            if (
                previous['status'] in {'filled', 'cancelled', 'completed'}
                and changes.get('status', previous['status']) != previous['status']
            ):
                raise ValueError('이미 종결된 주문을 미체결 상태로 되돌릴 수 없습니다.')
            report = dict(previous, **changes, updated_ms=int(time.time() * 1000))
            con.execute(
                'UPDATE orders SET report=? WHERE key_id=? AND client_id=?',
                (json.dumps(report), key_id, client_id),
            )
        return report
