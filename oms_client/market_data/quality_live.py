"""Read existing latest records for clock-offset monitoring without new feeds."""

from __future__ import annotations

import math
import os
import time
from urllib.parse import urlsplit

import duckdb
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from . import parquet, quality
from .validation import DataError, IDENTIFIER

MIN_TIMESTAMP_MS = 946684800000  # 2000-01-01 UTC
MAX_TIMESTAMP_MS = 4102444800000  # 2100-01-01 UTC
LIVE_KEYS = {'data_kind', 'exchanges', 'ticker', 'threshold_ms'}


def _timestamp(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return (
            parsed
            if math.isfinite(parsed) and MIN_TIMESTAMP_MS <= parsed < MAX_TIMESTAMP_MS
            else None
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _selection(payload):
    if not isinstance(payload, dict) or set(payload) - LIVE_KEYS:
        raise DataError('시간차 모니터의 조회 조건을 확인해 주세요.')
    kind = payload.get('data_kind', 'ob')
    if not isinstance(kind, str) or kind not in {'ob', 'tick', 'funding', 'ob_1s'}:
        raise DataError('시간차를 확인할 호가·체결·펀딩 데이터를 선택해 주세요.')
    available = quality.catalog()['exchanges'].get(kind, {})
    exchanges = payload.get('exchanges')
    if exchanges is None:
        exchanges = list(available)[:8]
    if (
        not isinstance(exchanges, list)
        or not 1 <= len(exchanges) <= 8
        or any(
            not isinstance(exchange, str) or not IDENTIFIER.fullmatch(exchange)
            for exchange in exchanges
        )
        or len(set(exchanges)) != len(exchanges)
    ):
        raise DataError('거래소는 중복 없이 1~8개 선택해 주세요.')
    ticker = payload.get('ticker', '')
    ticker = '' if ticker is None else ticker
    if (
        not isinstance(ticker, str)
        or len(ticker) > 80
        or (ticker.strip() and not IDENTIFIER.fullmatch(ticker.strip()))
    ):
        raise DataError('조회할 종목 이름을 확인해 주세요.')
    raw_threshold = payload.get('threshold_ms', 1000)
    if isinstance(raw_threshold, bool):
        raise DataError('시간차 임계값은 0 이상의 유한한 밀리초로 입력해 주세요.')
    try:
        threshold = float(raw_threshold)
        if not math.isfinite(threshold) or not 0 <= threshold <= 86400000:
            raise ValueError('range')
    except (TypeError, ValueError, OverflowError):
        raise DataError('시간차 임계값은 0~86,400,000ms 범위로 입력해 주세요.') from None
    selected = []
    for exchange in exchanges:
        symbols = available.get(exchange, [])
        lookup = {symbol.upper(): symbol for symbol in symbols}
        selected_ticker = (
            lookup.get(ticker.strip().upper())
            if ticker.strip()
            else lookup.get('BTC', symbols[0] if symbols else None)
        )
        selected.append((exchange, selected_ticker or ticker.strip() or 'BTC'))
    return kind, threshold, available, selected


def _redis_client():
    url = os.environ.get('DASHBOARD_REDIS_URL', 'redis://127.0.0.1:6379/0')
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {'redis', 'rediss'}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError('Redis URL')
        return redis.Redis.from_url(
            url,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=Retry(NoBackoff(), 0),
            max_connections=2,
            health_check_interval=0,
        )
    except Exception:
        raise DataError('현재 캐시 연결 설정을 확인하지 못했습니다.') from None


def _latest_parquet(exchange, ticker):
    source = parquet._resolve_source(exchange, ticker)
    if source['source_kind'] != 'orderbook_1s':
        raise DataError('선택한 1초 호가 저장소가 없습니다.')
    directory = source['directory']
    paths = sorted(
        path
        for path in directory.glob('*/*/*/*.parquet')
        if path.is_file() and parquet.within_root(path, directory)
    )
    if not paths:
        return None
    connection = parquet.connection()
    try:
        with parquet.query_deadline(connection):
            columns = {
                row[0]
                for row in connection.execute(
                    'DESCRIBE SELECT * FROM read_parquet(?)', [str(paths[-1])]
                ).fetchall()
            }
            received = (
                'try_cast(receivedTime AS DOUBLE)' if 'receivedTime' in columns else 'NULL::DOUBLE'
            )
            row = connection.execute(
                f'SELECT try_cast(desktopTime AS DOUBLE), try_cast(serverTime AS DOUBLE), {received} '
                'FROM read_parquet(?) ORDER BY desktopTime DESC LIMIT 1',
                [str(paths[-1])],
            ).fetchone()
            return row
    except duckdb.Error:
        raise DataError('최근 1초 호가의 시각을 읽지 못했습니다.') from None
    finally:
        connection.close()


def query_live(payload, *, redis_factory=None, clock=time.time):
    kind, threshold, available, selected = _selection(payload)
    series, client, connection_failed = [], None, False
    try:
        for exchange, ticker in selected:
            source = 'parquet' if kind == 'ob_1s' else 'redis'
            item = {
                'exchange': exchange,
                'ticker': ticker,
                'status': 'no_data',
                'source': source,
                'desktop_ms': None,
                'server_ms': None,
                'latency_ms': None,
                'age_ms': None,
                'above_threshold': False,
                'negative': False,
            }
            series.append(item)
            unsupported = quality.unmeasurable_clock_reason(exchange, kind)
            if unsupported:
                item['error'] = unsupported
                continue
            if ticker not in available.get(exchange, []):
                item['error'] = '선택한 거래소·종목의 해당 데이터가 없습니다.'
                continue
            try:
                if kind == 'ob_1s':
                    row = _latest_parquet(exchange, ticker)
                else:
                    if connection_failed:
                        item.update(status='error', error='현재 수집 캐시에 연결하지 못했습니다.')
                        continue
                    if client is None:
                        client = (redis_factory or _redis_client)()
                    raw = client.hget(f'latest:{exchange}:{kind}', ticker)
                    if raw is None:
                        item['error'] = '아직 수집된 최신 기록이 없습니다.'
                        continue
                    if not isinstance(raw, str) or len(raw) > 65536:
                        raise DataError('최신 기록의 형식을 확인하지 못했습니다.')
                    fields = raw.split(',')
                    expected = 22 if kind == 'ob' else 4
                    if len(fields) != expected:
                        raise DataError('최신 기록의 시각 컬럼 형식이 기존 저장 형식과 다릅니다.')
                    # Canonical producer/archive contract is desktop, server.
                    # The legacy columns:* cache can contain older layouts.
                    row = (fields[0], fields[1], None)
                if row is None:
                    item['error'] = '아직 기록된 시각이 없습니다.'
                    continue
                desktop, server = _timestamp(row[0]), _timestamp(row[1])
                if desktop is None or server is None:
                    item.update(
                        status='error',
                        error='시각이 없거나 UTC epoch 밀리초 형식이 올바르지 않습니다.',
                    )
                    continue
                latency = desktop - server
                item.update(
                    status='ok',
                    desktop_ms=desktop,
                    server_ms=server,
                    latency_ms=latency,
                    above_threshold=latency > threshold,
                    negative=latency < 0,
                )
                received = _timestamp(row[2])
                if received is not None:
                    item['received_latency_ms'] = received - server
            except (redis.ConnectionError, redis.TimeoutError):
                connection_failed = True
                item.update(status='error', error='현재 수집 캐시에 연결하지 못했습니다.')
            except DataError as error:
                item.update(status='error', error=str(error))
            except Exception:
                item.update(status='error', error='현재 기록의 시각을 읽지 못했습니다.')
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    # A feed can advance while this request reads the other exchanges. Compare
    # against the observation end so fresh records are not called future clocks.
    observed = int(clock() * 1000)
    for item in series:
        if item['status'] == 'ok':
            item.update(
                age_ms=observed - item['desktop_ms'], future_desktop=item['desktop_ms'] > observed
            )
    warnings = [
        'desktopTime은 상위 수집 PC의 기록 시각입니다. 차이에는 시계 오차·수집 처리 시간이 포함되며 네트워크 왕복 RTT가 아닙니다.'
    ]
    if kind == 'ob_1s':
        warnings.append(
            '1초 호가는 약 5초마다 반영되는 최근 저장 스냅샷입니다. 채택 시각 차이에는 호가 나이가 포함되며 수신 시각 기준 차이를 함께 제공합니다. 10초보다 오래된 호가는 저장하지 않으므로 누락 구간도 확인하세요.'
        )
    return {
        'mode': 'live',
        'observed_at_ms': observed,
        'data_kind': kind,
        'threshold_ms': threshold,
        'series': series,
        'meta': {'definition': 'desktopTime - serverTime', 'unit': 'ms', 'warnings': warnings},
    }
