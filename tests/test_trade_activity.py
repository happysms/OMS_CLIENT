"""Execution-only turnover, per-market volumes and transactional checkpointing."""

from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from oms_client.paper_trading.activity import (
    TradingActivity,
    ExecutedTrade,
    ACTIVITY_CSV_FIELDS,
    activity_csv_values,
)
from oms_client.paper_trading.service import PaperTradingService
from oms_client.replay.engine import ReplayEngine
from oms_client.replay.store import ReplayStore
from oms_client.storage.state import StateStore
from test_replay import config, book, tick


def trade(side, qty, price, fee=0, fx=1000, ticker='XYZ', exchange='bithumb'):
    return ExecutedTrade(
        exchange,
        ticker,
        side,
        D(str(qty)),
        D(str(price)),
        D(str(fee)),
        D(str(fx)) if fx is not None else None,
    )


class ActivityTests(unittest.TestCase):
    def test_gross_turnover_side_vwap_and_historical_fx(self):
        activity = TradingActivity()
        activity.record(trade('buy', 2, 100, 2, 1000))
        frozen = deepcopy(activity.report())
        activity.record(trade('buy', 1, 200, 2, 2000))
        activity.record(trade('sell', 1, 150, 1.5, 1500))
        result = activity.report()
        krw = result['KRW']
        usdt = result['USDT']
        self.assertEqual(
            (krw['buy_turnover'], krw['sell_turnover'], krw['turnover']), (400, 150, 550)
        )
        self.assertEqual(krw['fees_paid'], 5.5)
        self.assertEqual(
            (krw['buy_fill_count'], krw['sell_fill_count'], krw['fill_count']), (2, 1, 3)
        )
        row = krw['markets'][0]
        self.assertEqual(
            (row['buy_quantity'], row['sell_quantity'], row['traded_quantity']), (3, 1, 4)
        )
        self.assertAlmostEqual(row['buy_vwap'], 400 / 3)
        self.assertEqual(row['sell_vwap'], 150)
        self.assertAlmostEqual(usdt['buy_turnover'], 0.3)
        self.assertAlmostEqual(usdt['sell_turnover'], 0.1)
        self.assertAlmostEqual(usdt['turnover'], 0.4)
        self.assertAlmostEqual(usdt['fees_paid'], 0.004)
        self.assertEqual(frozen['KRW']['turnover'], 200)
        restored = TradingActivity(json.loads(json.dumps(activity.state())))
        self.assertEqual(restored.report(), result)

    def test_market_quantities_do_not_mix_and_missing_fx_is_partial(self):
        activity = TradingActivity()
        activity.record(trade('buy', 2, 100, fx=None))
        activity.record(trade('sell', 1, 120, fx=1200))
        activity.record(trade('buy', 0.001, 100000, fx=1000, ticker='BTC'))
        result = activity.report()
        self.assertEqual(result['KRW']['turnover'], 420)
        self.assertNotIn('traded_quantity', result['KRW'])
        self.assertEqual(len(result['KRW']['markets']), 2)
        self.assertIsNone(result['USDT']['turnover'])
        self.assertIsNone(result['USDT']['buy_turnover'])
        self.assertEqual(result['USDT']['sell_turnover'], 0.1)
        btc = next(m for m in result['USDT']['markets'] if m['ticker'] == 'BTC')
        self.assertEqual(btc['turnover'], 0.1)
        self.assertEqual(btc['buy_quantity'], 0.001)
        self.assertIsNone(btc['sell_vwap'])

    def test_invalid_trade_cannot_change_existing_totals(self):
        activity = TradingActivity()
        activity.record(trade('buy', 1, 100))
        before = activity.state()
        for kwargs in [dict(qty=0), dict(price=-1), dict(fx=0), dict(fee='NaN')]:
            args = dict(side='buy', qty=1, price=100)
            args.update(kwargs)
            with self.assertRaises(ValueError):
                activity.record(trade(**args))
        self.assertEqual(activity.state(), before)

    def test_empty_account_has_no_volume_and_undefined_vwap(self):
        report = TradingActivity().report()
        for unit in report.values():
            self.assertEqual(unit['turnover'], 0)
            self.assertEqual(unit['fill_count'], 0)
            self.assertEqual(unit['markets'], [])


class PaperActivityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = StateStore(Path(temp.name))
        self.now = 1000
        self.price = 100
        self.rate = 1000
        self.paper = self.reopen()
        self.paper.reset(dict(initial_cash=100000, fee_bps=10, slippage_bps=0))

    def quote(self, exchange, ticker):
        price = self.rate if ticker == 'USDT' else self.price
        return dict(
            exchange=exchange,
            ticker=ticker,
            bid=price,
            ask=price,
            ts=self.now * 1000,
            receivedAt=self.now * 1000,
        )

    def reopen(self):
        return PaperTradingService(
            self.store, self.quote, clock=lambda: self.now, symbol_validator=lambda e, t: (e, t)
        )

    def order(self, side='buy', quantity=1, **extra):
        return self.paper.place_order(
            dict(exchange='bithumb', ticker='XYZ', side=side, quantity=quantity, **extra)
        )

    def test_fill_snapshot_retry_restart_and_reset(self):
        buy = self.order(quantity=2, client_order_id='once')['fill_report']
        self.price = 150
        self.rate = 1500
        self.now += 1
        sell = self.order('sell')['fill_report']
        self.assertEqual(buy['activity']['KRW']['turnover'], 200)
        self.assertEqual(sell['activity']['KRW']['turnover'], 350)
        self.assertAlmostEqual(sell['activity']['USDT']['turnover'], 0.3)
        self.paper = self.reopen()
        retried = self.order(quantity=2, client_order_id='once')
        self.assertEqual(retried['fill_report'], buy)
        self.assertEqual(retried['activity']['KRW']['fill_count'], 2)
        self.rate = 3000
        self.assertEqual(self.paper.snapshot()['activity'], sell['activity'])
        reset = self.paper.reset(dict(initial_cash=100000))
        self.assertEqual(reset['activity']['KRW']['turnover'], 0)
        self.assertEqual(reset['activity']['KRW']['markets'], [])

    def test_backfill_uses_all_saved_executions_and_only_frozen_fx(self):
        first = self.order()['order']['id']
        self.price = 120
        self.rate = 1200
        self.order('sell')
        with self.store.connection() as db:
            db.execute('DELETE FROM paper_activity_state')
            row = db.execute(
                'SELECT report FROM paper_fill_reports WHERE fill_id=?', (first,)
            ).fetchone()
            old = json.loads(row['report'])
            old.pop('usdt')
            old.pop('activity')
            db.execute(
                'UPDATE paper_fill_reports SET report=? WHERE fill_id=?', (json.dumps(old), first)
            )
        self.rate = 9999
        result = self.reopen().snapshot()['activity']
        self.assertEqual(result['KRW']['turnover'], 220)
        self.assertIsNone(result['USDT']['turnover'])
        self.assertEqual(result['USDT']['sell_turnover'], 0.1)
        self.assertEqual(self.reopen().snapshot()['activity'], result)
        # Historical reports are not rewritten with reconstructed cumulative values.
        self.assertNotIn('activity', self.paper.fills()['items'][-1])

    def test_accumulator_covers_more_than_the_recent_100_fills(self):
        for _ in range(101):
            self.order(quantity=0.01)
        snapshot = self.paper.snapshot()
        self.assertEqual(len(snapshot['fills']), 100)
        self.assertEqual(snapshot['activity']['KRW']['fill_count'], 101)
        self.assertEqual(snapshot['activity']['KRW']['turnover'], 101)
        self.assertEqual(self.reopen().snapshot()['activity'], snapshot['activity'])

    def test_failed_report_insert_rolls_back_activity_and_order(self):
        before = self.paper.snapshot()['activity']
        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER fail_activity BEFORE INSERT ON paper_fill_reports BEGIN SELECT RAISE(ABORT,'failed'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.order()
        self.assertEqual(self.paper.snapshot()['activity'], before)
        self.assertEqual(self.paper.snapshot()['fill_count'], 0)

    def test_rejected_order_and_repeated_reads_add_no_volume(self):
        before = self.paper.snapshot()['activity']
        with self.assertRaises(RuntimeError):
            self.order('sell', 10)
        self.assertEqual(self.paper.snapshot()['activity'], before)
        self.assertEqual(self.paper.snapshot()['activity'], before)


class ReplayActivityTests(unittest.TestCase):
    def test_taker_uses_actual_prices_and_partial_fills_and_storage(self):
        engine = ReplayEngine(config(initial_krw='10000'))
        start = engine.now
        engine.on_event(book(start, quantity=2))
        engine.start()
        self.assertEqual(engine.snapshot()['activity']['KRW']['turnover'], 0)
        identifier = engine.place('buy', '105', '3', tif='ioc')
        engine.advance(start)
        report = engine.snapshot()['activity']['KRW']
        self.assertEqual(report['turnover'], 2 * 102 + 103)
        self.assertEqual(report['fill_count'], 2)
        self.assertEqual(report['markets'][0]['buy_quantity'], 3)
        frozen = deepcopy(engine.last_fills[0])
        engine.cancel(identifier)
        engine.place('buy', '99', '2')
        engine.advance(start)
        engine.finish()
        self.assertEqual(engine.snapshot()['activity']['KRW'], report)
        self.assertEqual(engine.last_fills[0], frozen)
        with tempfile.TemporaryDirectory() as temp:
            store = ReplayStore(temp)
            snapshot = dict(
                engine.snapshot(), id='activity-test', status='finished', config=engine.config
            )
            store.save(snapshot, engine.new_fills)
            self.assertEqual(store.get('activity-test')['activity'], snapshot['activity'])
            self.assertEqual(store.recent()[0]['activity'], snapshot['activity'])
            row = dict(
                zip(ACTIVITY_CSV_FIELDS, activity_csv_values(store.fills('activity-test')[0]))
            )
            self.assertEqual(row['fill_notional_krw'], 103)
            self.assertEqual(row['activity_krw_turnover'], 307)
            self.assertEqual(row['activity_krw_fill_count'], 2)

    def test_initial_coin_and_raw_ticks_are_not_trading_volume(self):
        engine = ReplayEngine(config(initial_coin='100', auto_mm=False))
        start = engine.now
        engine.on_event(book(start))
        engine.start()
        engine.on_event(tick(start + 1, 100, -100000))
        self.assertEqual(engine.snapshot()['activity']['KRW']['turnover'], 0)
        engine.place('buy', '102', '3', tif='fok')
        engine.advance(engine.now)
        self.assertEqual(engine.snapshot()['activity']['KRW']['fill_count'], 0)


if __name__ == '__main__':
    unittest.main()
