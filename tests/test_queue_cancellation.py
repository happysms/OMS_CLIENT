"""Shared queue conservation, tape reconciliation, stochastic distribution and replay."""
from copy import deepcopy
from decimal import Decimal as D
import tempfile
import unittest

from test_replay import config, book, tick
from oms_client.replay.engine import ReplayEngine, config_from
from oms_client.replay.queue_cancel import QueueCancellation
from oms_client.replay.service import ReplayManager
from oms_client.market_data.validation import DataError


class QueueAllocationTests(unittest.TestCase):
    def nodes(self):
        return [dict(kind='external',qty=D(30)),dict(kind='own',id='a'),dict(kind='external',qty=D(70))]

    def test_quantity_probability_bias_and_empirical_draws(self):
        for bias,expected in [('1',.3),('4',120/190),('0.25',7.5/77.5)]:
            front=0
            for seed in range(1000):
                model=QueueCancellation(dict(queue_cancel_mode='probabilistic',queue_cancel_bias=bias,
                                             queue_cancel_seed=seed,queue_cancel_chunks=1))
                nodes=self.nodes()
                self.assertAlmostEqual(model.position(nodes,'a')['queue_front_cancel_probability'],expected)
                self.assertEqual(model.cancel(nodes,D(1)),1)
                front+=30-model.position(nodes,'a')['queue_external_ahead']
                self.assertEqual(sum(n['qty'] for n in nodes if n['kind']=='external'),99)
                self.assertEqual([n['id'] for n in nodes if n['kind']=='own'],['a'])
            self.assertLess(abs(front/1000-expected),.05)

    def test_seed_reproducibility_packet_count_and_full_cancellation(self):
        results=[]
        for seed in [5,5,6]:
            m=QueueCancellation(dict(queue_cancel_mode='probabilistic',queue_cancel_seed=seed,queue_cancel_chunks=32))
            nodes=self.nodes();m.cancel(nodes,D(20));results.append(deepcopy(nodes))
            self.assertEqual(m.draws,32)
            self.assertEqual(m.cancel(nodes,D(999)),80)
            self.assertEqual(nodes,[dict(kind='own',id='a')])
            self.assertIsNone(m.position(nodes,'a')['queue_front_cancel_probability'])
        self.assertEqual(results[0],results[1]);self.assertNotEqual(results[0],results[2])

    def test_packet_shortage_spills_and_multiple_own_orders_share_one_draw(self):
        m=QueueCancellation(dict(queue_cancel_mode='probabilistic',queue_cancel_chunks=1))
        nodes=[dict(kind='external',qty=D(1)),dict(kind='own',id='a'),
               dict(kind='external',qty=D(2)),dict(kind='own',id='b'),dict(kind='external',qty=D(3))]
        self.assertEqual(m.position(nodes,'a')['queue_front_cancel_probability'],1/6)
        self.assertEqual(m.position(nodes,'b')['queue_front_cancel_probability'],.5)
        self.assertEqual(m.cancel(nodes,D(5)),5)
        self.assertEqual(sum(n['qty'] for n in nodes if n['kind']=='external'),1)
        self.assertEqual([n['id'] for n in nodes if n['kind']=='own'],['a','b'])
        self.assertGreaterEqual(m.draws,2)


