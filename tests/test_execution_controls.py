"""Causal feed-delay admission, delayed cancellation and hedge timing contracts."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from oms_client.replay.order_guard import FeedDelayGuard
from oms_client.replay.hedge_timing import HedgeTimingModel, HedgeTimingTrace
from oms_client.replay.engine import ReplayEngine
from oms_client.replay.service import ReplayManager
from oms_client.strategies.registry import StrategyRegistry, validate, present, DEFAULT_ID
from test_replay import config, book, tick

T=1792983600000


def profile(**changes):
    params=dict(ping_guard_mode='enabled',max_feed_delay_ms=500,feed_delay_spike_ms=250,
                feed_delay_resume_ms=200,feed_delay_stable_ms=100)
    params.update(changes)
    return present('guard-test',1,validate(dict(name='보호 테스트',type='single_exchange_maker',parameters=params)))


def quote(at,delay=50):
    event=book(at,quantity=0);event['server_time']=at-delay if delay is not None else None
    return event


class GuardTests(unittest.TestCase):
    def test_absolute_limit_spike_and_hysteresis_require_fresh_observations(self):
        guard=FeedDelayGuard(profile()['parameters'])
        guard.observe(T,T-50,1000);self.assertTrue(guard.check(T,1000))
        guard.observe(T+10,T+10-300,1000)
        self.assertTrue(guard.blocked);self.assertEqual(guard.reason,'delay_spike')
        guard.observe(T+20,T+20-200,1000);self.assertEqual(guard.reason,'recovering')
        # Clock advancement alone cannot open admission without a confirming quote.
        self.assertFalse(guard.check(T+120,1000))
        guard.observe(T+120,T+120-200,1000);self.assertFalse(guard.blocked)
        guard.observe(T+130,T+130-501,1000);self.assertEqual(guard.reason,'delay_limit')
        guard.observe(T+140,T+140-250,1000);self.assertEqual(guard.reason,'above_resume')
        guard.observe(T+150,T+150-100,1000);self.assertEqual(guard.recovery_since,T+150)
        guard.observe(T+200,T+200-300,1000);self.assertIsNone(guard.recovery_since)
        self.assertTrue(guard.blocked)
        self.assertEqual(guard.episodes,2)

    def test_missing_negative_stale_and_disabled_legacy(self):
        guard=FeedDelayGuard(profile()['parameters'])
        guard.observe(T,None,1000);self.assertEqual(guard.reason,'missing_timestamp')
        guard.observe(T+1,T+2,1000);self.assertEqual(guard.reason,'negative_delay')
        guard.observe(T+10,T+10,1000);guard.observe(T+110,T+110,1000)
        self.assertTrue(guard.check(T+110,1000))
        self.assertFalse(guard.check(T+1111,1000));self.assertEqual(guard.reason,'stale_sample')
        guard.observe(T+1112,T+1112-50,1000);self.assertEqual(guard.reason,'recovering')
        guard.observe(T+2300,T+2300-50,1000)
        self.assertTrue(guard.blocked);self.assertEqual(guard.recovery_since,T+2300)
        for settings in ({},{'ping_guard_mode':'disabled'}):
            disabled=FeedDelayGuard(settings);disabled.observe(T,None,1000)
            self.assertTrue(disabled.check(T+99999,1000))

    def engine(self,**parameters):
        settings=config(auto_mm=False,latency_ms=20);settings['strategy']=profile(**parameters)
        engine=ReplayEngine(settings);engine.on_event(quote(engine.now));engine.start()
        return engine

    def test_new_orders_blocked_but_inflight_orders_can_arrive_and_fill_before_cancel(self):
        engine=self.engine();start=engine.now
        identifier=engine.place('buy','100','2')
        self.assertEqual(engine.free()[0],800)
        engine.on_event(quote(start+10,600))
        self.assertEqual(engine.orders[identifier]['status'],'cancel_pending')
        self.assertEqual(engine.free()[0],800)
        with self.assertRaisesRegex(ValueError,'시간차 보호'):engine.place('buy','100','1')
        self.assertEqual(engine.order_sequence,1)
        engine.advance(start+20)
        self.assertEqual(engine.orders[identifier]['active_at'],start+20)
        self.assertEqual(engine.free()[0],800)
        engine.on_event(tick(start+25,100,-1))
        self.assertEqual(engine.coin,1)
        engine.advance(start+30)
        self.assertNotIn(identifier,engine.orders);self.assertEqual(engine.free()[0],900)
        self.assertEqual(engine.order_log[-1]['reason'],'requested')
        self.assertEqual(engine.guard.blocked_submissions,1)
        engine.on_event(quote(start+30,50));engine.on_event(quote(start+130,50))
        self.assertFalse(engine.guard.blocked)
        engine.place('buy','100','1')

    def test_existing_order_can_fill_while_cancel_is_pending_and_report_keeps_guard(self):
        engine=self.engine();start=engine.now
        identifier=engine.place('buy','100','3');engine.advance(start+20)
        engine.on_event(quote(start+30,600))
        engine.on_event(tick(start+40,100,-1))
        self.assertEqual(engine.coin,1)
        self.assertEqual(engine.orders[identifier]['remaining'],2)
        self.assertEqual(engine.free()[0],700)
        frozen=deepcopy(engine.last_fills[0]);self.assertTrue(frozen['order_guard']['blocked'])
        engine.advance(start+50);self.assertEqual(engine.free()[0],900)
        engine.on_event(tick(start+51,100,-9));self.assertEqual(engine.coin,1)
        engine.on_event(quote(start+60,50));engine.on_event(quote(start+160,50))
        self.assertEqual(engine.last_fills[0],frozen)

    def test_keep_policy_retains_existing_orders_but_blocks_new_ones(self):
        engine=self.engine(ping_existing_orders='keep');start=engine.now
        identifier=engine.place('buy','100','2');engine.advance(start+20)
        engine.on_event(quote(start+30,600))
        self.assertEqual(engine.orders[identifier]['status'],'live')
        engine.cancel(identifier);self.assertEqual(engine.orders[identifier]['status'],'cancel_pending')
        with self.assertRaises(ValueError):engine.place('sell','102','1')

    def test_registry_validation_and_immutable_builtin_upgrade(self):
        for changes in ({'feed_delay_resume_ms':501},{'max_feed_delay_ms':True},{'ping_guard_mode':'maybe'},
                        {'feed_delay_stable_ms':-1}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):profile(**changes)
        with tempfile.TemporaryDirectory() as temp:
            registry=StrategyRegistry(temp)
            original=registry.get(DEFAULT_ID)
            raw={key:original[key] for key in ('name','type','description','parameters','specification','created_at_ms')}
            raw['parameters']={key:value for key,value in raw['parameters'].items() if key not in {
                'ping_guard_mode','max_feed_delay_ms','feed_delay_spike_ms','feed_delay_resume_ms','feed_delay_stable_ms','ping_existing_orders'}}
            import json
            with registry.connect() as con:
                con.execute('UPDATE strategy_revisions SET definition=? WHERE strategy_id=? AND revision=1',(json.dumps(raw),DEFAULT_ID))
            reopened=StrategyRegistry(temp)
            self.assertEqual(reopened.get(DEFAULT_ID)['revision'],2)
            self.assertEqual(reopened.get(DEFAULT_ID)['parameters']['ping_guard_mode'],'enabled')
            self.assertEqual(reopened.get(DEFAULT_ID,1)['parameters'],raw['parameters'])
            self.assertEqual(StrategyRegistry(temp).get(DEFAULT_ID)['revision'],2)

    def test_guard_economics_identical_in_batch_1x_and_32x(self):
        class Source:
            def __init__(self,cfg):self.config=cfg;self.total=6;self.skipped=0;self.manifest=[]
            def load(self):
                t=self.config['start_ms']
                return iter([quote(t-1),tick(t+30,100,-3),quote(t+40,600),quote(t+80),quote(t+180),tick(t+220,102,3)])
            def verify(self):pass
            def close(self):pass
        results=[]
        with tempfile.TemporaryDirectory() as temp:
            manager=ReplayManager(temp,Source)
            registered=manager.strategies.save(dict(name='핑 비교',type='single_exchange_maker',parameters={'feed_delay_stable_ms':100}))
            try:
                for pace in ({'fast':True},{'speed':1},{'speed':32}):
                    run=manager.create(dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.250+09:00',
                        initial_krw='1000',min_notional='1',quantity_step='1',fee_bps='0',latency_ms=20,
                        strategy_id=registered['id'],strategy_revision=1,**pace))
                    manager.active.thread.join(2);result=manager.get(run['id'])
                    self.assertEqual(result['status'],'finished');self.assertGreater(result['order_guard']['block_episodes'],0)
                    results.append((result['account'],manager.store.fills(run['id']),result['guard_events']))
            finally:manager.close()
        self.assertEqual(results[0],results[1]);self.assertEqual(results[0],results[2])


class HedgeTimingTests(unittest.TestCase):
    def test_stage_delays_ack_is_not_fill_and_partial_completion(self):
        model=HedgeTimingModel();plan=model.plan(T)
        self.assertEqual(plan['arrival_due_ms'],T+40);self.assertEqual(plan['ack_due_ms'],T+60)
        self.assertIsNone(plan['first_fill_latency_ms'])
        trace=HedgeTimingTrace('spot-1',T,'2',model)
        trace.record('noticed',T+20);trace.record('submitted',T+20);trace.record('arrived',T+40)
        trace.fill('hedge-1',T+45,'0.5') # May fill before order ACK.
        trace.record('acknowledged',T+60)
        report=trace.report(T+60)
        self.assertEqual(report['first_fill_latency_ms'],45)
        self.assertEqual(report['entry_latency_ms'],40);self.assertEqual(report['ack_latency_ms'],60)
        self.assertEqual(report['remaining_quantity'],'1.5');self.assertIsNone(report['completion_latency_ms'])
        trace.fill('hedge-1',T+45,'0.5');self.assertEqual(trace.report(T+60)['filled_quantity'],'0.5')
        self.assertTrue(trace.report(T+1001)['timeout_exceeded'])
        trace.fill('hedge-2',T+1200,'1.5')
        done=trace.report(T+2000)
        self.assertEqual(done['completion_latency_ms'],1200);self.assertEqual(done['unhedged_duration_ms'],1200)
        self.assertTrue(done['timeout_exceeded']);self.assertTrue(done['complete'])

    def test_model_validation_and_real_delays_propagate_without_fabricated_fills(self):
        for params in ({'hedge_fill_notice_ms':True},{'hedge_timeout_ms':0},{'hedge_submit_ms':2000,'hedge_timeout_ms':1000}):
            with self.subTest(params=params),self.assertRaises(ValueError):
                validate(dict(name='point',type='point2',parameters=params))
        trace=HedgeTimingTrace('spot',T,'1',HedgeTimingModel(decision_ms=10))
        trace.record('noticed',T+100)
        with self.assertRaises(ValueError):trace.record('submitted',T+109)
        trace.record('submitted',T+110)
        with self.assertRaises(ValueError):trace.record('arrived',T+129)
        trace.record('arrived',T+130);trace.record('acknowledged',T+150)
        self.assertIsNone(trace.report(T+500)['first_fill_latency_ms'])
        self.assertFalse(trace.report(T+500)['complete'])
        with self.assertRaises(ValueError):trace.fill('x',T+129,'1')
        with self.assertRaises(ValueError):trace.fill('x',T+160,'2')
        trace.fill('x',T+160,'1')
        with self.assertRaises(ValueError):trace.fill('x',T+161,'1')
        with self.assertRaises(ValueError):trace.record('arrived',T+131)
        with self.assertRaises(ValueError):trace.report(T+159)

if __name__=='__main__':unittest.main()
