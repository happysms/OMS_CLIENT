from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.strategies.simple_mm import (
    AccountView, MMConfig, QuoteView, SimpleMM, StartInputs, WorkingOrder,
)


class SimpleMMTests(unittest.TestCase):
    def setUp(self):
        # Synthetic instrument rules, not current Bithumb market specifications.
        self.config = MMConfig('XYZ', D('1'), D('1'), D('10'),
                               D('10'), D('20'), D('0.01'))
        self.strategy = SimpleMM(self.config)
        self.quote = QuoteView('XYZ', 'book-1', D('100'), D('102'), 1000)
        self.account = AccountView('run-bithumb', 'account-1', D('1000'), D('0'))

    def order(self, identifier='buy-1', side='buy', price='100', quantity='5', **kw):
        return WorkingOrder(identifier, kw.get('strategy_id', 'bithumb_simple_mm'), side,
                            D(price), D(quantity), kw.get('phase', 'live'))

    def plan(self, account=None, quote=None, orders=(), now=1000):
        return self.strategy.plan(quote or self.quote, account or self.account,
                                  orders, now_ms=now)

    def test_start_inputs_are_editable_and_ticker_is_required(self):
        defaults = StartInputs.from_payload({'ticker': ' xyz '})
        self.assertEqual((defaults.ticker, defaults.initial_krw, defaults.initial_coin),
                         ('XYZ', D('1000000'), D('0')))
        selected = StartInputs.from_payload({'ticker': 'ABC', 'initial_krw': '250000',
                                            'initial_coin': '12.5'})
        seed = selected.account_seed('My simulation')
        state = seed['venue_configs'][0]['initial_state']
        self.assertEqual(state['balances'], [{'currency': 'KRW', 'total': '250000'},
                                            {'currency': 'ABC', 'total': '12.5'}])
        self.assertEqual(state['spot_cost_basis'][0]['cost_policy'], 'start_mark')
        self.assertNotIn('qty', state['spot_cost_basis'][0])
        for payload in ({}, {'ticker': 'KRW'}, {'ticker': 'XYZ', 'initial_krw': -1},
                        {'ticker': 'XYZ', 'initial_coin': True},
                        {'ticker': 'XYZ', 'initial_krw': 'NaN'},
                        {'ticker': 'XYZ', 'initial_krw': '0E+1000'},
                        {'ticker': 'XYZ', 'unexpected': 1}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                StartInputs.from_payload(payload)

    def test_cash_only_places_bid_not_ask_and_includes_taker_fee_buffer(self):
        plan = self.plan()
        self.assertEqual(len(plan.new_orders), 1)
        order = plan.new_orders[0]
        self.assertEqual((order.side, order.price, order.quantity, order.tif),
                         ('buy', D('100'), D('9'), 'gtc'))
        self.assertEqual(order.reserve_krw, D('901.8'))
        self.assertLessEqual(order.reserve_krw, self.account.free_krw)

    def test_coin_only_places_ask_and_floors_quantity(self):
        plan = self.plan(account=replace(self.account, free_krw=D('0'), free_coin=D('2.8')))
        order, = plan.new_orders
        self.assertEqual((order.side, order.price, order.quantity, order.reserve_coin),
                         ('sell', D('102'), D('2'), D('2')))

    def test_both_balances_quote_both_sides_and_empty_account_quotes_neither(self):
        plan = self.plan(account=replace(self.account, free_coin=D('3')))
        self.assertEqual([o.side for o in plan.new_orders], ['buy', 'sell'])
        self.assertFalse(self.plan(account=replace(self.account, free_krw=D('0'))).new_orders)

    def test_fee_rounding_does_not_overspend_at_boundary(self):
        config = replace(self.config, maker_fee_bps=D('0.1'), taker_fee_bps=D('0.1'),
                         fee_quantum=D('1'))
        strategy = SimpleMM(config)
        for budget, expected in [('100.5', '0'), ('101', '1'), ('201', '2')]:
            with self.subTest(budget=budget):
                plan = strategy.plan(self.quote, replace(self.account, free_krw=D(budget)), (), now_ms=1000)
                self.assertEqual(sum((o.quantity for o in plan.new_orders), D(0)), D(expected))
                self.assertTrue(all(o.reserve_krw <= D(budget) for o in plan.new_orders))

    def test_fee_rebates_do_not_increase_spendable_cash(self):
        strategy = SimpleMM(replace(self.config, maker_fee_bps=D('-1'), taker_fee_bps=D('-2')))
        order, = strategy.plan(self.quote, replace(self.account, free_krw=D('100')), (), now_ms=1000).new_orders
        self.assertEqual((order.quantity, order.reserve_krw), (D('1'), D('100')))

    def test_subminimum_new_order_waits_without_canceling_small_valid_remainder(self):
        config = replace(self.config, quantity_step=D('0.01'), min_qty=D('0.01'), min_notional=D('10'))
        strategy = SimpleMM(config)
        tiny = self.order(quantity='0.01')
        plan = strategy.plan(self.quote, replace(self.account, free_krw=D('1')), (tiny,), now_ms=1000)
        self.assertEqual(plan.keep_order_ids, ('buy-1',))
        self.assertFalse(plan.new_orders)
        self.assertFalse(plan.cancel_order_ids)

    def test_locked_orders_are_kept_with_no_free_balance(self):
        plan = self.plan(account=replace(self.account, free_krw=D('0')),
                         orders=(self.order(), self.order('buy-2', quantity='2')))
        self.assertEqual(plan.keep_order_ids, ('buy-1', 'buy-2'))
        self.assertFalse(plan.new_orders)
        self.assertFalse(plan.cancel_order_ids)

    def test_same_price_order_is_kept_and_only_new_free_cash_is_added(self):
        plan = self.plan(orders=(self.order(quantity='50'),))
        self.assertEqual(plan.keep_order_ids, ('buy-1',))
        self.assertEqual(plan.new_orders[0].quantity, D('9'))
        self.assertFalse(plan.cancel_order_ids)

    def test_changed_price_cancels_only_that_side_then_waits(self):
        sell = self.order('sell-1', 'sell', '102')
        quote = replace(self.quote, bid=D('101'))
        plan = self.plan(quote=quote, orders=(self.order(), sell))
        self.assertEqual(plan.cancel_order_ids, ('buy-1',))
        self.assertEqual(plan.keep_order_ids, ('sell-1',))
        self.assertFalse(any(o.side == 'buy' for o in plan.new_orders))

    def test_cancel_pending_and_in_flight_do_not_duplicate_or_recancel(self):
        for phase in ('cancel_pending', 'in_flight'):
            with self.subTest(phase=phase):
                plan = self.plan(orders=(self.order(phase=phase),))
                self.assertFalse(plan.cancel_order_ids)
                self.assertFalse(plan.new_orders)

    def test_after_confirmed_cancel_the_new_snapshot_can_quote_again(self):
        waiting = self.plan(orders=(self.order(price='99', phase='cancel_pending'),))
        self.assertFalse(waiting.new_orders)
        ready = self.plan(account=replace(self.account, version='account-2'), orders=())
        self.assertEqual(ready.new_orders[0].price, D('100'))

    def test_opposite_order_pending_cancel_blocks_self_cross_even_for_other_owner(self):
        quote = replace(self.quote, bid=D('102'), ask=D('103'))
        for owner in ('bithumb_simple_mm', 'other_strategy'):
            for phase in ('live', 'in_flight', 'cancel_pending'):
                with self.subTest(owner=owner, phase=phase):
                    order = self.order('old-sell', 'sell', '101', strategy_id=owner, phase=phase)
                    plan = self.plan(quote=quote, orders=(order,))
                    self.assertFalse(any(o.side == 'buy' for o in plan.new_orders))
                    if owner == 'other_strategy':
                        self.assertFalse(plan.cancel_order_ids)

    def test_self_cross_guard_is_symmetric(self):
        order = self.order(price='103', strategy_id='other_strategy')
        plan = self.plan(account=replace(self.account, free_krw=D('0'), free_coin=D('5')),
                         orders=(order,))
        self.assertFalse(plan.new_orders)
        self.assertFalse(plan.cancel_order_ids)

    def test_stale_future_locked_and_crossed_books_cancel_only_owned_live(self):
        own, foreign = self.order(), self.order('foreign', strategy_id='other')
        for quote in (replace(self.quote, desktop_time_ms=0),
                      replace(self.quote, desktop_time_ms=2002),
                      replace(self.quote, ask=D('100')),
                      replace(self.quote, ask=D('99'))):
            with self.subTest(quote=quote):
                plan = self.plan(quote=quote, orders=(own, foreign), now=2001)
                self.assertEqual(plan.cancel_order_ids, ('buy-1',))
                self.assertFalse(plan.new_orders)

    def test_unready_or_unknown_account_does_not_release_or_trade(self):
        for account in (replace(self.account, synchronized=False),
                        replace(self.account, execution_known=False)):
            plan = self.plan(account=account, orders=(self.order(),))
            self.assertFalse(plan.new_orders)
            self.assertFalse(plan.cancel_order_ids)
            self.assertIn('account_not_ready', plan.reasons)

    def test_input_scope_and_duplicate_order_ids_are_rejected(self):
        for quote in (replace(self.quote, ticker='ABC'), replace(self.quote, includes_own_orders=True)):
            with self.assertRaises(ValueError):
                self.plan(quote=quote)
        with self.assertRaises(ValueError):
            self.plan(orders=(self.order(), self.order()))

    def test_same_versions_produce_stable_intents_and_new_account_version_changes_id(self):
        first = self.plan()
        self.assertEqual(first, self.plan(now=1100))
        second = self.plan(account=replace(self.account, version='account-2'))
        self.assertNotEqual(first.new_orders[0].client_order_id, second.new_orders[0].client_order_id)
        self.assertLessEqual(len(first.new_orders[0].client_order_id), 36)


if __name__ == '__main__':
    unittest.main()
