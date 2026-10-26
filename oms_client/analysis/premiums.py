"""Incremental, hourly Bithumb/OKX premium aggregation from local orderbooks."""
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import threading
import time

import numpy as np
import pandas as pd

from ..market_data import parquet
from ..market_data.validation import DataError
from ..storage.premiums import HOUR, PROFILE, PremiumStore
from ..storage.access import DATA_ACCESS

DAY = 24 * HOUR
GRACE_MS = 120_000


def completed_end(now_ms):
    # Give hourly writers two minutes to finish the most recent file.
    return (int(now_ms) - GRACE_MS) // HOUR * HOUR


def common_symbols():
    catalog = parquet.list_symbols()
    names = {exchange: {ticker.upper(): ticker for ticker in catalog.get(exchange, [])}
             for exchange in ('bithumb', 'okx')}
    return sorted(names['bithumb'].keys() & names['okx'].keys()), names


def hourly_statistics(bithumb, okx, usdt, hours, fingerprints):
    """One equally weighted sample per common second; no forward-fill."""
    prices = pd.concat([bithumb.rename('b'), okx.rename('o'), usdt.rename('fx')], axis=1, join='inner')
    prices = prices[(prices > 0).all(axis=1) & np.isfinite(prices).all(axis=1)]
    premium = (prices.b / prices.o / prices.fx - 1) * 100
    premium = premium[np.isfinite(premium)]
    groups = {int(hour): group for hour, group in premium.groupby(premium.index // HOUR * HOUR)}
    rows = []
    for hour in hours:
        group = groups.get(hour)
        count = len(group) if group is not None else 0
        total = float(group.sum()) if count else 0.0
        if not np.isfinite(total):
            raise DataError('괴리 합계가 유효한 수치 범위를 벗어났습니다.')
        rows.append(dict(hour_ms=hour, samples=count, total=total,
                         minimum=float(group.min()) if count else None,
                         maximum=float(group.max()) if count else None,
                         fingerprint=fingerprints[hour]))
    return rows


def source_snapshot(exchange, ticker, start, end):
    if ticker is None:
        return {'directory': None, 'files': []}
    source = parquet._resolve_source(exchange, ticker)
    paths = parquet.files_for_range(exchange, ticker, pd.Timestamp(start, unit='ms', tz='UTC'),
                                   pd.Timestamp(end, unit='ms', tz='UTC'))
    files = []
    for name in paths:
        path = Path(name)
        stat = path.stat()
        hour = int(path.stem) if path.stem.isdigit() and 0 <= int(path.stem) <= 23 else None
        files.append([name, stat.st_size, stat.st_mtime_ns, hour])
    return {'directory': str(source['directory']), 'kind': source['source_kind'], 'files': files}


def hour_fingerprint(sources, hour):
    utc_hour = hour // HOUR % 24
    selected = [{**source, 'files': [row for row in source['files'] if row[3] in (None, utc_hour)]}
                for source in sources]
    digest = hashlib.sha256(json.dumps([PROFILE, selected], sort_keys=True).encode()).hexdigest()
    return digest, all(source['files'] for source in selected)


def read_prices(exchange, ticker, start, end):
    frame, _ = parquet.read_buckets(exchange, ticker, pd.Timestamp(start, unit='ms', tz='UTC'),
                                    pd.Timestamp(end, unit='ms', tz='UTC'), 1000, price_mode='last')
    return frame['mid']


class PremiumService:
    def __init__(self, directory, *, autostart=True, clock=time.time):
        self.store = PremiumStore(directory)
        self.clock = clock
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.guard = threading.Lock()
        self.manual = False
        self.thread = None
        if self.store.get('enabled') is None:
            self.store.set('enabled', True)
        if autostart:
            self.thread = threading.Thread(target=self._loop, daemon=True, name='premium-hourly')
            self.thread.start()

    def status(self):
        tickers, _ = common_symbols()
        end = completed_end(self.clock() * 1000)
        result = self.store.summary(end, tickers)
        job = self.store.get('job', {'state': 'idle'})
        if job.get('state') == 'running':
            with (self.store.directory / 'premiums.lock').open('a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    job = {**job, 'state': 'interrupted', 'error': '이전 집계가 중단됐습니다. 저장된 시간부터 다시 확인합니다.'}
                except BlockingIOError:
                    pass
        return {**result, 'enabled': self.store.get('enabled', True), 'job': job,
                'next_update_ms': end + HOUR + GRACE_MS,
                'reference': 'bithumb USDT mid', 'universe': 'stored_orderbook_symbol_intersection'}

    def request_refresh(self):
        with self.guard:
            self.manual = True
        self.wake.set()

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise DataError('자동 누적 설정은 true 또는 false로 지정해 주세요.')
        self.store.set('enabled', enabled)
        self.wake.set()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                with self.guard:
                    manual, self.manual = self.manual, False
                if manual or self.store.get('enabled', True):
                    self.refresh(automatic=not manual)
            except Exception:
                logging.getLogger(__name__).exception('Premium aggregation failed')
            self.wake.wait(30)
            self.wake.clear()

    @DATA_ACCESS.protect
    def refresh(self, *, automatic=False):
        end = completed_end(self.clock() * 1000)
        with (self.store.directory / 'premiums.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            if (automatic and self.store.get('last_attempt_end') == end
                    and self.store.get('job', {}).get('state') not in {'running', 'interrupted'}):
                return False
            # Persist before work so a bad source does not trigger a busy retry loop.
            self.store.set('last_attempt_end', end)
            tickers, names = common_symbols()
            start = end - 7 * DAY
            job = dict(state='running', end_ms=end, started_ms=int(self.clock() * 1000),
                       completed_units=0, total_units=0, written_hours=0, unchanged_hours=0,
                       retained_hours=0, error_hours=0, symbols=len(tickers))
            days = list(range(start // DAY * DAY, end, DAY))
            job['total_units'] = len(days) * len(tickers)
            self.store.set('job', job)
            try:
                for day in days:
                    lo, hi = max(start, day), min(end, day + DAY)
                    hours = list(range(lo, hi, HOUR))
                    fx_name = names['bithumb'].get('USDT')
                    fx_snapshot = source_snapshot('bithumb', fx_name, lo, hi)
                    fx_prices, fx_error = None, None
                    for ticker in tickers:
                        if self.stop_event.is_set():
                            job['state'] = 'interrupted'
                            return False
                        job['ticker'] = ticker
                        previous = self.store.existing(ticker, lo, hi)
                        affected = hours
                        try:
                            b_name, o_name = names['bithumb'][ticker], names['okx'][ticker]
                            b_snapshot = source_snapshot('bithumb', b_name, lo, hi)
                            o_snapshot = source_snapshot('okx', o_name, lo, hi)
                            fingerprints, pending, empty = {}, [], []
                            for hour in hours:
                                fingerprint, available = hour_fingerprint([b_snapshot, o_snapshot, fx_snapshot], hour)
                                fingerprints[hour] = fingerprint
                                old = previous.get(hour)
                                if old and old['fingerprint'] == fingerprint and not old['error']:
                                    job['unchanged_hours'] += 1
                                elif not available and old and old['samples'] > 0:
                                    # Raw retention is not a request to erase previously accumulated history.
                                    job['retained_hours'] += 1
                                elif not available:
                                    empty.append(hour)
                                else:
                                    pending.append(hour)
                            if empty:
                                blank = pd.Series(dtype=float, index=pd.Index([], dtype='int64'))
                                self.store.save(ticker, hourly_statistics(blank, blank, blank, empty, fingerprints))
                                job['written_hours'] += len(empty)
                            affected = pending
                            if pending:
                                if fx_error:
                                    raise DataError(fx_error)
                                if fx_prices is None:
                                    try:
                                        fx_prices = read_prices('bithumb', fx_name, lo, hi)
                                        if source_snapshot('bithumb', fx_name, lo, hi) != fx_snapshot:
                                            raise DataError('USDT 원본 파일이 집계 중 변경됐습니다. 다음 갱신에서 재시도합니다.')
                                    except Exception as exc:
                                        fx_error = str(exc)
                                        raise
                                lower, upper = min(pending), max(pending) + HOUR
                                b_prices = read_prices('bithumb', b_name, lower, upper)
                                o_prices = read_prices('okx', o_name, lower, upper)
                                if (source_snapshot('bithumb', b_name, lo, hi) != b_snapshot
                                        or source_snapshot('okx', o_name, lo, hi) != o_snapshot
                                        or source_snapshot('bithumb', fx_name, lo, hi) != fx_snapshot):
                                    raise DataError('원본 파일이 집계 중 변경됐습니다. 다음 갱신에서 재시도합니다.')
                                rows = hourly_statistics(b_prices, o_prices, fx_prices, pending, fingerprints)
                                self.store.save(ticker, rows)
                                job['written_hours'] += len(rows)
                        except Exception as exc:
                            message = str(exc) if isinstance(exc, (DataError, OSError)) else '시간별 괴리 계산 중 오류가 발생했습니다.'
                            if not isinstance(exc, (DataError, OSError)):
                                logging.getLogger(__name__).exception('Premium source failed: %s', ticker)
                            self.store.fail(ticker, affected, message)
                            job['error_hours'] += len(affected)
                            job['last_error'] = message
                        job['completed_units'] += 1
                        self.store.set('job', job)
                job['state'] = 'complete' if not job['error_hours'] else 'partial'
                if not tickers:
                    job['last_error'] = '저장된 빗썸·OKX 공통 호가 종목이 없습니다.'
                elif not names['bithumb'].get('USDT'):
                    job['last_error'] = '빗썸 USDT 호가 데이터가 없어 괴리를 계산할 수 없습니다.'
                return True
            except Exception as exc:
                job.update(state='failed', last_error=str(exc)[:300])
                raise
            finally:
                job['finished_ms'] = int(self.clock() * 1000)
                self.store.set('job', job)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
