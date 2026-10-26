"""Fill-time currency accounting, causal FX streams and atomic persistence."""

from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from oms_client.paper_trading.currency import FxQuote, UsdtLedger, USDT_CSV_FIELDS, usdt_csv_values
from oms_client.paper_trading.service import PaperTradingService
from oms_client.replay.fx import AsOfFxQuotes
from oms_client.replay.engine import ReplayEngine
from oms_client.replay.source import ReplaySource
from oms_client.storage.state import StateStore
from test_replay import config, book, tick, SyntheticSource
from oms_client.replay.service import ReplayManager


def fx(rate, at=1000):
    return FxQuote(D(str(rate)), D(str(rate)), at, at, f'fx:{at}:{rate}')


def position(qty, mark, theory=None):
    return dict(
        exchange='bithumb',
        ticker='XYZ',
        quantity=qty,
        market_value=qty * mark,
        mark_price=mark,
        theory_price=theory,
        theory_market_value=qty * theory if theory is not None else None,
    )


class CurrencyTests(unittest.TestCase):
    def test_buy_sell_use_distinct_historical_rates_and_cash_fx_reconciles(self):
        ledger = UsdtLedger()
        ledger.initialize(2000, {}, fx(1000))
        buy = ledger.record_fill('bithumb:XYZ', 'buy', 1, 1000, 0, 0, fx(1000))
        sell = ledger.record_fill('bithumb:XYZ', 'sell', 1, 1200, 0, 1, fx(1200))
        self.assertEqual(buy['price'], 1)
        self.assertEqual(sell['realized_pnl'], 0)  # KRW realized is +200.
        a = ledger.valuation(dict(cash=2200, equity=2200), [], fx(1200))['account']
        self.assertAlmostEqual(a['cash_fx_pnl'], -1 / 6)
        self.assertAlmostEqual(a['total_pnl'] + a['cash_fx_pnl'], a['equity_change_pnl'])
        self.assertEqual(
            ledger.valuation(dict(cash=2200, equity=2200), [], fx(900))['account']['realized_pnl'],
            0,
        )
        self.assertEqual(sell['fx']['krw_per_usdt'], 1200)

    def test_weighted_cost_partial_sell_fees_and_theory(self):
        ledger = UsdtLedger()
        ledger.initialize(10000, {}, fx(1000))
        ledger.record_fill('bithumb:XYZ', 'buy', 2, 1000, 20, 0, fx(1000))  # cost 2.02
        ledger.record_fill('bithumb:XYZ', 'buy', 2, 1200, 24, 2, fx(1200))  # cost 2.02
        sold = ledger.record_fill('bithumb:XYZ', 'sell', 1, 1500, 15, 4, fx(1500))
        self.assertAlmostEqual(sold['realized_pnl'], -0.02)
        a = ledger.valuation(dict(cash=7041, equity=11541), [position(3, 1500, 1800)], fx(1500))[
            'account'
        ]
        self.assertAlmostEqual(a['unrealized_pnl'], -0.03)
        self.assertAlmostEqual(a['theory_unrealized_pnl'], 0.57)
        self.assertAlmostEqual(a['fees_paid'], 0.05)
        self.assertAlmostEqual(a['total_pnl'] + a['cash_fx_pnl'], a['equity_change_pnl'])
        restored = UsdtLedger(json.loads(json.dumps(ledger.state())))
        self.assertEqual(restored.state(), ledger.state())

    def test_initial_coins_and_theory_use_historical_basis(self):
        ledger = UsdtLedger()
        ledger.initialize(1000, {'bithumb:XYZ': 1000}, fx(1000))
        a = ledger.valuation(dict(cash=1000, equity=3000), [position(1, 2000, 3000)], fx(2000))[
            'account'
        ]
        self.assertEqual(a['unrealized_pnl'], 0)
        self.assertEqual(a['theory_unrealized_pnl'], 0.5)
        self.assertEqual(a['initial_equity'], 2)
        self.assertEqual(a['cash_fx_pnl'], -0.5)

    def test_missing_buy_cost_not_backfilled_and_flat_resets_only_position_cost(self):
        ledger = UsdtLedger()
        ledger.initialize(10000, {}, fx(1000))
        ledger.record_fill('bithumb:XYZ', 'buy', 2, 1000, 0, 0, None)
        ledger.record_fill('bithumb:XYZ', 'sell', 1, 1200, 0, 2, fx(1200))
        a = ledger.valuation(dict(cash=9200, equity=10400), [position(1, 1200)], fx(1200))[
            'account'
        ]
        self.assertIsNone(a['realized_pnl'])
        self.assertIsNone(a['unrealized_pnl'])
        ledger.record_fill('bithumb:XYZ', 'sell', 1, 1200, 0, 1, fx(1200))
        ledger.record_fill('bithumb:XYZ', 'buy', 1, 1200, 0, 0, fx(1200))
        a = ledger.valuation(dict(cash=9200, equity=10400), [position(1, 1200)], fx(1200))[
            'account'
        ]
        self.assertEqual(a['unrealized_pnl'], 0)
        self.assertIsNone(a['realized_pnl'])
        self.assertIsNone(a['fees_paid'])

    def test_missing_current_fx_keeps_realized_but_not_position_value(self):
        ledger = UsdtLedger()
        ledger.initialize(10000, {}, fx(1000))
        ledger.record_fill('bithumb:XYZ', 'buy', 2, 1000, 0, 0, fx(1000))
        ledger.record_fill('bithumb:XYZ', 'sell', 1, 1500, 0, 2, fx(1000))
        a = ledger.valuation(dict(cash=9500, equity=11000), [position(1, 1500)], None)['account']
        self.assertEqual(a['realized_pnl'], 0.5)
        self.assertIsNone(a['unrealized_pnl'])
        self.assertIsNone(a['equity'])

    def test_fx_validation_rejects_future_stale_wrong_symbol_and_invalid_values(self):
        good = dict(exchange='bithumb', ticker='USDT', bid=999, ask=1001, ts=1000, receivedAt=1000)
        self.assertEqual(FxQuote.from_public(good, 1000).rate, 1000)
        self.assertIsNotNone(FxQuote.from_public(good, 11000))
        self.assertIsNone(FxQuote.from_public(good, 11001))
        for extra in [
            dict(ts=1001),
            dict(receivedAt=1001),
            dict(ts=None),
            dict(ts=True),
            dict(ts=float('inf')),
            dict(exchange='okx'),
            dict(ticker='USD'),
            dict(bid=0),
            dict(ask=998),
            dict(bid=float('nan')),
        ]:
            with self.subTest(extra=extra):
                self.assertIsNone(FxQuote.from_public(dict(good, **extra), 1000))

    def test_asof_ties_future_stale_backward_invalid_and_read_failure(self):
        stream = AsOfFxQuotes(
            [book(1000, 999, 1001), book(1020, 1199, 1201), book(1030, 1299, 1301)]
        )
        self.assertIsNone(stream.at(999))
        self.assertEqual(stream.at(1000).rate, 1000)
        self.assertEqual(stream.at(1019).rate, 1000)
        self.assertEqual(stream.at(1020).rate, 1200)
        self.assertIsNone(stream.at(1010))
        self.assertEqual(stream.at(1030).rate, 1300)
        self.assertIsNone(stream.at(11031))
        invalid = dict(book(1040, 1399, 1401), server_time=1041)
        self.assertIsNone(AsOfFxQuotes([book(1000), invalid]).at(1040))

        def broken():
            yield book(1000)
            raise OSError('reference unavailable')

        failed = AsOfFxQuotes(broken())
        self.assertIsNone(failed.at(1001))
        self.assertIsNotNone(failed.error)

    def test_csv_export_uses_only_frozen_fx_and_legacy_is_empty(self):
        ledger = UsdtLedger()
        ledger.initialize(2000, {}, fx(1000))
        fill = ledger.record_fill('bithumb:XYZ', 'buy', 1, 1000, 1, 0, fx(1000))
        report = dict(usdt=dict(fill=fill, account=dict(realized_pnl=0)))
        row = dict(zip(USDT_CSV_FIELDS, usdt_csv_values(report)))
        self.assertEqual(row['fx_krw_per_usdt'], 1000)
        self.assertEqual(row['fx_rate_decimal'], '1000')
        self.assertEqual(row['usdt_fee'], 0.001)
        self.assertTrue(all(value is None for value in usdt_csv_values({})))


class PaperCurrencyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = StateStore(Path(temp.name))
        self.now = 1000
        self.rate = 1000
        self.price = 1000
        self.missing = False
        self.paper = self.reopen()
        self.paper.reset(dict(initial_cash=10000, fee_bps=0, slippage_bps=0))

    def quote(self, exchange, ticker):
        if ticker == 'USDT' and self.missing:
            raise RuntimeError('no FX')
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

    def order(self, side, quantity=1, **extras):
        return self.paper.place_order(
            dict(exchange='bithumb', ticker='XYZ', side=side, quantity=quantity, **extras)
        )

    def test_roundtrip_restart_idempotency_and_immutable_reports(self):
        buy = self.order('buy', client_order_id='once')['fill_report']
        self.now += 1
        self.rate = self.price = 1200
        self.paper = self.reopen()
        retry = self.order('buy', client_order_id='once')['fill_report']
        self.assertEqual(retry, buy)
        sell = self.order('sell')['fill_report']
        self.assertEqual(sell['fill']['realized_pnl'], 200)
        self.assertEqual(sell['usdt']['fill']['realized_pnl'], 0)
        self.assertEqual(self.paper.fills()['items'][1]['usdt']['fill']['fx']['krw_per_usdt'], 1000)
        self.assertEqual(self.paper.snapshot()['usdt']['account']['realized_pnl'], 0)

    def test_report_insert_failure_rolls_back_currency_state_and_krw(self):
        before = self.paper.snapshot()
        with self.store.connection() as db:
            db.execute(
                "CREATE TRIGGER fail_report BEFORE INSERT ON paper_fill_reports BEGIN SELECT RAISE(ABORT, 'report failed'); END"
            )
            state = db.execute('SELECT state FROM paper_usdt_state').fetchone()[0]
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.order('buy')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT state FROM paper_usdt_state').fetchone()[0], state)
        after = self.paper.snapshot()
        self.assertEqual(before['account'], after['account'])
        self.assertEqual(after['fill_count'], 0)

    def test_legacy_account_is_not_rebased_using_todays_rate_and_reset_is_fresh(self):
        self.order('buy')
        with self.store.connection() as db:
            db.execute('DELETE FROM paper_usdt_state')
        self.rate = 2000
        self.paper = self.reopen()
        snapshot = self.paper.snapshot()['usdt']
        self.assertIsNone(snapshot['account']['initial_equity'])
        self.assertIsNone(snapshot['account']['realized_pnl'])
        self.assertIsNone(snapshot['account']['unrealized_pnl'])
        reset = self.paper.reset(dict(initial_cash=10000))
        self.assertEqual(reset['usdt']['account']['initial_equity'], 5)
        self.assertEqual(reset['fill_count'], 0)

    def test_missing_fill_fx_remains_missing_after_recovery(self):
        self.missing = True
        buy = self.order('buy')['fill_report']
        self.assertIsNone(buy['usdt']['fill']['fx'])
        self.missing = False
        self.assertIsNone(self.paper.snapshot()['usdt']['account']['unrealized_pnl'])
        self.assertIsNone(self.paper.fills()['items'][0]['usdt']['fill']['fx'])

    def test_execution_quote_wait_refreshes_fx_before_freezing_fill(self):
        original = self.paper._quote

        def delayed_quote(exchange, ticker):
            self.now += 1
            self.rate = 1200
            return original(exchange, ticker)

        self.paper._quote = delayed_quote
        report = self.order('buy')['fill_report']
        self.assertEqual(report['usdt']['fill']['fx']['krw_per_usdt'], 1200)
        self.assertEqual(
            report['usdt']['fill']['fx']['observed_at_ms'], report['fill']['timestamp']
        )


