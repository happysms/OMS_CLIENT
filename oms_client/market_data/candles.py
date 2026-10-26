"""Read completed REST minute candles as a separate historical price source.

A candle's close becomes available at its end, never at its opening timestamp.
Order-book prices and sizes cannot be inferred from OHLCV records.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from . import parquet
from .validation import DataError, iso_timestamp

MINUTE_MS = 60_000
FIELDS = ('open', 'high', 'low', 'close', 'volume', 'trade_value')


def _directory(exchange):
    return parquet.collector_data_root() / exchange / 'candles_1m' / 'USDT'


def _files(exchange):
    directory = _directory(exchange)
    root = parquet.collector_data_root()
    if not parquet.within_root(directory, root):
        return []
    return sorted(
        path
        for path in directory.glob('*/*/*.parquet')
        if path.is_file() and parquet.within_root(path, directory)
    )


def list_symbols():
    return {exchange: ['USDT'] for exchange in ('bithumb', 'upbit') if _files(exchange)}


def validate_symbol(exchange, ticker, available=None):
    available = list_symbols() if available is None else available
    if (
        isinstance(exchange, str)
        and isinstance(ticker, str)
        and exchange in available
        and ticker.strip().upper() in available[exchange]
    ):
        return exchange, ticker.strip().upper()
    raise DataError('선택한 거래소·종목의 1분봉 데이터가 없습니다.')


def bounds(exchange, ticker):
    exchange, ticker = validate_symbol(exchange, ticker)
    con = parquet.connection()
    try:
        with parquet.query_deadline(con):
            start, end = con.execute(
                'SELECT min(availableTime), max(availableTime) FROM read_parquet(?, union_by_name=true)',
                [[str(path) for path in _files(exchange)]],
            ).fetchone()
    except duckdb.Error as error:
        raise DataError('1분봉 파일의 시간 범위를 읽지 못했습니다.') from error
    finally:
        con.close()
    if start is None or end is None:
        raise DataError('저장된 1분봉이 없습니다.')
    return {
        'exchange': exchange,
        'ticker': ticker,
        'data_type': 'candle',
        'start': iso_timestamp(int(start)),
        'end': iso_timestamp(int(end)),
        'time_basis': 'candle_end',
        'candle_interval_ms': MINUTE_MS,
    }


def files_for_range(exchange, ticker, start, end):
    exchange, ticker = validate_symbol(exchange, ticker)
    # Files are partitioned by candle open, but filters use close availability.
    first = (start - pd.Timedelta(minutes=1)).floor('D')
    last = (end - pd.Timedelta(minutes=1, nanoseconds=1)).floor('D')
    parent = _directory(exchange)
    result = []
    for day in pd.date_range(first, last, freq='D'):
        path = parent / day.strftime('%Y/%m/%d.parquet')
        if path.is_file() and parquet.within_root(path, parent):
            result.append(str(path))
    return result


def read_buckets(exchange, ticker, start, end, step, *, price_mode='mean'):
    if step < MINUTE_MS:
        raise DataError(
            '1분봉은 1분 이상 간격으로 조회해 주세요. 1초 틱이나 호가 데이터가 아닙니다.'
        )
    if price_mode not in {'last', 'mean'}:
        raise DataError('조회 방식은 마지막 종가 또는 종가 평균으로 선택해 주세요.')
    paths = files_for_range(exchange, ticker, start, end)
    if not paths:
        raise DataError('선택한 기간에 저장된 1분봉이 없습니다.')
    finite = ' AND '.join(f'isfinite(try_cast("{field}" AS DOUBLE))' for field in FIELDS)
    fields = ', '.join(f'{field} := CAST("{field}" AS DOUBLE)' for field in FIELDS)
    projections = ', '.join(f'latest.{field} AS "{field}"' for field in FIELDS)
    value = 'latest.close' if price_mode == 'last' else 'mean_close'
    query = f'''
        WITH observations AS (
            SELECT *, row_number() OVER (PARTITION BY candleTime ORDER BY fetchedAt DESC) AS position
            FROM read_parquet($paths, union_by_name=true)
            WHERE availableTime >= $start AND availableTime < $end
              AND availableTime = candleTime + 60000 AND candleTime % 60000 = 0
              AND candle_interval_ms = 60000 AND market = 'KRW-USDT'
              AND price_unit = 'KRW' AND quantity_unit = 'USDT' AND source = 'rest_1m'
              AND {finite} AND "open" > 0 AND "close" > 0 AND "low" > 0
              AND "low" <= least("open", "close") AND "high" >= greatest("open", "close")
              AND volume >= 0 AND trade_value >= 0
        ), buckets AS (
            SELECT CAST(floor(availableTime / $step) AS BIGINT) * $step AS time,
                   avg(CAST("close" AS DOUBLE)) AS mean_close, count(*) AS source_rows,
                   arg_max(struct_pack({fields}, candle_time := CAST(candleTime AS BIGINT),
                                       available_time := CAST(availableTime AS BIGINT)), availableTime) AS latest
            FROM observations WHERE position = 1 GROUP BY time
        )
        SELECT time, {value} AS value, source_rows, {projections},
               latest.candle_time AS candle_time, latest.available_time AS available_time
        FROM buckets ORDER BY time
    '''
    con = parquet.connection()
    try:
        with parquet.query_deadline(con):
            frame = con.execute(
                query,
                {
                    'paths': paths,
                    'start': int(start.value // 1_000_000),
                    'end': int(end.value // 1_000_000),
                    'step': step,
                },
            ).df()
    except duckdb.Error as error:
        raise DataError('1분봉 파일을 읽지 못했습니다. 저장 스키마를 확인해 주세요.') from error
    finally:
        con.close()
    if frame.empty:
        raise DataError('선택한 기간에 유효한 완료 1분봉이 없습니다.')
    return frame.set_index('time'), int(frame['source_rows'].sum())
