"""Immutable per-fill valuations, partial exits, theory provenance and exports."""
import csv
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from oms_client.api import create_app
from oms_client.backtesting.engine import run_backtest
from oms_client.paper_trading.service import PaperTradingService, _fresh_quote
from oms_client.paper_trading.theory import PremiumTheoryProvider
from oms_client.storage.state import StateStore
from oms_client.storage.premiums import HOUR, PremiumStore
from oms_client.analysis.premiums import completed_end


class FillReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.store = StateStore(self.directory)
        self.now = 1792886400
        self.bid, self.ask, self.theory_price = 99, 101, 105
        self.missing = set()
        self.theory_offset = 0
        owner = self

        class Theory:
            def get_many(self, pairs):
                return {pair: dict(price=owner.theory_price, basis='test_strategy',
                                   available_at_ms=int(owner.now * 1000) + owner.theory_offset) for pair in pairs}
        self.theory = Theory()
        self.paper = self.reopen()
        self.paper.reset(dict(initial_cash=1000, fee_bps=10, slippage_bps=0))

    def quote(self, exchange, ticker):
        if ticker in self.missing:
            raise RuntimeError('Missing quote')
        return dict(exchange=exchange, ticker=ticker, bid=self.bid, ask=self.ask,
                    ts=int(self.now * 1000), receivedAt=int(self.now * 1000))

    def reopen(self):
        return PaperTradingService(self.store, self.quote, clock=lambda: self.now,
                                   symbol_validator=lambda e, t: (e, t), theory_provider=self.theory)

    def order(self, side, quantity, ticker='BTC', **extra):
        return self.paper.place_order(dict(exchange='bithumb', ticker=ticker, side=side, quantity=quantity, **extra))

    def test_partial_exit_snapshots_include_fees_once_and_reconcile_equity(self):
        buy = self.order('buy', 2)['fill_report']
        self.assertAlmostEqual(buy['account']['unrealized_pnl'], -4.202)
        self.assertAlmostEqual(buy['account']['theory_unrealized_pnl'], 7.798)
        self.assertEqual(buy['account']['realized_pnl'], 0)
        self.bid, self.ask, self.theory_price = 110, 112, 115
        sell = self.order('sell', 1)['fill_report']
        account = sell['account']
        self.assertAlmostEqual(sell['fill']['realized_pnl'], 8.789)
        self.assertAlmostEqual(account['realized_pnl'], 8.789)
        self.assertAlmostEqual(account['unrealized_pnl'], 8.899)
        self.assertAlmostEqual(account['theory_unrealized_pnl'], 13.899)
        self.assertAlmostEqual(account['total_pnl'], account['equity'] - 1000)
        self.assertAlmostEqual(account['theory_total_pnl'], account['theory_equity'] - 1000)
        self.assertAlmostEqual(account['fees_paid'], .312)
        self.assertEqual(sell['positions'][0]['quantity'], 1)
        self.assertEqual(sell['before']['quantity'], 2)

    def test_past_records_immutable_after_mark_changes_restart_and_retry(self):
        first = self.order('buy', 1, client_order_id='once')['fill_report']
        self.now += 100
        self.bid, self.ask, self.theory_price = 200, 202, 210
        self.paper = self.reopen()
        retried = self.order('buy', 1, client_order_id='once')
        self.assertEqual(first, retried['fill_report'])
        self.assertEqual(self.paper.fills()['total'], 1)
        self.assertNotEqual(retried['account']['unrealized_pnl'], first['account']['unrealized_pnl'])

    def test_missing_other_position_prevents_false_complete_account_pnl(self):
        self.order('buy', 1, ticker='ETH')
        self.missing.add('ETH')
        report = self.order('buy', 1)['fill_report']
        self.assertEqual(len(report['positions']), 2)
        self.assertIsNone(report['account']['unrealized_pnl'])
        self.assertIsNone(report['account']['total_pnl'])
        self.assertIsNotNone(report['account']['theory_unrealized_pnl'])

    def test_future_stale_missing_theories_are_not_zero_but_flat_account_is_zero(self):
        for offset in (1, -10001):
            self.theory_offset = offset
            report = self.order('buy', 1)['fill_report']
            self.assertIsNone(report['account']['theory_unrealized_pnl'])
            self.assertIsNotNone(report['account']['unrealized_pnl'])
        self.theory_price = None
        report = self.order('sell', 2)['fill_report']
        self.assertEqual(report['account']['theory_unrealized_pnl'], 0)
        self.assertEqual(report['account']['unrealized_pnl'], 0)
        self.assertEqual(report['positions'], [])

    def test_rejected_order_writes_no_report_or_cash_change(self):
        before = self.paper.snapshot()['account']['cash']
        with self.assertRaises(RuntimeError):
            self.order('buy', 100)
        self.assertEqual(self.paper.fills()['total'], 0)
        self.assertEqual(self.paper.snapshot()['account']['cash'], before)

    def test_report_write_failure_rolls_back_fill_cash_and_position(self):
        with patch('oms_client.paper_trading.service.json.dumps', side_effect=ValueError('report encoding failed')):
            with self.assertRaises(ValueError):
                self.order('buy', 1)
        snapshot = self.paper.snapshot()
        self.assertEqual(snapshot['account']['cash'], 1000)
        self.assertEqual(snapshot['positions'], [])
        self.assertEqual(snapshot['orders'], [])
        self.assertEqual(snapshot['fill_count'], 0)

    def test_default_theory_feed_is_separate_from_execution_and_closed(self):
        class Quotes:
            def __init__(self):
                self.closed = False
            def close(self):
                self.closed = True
        execution, reference = Quotes(), Quotes()
        with patch('oms_client.paper_trading.service.PublicQuoteProvider', side_effect=[execution, reference]):
            paper = PaperTradingService(self.store)
        self.assertIs(paper.quote_provider, execution)
        self.assertIs(paper.theory_provider.quotes, reference)
        paper.close()
        self.assertTrue(execution.closed)
        self.assertTrue(reference.closed)

    def test_same_time_pagination_and_legacy_migration_do_not_fabricate_pnl(self):
        one = self.order('buy', 1)['order']['id']
        self.order('buy', 1)
        first = self.paper.fills(limit=1)
        second = self.paper.fills(limit=1, before=first['next_before'])
        self.assertNotEqual(first['items'][0]['fill']['id'], second['items'][0]['fill']['id'])
        self.assertIsNone(second['next_before'])
        with self.store.connection() as db:
            db.execute('DELETE FROM paper_fill_reports WHERE fill_id=?', (one,))
        reopened = self.reopen()
        legacy = next(item for item in reopened.fills()['items'] if item['fill']['id'] == one)
        self.assertEqual(legacy['valuation_status'], 'legacy_unavailable')
        self.assertIsNone(legacy['account'])
        self.assertEqual(legacy['fill']['price'], 101)

    def test_fill_api_and_csv_retain_account_and_source_metadata(self):
        self.order('buy', 2)
        app = create_app(dict(TESTING=True, STATE_DIR=str(self.directory), PASSWORD='test-password-only', PAPER_SERVICE=self.paper))
        self.addCleanup(app.extensions['close_services'])
        client = app.test_client()
        self.assertEqual(client.get('/api/paper/fills').status_code, 401)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)[1]
        client.post('/login', data={'password': 'test-password-only', 'csrf_token': csrf})
        self.assertEqual(client.get('/api/paper/fills?limit=1000').status_code, 400)
        self.assertEqual(client.get('/api/paper/fills').json['total'], 1)
        response = client.get('/api/paper/fills/csv')
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        self.assertAlmostEqual(float(rows[0]['theory_unrealized_pnl']), 7.798)
        provenance = json.loads(rows[0]['valuation_json'])
        self.assertEqual(provenance['execution_theory']['basis'], 'test_strategy')
        self.assertEqual(provenance['as_of_ms'], int(self.now * 1000))

    def test_live_theory_uses_stored_premium_percent_and_records_partial_coverage(self):
        now = int(self.now * 1000)
        end = completed_end(now)
        premiums = PremiumStore(self.directory)
        premiums.save('BTC', [dict(hour_ms=end-HOUR, total=4, samples=2, minimum=2, maximum=2, fingerprint='test')])
        class Quotes:
            def get_many(self, pairs, timeout):
                return {pair: dict(exchange=pair[0], ticker=pair[1], bid=100 if pair[0]=='okx' else 1000,
                                   ask=100 if pair[0]=='okx' else 1000, ts=now, receivedAt=now) for pair in pairs}
        theory = PremiumTheoryProvider(self.directory, Quotes(), _fresh_quote, lambda: self.now)
        result = theory.get_many([('bithumb','BTC')])[('bithumb','BTC')]
        self.assertEqual(result['price'], 102000)
        self.assertEqual(result['mean_premium_pct'], 2)
        self.assertEqual(result['samples'], 2)
        self.assertLess(result['coverage_pct'], 100)
        self.assertEqual(result['window_end_ms'], end)

    def test_backtest_persists_post_fill_marks_without_current_premium_lookup(self):
        origin = int(self.now * 1000)
        source = {'full': {'times': [origin+i*60000 for i in range(4)],
                          'legs': [dict(mid=[100]*4, bid=[99]*4, ask=[101]*4)]}, 'meta': {}}
        payload = dict(exchange='bithumb', ticker='BTC', start='2026-10-25T00:00:00Z',
                       end='2026-10-25T00:04:00Z', interval='1min', strategy='buy_hold', initial_cash=1000,
                       fee_bps=10, slippage_bps=0, close_on_end=False)
        result = run_backtest(payload, data_provider=lambda _:source)
        fill = result['trades'][0]
        self.assertIsNone(fill['theory_unrealized_pnl'])
        self.assertAlmostEqual(fill['total_pnl'], fill['equity']-1000)
        self.assertLess(fill['unrealized_pnl'], 0)
        supplied = run_backtest(payload, data_provider=lambda _:source,
                               theory_provider=lambda stamp,*_:dict(price=105,available_at_ms=stamp,basis='historical_strategy'))
        self.assertGreater(supplied['trades'][0]['theory_unrealized_pnl'], 0)
        self.assertEqual(supplied['trades'][0]['valuation']['theory']['basis'], 'historical_strategy')


if __name__ == '__main__':
    unittest.main()
