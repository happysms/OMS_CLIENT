"""Bounded raw streams, retaining equal rows and physical row tie order."""

from contextlib import ExitStack
import hashlib
import heapq
import math
from pathlib import Path

import pandas as pd

from ..market_data import parquet, ticks
from ..market_data.validation import DataError
from .fx import AsOfFxQuotes

MAX_EVENTS = 5_000_000


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class ReplaySource:
    def __init__(self, config):
        self.config = config
        self.warmup_ms = max(
            5000, config.get('strategy', {}).get('parameters', {}).get('max_quote_age_ms', 1000)
        )
        self.stack = ExitStack()
        self.manifest = []
        self.total = 0
        self.skipped = 0
        self.counts = {}
        self.fx_quotes = AsOfFxQuotes()

    def fx_at(self, now):
        return self.fx_quotes.at(now)

    def _load_fx(self, end):
        # Independent reference stream: never changes market event counts/order.
        try:
            source = parquet._resolve_source('bithumb', 'USDT')
            start = pd.Timestamp(self.config['start_ms'] - 10000, unit='ms', tz='UTC')
            paths = parquet.files_for_range('bithumb', 'USDT', start, end)
            if not paths or len(paths) > 256:
                return
            reference = ReplaySource(dict(self.config, ticker='USDT'))
            reference.warmup_ms = 10000
            self.stack.callback(reference.close)
            events = reference._stream(
                'bithumb', 'book', paths, source['source_kind'] == 'orderbook_1s', paths
            )
            for name in paths:
                info = Path(name).stat()
                if not any(item['path'] == name for item in self.manifest):
                    self.manifest.append(
                        dict(path=name, size=info.st_size, mtime_ns=info.st_mtime_ns)
                    )
            self.fx_quotes = AsOfFxQuotes(events)
        except Exception:
            self.fx_quotes = AsOfFxQuotes()
            self.fx_quotes.error = '과거 USDT/KRW 호가가 없어 USDT 손익을 평가할 수 없습니다.'

    def _stream(self, venue, kind, paths, native, originals, max_events=MAX_EVENTS):
        con = self.stack.enter_context(parquet.connection())
        columns = {
            r[0]
            for r in con.execute(
                'DESCRIBE SELECT * FROM read_parquet(?,union_by_name=true)', [paths]
            ).fetchall()
        }
        required = (
            {'desktopTime', 'price', 'qty'} if kind == 'tick' else {'desktopTime', 'b1_p', 'a1_p'}
        )
        if not required <= columns:
            raise DataError(f'{venue} {kind} 원본 필수 컬럼이 없습니다.')
        fields = ['desktopTime', 'serverTime']
        if kind == 'tick':
            fields += ['price', 'qty']
        else:
            fields += [
                name
                for i in range(1, 6)
                for name in (
                    f'b{i}_p',
                    f'a{i}_p',
                    ('bid_size' if i == 1 else f'bid_size_{i}') if native else f'b{i}_q',
                    ('ask_size' if i == 1 else f'ask_size_{i}') if native else f'a{i}_q',
                )
            ]
        selected = ','.join(
            (
                f'try_cast("{name}" AS DOUBLE) AS "{name}"'
                if name in columns
                else f'NULL::DOUBLE AS "{name}"'
            )
            for name in fields
        )
        params = dict(
            paths=paths, start=self.config['start_ms'] - self.warmup_ms, end=self.config['end_ms']
        )
        where = 'try_cast(desktopTime AS BIGINT)>=$start AND try_cast(desktopTime AS BIGINT)<$end'
        with parquet.query_deadline(con):
            count = con.execute(
                f'SELECT count(*) FROM read_parquet($paths,union_by_name=true) WHERE {where}',
                params,
            ).fetchone()[0]
        self.total += count
        self.counts[f'{venue}:{kind}'] = count
        if self.total > max_events:
            raise DataError(
                f'원본 이벤트 {max_events:,}개 조회 한도를 넘었습니다. 구간의 원본을 모두 표시할 수 없습니다.'
            )
        with parquet.query_deadline(con):
            con.execute(
                f'SELECT {selected},filename,file_row_number FROM read_parquet($paths,union_by_name=true,filename=true,file_row_number=true) WHERE {where} ORDER BY desktopTime,list_position($paths,filename),file_row_number',
                params,
            )
        ranks = {name: i for i, name in enumerate(paths)}
        source_prefixes = {
            path: f'{venue}:{kind}:{hashlib.sha256(originals[index].encode()).hexdigest()[:12]}:'
            for path, index in ranks.items()
        }
        # Column layout is fixed by SELECT, even when a source column is missing.
        # Resolve it once per stream instead of formatting names for every row.
        side_columns = (
            ('bids', tuple((2 + 4 * i, 4 + 4 * i) for i in range(5))),
            ('asks', tuple((3 + 4 * i, 5 + 4 * i) for i in range(5))),
        )

        def rows():
            while batch := con.fetchmany(2048):
                for row in batch:
                    stamp = row[0]
                    if not _finite(stamp):
                        self.skipped += 1
                        continue
                    event = dict(
                        time=int(stamp),
                        venue=venue,
                        kind=kind,
                        server_time=row[1] if _finite(row[1]) else None,
                        source_id=source_prefixes[row[-2]] + str(row[-1]),
                    )
                    if kind == 'tick':
                        price, qty = row[2], row[3]
                        if not _finite(price) or price <= 0 or not _finite(qty) or qty == 0:
                            self.skipped += 1
                            continue
                        event.update(price=price, qty=qty, direction_known=venue == 'bithumb')
                    else:
                        for side, indices in side_columns:
                            levels = []
                            for price_index, quantity_index in indices:
                                price, qty = row[price_index], row[quantity_index]
                                if not _finite(price) or price <= 0:
                                    break
                                if levels and not (
                                    price < levels[-1]['price']
                                    if side == 'bids'
                                    else price > levels[-1]['price']
                                ):
                                    break
                                quantity = (
                                    (qty if native else qty / price)
                                    if _finite(qty) and qty >= 0
                                    else None
                                )
                                levels.append(dict(price=price, quantity=quantity))
                            event[side] = levels
                        event['quantity_basis'] = (
                            'native_base' if native else 'base_from_stored_notional'
                        )
                        if (
                            not event['bids']
                            or not event['asks']
                            or event['bids'][0]['price'] >= event['asks'][0]['price']
                        ):
                            self.skipped += 1
                            continue
                    event['_key'] = (
                        event['time'],
                        0 if kind == 'tick' else 1,
                        venue,
                        ranks[row[-2]],
                        row[-1],
                    )
                    yield event

        return rows()

    def open_context_stream(self, venue, kind, max_events=100000):
        """Read one bounded market stream for inspection, without account or FX work."""
        if venue not in self.config['venues'] or kind not in {'book', 'tick'}:
            raise DataError('체결 상세 데이터 종류를 확인하세요.')
        provider = parquet if kind == 'book' else ticks
        _, ticker = provider.validate_symbol(venue, self.config['ticker'])
        native = (
            kind == 'book'
            and parquet._resolve_source(venue, ticker)['source_kind'] == 'orderbook_1s'
        )
        start = pd.Timestamp(self.config['start_ms'] - self.warmup_ms, unit='ms', tz='UTC')
        end = pd.Timestamp(self.config['end_ms'], unit='ms', tz='UTC')
        paths = provider.files_for_range(venue, ticker, start, end)
        if not paths:
            raise DataError('이 구간의 원본 파일이 없습니다.')
        if len(paths) > 8:
            raise DataError('상세 조회 원본 파일 수 한도를 넘었습니다.')
        for name in paths:
            stat = Path(name).stat()
            self.manifest.append(dict(path=name, size=stat.st_size, mtime_ns=stat.st_mtime_ns))
        readable = self.stack.enter_context(ticks._materialize(paths)) if kind == 'tick' else paths
        return self._stream(venue, kind, readable, native, paths, max_events)

    def load(self):
        config = self.config
        start = pd.Timestamp(config['start_ms'] - self.warmup_ms, unit='ms', tz='UTC')
        end = pd.Timestamp(config['end_ms'], unit='ms', tz='UTC')
        streams = []
        for venue in config['venues']:
            _, ticker = parquet.validate_symbol(venue, config['ticker'])
            source = parquet._resolve_source(venue, ticker)
            for kind in ('book', 'tick'):
                if kind == 'book':
                    paths = parquet.files_for_range(venue, ticker, start, end)
                else:
                    try:
                        paths = ticks.files_for_range(venue, ticker, start, end)
                    except DataError:
                        paths = []
                if not paths:
                    if venue == 'bithumb' or kind == 'book':
                        raise DataError(f'{venue} {ticker}의 {kind} 원본이 선택 기간에 없습니다.')
                    continue
                if len(paths) > 256:
                    raise DataError('원본 파일이 너무 많습니다. 기간을 줄여 주세요.')
                for name in paths:
                    stat = Path(name).stat()
                    self.manifest.append(
                        dict(path=name, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
                    )
                readable = (
                    self.stack.enter_context(ticks._materialize(paths)) if kind == 'tick' else paths
                )
                streams.append(
                    self._stream(
                        venue, kind, readable, source['source_kind'] == 'orderbook_1s', paths
                    )
                )
        if not self.counts.get('bithumb:book') or not self.counts.get('bithumb:tick'):
            raise DataError('빗썸 원본 호가와 틱이 모두 필요합니다.')
        self._load_fx(end)
        return heapq.merge(*streams, key=lambda event: event['_key'])

    def verify(self):
        for item in self.manifest:
            stat = Path(item['path']).stat()
            if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime_ns']):
                raise DataError(
                    '실행 중 원본 파일이 변경됐습니다. 이 실행은 완료 결과로 사용할 수 없습니다.'
                )

    def close(self):
        self.stack.close()
