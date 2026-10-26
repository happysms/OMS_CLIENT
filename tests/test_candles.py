"""Historical candle prices retain their own provenance and close availability."""

import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.history import analyze, catalog
from oms_client.api.exports import _write_csv
from oms_client.market_data import candles
from oms_client.market_data.validation import DataError


class CandleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(
            os.environ,
            {
                'DASHBOARD_DATA_ROOT': str(self.root / 'legacy'),
                'DASHBOARD_COLLECTOR_DATA_ROOT': str(self.root / 'market'),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = int(pd.Timestamp('2026-10-25T00:00:00Z').timestamp() * 1000)
        self.frame = pd.DataFrame(
            {
                'candleTime': [self.origin + minutes * 60_000 for minutes in [0, 1, 3]],
                'availableTime': [self.origin + minutes * 60_000 for minutes in [1, 2, 4]],
                'open': [1342.0, 1343.0, 1345.0],
                'high': [1344.0, 1345.0, 1347.0],
                'low': [1341.0, 1342.0, 1344.0],
                'close': [1343.0, 1344.0, 1346.0],
                'volume': [0.125, 12.5, 0.0],
                'trade_value': [167.875, 16800.0, 0.0],
                'fetchedAt': [self.origin + 3600_000] * 3,
                'market': ['KRW-USDT'] * 3,
                'price_unit': ['KRW'] * 3,
                'quantity_unit': ['USDT'] * 3,
                'candle_interval_ms': [60_000] * 3,
                'source': ['rest_1m'] * 3,
            }
        )
        self.write()

    def write(self, exchange='bithumb', frame=None):
        frame = self.frame if frame is None else frame
        for day, chunk in frame.groupby(
            pd.to_datetime(frame['candleTime'], unit='ms', utc=True).dt.strftime('%Y/%m/%d')
        ):
            path = self.root / 'market' / exchange / 'candles_1m/USDT' / f'{day}.parquet'
            path.parent.mkdir(parents=True, exist_ok=True)
            chunk.to_parquet(path, index=False)

    def query(self, **overrides):
        payload = {
            'legs': [{'id': 'A', 'exchange': 'bithumb', 'ticker': 'usdt', 'data_type': 'candle'}],
            'formula': 'A',
            'interval': '1min',
            'price_mode': 'last',
            'ewm_span': 1,
            'start': '2026-10-25T00:00:00Z',
            'end': '2026-10-25T00:05:00Z',
        }
        payload.update(overrides)
        return analyze(payload)

    def test_discovery_bounds_values_and_metadata_are_distinct_from_orderbooks(self):
        self.write('upbit')
        self.assertEqual(candles.list_symbols(), {'bithumb': ['USDT'], 'upbit': ['USDT']})
        self.assertEqual(catalog()['candle_exchanges'], candles.list_symbols())
        limits = candles.bounds('bithumb', 'usdt')
        self.assertEqual(limits['start'], '2026-10-25T09:01:00+09:00')
        self.assertEqual(limits['time_basis'], 'candle_end')
        result = self.query()
        leg = result['legs'][0]
        self.assertEqual(result['result'], [1343, 1344, 1346])
        self.assertEqual(result['times'], [self.origin + minutes * 60_000 for minutes in [1, 2, 4]])
        self.assertEqual(leg['volume'], [0.125, 12.5, 0])
        self.assertEqual(leg['quantity_unit'], 'USDT')
        self.assertEqual(leg['source_kind'], 'rest_1m')
        self.assertFalse(set(leg) & {'bid', 'ask', 'mid', 'bid_size', 'ask_size', 'quote_times'})
        self.assertIn('완성시각', ' '.join(result['meta']['warnings']))
        json.dumps(result, allow_nan=False)

    def test_prices_are_unavailable_until_candle_completion_and_end_is_exclusive(self):
        with self.assertRaises(DataError):
            self.query(end='2026-10-25T00:01:00Z')
        result = self.query(end='2026-10-25T00:01:00.001Z')
        self.assertEqual(result['result'], [1343])
        self.assertEqual(result['legs'][0]['available_times'], [self.origin + 60_000])

    def test_no_trade_minute_stays_missing_and_subminute_queries_are_rejected(self):
        result = self.query()
        self.assertNotIn(self.origin + 180_000, result['times'])
        for interval in ('1s', '10s'):
            with self.subTest(interval=interval), self.assertRaisesRegex(DataError, '1분'):
                self.query(interval=interval)

    def test_mean_uses_close_average_but_keeps_last_raw_candle_metadata(self):
        result = self.query(interval='5min', price_mode='mean')
        leg = result['legs'][0]
        self.assertAlmostEqual(leg['values'][0], (1343 + 1344 + 1346) / 3)
        self.assertEqual(leg['open'], [1345])
        self.assertEqual(leg['close'], [1346])
        self.assertEqual(leg['volume'], [0])
        self.assertEqual(leg['candle_times'], [self.origin + 180_000])
        self.assertEqual(leg['available_times'], [self.origin + 240_000])
        self.assertIn('마지막 원본', ' '.join(result['meta']['warnings']))

    def test_previous_utc_day_open_is_included_when_available_at_midnight(self):
        previous = self.frame.iloc[:1].copy()
        previous['candleTime'], previous['availableTime'] = self.origin - 60_000, self.origin
        self.write(frame=previous)
        result = self.query(end='2026-10-25T00:00:00.001Z')
        self.assertEqual(result['times'], [self.origin])
        self.assertEqual(result['legs'][0]['candle_times'], [self.origin - 60_000])

    def test_latest_fetched_duplicate_keeps_one_consistent_candle(self):
        duplicate = self.frame.iloc[:1].copy()
        duplicate['close'], duplicate['high'], duplicate['volume'], duplicate['fetchedAt'] = (
            1350.0,
            1351.0,
            4.5,
            self.origin + 7200_000,
        )
        self.write(frame=pd.concat([self.frame, duplicate]))
        result = self.query()
        self.assertEqual(result['meta']['rows_read'], 3)
        self.assertEqual(result['legs'][0]['values'][0], 1350)
        self.assertEqual(result['legs'][0]['high'][0], 1351)
        self.assertEqual(result['legs'][0]['volume'][0], 4.5)

    def test_invalid_units_clock_or_prices_are_excluded(self):
        for field, value in [
            ('price_unit', 'USDT'),
            ('quantity_unit', 'KRW'),
            ('source', 'tick'),
            ('availableTime', self.origin),
            ('close', float('nan')),
            ('volume', -1),
            ('high', 1),
            ('candle_interval_ms', 1000),
        ]:
            with self.subTest(field=field):
                frame = self.frame.copy()
                frame[field] = value
                self.write(frame=frame)
                with self.assertRaises(DataError):
                    self.query()

    def test_candle_candle_formulas_and_sampling_keep_full_arrays_aligned(self):
        other = self.frame.copy()
        other[['open', 'high', 'low', 'close']] += 2
        self.write('upbit', other)
        result = self.query(
            legs=[
                {'id': 'A', 'exchange': 'bithumb', 'ticker': 'USDT', 'data_type': 'candle'},
                {'id': 'B', 'exchange': 'upbit', 'ticker': 'USDT', 'data_type': 'candle'},
            ],
            formula='A - B',
            max_points=2,
            include_full_data=True,
        )
        self.assertEqual(result['result'], [-2, -2])
        self.assertEqual(result['full']['result'], [-2, -2, -2])
        self.assertEqual(result['legs'][0]['volume'], [0.125, 0])
        self.assertEqual(result['full']['legs'][0]['volume'], [0.125, 12.5, 0])

    def test_full_csv_labels_candle_value_last_metadata_and_mode_without_fake_quotes(self):
        for mode in ('last', 'mean'):
            with self.subTest(mode=mode):
                result = self.query(price_mode=mode, include_full_data=True, max_points=2)
                path = self.root / 'result.csv'
                _write_csv(path, result, 'KRW')
                rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0]['A_bithumb_USDT_candle_value_krw'], '1343.0')
                self.assertEqual(rows[0]['A_last_candle_volume_usdt'], '0.125')
                self.assertEqual(rows[0]['A_source_kind'], 'rest_1m')
                self.assertEqual(rows[0]['price_mode'], mode)
                self.assertNotIn('A_bid', rows[0])
                self.assertNotIn('A_ask', rows[0])

    def test_source_and_symlink_paths_cannot_escape_the_candle_store(self):
        for exchange, ticker in [
            ('../bithumb', 'USDT'),
            ('bithumb', '../../USDT'),
            ('bithumb', 'BTC'),
        ]:
            with self.subTest(exchange=exchange, ticker=ticker), self.assertRaises(DataError):
                candles.bounds(exchange, ticker)
        path = self.root / 'market/bithumb/candles_1m/USDT/2026/10/25.parquet'
        outside = self.root / 'outside.parquet'
        path.rename(outside)
        path.symlink_to(outside)
        self.assertEqual(candles.list_symbols(), {})
