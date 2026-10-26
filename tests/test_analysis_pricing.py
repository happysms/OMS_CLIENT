"""Selected prices drive formulas, exports and source identity independently."""

import csv
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from oms_client.analysis.history import analyze
from oms_client.api.exports import _write_csv


class AnalysisPricingTests(unittest.TestCase):
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
        path = self.root / 'legacy/bithumb/ob/BTC/2026/10/25/00.parquet'
        path.parent.mkdir(parents=True)
        pd.DataFrame(
            {
                'desktopTime': [self.origin + offset for offset in [100, 900, 1100]],
                'b1_p': [100] * 3,
                'a1_p': [101] * 3,
                'b1_q': [200] * 3,
                'a1_q': [202, 101, 202],
                'b2_p': [99] * 3,
                'a2_p': [102] * 3,
                'b2_q': [198] * 3,
                'a2_q': [204, 0, 204],
            }
        ).to_parquet(path, index=False)

    def query(self, legs, **overrides):
        payload = {
            'legs': [
                {'id': identifier, 'exchange': 'bithumb', 'ticker': 'BTC', **options}
                for identifier, options in legs
            ],
            'formula': 'A',
            'start': '2026-10-25T00:00:00Z',
            'end': '2026-10-25T00:00:02Z',
            'interval': '1s',
            'price_mode': 'last',
            'ewm_span': 1,
            'include_full_data': True,
        }
        payload.update(overrides)
        return analyze(payload)

    def test_same_market_with_different_nth_selections_does_not_share_wrong_series(self):
        result = self.query(
            [
                ('A', {'price_basis': 'bid', 'book_level': 2}),
                ('B', {'price_basis': 'ask', 'book_level': 1}),
            ],
            formula='A - B',
        )
        self.assertEqual(result['result'], [-2, -2])
        a, b = result['legs']
        self.assertEqual(a['values'], [99, 99])
        self.assertEqual(b['values'], [101, 101])
        self.assertEqual(a['mid'], [100.5, 100.5])
        self.assertEqual(a['bid'], [100, 100])

    def test_same_market_vwap_target_amounts_have_separate_series_and_null_final_snapshot(self):
        result = self.query(
            [
                ('A', {'price_basis': 'buy_vwap', 'liquidity_amount': 303}),
                ('B', {'price_basis': 'buy_vwap', 'liquidity_amount': 100}),
            ],
            formula='A - B',
        )
        self.assertIsNone(result['result'][0])
        self.assertAlmostEqual(result['result'][1], 303 / (2 + 101 / 102) - 101)
        self.assertIsNone(result['legs'][0]['values'][0])
        self.assertEqual(result['legs'][1]['values'], [101, 101])
        self.assertEqual(result['legs'][0]['quote_times'][0], self.origin + 900)

    def test_mean_reports_excluded_snapshot_coverage_and_csv_preserves_configuration(self):
        result = self.query(
            [('A', {'price_basis': 'buy_vwap', 'liquidity_amount': 303})], price_mode='mean'
        )
        leg = result['legs'][0]
        expected = 303 / (2 + 101 / 102)
        self.assertAlmostEqual(leg['values'][0], expected)
        self.assertEqual(leg['pricing_valid_rows'], [1, 1])
        self.assertEqual(leg['pricing_missing_rows'], [1, 0])
        self.assertIn('스냅샷 1개', ' '.join(result['meta']['warnings']))
        path = self.root / 'result.csv'
        _write_csv(path, result, 'KRW')
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
        self.assertAlmostEqual(float(rows[0]['A_selected_price']), expected)
        self.assertEqual(rows[0]['A_price_basis'], 'buy_vwap')
        self.assertEqual(rows[0]['A_liquidity_amount'], '303.0')
        self.assertEqual(rows[0]['A_liquidity_currency'], 'KRW')
        self.assertEqual(rows[0]['A_pricing_missing_rows'], '1.0')
        self.assertEqual(rows[0]['price_mode'], 'mean')

    def test_last_csv_does_not_replace_unfilled_price_with_raw_midpoint(self):
        result = self.query([('A', {'price_basis': 'buy_vwap', 'liquidity_amount': 303})])
        path = self.root / 'last.csv'
        _write_csv(path, result, 'KRW')
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
        self.assertEqual(rows[0]['A_selected_price'], '')
        self.assertEqual(float(rows[0]['A_bithumb_BTC_mid']), 100.5)
        self.assertEqual(rows[0]['price_mode'], 'last')
