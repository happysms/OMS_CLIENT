"""Latest-offset monitoring reads existing records and preserves clock anomalies."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import redis

from oms_client.market_data import quality_live
from oms_client.market_data.validation import DataError

NOW = 1792911600000
CATALOG = {'exchanges': {
    'ob': {'bithumb': ['BTC', 'ETH'], 'upbit': ['BTC'], 'ls': ['005930']},
    'tick': {'bithumb': ['BTC'], 'upbit': ['BTC']},
    'funding': {'binance': ['BTC'], 'okx': ['BTC'], 'hyperliquid': ['xyz:AAOI']},
    'ob_1s': {'bithumb': ['USDT'], 'upbit': ['USDT']},
}}


class FakeRedis:
    def __init__(self, records=None, error=None):
        self.records = records or {}
        self.error = error
        self.calls = []
        self.closed = False

    def hget(self, key, ticker):
        self.calls.append((key, ticker))
        if self.error:
            raise self.error
        return self.records.get((key, ticker))

    def close(self):
        self.closed = True


class LiveQualityTests(unittest.TestCase):
    def setUp(self):
        self.catalog = patch.object(quality_live.quality, 'catalog', return_value=CATALOG)
        self.catalog.start()
        self.addCleanup(self.catalog.stop)

    def run_query(self, payload, client):
        return quality_live.query_live(payload, redis_factory=lambda: client, clock=lambda: NOW / 1000)

    def test_signed_offsets_threshold_and_independent_record_age_use_only_hget(self):
        records = {('latest:bithumb:ob', 'BTC'): ','.join(map(str, [NOW-30, NOW-130, *([0] * 20)])),
                   ('latest:upbit:ob', 'BTC'): ','.join(map(str, [NOW-20, NOW+5, *([0] * 20)]))}
        client = FakeRedis(records)
        result = self.run_query({'exchanges': ['bithumb', 'upbit'], 'threshold_ms': 50}, client)
        first, second = result['series']
        self.assertEqual(first['latency_ms'], 100)
        self.assertEqual(first['age_ms'], 30)
        self.assertTrue(first['above_threshold'])
        self.assertEqual(second['latency_ms'], -25)
        self.assertTrue(second['negative'])
        self.assertEqual(client.calls, [('latest:bithumb:ob', 'BTC'), ('latest:upbit:ob', 'BTC')])
        self.assertTrue(client.closed)
        json.dumps(result, allow_nan=False)

    def test_trade_format_is_not_guessed_from_legacy_columns_cache(self):
        client = FakeRedis({('latest:bithumb:tick', 'BTC'): f'{NOW},{NOW-12},100,-0.001'})
        item = self.run_query({'data_kind': 'tick', 'exchanges': ['bithumb'], 'ticker': 'btc'}, client)['series'][0]
        self.assertEqual(item['latency_ms'], 12)
        self.assertEqual(item['ticker'], 'BTC')
        self.assertEqual(client.calls, [('latest:bithumb:tick', 'BTC')])

    def test_synthetic_server_clocks_are_unmeasurable_without_reading_redis(self):
        client = FakeRedis()
        result = self.run_query({'data_kind': 'funding', 'exchanges': ['okx', 'hyperliquid']}, client)
        self.assertTrue(all(item['status'] == 'no_data' and item['latency_ms'] is None for item in result['series']))
        self.assertTrue(all('생성' in item['error'] for item in result['series']))
        self.assertEqual(client.calls, [])
        ls = self.run_query({'data_kind': 'ob', 'exchanges': ['ls']}, client)['series'][0]
        self.assertEqual(ls['status'], 'no_data')
        self.assertEqual(client.calls, [])

    def test_missing_or_malformed_records_are_isolated_and_never_a_zero_offset(self):
        for value in [None, 'not,csv', f'{NOW},0,1,1', f'{NOW},NaN,1,1', f'{NOW},{NOW*1000},1,1', f'{NOW},1792911600,1,1']:
            with self.subTest(value=value):
                client = FakeRedis({('latest:bithumb:tick', 'BTC'): value,
                                    ('latest:upbit:tick', 'BTC'): f'{NOW},{NOW-20},100,1'})
                result = self.run_query({'data_kind': 'tick', 'exchanges': ['bithumb', 'upbit']}, client)
                self.assertIsNone(result['series'][0]['latency_ms'])
                self.assertEqual(result['series'][1]['latency_ms'], 20)

    def test_redis_outage_is_sanitized_and_not_retried_per_exchange(self):
        client = FakeRedis(error=redis.ConnectionError('redis://user:secret@private.example'))
        result = self.run_query({'exchanges': ['bithumb', 'upbit']}, client)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.closed)
        self.assertTrue(all(item['status'] == 'error' for item in result['series']))
        self.assertNotIn('secret', json.dumps(result))

    def test_future_collector_clock_is_preserved_and_flagged(self):
        client = FakeRedis({('latest:bithumb:tick', 'BTC'): f'{NOW+50},{NOW},100,1'})
        item = self.run_query({'data_kind': 'tick', 'exchanges': ['bithumb']}, client)['series'][0]
        self.assertEqual(item['age_ms'], -50)
        self.assertTrue(item['future_desktop'])
        self.assertEqual(item['latency_ms'], 50)

    def test_observation_clock_is_read_after_feeds_to_avoid_false_future_warning(self):
        client = FakeRedis({('latest:bithumb:tick', 'BTC'): f'{NOW+10},{NOW},100,1'})

        def observed_clock():
            self.assertTrue(client.closed)
            return (NOW + 20) / 1000

        result = quality_live.query_live({'data_kind': 'tick', 'exchanges': ['bithumb']},
                                         redis_factory=lambda: client, clock=observed_clock)
        self.assertEqual(result['observed_at_ms'], NOW + 20)
        self.assertEqual(result['series'][0]['age_ms'], 10)
        self.assertFalse(result['series'][0]['future_desktop'])

    def test_native_snapshot_and_receive_offsets_are_distinct(self):
        with patch.object(quality_live, '_latest_parquet', return_value=(NOW-100, NOW-1100, NOW-1050)):
            result = self.run_query({'data_kind': 'ob_1s', 'exchanges': ['bithumb']}, FakeRedis())
        item = result['series'][0]
        self.assertEqual(item['source'], 'parquet')
        self.assertEqual(item['latency_ms'], 1000)
        self.assertEqual(item['received_latency_ms'], 50)
        self.assertIn('10초', ' '.join(result['meta']['warnings']))

    def test_latest_native_row_is_read_without_reverting_to_previous_valid_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'bithumb/ob_1s/USDT/2026/10/25/07.parquet'
            path.parent.mkdir(parents=True)
            pq.write_table(pa.table({'desktopTime': [NOW-100, NOW], 'serverTime': [NOW-150, None],
                                     'receivedTime': [NOW-120, NOW-5]}), path)
            with patch.dict(os.environ, {'DASHBOARD_DATA_ROOT': str(root / 'legacy'), 'DASHBOARD_COLLECTOR_DATA_ROOT': str(root)}):
                item = self.run_query({'data_kind': 'ob_1s', 'exchanges': ['bithumb']}, FakeRedis())['series'][0]
            self.assertEqual(item['status'], 'error')
            self.assertIsNone(item['latency_ms'])

    def test_bad_inputs_and_redis_endpoint_overrides_are_rejected(self):
        invalid = [None, [], {'redis_url': 'redis://elsewhere'}, {'data_kind': 'candle'}, {'exchanges': []},
                   {'exchanges': ['bithumb', 'bithumb']}, {'exchanges': ['../bithumb']}, {'ticker': False},
                   {'ticker': '../../BTC'}, {'threshold_ms': -1}, {'threshold_ms': True}, {'threshold_ms': float('inf')}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(DataError):
                self.run_query(payload, FakeRedis())

    def test_redis_connection_has_bounded_timeouts_without_secret_errors(self):
        with patch.dict(os.environ, {'DASHBOARD_REDIS_URL': 'redis://user:secret@127.0.0.1:6379/0'}), \
             patch.object(redis.Redis, 'from_url', return_value=FakeRedis()) as factory:
            quality_live._redis_client()
            self.assertEqual(factory.call_args.kwargs['socket_timeout'], 1)
            self.assertEqual(factory.call_args.kwargs['socket_connect_timeout'], 1)
            self.assertEqual(factory.call_args.kwargs['protocol'], 2)
        for url in ['file:///etc/passwd', 'redis://localhost?socket_timeout=999']:
            with patch.dict(os.environ, {'DASHBOARD_REDIS_URL': url}), self.assertRaises(DataError) as caught:
                quality_live._redis_client()
            self.assertNotIn(url, str(caught.exception))
