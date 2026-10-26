"""Correctness boundaries for cached projections and optimized replay hot paths."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from oms_client.replay.orders import ReplayOrder
from oms_client.replay.queue_cancel import QueueCancellation
from oms_client.replay.service import ReplaySession
from oms_client.replay.source import ReplaySource
from oms_client.replay.store import ReplayStore
from oms_client.replay.strategy_view import StrategyOrderViews
from oms_client.strategies.simple_mm import MMConfig, SimpleMM
from test_replay import SyntheticSource, book, config, tick


def pending():
    return ReplayOrder('O1', 'manual', 'buy', D('100'), D('2'), D('2'), D('200'), 0)


class StrategyProjectionTests(unittest.TestCase):
    def test_reuses_immutable_view_and_refreshes_every_changed_input(self):
        projection = StrategyOrderViews()
        order = pending()
        current, = projection.project([order])
        self.assertIs(projection.project([order])[0], current)
        for field, value in [('owner', 'mm'), ('side', 'sell'), ('price', D('101')),
                             ('remaining', D('1')), ('status', 'live'), ('id', 'O2')]:
            with self.subTest(field=field):
                setattr(order, field, value)
                changed, = projection.project([order])
                self.assertIsNot(changed, current)
                self.assertEqual((changed.order_id, changed.strategy_id, changed.side,
                                  changed.price, changed.remaining_qty, changed.phase),
                                 (order.id, order.owner, order.side, order.price, order.remaining, order.status))
                current = changed
        order.status = 'execution_unknown'
        with self.assertRaises(ValueError):
            projection.project([order])

    def test_cache_is_bounded_and_accounts_do_not_share_views(self):
        first, second = StrategyOrderViews(), StrategyOrderViews()
        order = pending()
        original = first.project([order])[0]
        self.assertIsNot(second.project([order])[0], original)
        first.project([])
        self.assertIsNot(first.project([order])[0], original)
        for index in range(1000):
            order.id = str(index)
            first.project([order])
        self.assertEqual(len(first._views), 1)

    def test_gc_still_removes_terminal_order_after_finish_without_due_timer(self):
        from oms_client.replay.engine import ReplayEngine
        engine = ReplayEngine(config())
        engine.on_event(book(engine.now, quantity=0))
        identifier = engine.place('buy', '100', '2')
        engine.advance(engine.now)
        engine.finish()
        self.assertTrue(engine.order_book.collection_pending)
        engine.advance(engine.now)
        self.assertNotIn(identifier, engine.orders)
        self.assertFalse(engine.queues)
        self.assertFalse(engine.order_book.collection_pending)


class SnapshotIsolationTests(unittest.TestCase):
    def test_reads_remain_frozen_until_commit_and_callers_cannot_mutate_them(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ReplayStore(temp)
            session = ReplaySession('read', config(), store, SyntheticSource)
            session.engine.on_event(book(session.engine.now, quantity=0))
            session.engine.place('buy', '100', '2')
            session.engine.advance(session.engine.now)
            session._publish()
            committed = session.get()
            response = session.get()
            response['account']['cash'] = -1
            response['orders'][0]['remaining'] = -1
            response['books']['bithumb']['bids'][0]['price'] = -1
            self.assertEqual(session.get(), committed)
            session.engine.on_event(tick(session.engine.now + 1, 100, -1))
            self.assertEqual(session.get(), committed)
            with patch.object(store, 'save', side_effect=RuntimeError('disk failure')):
                with self.assertRaises(RuntimeError):
                    session._publish()
            self.assertEqual(session.get(), committed)
            self.assertTrue(session.engine.new_fills)
            session._publish()
            self.assertEqual(session.get(), store.get('read'))
            self.assertEqual(session.get()['fill_count'], 1)
            self.assertFalse(session.engine.new_fills)

    def test_worker_failure_exposes_last_committed_account_with_failed_status(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ReplayStore(temp)
            session = ReplaySession('failed', config(), store, SyntheticSource)
            before = session.get()
            with patch.object(store, 'save', side_effect=RuntimeError('disk failure')):
                with self.assertLogs('oms_client.replay.service', level='ERROR'):
                    session._run()
            result = session.get()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['account'], before['account'])
            self.assertEqual(result['fills'], before['fills'])
            self.assertIn('마지막 저장 결과', result['error'])


class ExactSizingTests(unittest.TestCase):
    def test_fast_upper_bound_and_rounding_fallback_are_maximal_without_overspending(self):
        rng = random.Random(41)
        cfg = MMConfig('XYZ', D('1'), D('1'), D('1'), D('5'), D('10'), D('0.00000001'))
        for index in range(500):
            with self.subTest(index=index), localcontext() as context:
                context.prec = 160
                step = rng.choice([D('1'), D('.01'), D('.00000001')])
                fee = rng.choice([D('0'), D('0.1'), D('10'), D('1000'), D('-1')])
                quantum = rng.choice([D('1'), D('.01'), D('.00000001')])
                price = D(rng.randrange(1, 20000)) / 100
                cash = D(rng.randrange(0, 1000000)) / 100
                strategy = SimpleMM(replace(cfg, quantity_step=step, maker_fee_bps=fee,
                                            taker_fee_bps=fee, fee_quantum=quantum))
                quantity, reserve = strategy._buy_size(cash, price)

                def required(size):
                    notional = Fraction(size) * Fraction(price)
                    units = notional * max(Fraction(0), Fraction(fee)) / 10000 / Fraction(quantum)
                    rounded_units = (units.numerator + units.denominator - 1) // units.denominator
                    return notional + rounded_units * Fraction(quantum)

                self.assertEqual(quantity % step, 0)
                self.assertEqual(Fraction(reserve), required(quantity))
                self.assertLessEqual(required(quantity), Fraction(cash))
                self.assertGreater(required(quantity + step), Fraction(cash))


class SourceLayoutTests(unittest.TestCase):
    def test_five_level_column_layout_and_source_ids_across_native_files(self):
        import duckdb
        cfg = config()
        with tempfile.TemporaryDirectory() as temp:
            paths = []
            con = duckdb.connect()
            try:
                for index in range(2):
                    fields = [f"{cfg['start_ms']} AS desktopTime", f"{cfg['start_ms']} AS serverTime"]
                    for level in range(1, 6):
                        suffix = '' if level == 1 else f'_{level}'
                        fields += [f'{101-level} AS b{level}_p', f'{101+level} AS a{level}_p',
                                   f'{level+index} AS bid_size{suffix}', f'{10+level+index} AS ask_size{suffix}']
                    path = str(Path(temp) / f'{index}.parquet')
                    con.sql('SELECT ' + ','.join(fields)).write_parquet(path)
                    paths.append(path)
            finally:
                con.close()
            source = ReplaySource(cfg)
            originals = ['first-original.parquet', 'second-original.parquet']
            try:
                events = list(source._stream('bithumb', 'book', paths, True, originals))
                for index, event in enumerate(events):
                    self.assertEqual([v['quantity'] for v in event['bids']], list(range(1+index, 6+index)))
                    self.assertEqual([v['quantity'] for v in event['asks']], list(range(11+index, 16+index)))
                    file_hash = hashlib.sha256(originals[index].encode()).hexdigest()[:12]
                    self.assertEqual(event['source_id'], f'bithumb:book:{file_hash}:0')
                    self.assertEqual(event['_key'][-2:], (index, 0))
                json.dumps(events, allow_nan=False)
                self.assertEqual(len(events), 2)
            finally:
                source.close()


class QueueProjectionTests(unittest.TestCase):
    def test_batch_positions_preserve_rounding_and_refresh_after_cancellation(self):
        for bias in ('0.01', '1', '100'):
            with self.subTest(bias=bias):
                model = QueueCancellation(dict(queue_cancel_mode='probabilistic', queue_cancel_bias=bias))
                nodes = [dict(kind='external', qty=D('1.123456789012345678901234567')),
                         dict(kind='own', id='A'), dict(kind='external', qty=D('1e-27')),
                         dict(kind='own', id='B'), dict(kind='external', qty=D('3.333333333333333333333333333'))]
                before = {identifier: model.position(nodes, identifier) for identifier in ('A', 'B', 'missing')}
                self.assertEqual(model.positions(nodes, before), before)
                model.cancel(nodes, D('0.25'))
                after = {identifier: model.position(nodes, identifier) for identifier in before}
                self.assertEqual(model.positions(nodes, before), after)
                self.assertNotEqual(before, after)


if __name__ == '__main__':
    unittest.main()
