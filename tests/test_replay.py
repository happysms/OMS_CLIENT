"""Causal replay, cash reservation, event budgets, clock and checkpoint invariants."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.replay.clock import PlaybackClock
from oms_client.replay.engine import ReplayEngine, config_from
from oms_client.replay.service import ReplayManager, ReplaySession
from oms_client.replay.source import ReplaySource
from oms_client.replay.store import ReplayStore
from oms_client.market_data.validation import DataError


def config(**kw):
    result=dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.200+09:00',
                initial_krw='1000',initial_coin='0',fee_bps='0',quantity_step='1',min_notional='1',latency_ms=0,auto_mm=False,
                queue_cancel_mode='conservative')  # Existing tests also cover the selectable legacy model.
    result.update(kw)
    return config_from(result)


def book(at,bid=100,ask=102,quantity=2):
    return dict(time=at,kind='book',venue='bithumb',source_id='book',server_time=at,
                bids=[dict(price=bid-i,quantity=quantity) for i in range(5)],
                asks=[dict(price=ask+i,quantity=quantity) for i in range(5)])


def tick(at,price,qty):
    return dict(time=at,kind='tick',venue='bithumb',source_id=f'tick:{at}:{price}:{qty}',price=price,qty=qty,direction_known=True)


class ClockTests(unittest.TestCase):
    def test_continuous_1x_32x_pause_resume(self):
        wall=[10.]
        clock=PlaybackClock(1000,monotonic=lambda:wall[0])
        wall[0]+=1;self.assertEqual(clock.now(),2000)
        clock.set(speed=32);self.assertEqual(clock.now(),2000)
        wall[0]+=1;self.assertEqual(clock.now(),34000)
        clock.set(paused=True);wall[0]+=9;self.assertEqual(clock.now(),34000)
        clock.set(speed=3);clock.set(paused=False);wall[0]+=1;self.assertEqual(clock.now(),37000)
        for speed in (0,33,True,1.5):
            with self.assertRaises(ValueError):clock.set(speed=speed)


class EngineTests(unittest.TestCase):
    def engine(self,quantity=2,**kw):
        engine=ReplayEngine(config(**kw));engine.on_event(book(engine.now,quantity=quantity));engine.start()
        return engine

    def invariant(self,e):
        cash,coin=e.free()
        self.assertGreaterEqual(cash,0);self.assertGreaterEqual(coin,0)
        self.assertGreaterEqual(e.cash,0);self.assertGreaterEqual(e.coin,0)
        valuation=e.valuation()['account']
        if valuation['equity'] is not None:
            self.assertAlmostEqual(valuation['equity']-float(e.initial_equity),valuation['total_pnl'],places=8)

    def test_fifo_partial_shared_budget_and_frozen_report(self):
        e=self.engine();now=e.now
        a=e.place('buy','100','3');b=e.place('buy','100','2');e.advance(now)
        e.on_event(tick(now+1,100,-4)) # 2 external, 2 own
        self.assertEqual(e.orders[a].remaining,1)
        self.assertEqual(e.orders[b].remaining,2)
        self.assertEqual(e.coin,2);self.assertEqual(e.fill_count,1)
        frozen=deepcopy(e.last_fills[0])
        e.on_event(tick(now+2,100,-2))
        self.assertEqual(e.fill_count,3);self.assertEqual(e.orders[b].remaining,1)
        self.assertEqual(e.coin,4);self.assertEqual(e.last_fills[0],frozen)
        self.invariant(e)

    def test_single_tick_budget_across_price_levels(self):
        e=self.engine(quantity=0);now=e.now
        a=e.place('buy','100','3');b=e.place('buy','99','3');e.advance(now)
        e.on_event(tick(now+1,99,-4))
        self.assertNotIn(a,e.orders);self.assertEqual(e.orders[b].filled,1)
        self.assertEqual(sum(f['quantity'] for f in e.last_fills),4)
        self.invariant(e)

    def test_cancel_race_holds_cash_until_ack_and_can_fill(self):
        e=self.engine(quantity=0,latency_ms=20);now=e.now
        a=e.place('buy','100','5');e.advance(now+20);e.cancel(a)
        self.assertEqual(e.free()[0],500)
        e.on_event(tick(now+25,100,-2));self.assertEqual(e.coin,2)
        self.assertEqual(e.free()[0],500)
        e.advance(now+40);self.assertNotIn(a,e.orders);self.assertEqual(e.free()[0],800)
        e.on_event(tick(now+41,100,-100));self.assertEqual(e.coin,2)
        self.invariant(e)

    def test_same_ms_and_inflight_order_do_not_fill(self):
        e=self.engine(quantity=0,latency_ms=20);now=e.now
        e.place('buy','100','1')
        e.on_event(tick(now+10,100,-9));e.on_event(tick(now+20,100,-9));self.assertEqual(e.coin,0)
        e.on_event(tick(now+21,100,-1));self.assertEqual(e.coin,1)

    def test_same_ms_tick_cannot_consume_new_order_queue_ahead(self):
        e=self.engine(quantity=2,latency_ms=20);now=e.now
        e.place('buy','100','1')
        e.on_event(tick(now+20,100,-2))
        e.on_event(tick(now+21,100,-2));self.assertEqual(e.coin,0)
        e.on_event(tick(now+22,100,-1));self.assertEqual(e.coin,1)

    def test_outside_depth_requires_observed_queue(self):
        e=self.engine();now=e.now
        a=e.place('buy','90','2');e.advance(now)
        self.assertFalse(e.snapshot()['orders'][0]['queue_known'])
        e.on_event(tick(now+1,90,-50));self.assertEqual(e.coin,0)
        e.on_event(book(now+2,bid=90,ask=92,quantity=3))
        self.assertTrue(e.snapshot()['orders'][0]['queue_known'])
        e.on_event(tick(now+3,90,-4));self.assertEqual(e.orders[a].remaining,1)
        self.invariant(e)

    def test_repeated_book_does_not_reset_consumed_queue_and_cancel_does_not_advance(self):
        e=self.engine(quantity=10);now=e.now
        e.place('buy','100','3');e.advance(now)
        e.on_event(tick(now+1,100,-5));e.on_event(book(now+2,quantity=10))
        e.on_event(tick(now+3,100,-5));self.assertEqual(e.coin,0)
        e.on_event(book(now+4,quantity=0));e.on_event(tick(now+5,100,-1));self.assertEqual(e.coin,1)

    def test_new_order_after_trade_snapshot_does_not_double_subtract_trade(self):
        e=self.engine(quantity=10);now=e.now
        a=e.place('buy','100','1');e.advance(now)
        e.on_event(tick(now+1,100,-5));e.cancel(a);e.advance(now+1)
        e.on_event(book(now+2,quantity=5))
        e.place('buy','100','1');e.advance(now+2)
        self.assertEqual(e.snapshot()['orders'][0]['queue_ahead'],5)
        e.on_event(tick(now+3,100,-5));self.assertEqual(e.coin,0)
        e.on_event(tick(now+4,100,-1));self.assertEqual(e.coin,1)

    def test_maker_cross_rejection_releases_reservation(self):
        e=self.engine();now=e.now
        a=e.place('buy','103','2');self.assertEqual(e.free()[0],794)
        e.advance(now);self.assertNotIn(a,e.orders);self.assertEqual(e.free()[0],1000)
        self.assertEqual(e.order_log[-1]['reason'],'post_only_cross')

    def test_initial_coin_cost_and_roundtrip_fee_pnl(self):
        e=self.engine(quantity=0,fee_bps='10',initial_coin='2',theory_price='105');now=e.now
        self.assertEqual(e.initial_equity,1200);self.assertEqual(e.fill_count,0)
        e.place('sell','102','2');e.advance(now);e.on_event(tick(now+1,102,2))
        self.assertEqual(e.realized,D('3.796'));self.assertEqual(e.cost,0)
        self.invariant(e)
        e.place('buy','100','3');e.advance(now+1)
        e.on_event(tick(now+2,100,-1));e.on_event(tick(now+3,100,-2))
        self.assertEqual(e.cost,D('300.3'))
        self.assertAlmostEqual(e.valuation()['account']['theory_unrealized_pnl'],14.7)
        self.invariant(e)

    def test_cumulative_fee_rounding_never_exceeds_reserved_total(self):
        e=self.engine(quantity=0,fee_bps='0.0000001');now=e.now
        e.place('buy','100','3');e.advance(now)
        for i in range(1,4):e.on_event(tick(now+i,100,-1));self.invariant(e)
        self.assertEqual(e.fees,D('0.00000001'))
        self.assertEqual(e.cash,D('699.99999999'))

    def test_mm_requotes_only_after_cancel_ack_and_stale_cancels(self):
        e=self.engine(quantity=0,auto_mm=True,latency_ms=20);now=e.now
        e.advance(now+20);self.assertEqual(len(e.working()),1)
        e.on_event(book(now+30,bid=99,ask=101,quantity=0))
        self.assertEqual(len(e.working()),1);self.assertEqual(e.working()[0].status,'cancel_pending')
        e.advance(now+50);self.assertEqual(e.working()[0].price,99)
        e.advance(now+1071);self.assertFalse(e.working());self.invariant(e)

    def test_config_rejects_unsupported_modes_and_more_than_two_venues(self):
        for kw in ({'speed':33},{'speed':True},{'latency_ms':-1},{'initial_coin':-1},{'venues':['a','b','c']},{'fast':'yes'}):
            with self.subTest(kw=kw),self.assertRaises(DataError):config(**kw)
        self.assertEqual(config(include_okx=True)['venues'],['bithumb','okx'])


class SyntheticSource:
    def __init__(self,config):
        self.config=config;self.total=4;self.skipped=0;self.manifest=[]
    def load(self):
        start=self.config['start_ms']
        return iter([book(start-1,quantity=0),tick(start+30,100,-3),tick(start+60,102,3),book(start+80,quantity=0)])
    def verify(self):pass
    def close(self):pass


class SessionTests(unittest.TestCase):
    def test_fast_1x_and_32x_have_identical_economic_results(self):
        results=[]
        with tempfile.TemporaryDirectory() as temp:
            manager=ReplayManager(temp,SyntheticSource)
            try:
                for mode in ({'fast':True},{'speed':1},{'speed':32}):
                    result=manager.create(dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.120+09:00',quantity_step='1',min_notional='1',fee_bps='0',latency_ms=0,**mode))
                    manager.active.thread.join(2)
                    end=manager.get(result['id']);self.assertEqual(end['status'],'finished')
                    fills=manager.store.fills(result['id'])
                    self.assertEqual(len(fills),2)
                    self.assertEqual(end['fill_count'],len(fills))
                    results.append((end['account'],fills,end['event_count']))
            finally:manager.close()
        self.assertEqual(results[0],results[1]);self.assertEqual(results[0],results[2])

    def test_pause_manual_multi_orders_stop_and_saved_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=ReplayManager(temp,SyntheticSource)
            try:
                result=manager.create(dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:10+09:00',auto_mm=False,quantity_step='1',min_notional='1'))
                session=manager.active
                deadline=time.monotonic()+2
                while manager.get(result['id'])['status']=='loading' and time.monotonic()<deadline:time.sleep(.005)
                paused=manager.control(result['id'],{'action':'pause'})
                time.sleep(.04);self.assertEqual(session.engine.now,paused['virtual_time_ms'])
                for price in ['100','90']:
                    manager.control(result['id'],{'action':'order','side':'buy','price':price,'quantity':'1'})
                self.assertEqual(len(manager.get(result['id'])['orders']),2)
                with self.assertRaises(ValueError):manager.control(result['id'],{'action':'pace','speed':33})
                manager.control(result['id'],{'action':'pace','speed':32,'fast':True})
                self.assertTrue(manager.get(result['id'])['paused'])
                manager.control(result['id'],{'action':'stop'});session.thread.join(2)
                result=manager.get(result['id']);self.assertEqual(result['status'],'stopped')
                self.assertEqual(result['orders'],[]);self.assertEqual(result['account']['cash'],result['account']['free_cash'])
                with manager.store.connect() as con:self.assertEqual(con.execute('SELECT count(*) FROM commands').fetchone()[0],5)
            finally:manager.close()

    def test_atomic_fill_checkpoint_and_restart_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ReplayStore(temp)
            snapshot=dict(id='a',status='running',fill_count=1,account={'cash':900})
            store.save(snapshot,[{'sequence':1}])
            with self.assertRaises(sqlite3.IntegrityError):
                store.save(dict(snapshot,fill_count=2,account={'cash':800}),[{'sequence':2},{'sequence':1}])
            self.assertEqual(store.get('a')['account']['cash'],900)
            self.assertEqual(store.fills('a'),[{'sequence':1}])
            restarted=ReplayStore(temp);self.assertEqual(restarted.get('a')['status'],'interrupted')

    def test_source_change_marks_run_failed(self):
        class Changed(SyntheticSource):
            def verify(self):raise DataError('source changed')
        with tempfile.TemporaryDirectory() as temp:
            session=ReplaySession('changed',config(fast=True),ReplayStore(temp),Changed)
            session.thread.start();session.thread.join(2)
            self.assertEqual(session.get()['status'],'failed')
            self.assertEqual(session.get()['error'],'source changed')


class SourceTests(unittest.TestCase):
    def test_raw_duplicates_stable_ties_depth_conversion_and_invalid_skip(self):
        import duckdb
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'ticks.parquet'
            con=duckdb.connect()
            con.execute('CREATE TABLE ticks(desktopTime BIGINT,serverTime DOUBLE,price DOUBLE,qty DOUBLE)')
            cfg=config();start=cfg['start_ms']
            con.executemany('INSERT INTO ticks VALUES (?,?,?,?)',[(start, float('nan'),101,2),(start,1,100,-1),(start,1,100,-1),(start+1,1,0,1),(cfg['end_ms'],1,100,1)])
            con.sql('SELECT * FROM ticks').write_parquet(str(path))
            source=ReplaySource(cfg)
            try:
                events=list(source._stream('bithumb','tick',[str(path)],False,[str(path)]))
                self.assertEqual([e['price'] for e in events],[101,100,100])
                self.assertEqual(len({e['source_id'] for e in events}),3)
                self.assertIsNone(events[0]['server_time']);self.assertEqual(source.skipped,1)
                json.dumps(events,allow_nan=False)
            finally:source.close()
            path=Path(temp)/'books.parquet'
            con.execute('CREATE TABLE books(desktopTime BIGINT,b1_p DOUBLE,b1_q DOUBLE,a1_p DOUBLE,a1_q DOUBLE)')
            con.execute('INSERT INTO books VALUES (?,100,250,102,306)',[start])
            con.sql('SELECT * FROM books').write_parquet(str(path))
            source=ReplaySource(cfg)
            try:
                events=list(source._stream('bithumb','book',[str(path)],False,[str(path)]))
                self.assertEqual(events[0]['bids'],[{'price':100,'quantity':2.5}]);self.assertEqual(events[0]['asks'][0]['quantity'],3)
            finally:source.close();con.close()


class ReplayApiTests(unittest.TestCase):
    def setUp(self):
        from oms_client.api import create_app
        import re
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app({'TESTING':True,'STATE_DIR':self.temp.name,'PASSWORD':'test-replay-password'})
        self.addCleanup(self.app.extensions['close_services'])
        self.manager=self.app.extensions['replay_manager'];self.manager.source_factory=SyntheticSource
        self.client=self.app.test_client()
        page=self.client.get('/login').get_data(as_text=True)
        token=re.search(r'name="csrf_token" value="([^"]+)"',page)[1]
        self.client.post('/login',data={'csrf_token':token,'password':'test-replay-password'})
        self.headers={'X-CSRF-Token':self.client.get('/api/session').json['csrf_token']}

    def payload(self,**kw):
        result=dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.120+09:00',
                    quantity_step='1',min_notional='1',latency_ms=0,fast=True)
        result.update(kw);return result

    def test_limit_capabilities_controls_unknown_rejection_and_closed_order_export(self):
        self.assertEqual(self.app.test_client().get('/api/replays/capabilities').status_code,401)
        capabilities=self.client.get('/api/replays/capabilities').json
        self.assertEqual(len(capabilities['cases']),142)
        self.assertIn('fok',capabilities['tifs']);self.assertIn('okx_execution',capabilities['unsupported'])
        response=self.client.post('/api/replays',json=self.payload(auto_mm=False,fast=False,end='2026-10-26T12:01:00+09:00',
            maker_fee_bps='5',taker_fee_bps='10',price_step='1',end_order_policy='preserve'),headers=self.headers)
        self.assertEqual(response.status_code,201);identifier=response.json['id'];session=self.manager.active
        deadline=time.monotonic()+2
        while self.manager.get(identifier)['status']=='loading' and time.monotonic()<deadline:time.sleep(.005)
        base=f'/api/replays/{identifier}'
        self.assertEqual(self.client.post(base+'/control',json={'action':'pause'},headers=self.headers).status_code,200)
        for extra in ({'tif':'stop_limit'},{'reduce_only':True},{'venue':'okx'}):
            response=self.client.post(base+'/control',json=dict(action='order',side='buy',price='100',quantity='1',**extra),headers=self.headers)
            self.assertEqual(response.status_code,400)
        for _ in range(2):
            response=self.client.post(base+'/control',json=dict(action='order',side='buy',price='100',quantity='1',tif='ioc',client_order_id='retry'),headers=self.headers)
            self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json['orders']),1)
        self.client.post(base+'/control',json={'action':'play'},headers=self.headers)
        deadline=time.monotonic()+2
        while not self.manager.get(identifier)['order_event_count'] and time.monotonic()<deadline:time.sleep(.005)
        self.client.post(base+'/control',json={'action':'stop'},headers=self.headers);session.thread.join(2)
        exported=self.client.get(base+'/result.json').json
        self.assertEqual(len(exported['closed_orders']),1)
        self.assertEqual(exported['closed_orders'][0]['quantity_state']['cancelled'],1)
        self.assertEqual(exported['initial_config']['taker_fee_bps'],'10')

    def test_auth_csrf_and_invalid_speed(self):
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.get('/api/replays').status_code,401)
        self.assertEqual(self.client.post('/api/replays',json=self.payload()).status_code,403)
        for payload in (self.payload(speed=0),self.payload(speed=33),self.payload(ticker='../BTC'),[] ,None):
            response=self.client.post('/api/replays',json=payload,headers=self.headers)
            self.assertEqual(response.status_code,400,response.get_data(as_text=True))

    def test_run_fill_export_history_and_missing_routes(self):
        import csv,io
        response=self.client.post('/api/replays',json=self.payload(),headers=self.headers)
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        identifier=response.json['id'];self.manager.active.thread.join(2)
        result=self.client.get('/api/replays/'+identifier).json
        self.assertEqual(result['status'],'finished');self.assertEqual(result['fill_count'],2)
        self.assertEqual(len(self.client.get('/api/replays').json['items']),1)
        fills=self.client.get(f'/api/replays/{identifier}/fills').json['items']
        self.assertEqual(len(fills),2)
        response=self.client.get(f'/api/replays/{identifier}/fills.csv')
        self.assertEqual(response.status_code,200)
        exported=list(csv.DictReader(io.StringIO(response.get_data(as_text=True).lstrip('\ufeff'))))
        self.assertEqual(len(exported),2)
        self.assertEqual(float(exported[0]['cumulative_realized_pnl']),fills[0]['account']['realized_pnl'])
        self.assertEqual(self.client.get(f'/api/replays/{identifier}/fills?before=-1').status_code,400)
        self.assertEqual(self.client.get('/api/replays/unknown').status_code,404)
        self.assertEqual(self.client.post('/api/replays/unknown/control',json={'action':'pause'},headers=self.headers).status_code,404)
        self.assertEqual(self.client.get('/features/replay/controller.js').status_code,200)

    def test_account_name_validation_history_and_complete_result_export(self):
        for name in (None,123,'x'*81,'line\nfeed'):
            response=self.client.post('/api/replays',json=self.payload(account_name=name),headers=self.headers)
            self.assertEqual(response.status_code,400)
        response=self.client.post('/api/replays',json=self.payload(account_name='MM 실험 A'),headers=self.headers)
        self.assertEqual(response.status_code,201)
        identifier=response.json['id'];self.manager.active.thread.join(2)
        saved=self.manager.store.get(identifier)
        # More than the UI's 100 fills must all be included in the result file.
        extra=[dict(sequence=i,fill_id=f'F{i:08d}',account={'cash':1000}) for i in range(saved['fill_count']+1,106)]
        saved['fill_count']=105
        self.manager.store.save(saved,extra)
        exported=self.client.get(f'/api/replays/{identifier}/result.json')
        self.assertEqual(exported.status_code,200)
        self.assertIn('attachment',exported.headers['Content-Disposition'])
        result=exported.json
        self.assertEqual(result['virtual_account']['name'],'MM 실험 A')
        self.assertEqual(len(result['fills']),105)
        self.assertEqual(result['fills'][0]['sequence'],105)
        self.assertEqual(result['fills'][-1]['sequence'],1)
        self.assertEqual(result['initial_config']['initial_krw'],'1000000')
        summary=self.client.get('/api/replays').json
        self.assertEqual(summary['items'][0]['virtual_account']['name'],'MM 실험 A')
        self.assertIsNone(summary['next_before'])
        self.assertEqual(self.client.get('/api/replays?before=-1').status_code,400)
        self.assertEqual(self.client.get('/api/replays?before='+('9'*30)).status_code,400)
        self.assertEqual(self.app.test_client().get(f'/api/replays/{identifier}/result.json').status_code,401)


class AccountPersistenceTests(unittest.TestCase):
    def test_named_accounts_remain_independent_across_runs_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            manager=ReplayManager(temp,SyntheticSource)
            try:
                first=manager.create(dict(ticker='XYZ',account_name='첫 실험',initial_krw='1000',initial_coin='2',
                    start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.120+09:00',
                    quantity_step='1',min_notional='1',latency_ms=0,fast=True))
                manager.active.thread.join(2)
                original=manager.store.get(first['id']);original_fills=manager.store.fills(first['id'])
                second=manager.create(dict(ticker='XYZ',account_name='두 번째 실험',initial_krw='5000',initial_coin='0',
                    start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.120+09:00',
                    quantity_step='1',min_notional='1',latency_ms=0,fast=True))
                manager.active.thread.join(2)
                self.assertNotEqual(first['virtual_account']['id'],second['virtual_account']['id'])
                self.assertEqual(manager.store.get(first['id']),original)
                self.assertEqual(manager.store.fills(first['id']),original_fills)
            finally:manager.close()
            reopened=ReplayStore(temp)
            self.assertEqual(reopened.get(first['id']),original)
            items=reopened.recent()
            self.assertEqual([item['virtual_account']['name'] for item in items],['두 번째 실험','첫 실험'])
            self.assertEqual(items[1]['virtual_account']['initial_coin'],'2')
            self.assertEqual(items[1]['account'],original['account'])

    def test_account_pagination_is_stable_and_seed_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ReplayStore(temp)
            for index in range(25):
                store.save(dict(id=str(index),status='finished',config=config(),account={},fill_count=0))
            first=store.recent();self.assertEqual(len(first),20)
            cursor=first[-1]['history_sequence']
            store.save(dict(id='new',status='finished',config=config(),account={},fill_count=0))
            second=store.recent(cursor)
            self.assertEqual(len(second),5)
            self.assertEqual(len({row['id'] for row in first+second}),25)
            before=store.get('0')
            modified=deepcopy(before);modified['virtual_account']['initial_krw']='9999'
            with self.assertRaises(ValueError):store.save(modified)
            self.assertEqual(store.get('0'),before)

    def test_legacy_runs_migrate_without_losing_fills(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'replays.sqlite3'
            with sqlite3.connect(path) as con:
                con.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,updated REAL NOT NULL,snapshot TEXT NOT NULL)')
                original=dict(id='legacy',status='finished',config=config(),account={'cash':900},fill_count=1)
                con.execute('INSERT INTO runs VALUES (?,?,?)',('legacy',123456,json.dumps(original)))
                con.execute('CREATE TABLE fills(run_id TEXT, sequence INTEGER, report TEXT, PRIMARY KEY(run_id,sequence))')
                con.execute('INSERT INTO fills VALUES (?,?,?)',('legacy',1,json.dumps({'sequence':1,'quantity':1})))
            store=ReplayStore(temp);migrated=store.get('legacy')
            self.assertEqual(migrated['virtual_account']['id'],'va_legacy')
            self.assertEqual(migrated['account'],original['account'])
            self.assertEqual(store.fills('legacy'),[{'sequence':1,'quantity':1}])
            again=ReplayStore(temp)
            self.assertEqual(again.get('legacy'),migrated)
            self.assertEqual(len(again.recent()),1)

if __name__=='__main__':unittest.main()
