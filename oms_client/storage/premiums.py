"""Durable hourly premium sufficient statistics, separate from result caches."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time

HOUR = 3_600_000
PROFILE = 'bithumb_okx_usdt_mid_last_1s_v1'
FORMULA = '(bithumb_mid / (okx_mid * bithumb_usdt_mid) - 1) * 100'


class PremiumStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.directory / 'premiums.sqlite3'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(
                '''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hours (
                    profile TEXT NOT NULL, ticker TEXT NOT NULL, hour_ms INTEGER NOT NULL,
                    total REAL NOT NULL, samples INTEGER NOT NULL, minimum REAL, maximum REAL,
                    fingerprint TEXT NOT NULL, error TEXT, updated_ms INTEGER NOT NULL,
                    PRIMARY KEY(profile,ticker,hour_ms)
                );
                CREATE INDEX IF NOT EXISTS hours_window ON hours(profile,hour_ms);
            '''
            )
        os.chmod(self.database, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set(self, key, value):
        with self.connection() as db:
            db.execute(
                'INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (key, json.dumps(value, ensure_ascii=False, allow_nan=False)),
            )

    def existing(self, ticker, start, end):
        with self.connection() as db:
            rows = db.execute(
                'SELECT * FROM hours WHERE profile=? AND ticker=? AND hour_ms>=? AND hour_ms<?',
                (PROFILE, ticker, start, end),
            ).fetchall()
        return {row['hour_ms']: dict(row) for row in rows}

    def save(self, ticker, rows):
        """Atomic replacement per batch: rerunning never adds counts twice."""
        with self.connection() as db:
            for row in rows:
                db.execute(
                    '''INSERT INTO hours VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(profile,ticker,hour_ms) DO UPDATE SET
                    total=excluded.total,samples=excluded.samples,minimum=excluded.minimum,
                    maximum=excluded.maximum,fingerprint=excluded.fingerprint,error=NULL,updated_ms=excluded.updated_ms''',
                    (
                        PROFILE,
                        ticker,
                        row['hour_ms'],
                        row['total'],
                        row['samples'],
                        row['minimum'],
                        row['maximum'],
                        row['fingerprint'],
                        None,
                        int(time.time() * 1000),
                    ),
                )

    def fail(self, ticker, hours, error):
        with self.connection() as db:
            for hour in hours:
                # Retain the previous result for diagnosis; exclude it from rollups
                # until the failed source can be read and recomputed successfully.
                db.execute(
                    '''INSERT INTO hours VALUES (?,?,?,0,0,NULL,NULL,'',?,?)
                    ON CONFLICT(profile,ticker,hour_ms) DO UPDATE SET error=excluded.error''',
                    (PROFILE, ticker, hour, error[:300], int(time.time() * 1000)),
                )

    def summary(self, end, tickers):
        with self.connection() as db:
            rows = db.execute(
                '''SELECT ticker,
                sum(CASE WHEN hour_ms>=? AND error IS NULL THEN total ELSE 0 END) total3,
                sum(CASE WHEN hour_ms>=? AND error IS NULL THEN samples ELSE 0 END) count3,
                sum(CASE WHEN error IS NULL THEN total ELSE 0 END) total7,
                sum(CASE WHEN error IS NULL THEN samples ELSE 0 END) count7,
                sum(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) errors,
                max(CASE WHEN error IS NULL AND samples>0 THEN hour_ms END) latest_hour_ms
                FROM hours WHERE profile=? AND hour_ms>=? AND hour_ms<? GROUP BY ticker''',
                (end - 72 * HOUR, end - 72 * HOUR, PROFILE, end - 168 * HOUR, end),
            ).fetchall()
        lookup = {row['ticker']: dict(row) for row in rows}
        items = []
        for ticker in tickers:
            row = lookup.get(ticker, {})
            windows = {}
            for days in (3, 7):
                count = row.get(f'count{days}', 0)
                expected = days * 86400
                windows[str(days)] = dict(
                    mean_pct=row[f'total{days}'] / count if count else None,
                    samples=count,
                    expected_samples=expected,
                    coverage_pct=count / expected * 100,
                    complete=count == expected,
                )
            items.append(
                dict(
                    ticker=ticker,
                    windows=windows,
                    error_hours=row.get('errors', 0),
                    latest_hour_ms=row.get('latest_hour_ms'),
                )
            )
        return dict(
            profile=PROFILE,
            formula=FORMULA,
            end_ms=end,
            sample_interval_ms=1000,
            windows_days=[3, 7],
            items=items,
        )

    def hourly(self, ticker, end, days=7):
        rows = self.existing(ticker, end - days * 24 * HOUR, end)
        items = []
        for hour in range(end - days * 24 * HOUR, end, HOUR):
            row = rows.get(hour)
            samples = row['samples'] if row and not row['error'] else 0
            items.append(
                dict(
                    hour_ms=hour,
                    mean_pct=row['total'] / samples if samples else None,
                    samples=samples,
                    coverage_pct=samples / 36,
                    minimum=row['minimum'] if samples else None,
                    maximum=row['maximum'] if samples else None,
                    status=(
                        'error'
                        if row and row['error']
                        else (
                            'complete'
                            if samples == 3600
                            else 'partial' if samples else 'empty' if row else 'pending'
                        )
                    ),
                    error=row['error'] if row else None,
                )
            )
        return dict(ticker=ticker, profile=PROFILE, end_ms=end, items=items)
