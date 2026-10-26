"""Fill inspection is bounded, causal, read-only and survives unavailable market files."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.api import create_app
from oms_client.market_data.validation import DataError
from oms_client.replay.fill_context import ContextMarketReader, ContextWindow, FillContextService, book_frames, nearby_ticks
from oms_client.replay.source import ReplaySource
from oms_client.replay.store import ReplayStore
from test_replay import SyntheticSource, book, config, tick

T = config()['start_ms']


def saved_fill(sequence, at=T):
    return dict(sequence=sequence, timestamp=at, order_id=f'O{sequence}', side='buy', price=100,
                quantity=1, remaining=0, realized_pnl=0, account={}, source_tick=f'tick:{at}')


class ContextStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store = ReplayStore(self.temp.name)
        self.snapshot = dict(id='run', status='running', config=config(), virtual_time_ms=T+10,
                             fill_count=3, order_event_count=1)
        self.fills = [saved_fill(1, T-60000), saved_fill(2), saved_fill(3, T+10)]
        self.orders = [dict(sequence=1, id='O1', closed_at=T, status='filled')]
        self.store.save(self.snapshot, self.fills, order_events=self.orders)

    def test_active_window_freezes_progress_and_read_does_not_change_account(self):
        before = self.store.get('run')
        result = self.store.fill_context('run', 2)
        self.assertEqual(result['start_ms'], T-60000)
        self.assertEqual(result['requested_end_ms'], T+60001)
        self.assertEqual(result['end_ms'], T+11)
        self.assertTrue(result['future_hidden'])
        self.assertEqual([row['sequence'] for row in result['fills']], [1,2,3])
        self.assertEqual(result['order_events'], self.orders)
        self.assertEqual(self.store.get('run'), before)
        result['fill']['price'] = 999
        self.assertEqual(self.store.fill_context('run', 2)['fill']['price'], 100)
        for sequence in [0, 100001, True, '2']:
            with self.assertRaises(ValueError):self.store.fill_context('run', sequence)
        with self.assertRaises(KeyError):self.store.fill_context('other', 2)
        with self.assertRaises(KeyError):self.store.fill_context('run', 9)

    def test_finished_context_bounds_and_truncation_keep_selected_fill(self):
        fills = [saved_fill(i, T) for i in range(4, 1204)]
        orders = [dict(sequence=i, id=f'O{i}', closed_at=T, status='cancelled') for i in range(2, 250)]
        self.store.save(dict(self.snapshot, status='finished', fill_count=1203, order_event_count=249), fills, order_events=orders)
        result = self.store.fill_context('run', 1203)
        self.assertEqual(result['end_ms'], T+60001)
        self.assertFalse(result['future_hidden'])
        self.assertEqual(len(result['fills']), 1000)
        self.assertIn(1203, [row['sequence'] for row in result['fills']])
        self.assertTrue(result['fills_truncated']);self.assertTrue(result['orders_truncated'])
        self.assertEqual(len(result['order_events']), 200)
        with self.store.connect() as con:
            plan = con.execute("EXPLAIN QUERY PLAN SELECT report FROM fills WHERE run_id=? AND json_extract(report,'$.timestamp')>=? AND json_extract(report,'$.timestamp')<?", ('run', T, T+1)).fetchall()
        self.assertIn('fills_time', str(plan))


class ContextSamplingTests(unittest.TestCase):
    def test_books_use_past_rows_and_same_timestamp_last_book(self):
        window = ContextWindow(T-1000, T+1001, T)
        frames = book_frames(iter([book(T-900, bid=99), book(T, bid=100), book(T, bid=101), book(T+900, bid=102)]), window)['frames']
        self.assertEqual([frame['time'] for frame in frames], [T-1000,T,T+1000])
        self.assertIsNone(frames[0]['book'])
        self.assertEqual(frames[1]['book']['bids'][0]['price'], 101)
        self.assertEqual(frames[2]['book']['time'], T+900)
        self.assertTrue(all(frame['book'] is None or frame['book']['time']<=frame['time'] for frame in frames))

    def test_tick_cap_keeps_trigger_and_equal_rows_without_deduplication(self):
        window = ContextWindow(T-60000, T+60001, T)
        events = [dict(tick(T, 100, -1), source_id=f'tick-{i}') for i in range(1005)]
        events += [tick(T+60001, 100, -1)]
        result = nearby_ticks(iter(events), window, 'tick-1004')
        self.assertEqual(result['event_count'], 1005)
        self.assertEqual(len(result['items']), 1000)
        self.assertTrue(result['truncated']);self.assertTrue(result['trigger_found'])
        self.assertIn('tick-1004', [row['source_id'] for row in result['items']])


class ContextRawTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        env=patch.dict(os.environ,DASHBOARD_DATA_ROOT=str(self.root/'data'),DASHBOARD_COLLECTOR_DATA_ROOT=str(self.root/'collector'))
        env.start();self.addCleanup(env.stop)
        self.paths=[]
        for kind in ['ob','tick']:
            path=self.root/'data/bithumb'/kind/'XYZ/2026/10/26/03.parquet'
            path.parent.mkdir(parents=True)
            fields=dict(desktopTime=[T-1000,T,T+1000],serverTime=[T-1001,T-1,T+999])
            if kind=='ob':fields.update(b1_p=[99,100,101],a1_p=[101,102,103],b1_q=[198,200,202],a1_q=[202,204,206])
            else:fields.update(price=[100,101,102],qty=[-1,-2,1])
            pd.DataFrame(fields).to_parquet(path,index=False)
            self.paths.append(path)
        self.snapshot=dict(config=config(),manifest=[dict(path=str(path),size=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns) for path in self.paths])
        self.window=ContextWindow(T-1000,T+1001,T)

    def test_real_raw_depth_and_future_cutoff_missing_ticks_do_not_erase_book(self):
        reader=ContextMarketReader()
        market=reader.read(self.snapshot,saved_fill(1),self.window)[0]
        self.assertEqual(market['streams']['book']['provenance'],'matched')
        self.assertEqual(market['frames'][1]['book']['bids'][0]['quantity'],2)
        self.assertEqual([row['qty'] for row in market['ticks']],[-1,-2,1])
        limited=reader.read(self.snapshot,saved_fill(1),ContextWindow(T-1000,T+1,T))[0]
        self.assertTrue(all(row['time']<=T for row in limited['ticks']))
        self.paths[1].unlink()
        missing=reader.read(self.snapshot,saved_fill(1),self.window)[0]
        self.assertEqual(missing['streams']['tick']['status'],'unavailable')
        self.assertEqual(missing['streams']['book']['status'],'ok')
        json.dumps(missing,allow_nan=False)

    def test_changed_manifest_is_not_presented_as_original_and_limits_close_source(self):
        self.snapshot['manifest'][0]['size']+=1
        changed=ContextMarketReader().read(self.snapshot,saved_fill(1),self.window)[0]
        self.assertEqual(changed['frames'],[])
        self.assertEqual(changed['streams']['book']['status'],'unavailable')
        self.assertEqual(changed['streams']['tick']['status'],'ok')
        source=ReplaySource(dict(config(),start_ms=T-1000,end_ms=T+1001))
        try:
            with self.assertRaises(DataError):source.open_context_stream('bithumb','tick',max_events=2)
        finally:source.close()
        unknown=ContextMarketReader().read(dict(self.snapshot,manifest=[]),saved_fill(1),self.window)[0]
        self.assertEqual(unknown['streams']['book']['provenance'],'unverified')


class ContextApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.calls=[]
        owner=self
        class Reader:
            def read(self,snapshot,fill,window):
                owner.calls.append((snapshot['id'],fill['sequence'],window))
                return []
        self.reader=Reader()
        self.app=create_app(dict(TESTING=True,STATE_DIR=self.temp.name,PASSWORD='context-test',REPLAY_CONTEXT_READER=self.reader))
        self.addCleanup(self.app.extensions['close_services'])
        self.manager=self.app.extensions['replay_manager'];self.manager.source_factory=SyntheticSource
        self.client=self.app.test_client()
        csrf=re.search(r'name="csrf_token" value="([^"]+)"',self.client.get('/login').text)[1]
        self.client.post('/login',data=dict(password='context-test',csrf_token=csrf))
        headers={'X-CSRF-Token':self.client.get('/api/session').json['csrf_token']}
        response=self.client.post('/api/replays',json=dict(ticker='XYZ',start='2026-10-26T12:00:00',end='2026-10-26T12:00:00.120',quantity_step='1',min_notional='1',latency_ms=0,fast=True),headers=headers)
        self.identifier=response.json['id'];self.manager.active.thread.join(2)
        self.url=f'/api/replays/{self.identifier}/fills/1/context'

    def test_auth_identity_and_no_replay_mutation(self):
        self.assertEqual(self.app.test_client().get(self.url).status_code,401)
        before=self.manager.get(self.identifier)
        result=self.client.get(self.url)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json['fill']['sequence'],1)
        self.assertEqual(result.json['fill'],self.manager.store.fills(self.identifier)[-1])
        self.assertNotIn('snapshot',result.json)
        self.assertEqual(self.manager.get(self.identifier),before)
        self.assertEqual(self.client.get(self.url.replace('/fills/1/','/fills/999/')).status_code,404)
        self.assertEqual(self.client.get(self.url.replace('/fills/1/','/fills/100001/')).status_code,400)
        self.assertEqual(self.client.get(self.url.replace(self.identifier,'unknown')).status_code,404)
        self.assertEqual(len(self.calls),1)

    def test_concurrent_inspection_rejected_and_slot_released(self):
        entered,release=threading.Event(),threading.Event()
        original=self.reader.read
        def blocked(*args):
            entered.set();release.wait(2);return original(*args)
        self.reader.read=blocked
        outcome=[]
        worker=threading.Thread(target=lambda:outcome.append(self.client.get(self.url).status_code))
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            other=self.app.test_client()
            with self.client.session_transaction() as session:
                copied=dict(session)
            with other.session_transaction() as session:session.update(copied)
            self.assertEqual(other.get(self.url).status_code,429)
        finally:release.set();worker.join(2)
        self.assertEqual(outcome,[200])
        self.reader.read=original
        self.assertEqual(self.client.get(self.url).status_code,200)


if __name__=='__main__':unittest.main()
