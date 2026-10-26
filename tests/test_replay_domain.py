"""Domain boundaries: settlement invariants, state ownership and deterministic time."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal as D
import unittest

from oms_client.market_data.validation import DataError
from oms_client.replay.ledger import FeeSchedule, SpotLedger
from oms_client.replay.limit_execution import ExecutionDepth, LimitOrderPlanner
from oms_client.replay.orders import OrderBook, ReplayOrder, ReplacementRequest
from oms_client.replay.scheduler import EventScheduler


def order(side='buy', quantity='2', reserve='200', **kwargs):
    return ReplayOrder('O1', 'manual', side, D('100'), D(quantity), D(quantity),
                       D(reserve), 0, **kwargs)


class LedgerTests(unittest.TestCase):
    def test_buy_sell_round_trip_reconciles_cost_fees_and_realized_pnl(self):
        fees = FeeSchedule(D('5'), D('10'))
        ledger = SpotLedger(D('1000'), D('0'), fees)
        ledger.initialize(D('99'))
        buy = order(reserve=str(fees.reserve_buy(D('100'), D('2'), 'gtc')), tif='gtc')
        ledger.settle(buy, D('1'), D('99'), 'taker')
        ledger.settle(buy, D('1'), D('100'), 'maker')
        self.assertEqual((ledger.cash, ledger.coin, ledger.cost), (D('800.851'), D('2'), D('199.149')))
        self.assertEqual(buy.fees, D('0.149'))
        self.assertEqual(buy.reserve_cash, 0)
        sell = order('sell', reserve='0')
        first = ledger.settle(sell, D('1'), D('110'), 'maker')
        second = ledger.settle(sell, D('1'), D('105'), 'taker')
        self.assertEqual(first.realized_pnl, D('10.3705'))
        self.assertEqual(second.realized_pnl, D('5.3205'))
        self.assertEqual((ledger.coin, ledger.cost), (0, 0))
        self.assertEqual(ledger.fees, D('0.309'))
        self.assertEqual(ledger.realized, D('15.691'))
        self.assertEqual(ledger.cash - ledger.initial_equity, ledger.realized)

    def test_rejected_settlement_preserves_both_account_and_order(self):
        scenarios = [
            ('oversize', {}, '3', '100', 'maker'),
            ('zero', {}, '0', '100', 'maker'),
            ('limit', {}, '1', '101', 'maker'),
            ('reserve', {'reserve': '50'}, '1', '100', 'maker'),
            ('unknown', {'status': 'execution_unknown'}, '1', '100', 'maker'),
            ('terminal', {'status': 'cancelled'}, '1', '100', 'maker'),
            ('role', {}, '1', '100', 'invalid'),
            ('fee_mismatch', {'maker_fees': D('1')}, '1', '100', 'maker'),
            ('sell_balance', {'side': 'sell', 'reserve': '0'}, '1', '100', 'maker'),
        ]
        for name, changes, quantity, price, role in scenarios:
            with self.subTest(name=name):
                ledger = SpotLedger(D('1000'), D('0'), FeeSchedule(D('0'), D('0')))
                pending = order(**changes)
                before = deepcopy((ledger, pending))
                with self.assertRaises((DataError, ValueError)):
                    ledger.settle(pending, D(quantity), D(price), role)
                self.assertEqual((ledger, pending), before)

    def test_unknown_and_pending_cancel_keep_reserves_until_terminal(self):
        ledger = SpotLedger(D('1000'), D('5'), FeeSchedule(D('0'), D('0')))
        buy, sell = order(), order('sell', reserve='0')
        buy.mark_unknown('missing_depth', 1)
        sell.request_cancel(wait_for_ack=True)
        self.assertEqual(ledger.available([buy, sell]), (D('800'), D('3')))
        sell.finish('cancelled', 'requested', 2)
        self.assertEqual(ledger.available([buy, sell]), (D('800'), D('5')))

    def test_partial_fills_charge_same_role_cumulative_ceiling_once(self):
        fees = FeeSchedule(D('1'), D('1'))
        ledger = SpotLedger(D('10'), D('0'), fees)
        buy = order(quantity='0.00000003', reserve='0.00000301')
        for _ in range(3):
            ledger.settle(buy, D('0.00000001'), D('100'), 'maker')
        self.assertEqual(buy.fees, D('0.00000001'))
        self.assertEqual(ledger.cash, D('9.99999699'))

    def test_accounts_have_independent_balances_and_initial_cost_is_set_once(self):
        fees = FeeSchedule(D('0'), D('0'))
        first, second = (SpotLedger(D('1000'), D('2'), fees) for _ in range(2))
        first.initialize(D('99'))
        first.initialize(D('200'))
        second.initialize(D('99'))
        before = deepcopy(second)
        first.settle(order(), D('1'), D('100'), 'maker')
        self.assertEqual(second, before)
        self.assertEqual(first.initial_equity, D('1198'))
        self.assertEqual(first.cost, D('298'))


class OrderTests(unittest.TestCase):
    def test_cancel_before_ack_still_allows_fill_until_cancel_confirmation(self):
        pending = order()
        self.assertTrue(pending.request_cancel(wait_for_ack=True))
        self.assertFalse(pending.request_cancel(wait_for_ack=True))
        self.assertEqual(pending.quantity_state()['in_flight'], 2)
        self.assertTrue(pending.arrive(20))
        ledger = SpotLedger(D('1000'), D('0'), FeeSchedule(D('0'), D('0')))
        ledger.settle(pending, D('1'), D('100'), 'maker')
        pending.finish('cancelled', 'requested', 40)
        self.assertEqual(pending.quantity_state(), dict(
            filled=1, in_flight=0, live=0, cancelled=1, rejected=0, unknown=0))
        self.assertEqual(pending.reserve_cash, 0)
        with self.assertRaises(ValueError):
            pending.arrive(50)

    def test_identity_and_completion_survive_completed_order_collection(self):
        book = OrderBook()
        pending = book.submit(side='buy', price=D('100'), quantity=D('2'), owner='manual',
                              tif='gtc', stp='cancel_taker', expires_at_ms=None,
                              client_order_id='request-1', reserve_cash=D('200'), now=0,
                              fingerprint=('buy', '100', '2'))
        with self.assertRaises(TypeError):
            book.orders[pending.id] = pending
        self.assertTrue(book.finish(pending, 'cancelled', 'requested', 20))
        self.assertFalse(book.finish(pending, 'cancelled', 'requested', 30))
        self.assertEqual(pending.closed_at, 20)
        self.assertEqual(book.collect_completed(), set())
        self.assertNotIn(pending.id, book.orders)
        self.assertTrue(book.completed(pending.id))
        self.assertEqual(book.retry('request-1', ('buy', '100', '2')), pending.id)
        with self.assertRaises(ValueError):
            book.retry('request-1', ('buy', '101', '2'))

    def test_replacement_is_immutable_and_dropped_if_old_order_fills(self):
        book = OrderBook()
        pending = book.submit(side='buy', price=D('100'), quantity=D('2'), owner='manual',
                              tif='gtc', stp='cancel_taker', expires_at_ms=None,
                              client_order_id=None, reserve_cash=D('200'), now=0, fingerprint=())
        request = ReplacementRequest(D('99'), D('3'), 'gtc')
        book.queue_replacement(pending.id, request)
        with self.assertRaises(FrozenInstanceError):
            request.total = D('5')
        ledger = SpotLedger(D('1000'), D('0'), FeeSchedule(D('0'), D('0')))
        ledger.settle(pending, D('2'), D('100'), 'maker')
        book.finish(pending, 'filled', 'filled', 20)
        self.assertIsNone(book.pop_replacement(pending.id))


class SchedulingAndPlanningTests(unittest.TestCase):
    def test_same_time_events_keep_fifo_even_when_dispatch_schedules_another(self):
        scheduler = EventScheduler()
        scheduler.schedule(20, 'activate', 'A')
        scheduler.schedule(10, 'stale', 0)
        scheduler.schedule(20, 'cancel', 'A')
        self.assertIsNone(scheduler.pop_due(9))
        self.assertEqual(scheduler.pop_due(10).kind, 'stale')
        self.assertEqual(scheduler.next_time(), 20)
        self.assertEqual(scheduler.pop_due(20).kind, 'activate')
        scheduler.schedule(20, 'expire', 'B')
        self.assertEqual(scheduler.pop_due(20).kind, 'cancel')
        self.assertEqual(scheduler.pop_due(20).kind, 'expire')
        self.assertIsNone(scheduler.next_time())
        self.assertIsNone(scheduler.pop_due(100))

    def test_fok_dry_run_with_stp_cannot_mutate_orders_or_depth(self):
        taker = order(quantity='3', tif='fok', stp='cancel_maker')
        maker = order('sell', reserve='0', status='live', active_at=0)
        maker.id = 'O2'
        market = {'asks': [{'price': 100, 'quantity': 2}],
                  'bids': [{'price': 99, 'quantity': 2}]}
        depth = ExecutionDepth()
        before = deepcopy((taker, maker, market, depth.self_debt, depth.public_debt))
        planner = LimitOrderPlanner(D('1'))
        plan = planner.plan(taker, market, depth, [taker, maker])
        self.assertEqual(plan.outcome, 'cancel')
        self.assertEqual(plan.reason, 'fok_unfilled')
        self.assertEqual(plan.fill_count, 1)
        self.assertEqual((taker, maker, market, depth.self_debt, depth.public_debt), before)
        self.assertEqual(planner.plan(taker, market, depth, [taker, maker]), plan)
        with self.assertRaises(FrozenInstanceError):
            plan.actions[0].order_id = 'different'


if __name__ == '__main__':
    unittest.main()