class QueueEngineTests(unittest.TestCase):
    def engine(self,quantity=30,**changes):
        cfg=config(queue_cancel_mode='probabilistic',**changes)
        e=ReplayEngine(cfg);e.on_event(book(e.now,quantity=quantity));e.start()
        return e

    def order(self,e,side='buy',price='100',quantity='1'):
        identifier=e.place(side,price,quantity);e.advance(e.now)
        return identifier

    def public(self,e,identifier):
        return next(o for o in e.snapshot()['orders'] if o['id']==identifier)

    def external(self,e,side='buy',price=100):
        return sum((n['qty'] for n in e.queues[(side,D(price))] if n['kind']=='external'),D(0))

    def test_additions_go_behind_then_tickless_cancel_changes_position_without_fill(self):
        e=self.engine();t=e.now;a=self.order(e)
        e.on_event(book(t+1,quantity=100))
        p=self.public(e,a)
        self.assertEqual((p['queue_external_ahead'],p['queue_external_behind']),(30,70))
        self.assertEqual(p['queue_front_cancel_probability'],.3)
        funds=e.free();e.on_event(book(t+2,quantity=80))
        p=self.public(e,a)
        self.assertLess(p['queue_external_ahead'],30);self.assertLess(p['queue_external_behind'],70)
        self.assertEqual(self.external(e),80)
        self.assertEqual(e.fill_count,0);self.assertEqual(e.free(),funds)
        report=e.snapshot()['queue_cancel_events'][0]
        self.assertEqual(D(report['applied_cancel_quantity']),20)
        self.assertEqual(report['positions_before'][a]['queue_front_cancel_probability'],.3)

    def test_tape_and_virtual_consumption_not_double_counted(self):
        e=self.engine(quantity=10);t=e.now;a=self.order(e)
        e.on_event(tick(t+1,100,-4));e.on_event(book(t+2,quantity=3))
        report=e.snapshot()['queue_cancel_events'][0]
        self.assertEqual(D(report['trade_quantity']),4)
        self.assertEqual(D(report['inferred_cancel_quantity']),3)
        self.assertEqual(self.public(e,a)['queue_external_ahead'],3)
        self.assertEqual(e.fill_count,0)
        # Same size after a trade implies net replenishment at the tail.
        e.on_event(tick(t+3,100,-2));e.on_event(book(t+4,quantity=3))
        self.assertEqual(self.public(e,a)['queue_external_ahead'],1)
        self.assertEqual(self.public(e,a)['queue_external_behind'],2)
        self.assertEqual(e.queue_cancel.count,1)

    def test_no_prior_order_tick_still_reduces_initial_queue(self):
        e=self.engine(quantity=10);t=e.now
        e.on_event(tick(t+1,100,-4));a=self.order(e)
        self.assertEqual(self.public(e,a)['queue_external_ahead'],6)
        e.on_event(book(t+2,quantity=6))
        self.assertEqual(e.queue_cancel.count,0);self.assertEqual(self.external(e),6)

    def test_shared_multi_order_fifo_and_one_cancel_budget(self):
        e=self.engine(quantity=10);t=e.now;a=self.order(e)
        e.on_event(book(t+1,quantity=30));b=self.order(e)
        e.on_event(book(t+2,quantity=60))
        self.assertEqual(self.public(e,a)['queue_external_behind'],50)
        self.assertEqual(self.public(e,b)['queue_external_ahead'],30)
        self.assertEqual(self.public(e,b)['queue_ahead'],31)
        e.on_event(book(t+3,quantity=48))
        self.assertEqual(self.external(e),48);self.assertEqual(e.queue_cancel.cancelled,12)
        self.assertEqual(e.orders[a].remaining,1);self.assertEqual(e.orders[b].remaining,1)
        e.on_event(tick(t+4,100,-50))
        self.assertEqual([f['order_id'] for f in e.last_fills],[a,b])
        self.assertEqual(e.coin,2)

    def test_missing_or_outside_depth_does_not_cancel_and_return_rebases_ahead(self):
        e=self.engine(quantity=10);t=e.now;a=self.order(e)
        e.on_event(book(t+1,bid=90,ask=92,quantity=1))
        p=self.public(e,a);self.assertFalse(p['queue_known']);self.assertIsNone(p['queue_external_ahead'])
        e.on_event(tick(t+2,100,-100));self.assertEqual(e.fill_count,0)
        e.on_event(book(t+3,quantity=5))
        self.assertEqual(self.public(e,a)['queue_external_ahead'],5)
        missing=book(t+4,quantity=1);missing['bids'][0]['quantity']=None
        e.on_event(missing);self.assertFalse(self.public(e,a)['queue_known'])
        e.on_event(book(t+5,quantity=7));self.assertEqual(self.public(e,a)['queue_external_ahead'],7)
        self.assertEqual(e.queue_cancel.count,0)

    def test_zero_external_quantity_requires_later_tick_and_fill_freezes_model(self):
        e=self.engine(quantity=10);t=e.now;a=self.order(e)
        e.on_event(book(t+1,quantity=0))
        self.assertEqual(self.public(e,a)['queue_external_ahead'],0);self.assertEqual(e.fill_count,0)
        e.on_event(tick(t+2,100,-1));self.assertEqual(e.fill_count,1)
        frozen=deepcopy(e.last_fills[0]);self.assertEqual(D(frozen['queue_model']['cancelled_external_quantity']),10)
        self.assertEqual(frozen['queue_position']['queue_external_ahead'],0)
        e.on_event(book(t+3,quantity=100));self.assertEqual(e.last_fills[0],frozen)

    def test_sell_queue_and_cancel_pending_order_remain_owned(self):
        e=self.engine(quantity=10,initial_coin='2',latency_ms=20);t=e.now
        a=e.place('sell','102','1');e.advance(t+20);e.cancel(a)
        e.on_event(book(t+21,quantity=0))
        self.assertEqual(e.orders[a].status,'cancel_pending');self.assertEqual(e.free()[1],1)
        e.on_event(tick(t+22,102,1));self.assertEqual(e.coin,1)
        e.advance(t+40);self.assertEqual(e.free()[1],1)

    def test_own_fills_reconcile_historical_external_volume_without_false_cancels(self):
        e=self.engine(quantity=2);t=e.now;a=self.order(e);b=self.order(e)
        e.on_event(book(t+1,quantity=8));e.on_event(tick(t+2,100,-3))
        self.assertNotIn(a,e.orders);self.assertEqual(e.coin,1)
        e.on_event(book(t+3,quantity=5))
        self.assertEqual(self.external(e),5);self.assertEqual(e.queue_cancel.count,0)
        self.assertEqual(self.public(e,b)['queue_external_behind'],5)

    def test_compaction_bounds_segments_after_many_books(self):
        e=self.engine(quantity=10);t=e.now;self.order(e)
        for i in range(1,500):e.on_event(book(t+i,quantity=10+i))
        self.assertEqual(len(e.queues[('buy',D(100))]),3)

    def test_configuration_validation_and_missing_legacy_mode(self):
        for change in ({'queue_cancel_mode':'x'},{'queue_cancel_bias':0},{'queue_cancel_bias':'nan'},
                       {'queue_cancel_chunks':True},{'queue_cancel_chunks':257},{'queue_cancel_seed':1.5},
                       {'queue_cancel_seed':-1},{'queue_cancel_seed':4294967296}):
            with self.subTest(change=change),self.assertRaises(DataError):config(**change)
        cfg=config();cfg.pop('queue_cancel_mode')
        self.assertFalse(ReplayEngine(cfg).queue_cancel.enabled)


