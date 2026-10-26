"""Executable spot branches from A-L; no claim of native futures fidelity."""

from copy import deepcopy
from decimal import Decimal as D
import tempfile
import unittest

from test_replay import config, book, tick
from oms_client.replay.engine import ReplayEngine
from oms_client.replay.store import ReplayStore
from oms_client.replay.limit_execution import capabilities
from oms_client.replay.service import ReplaySession


class LimitExecutionTests(unittest.TestCase):
    def test_case_matrix_covers_all_142_design_ids_without_claiming_unsupported_execution(self):
        import re
        from pathlib import Path

        design = Path('docs/point2_limit_fill_cases.ko.md').read_text()
        expected = set(
            re.findall(
                r'^\| \[([A-L]\d\d)\]\(illustrated/limit-[a-l]\.ko\.md#[a-l]\d\d\) \|', design, re.M
            )
        )
        report = capabilities()
        actual = [row['id'] for row in report['cases']]
        self.assertEqual(len(actual), 142)
        self.assertEqual(set(actual), expected)
        self.assertEqual(sum(report['counts'].values()), 142)
        for row in report['cases']:
            if row['id'].startswith('H'):
                self.assertEqual(row['status'], 'unsupported')
            self.assertTrue(row['note'])

    def engine(self, quantity=2, **kw):
        e = ReplayEngine(config(initial_krw='100000', initial_coin='100', **kw))
        e.on_event(book(e.now, quantity=quantity))
        e.start()
        return e

    def place(self, e, side='buy', price='102', quantity='5', **kw):
        identifier = e.place(side, price, quantity, **kw)
        e.advance(e.now)
        return identifier

    def check(self, e):
        self.assertGreaterEqual(e.free()[0], 0)
        self.assertGreaterEqual(e.free()[1], 0)
        for o in [*e.snapshot()['orders'], *[r for r in e.order_log if 'quantity_state' in r]]:
            self.assertAlmostEqual(sum(o['quantity_state'].values()), o['quantity'])
        for f in e.last_fills:
            (
                self.assertLessEqual(f['price'], f['limit_price'])
                if f['side'] == 'buy'
                else self.assertGreaterEqual(f['price'], f['limit_price'])
            )
        a = e.valuation()['account']
        self.assertAlmostEqual(a['equity'] - float(e.initial_equity), a['total_pnl'])

    def test_B01_B05_B06_B07_B08_B09_GTC_IOC_post_only_both_sides(self):
        for side, price in [('buy', '102'), ('sell', '100')]:
            for tif in ('gtc', 'ioc', 'post_only'):
                with self.subTest(side=side, tif=tif):
                    e = self.engine()
                    identifier = self.place(e, side, price, tif=tif)
                    if tif == 'post_only':
                        self.assertEqual(e.fill_count, 0)
                        self.assertEqual(e.order_log[-1]['status'], 'rejected')
                    else:
                        self.assertEqual(sum(f['quantity'] for f in e.last_fills), 2)
                        if tif == 'gtc':
                            self.assertEqual(e.orders[identifier].remaining, 3)
                        else:
                            self.assertEqual(e.order_log[-1]['quantity_state']['cancelled'], 3)
                    self.check(e)
        for tif in ('gtc', 'ioc', 'post_only'):
            e = self.engine()
            identifier = self.place(e, price='100', tif=tif)
            self.assertEqual(e.fill_count, 0)
            self.assertEqual(identifier in e.orders, tif != 'ioc')

    def test_B02_B03_B04_limit_bounds_and_multiple_actual_prices(self):
        for side, limit, prices in [('buy', '104', [102, 103, 104]), ('sell', '98', [100, 99, 98])]:
            e = self.engine()
            self.place(e, side, limit, tif='gtc')
            self.assertEqual([f['price'] for f in e.last_fills], prices)
            self.assertEqual([f['quantity'] for f in e.last_fills], [2, 2, 1])
            self.assertTrue(all(f['liquidity'] == 'taker' for f in e.last_fills))
            self.check(e)

    def test_L03_L04_fok_atomic_complete_insufficient_and_unknown(self):
        for side, limit in [('buy', '104'), ('sell', '98')]:
            e = self.engine()
            self.place(e, side, limit, tif='fok')
            self.assertEqual(sum(f['quantity'] for f in e.last_fills), 5)
            self.check(e)
        e = self.engine()
        self.place(e, price='102', tif='fok')
        self.assertEqual(e.fill_count, 0)
        self.assertEqual(e.order_log[-1]['reason'], 'fok_unfilled')
        self.assertEqual(e.depth.self_debt, {})
        e = self.engine()
        identifier = self.place(e, price='110', quantity='20', tif='fok')
        self.assertEqual(e.fill_count, 0)
        self.assertEqual(e.depth.self_debt, {})
        self.assertEqual(e.orders[identifier].status, 'execution_unknown')
        self.check(e)

    def test_B10_K03_unknown_depth_preserves_known_fills_and_reserves(self):
        for tif in ('gtc', 'ioc'):
            e = self.engine()
            identifier = self.place(e, price='110', quantity='20', tif=tif)
            self.assertEqual(sum(f['quantity'] for f in e.last_fills), 10)
            self.assertEqual(e.orders[identifier].remaining, 10)
            self.assertGreater(e.orders[identifier].reserve_cash, 0)
            with self.assertRaises(ValueError):
                e.cancel(identifier)
            with self.assertRaises(ValueError):
                e.place('buy', '100', '1')
            e.finish()
            self.assertEqual(e.orders[identifier].status, 'execution_unknown')
            self.check(e)
        for tif in ('gtc', 'ioc', 'fok'):
            e = self.engine()
            b = book(e.now + 1)
            b['asks'][1]['quantity'] = None
            e.on_event(b)
            identifier = self.place(e, price='103', tif=tif)
            self.assertEqual(e.orders[identifier].status, 'execution_unknown')
            self.assertEqual(sum(f['quantity'] for f in e.last_fills), 0 if tif == 'fok' else 2)

    def test_B11_D08_K09_self_debt_survives_repeated_and_increased_books(self):
        e = self.engine()
        t = e.now
        self.place(e, quantity='2', tif='ioc')
        self.place(e, quantity='2', tif='ioc')
        self.assertEqual(e.fill_count, 1)
        e.on_event(book(t + 1, quantity=2))
        self.place(e, quantity='2', tif='ioc')
        self.assertEqual(e.fill_count, 1)
        e.on_event(book(t + 2, quantity=3))
        self.place(e, quantity='2', tif='ioc')
        self.assertEqual(sum(f['quantity'] for f in e.last_fills), 3)
        self.check(e)

    def test_B12_arrival_uses_updated_book(self):
        e = self.engine(latency_ms=20)
        t = e.now
        identifier = e.place('buy', '102', '2', tif='gtc')
        e.on_event(book(t + 10, bid=98, ask=100, quantity=5))
        e.advance(t + 20)
        self.assertEqual(e.last_fills[0]['price'], 100)
        self.assertNotIn(identifier, e.orders)
        self.check(e)

    def test_G01_G06_stp_modes_after_better_external_fill(self):
        for stp in ('cancel_taker', 'cancel_maker', 'cancel_both'):
            e = self.engine()
            maker = self.place(e, 'sell', '103', '1')
            incoming = self.place(e, price='104', tif='gtc', stp=stp)
            expected = 5 if stp == 'cancel_maker' else 2
            self.assertEqual(sum(f['quantity'] for f in e.last_fills), expected)
            self.assertTrue(all(f['order_id'] == incoming for f in e.last_fills))
            self.assertEqual(maker in e.orders, stp == 'cancel_taker')
            self.check(e)
        e = self.engine()
        maker = self.place(e, 'sell', '103', '1')
        self.place(e, price='103', quantity='5', tif='fok', stp='cancel_maker')
        self.assertIn(maker, e.orders)
        self.assertEqual(e.fill_count, 0)

    def test_B05_H11_spot_mixed_roles_fees_and_price_improvement(self):
        e = self.engine(maker_fee_bps='5', taker_fee_bps='10')
        t = e.now
        identifier = self.place(e, quantity='5', tif='gtc')
        e.on_event(tick(t + 1, 102, -3))
        self.assertNotIn(identifier, e.orders)
        self.assertEqual([f['liquidity'] for f in e.last_fills], ['taker', 'maker'])
        self.assertEqual(e.fees, D('0.357'))
        self.check(e)
        e = self.engine(maker_fee_bps='10', taker_fee_bps='5')
        self.place(e, price='104', quantity='2', tif='gtc')
        self.assertEqual(e.cash, D('99795.898'))
        self.assertEqual(e.free()[0], e.cash)

    def test_auto_mm_reserves_configured_role_fees_instead_of_legacy_common_fee(self):
        e = self.engine(
            quantity=0,
            auto_mm=True,
            quantity_step='0.00000001',
            maker_fee_bps='10',
            taker_fee_bps='20',
        )
        self.assertTrue(e.working())
        self.assertGreaterEqual(e.free()[0], 0)
        e.advance(e.now)
        self.check(e)

    def test_mixed_role_fee_rounding_cannot_exceed_reserved_cash(self):
        e = self.engine(fee_bps='0.0000001')
        t = e.now
        identifier = self.place(e, quantity='3', tif='gtc')
        e.on_event(tick(t + 1, 102, -1))
        self.assertNotIn(identifier, e.orders)
        self.assertEqual(e.fees, D('0.00000002'))
        self.check(e)

    def test_C02_C03_no_fill_on_touch_or_quote_cross(self):
        e = self.engine(quantity=0)
        t = e.now
        identifier = self.place(e, price='101', quantity='1')
        e.on_event(book(t + 1, bid=99, ask=101, quantity=3))
        e.on_event(book(t + 2, bid=98, ask=100, quantity=3))
        self.assertEqual(e.fill_count, 0)
        self.assertIn(identifier, e.orders)

    def test_D06_better_external_levels_consume_shared_tick_first(self):
        e = self.engine()
        t = e.now
        identifier = self.place(e, price='99', quantity='1')
        e.on_event(tick(t + 1, 99, -3))
        self.assertEqual(e.fill_count, 0)
        e.on_event(tick(t + 2, 99, -2))
        self.assertEqual(e.fill_count, 1)
        self.assertNotIn(identifier, e.orders)
        self.check(e)

    def test_C12_spread_improvement_is_known_empty_queue(self):
        e = self.engine(quantity=3)
        t = e.now
        identifier = self.place(e, price='101', quantity='1')
        self.assertEqual(e.snapshot()['orders'][0]['queue_ahead'], 0)
        e.on_event(tick(t + 1, 101, -1))
        self.assertNotIn(identifier, e.orders)

    def test_A10_idempotent_retry_and_conflicting_request_after_completion(self):
        e = self.engine()
        a = self.place(e, quantity='2', tif='ioc', client_order_id='same')
        self.assertEqual(e.place('buy', '102.0', '2.00', tif='ioc', client_order_id='same'), a)
        self.assertEqual(e.fill_count, 1)
        with self.assertRaises(ValueError):
            e.place('buy', '103', '2', tif='ioc', client_order_id='same')
        e.cancel(a)
        self.assertEqual(e.fill_count, 1)

    def test_E09_cancel_on_ack_and_E06_same_time_priority(self):
        e = self.engine(quantity=0, latency_ms=20, cancel_ack_policy='on_ack')
        t = e.now
        a = e.place('buy', '100', '2')
        e.advance(t + 1)
        e.cancel(a)
        self.assertEqual(e.snapshot()['orders'][0]['quantity_state']['in_flight'], 2)
        e.advance(t + 21)
        self.assertIn(a, e.orders)
        e.on_event(tick(t + 30, 100, -1))
        self.assertEqual(e.coin, 101)
        e.on_event(tick(t + 40, 100, -1))
        self.assertEqual(e.coin, 101)
        self.assertNotIn(a, e.orders)

    def test_F01_F03_F07_F09_F12_replace_recomputes_remaining_after_cancel(self):
        e = self.engine(quantity=0, latency_ms=20)
        t = e.now
        a = e.place('buy', '100', '5')
        e.advance(t + 20)
        e.replace(a, '100', '5')
        self.assertEqual(e.orders[a].status, 'live')
        e.replace(a, '99', '4')
        e.on_event(tick(t + 25, 100, -2))
        e.advance(t + 40)
        new = e.working()[0]
        self.assertEqual(new.remaining, 2)
        self.assertEqual(new.price, 99)
        self.assertEqual(new.replaces, a)
        self.assertEqual(new.status, 'in_flight')
        self.check(e)

    def test_F10_invalid_replace_preserves_old_or_records_new_failure(self):
        e = self.engine(quantity=0, price_step='1')
        a = self.place(e, price='100', quantity='2')
        with self.assertRaises(ValueError):
            e.replace(a, '99.5', '2')
        self.assertEqual(e.orders[a].status, 'live')
        e.replace(a, '100', '10000000')
        e.advance(e.now)
        self.assertEqual(e.order_log[-1]['status'], 'replace_failed')
        self.assertFalse(e.working())

    def test_L05_expiry_before_arrival_and_partial_fill_expiry(self):
        e = self.engine(quantity=0, latency_ms=20)
        t = e.now
        a = e.place('buy', '100', '2', tif='gtd', expires_at_ms=t + 10)
        e.advance(t + 20)
        self.assertNotIn(a, e.orders)
        self.assertEqual(e.fill_count, 0)
        e = self.engine(quantity=0)
        t = e.now
        a = self.place(e, price='100', quantity='2', tif='gtd', expires_at_ms=t + 10)
        e.on_event(tick(t + 5, 100, -1))
        e.on_event(tick(t + 10, 100, -1))
        self.assertEqual(e.fill_count, 1)
        self.assertEqual(e.order_log[-1]['reason'], 'expired')
        self.check(e)

    def test_L01_L02_end_preserves_last_state_and_unknown_risk(self):
        e = self.engine(end_order_policy='preserve')
        a = self.place(e, price='100')
        before = e.free()
        e.finish('stopped')
        self.assertEqual(e.free(), before)
        self.assertEqual(e.end_orders[0]['id'], a)
        self.assertEqual(e.orders[a].status, 'live')

    def test_K02_unknown_tick_direction_is_not_a_fill(self):
        e = self.engine(quantity=0)
        t = e.now
        self.place(e, price='100', quantity='1')
        event = tick(t + 1, 100, -1)
        event['direction_known'] = False
        e.on_event(event)
        self.assertEqual(e.fill_count, 0)

    def test_all_closed_orders_persist_beyond_recent_100_and_fill_rows_are_frozen(self):
        e = self.engine(quantity=0)
        for _ in range(105):
            self.place(e, quantity='1', tif='ioc')
        self.assertEqual(len(e.order_log), 100)
        self.assertEqual(e.order_event_count, 105)
        snapshot = dict(id='orders', status='finished', config=e.config, **e.snapshot())
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(tmp)
            store.save(snapshot, e.new_fills, [], e.new_order_events)
            self.assertEqual(len(store.order_events('orders', 106)), 105)
            reopened = ReplayStore(tmp)
            self.assertEqual(len(reopened.order_events('orders', 106)), 105)

    def test_mixed_taker_maker_fills_and_order_results_identical_in_batch_1x_32x(self):
        class Source:
            def __init__(self, cfg):
                self.config = cfg
                self.total = 2
                self.skipped = 0
                self.manifest = []

            def load(self):
                t = self.config['start_ms']
                return iter([book(t, quantity=2), tick(t + 30, 102, -3)])

            def close(self):
                pass

            def verify(self):
                pass

        results = []
        with tempfile.TemporaryDirectory() as tmp:
            store = ReplayStore(tmp)
            for index, pace in enumerate(({'fast': True}, {'speed': 1}, {'speed': 32})):
                cfg = config(latency_ms=20, maker_fee_bps='5', taker_fee_bps='10', **pace)
                session = ReplaySession(str(index), cfg, store, Source)
                session.engine.on_event(book(cfg['start_ms'], quantity=2))
                session.engine.place('buy', '102', '5', tif='gtc')
                session.thread.start()
                session.thread.join(2)
                saved = store.get(str(index))
                self.assertEqual(saved['status'], 'finished')
                fills = store.fills(str(index))
                self.assertEqual({f['liquidity'] for f in fills}, {'maker', 'taker'})
                results.append(
                    (
                        saved['account'],
                        fills,
                        store.order_events(str(index), saved['order_event_count'] + 1),
                    )
                )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])


if __name__ == '__main__':
    unittest.main()