class ReplayCurrencyTests(unittest.TestCase):
    def test_1x_32x_fast_store_identical_historical_currency_results(self):
        class Source(SyntheticSource):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.fx = AsOfFxQuotes()

            def load(self):
                start = self.config['start_ms']
                self.fx = AsOfFxQuotes([book(start - 1, 999, 1001), book(start + 40, 1199, 1201)])
                return super().load()

            def fx_at(self, now):
                return self.fx.at(now)

        results = []
        with tempfile.TemporaryDirectory() as temp:
            manager = ReplayManager(temp, Source)
            try:
                for mode in ({'fast': True}, {'speed': 1}, {'speed': 32}):
                    result = manager.create(
                        dict(
                            ticker='XYZ',
                            start='2026-10-26T12:00:00+09:00',
                            end='2026-10-26T12:00:00.120+09:00',
                            quantity_step='1',
                            min_notional='1',
                            fee_bps='0',
                            latency_ms=0,
                            **mode,
                        )
                    )
                    manager.active.thread.join(2)
                    end = manager.get(result['id'])
                    self.assertEqual(end['status'], 'finished')
                    fills = manager.store.fills(result['id'])
                    self.assertEqual(len(fills), 2)
                    self.assertEqual(fills[0]['usdt']['fill']['fx']['krw_per_usdt'], 1200)
                    self.assertEqual(fills[1]['usdt']['fill']['fx']['krw_per_usdt'], 1000)
                    self.assertEqual(manager.store.recent()[0]['usdt'], end['usdt'])
                    results.append((end['account'], end['usdt'], fills, end['event_count']))
            finally:
                manager.close()
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_fill_between_market_books_uses_new_fx_without_changing_execution(self):
        cfg = config(initial_krw='10000', latency_ms=20)
        start = cfg['start_ms']
        stream = AsOfFxQuotes([book(start, 999, 1001), book(start + 10, 1199, 1201)])
        baseline = ReplayEngine(cfg)
        rated = ReplayEngine(deepcopy(cfg), stream.at)
        for engine in (baseline, rated):
            engine.on_event(book(start, 999, 1001, 0))
            engine.start()
            engine.place('buy', '999', '1')
            engine.advance(start + 20)
            engine.on_event(tick(start + 21, 999, -1))
        report = rated.last_fills[-1]
        self.assertEqual(report['usdt']['fill']['fx']['krw_per_usdt'], 1200)
        self.assertAlmostEqual(report['usdt']['fill']['price'], 999 / 1200)

        def krw(value):
            if isinstance(value, dict):
                return {k: krw(v) for k, v in value.items() if k not in {'usdt', 'USDT'}}
            if isinstance(value, list):
                return [krw(v) for v in value]
            return value

        self.assertEqual(krw(baseline.snapshot()), krw(rated.snapshot()))
        frozen = deepcopy(report)
        rated.advance(start + 30)
        rated.valuation()
        self.assertEqual(report, frozen)

    def test_injected_stale_fx_is_rejected_at_engine_boundary(self):
        cfg = config()
        start = cfg['start_ms']
        engine = ReplayEngine(cfg, lambda now: fx(1000, start - 10001))
        engine.on_event(book(start, quantity=0))
        engine.start()
        engine.place('buy', '100', '1')
        engine.advance(start)
        engine.on_event(tick(start + 1, 100, -1))
        self.assertIsNone(engine.last_fills[-1]['usdt']['fill']['fx'])

    def test_real_reference_parquet_native_and_legacy_and_optional_missing(self):
        import duckdb
        import pandas as pd

        cfg = config()
        start = cfg['start_ms']
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / 'usdt.parquet')
            con = duckdb.connect()
            con.sql(
                f'SELECT {start-1000} AS desktopTime, {start-1000} AS serverTime, 999 AS b1_p, 1001 AS a1_p, 10 AS bid_size, 10 AS ask_size'
            ).write_parquet(path)
            con.close()
            for kind in ('orderbook_1s', 'legacy'):
                source = ReplaySource(cfg)
                try:
                    with (
                        patch(
                            'oms_client.replay.source.parquet._resolve_source',
                            return_value={'source_kind': kind},
                        ),
                        patch(
                            'oms_client.replay.source.parquet.files_for_range', return_value=[path]
                        ),
                    ):
                        source._load_fx(pd.Timestamp(cfg['end_ms'], unit='ms', tz='UTC'))
                    self.assertEqual(source.fx_at(start).rate, 1000)
                    self.assertEqual(source.total, 0)
                    self.assertEqual(source.counts, {})
                    self.assertEqual(len(source.manifest), 1)
                    source.verify()
                finally:
                    source.close()
            source = ReplaySource(cfg)
            try:
                with patch(
                    'oms_client.replay.source.parquet._resolve_source',
                    side_effect=ValueError('missing'),
                ):
                    source._load_fx(None)
                self.assertIsNone(source.fx_at(start))
            finally:
                source.close()


if __name__ == '__main__':
    unittest.main()