class QueueSessionTests(unittest.TestCase):
    def test_batch_1x_32x_and_saved_account_retain_same_draws_fills_and_pnl(self):
        class Source:
            def __init__(self,cfg):self.config=cfg;self.total=6;self.skipped=0;self.manifest=[]
            def load(self):
                t=self.config['start_ms']
                return iter([book(t-1,quantity=30),book(t+10,quantity=100),book(t+20,quantity=50),
                             tick(t+30,100,-60),tick(t+40,102,70),book(t+50,quantity=0)])
            def verify(self):pass
            def close(self):pass
        results=[]
        with tempfile.TemporaryDirectory() as temp:
            manager=ReplayManager(temp,Source)
            try:
                for pace in ({'fast':True},{'speed':1},{'speed':32}):
                    run=manager.create(dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.100+09:00',
                        initial_krw='1000',min_notional='1',quantity_step='1',fee_bps='0',latency_ms=0,
                        queue_cancel_seed=73,queue_cancel_bias='1.5',**pace))
                    manager.active.thread.join(2);saved=manager.store.get(run['id'])
                    self.assertEqual(saved['status'],'finished');self.assertGreater(saved['fill_count'],0)
                    self.assertGreater(saved['queue_model']['random_draws'],0)
                    self.assertEqual(saved['initial_config']['queue_cancel_seed'],73)
                    results.append((saved['queue_model'],saved['queue_cancel_events'],saved['account'],manager.store.fills(run['id'])))
            finally:manager.close()
        self.assertEqual(results[0],results[1]);self.assertEqual(results[0],results[2])


if __name__=='__main__':unittest.main()
