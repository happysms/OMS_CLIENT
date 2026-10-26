"""Analysis clocks use paired source rows, not bucket or polling times."""
import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.history import analyze
from oms_client.analysis.live import _LiveSession
from oms_client.api.exports import _write_csv


class AnalysisTimestampTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, DASHBOARD_DATA_ROOT=str(self.root),
                                 DASHBOARD_COLLECTOR_DATA_ROOT=str(self.root / 'collector'))
        environment.start()
        self.addCleanup(environment.stop)
        self.origin = 1792886400000

    def write(self, offsets, servers=None, *, exchange='bithumb', kind='ob', bids=None, hour='00'):
        path = self.root / exchange / kind / 'BTC' / '2026/10/25' / f'{hour}.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {'desktopTime': [self.origin + offset for offset in offsets]}
        if servers is not None:
            data['serverTime'] = servers
        if kind == 'funding':
            data['fundingRate'] = [0.0001] * len(offsets)
        else:
            data['b1_p'] = bids or [100] * len(offsets)
            data['a1_p'] = [bid + 2 for bid in data['b1_p']]
        pd.DataFrame(data).to_parquet(path, index=False)

    def query(self, *, exchange='bithumb', kind='price', **changes):
        config = dict(legs=[dict(id='A', exchange=exchange, ticker='BTC', data_type=kind)],
                      formula='A', start='2026-10-25T00:00:00Z', end='2026-10-25T00:05:00Z',
                      interval='1min', price_mode='last', ewm_span=1)
        config.update(changes)
        return analyze({**config, 'include_full_data': True})

    def test_last_and_mean_use_source_pairs_and_keep_signed_values(self):
        self.write([1000, 59000, 61000],
                   [self.origin + 900, self.origin + 59020, self.origin + 61000])
        last = self.query()
        self.assertEqual(last['legs'][0]['timestamp_delta_ms'], [-20, 0])
        self.assertEqual(self.query(price_mode='mean')['legs'][0]['timestamp_delta_ms'], [40, 0])
        self.assertEqual(last['times'], [self.origin, self.origin + 60000])
        json.dumps(last, allow_nan=False)

    def test_invalid_last_is_not_replaced_by_earlier_clock_or_zero(self):
        self.write([1000, 59000, 61000, 121000, 181000, 241000],
                   [str(self.origin + 900), None, 'wrong', '1792886521', '1792886581000000', 'inf'])
        self.assertEqual(self.query()['legs'][0]['timestamp_delta_ms'], [None] * 5)
        self.assertEqual(self.query(price_mode='mean')['legs'][0]['timestamp_delta_ms'], [100, None, None, None, None])

    def test_optional_server_column_and_mixed_schema_do_not_break_prices(self):
        self.write([1000])
        self.write([3601000], [self.origin + 3600990], hour='01')
        result = self.query(end='2026-10-25T02:00:00Z')
        self.assertEqual(result['legs'][0]['timestamp_delta_ms'], [None, 10])
        self.assertEqual(result['result'], [101, 101])

    def test_equal_timestamp_clock_stays_with_selected_price_row(self):
        self.write([1000, 1000], [self.origin + 990, self.origin + 980], bids=[100, 200])
        leg = self.query()['legs'][0]
        self.assertIn((leg['bid'][0], leg['timestamp_delta_ms'][0]), {(100, 10), (200, 20)})

    def test_synthetic_clocks_remain_unmeasurable(self):
        for exchange, storage, kind in [('ls', 'ob', 'price'), ('okx', 'funding', 'funding'),
                                         ('hyperliquid', 'funding', 'funding')]:
            with self.subTest(exchange=exchange):
                self.write([1000], [self.origin + 1000], exchange=exchange, kind=storage)
                leg = self.query(exchange=exchange, kind=kind)['legs'][0]
                self.assertEqual(leg['timestamp_delta_ms'], [None])
                self.assertTrue(leg['timestamp_delta_unavailable_reason'])

    def test_funding_mean_uses_paired_differences_not_last_metadata(self):
        self.write([1000, 59000], [self.origin + 900, self.origin + 59020], exchange='binance', kind='funding')
        leg = self.query(exchange='binance', kind='funding', price_mode='mean')['legs'][0]
        self.assertEqual(leg['timestamp_delta_ms'], [40])
        self.assertEqual(leg['source_times'], [self.origin + 59000])
        self.assertEqual(self.query(exchange='binance', kind='funding')['legs'][0]['timestamp_delta_ms'], [-20])

    def test_sampling_alignment_and_full_csv(self):
        offsets = [1000, 61000, 121000, 181000, 241000]
        self.write(offsets, [self.origin + offset - delta for offset, delta in zip(offsets, [10, 20, 30, 40, 50])])
        result = self.query(max_points=2)
        self.assertEqual(result['legs'][0]['timestamp_delta_ms'], [10, 50])
        self.assertEqual(result['full']['legs'][0]['timestamp_delta_ms'], [10, 20, 30, 40, 50])
        path = self.root / 'export.csv'
        _write_csv(path, result, 'KRW')
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([float(row['A_timestamp_delta_ms']) for row in rows], [10, 20, 30, 40, 50])

    def test_live_uses_receipt_not_poll_time_and_retains_negative_clock(self):
        config = dict(legs=[dict(id='A', exchange='bithumb', ticker='BTC')], formula='A')
        now = self.origin + 2000
        session = _LiveSession('clock-test', config, lambda *args: None, clock=lambda: now / 1000)
        session._connection_status(dict(exchange='bithumb', state='connected'))
        for server, expected in [(self.origin + 900, 100), (self.origin + 1050, -50)]:
            session._quote(dict(id='A', bid=100, ask=102, mid=101, ts=server, receivedAt=self.origin + 1000))
            session._tick()
            self.assertEqual(session.result['legs'][0]['timestamp_delta_ms'][-1], expected)
            now += 1000


if __name__ == '__main__':
    unittest.main()
