"""User registration, immutable revisions and exact strategy binding to accounts."""
from copy import deepcopy
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from oms_client.strategies.registry import StrategyRegistry, DEFAULT_ID, validate
from oms_client.replay.engine import ReplayEngine
from oms_client.replay.service import ReplayManager
from test_replay import SyntheticSource, config, book


def maker(name='내 메이커',direction='both'):
    return dict(name=name,type='single_exchange_maker',parameters={'direction':direction,'max_quote_age_ms':500})


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.registry=StrategyRegistry(self.temp.name)

    def test_defaults_point_profile_and_registration_survive_restart(self):
        default=self.registry.list();self.assertEqual(len(default),3)
        self.assertTrue(default[0]['executable']);self.assertFalse(default[1]['executable'])
        point=self.registry.save(dict(name='Point 버퍼 실험',type='point2',parameters={'buffer_bps':'15','hedge_execution':'limit_gtc'}))
        self.assertEqual(point['parameters']['baseline_bps'],'-100')
        self.assertEqual(point['parameters']['buffer_bps'],'15')
        self.assertEqual(point['specification']['reference_source'],'okx_swap')
        self.assertEqual(point['specification']['variant'],'modified_hedge_execution')
        self.assertEqual(point['required_venues'],['bithumb','okx'])
        self.assertFalse(point['executable'])
        self.assertEqual(StrategyRegistry(self.temp.name).get(point['id']),point)
        with self.assertRaisesRegex(ValueError,'등록만'):self.registry.bind(point['id'])

    def test_minmaxma_is_separate_and_validates_reference_and_signal_parameters(self):
        original=self.registry.get('builtin_minmaxma')
        self.assertEqual(original['name'],'minmaxma 전략')
        self.assertEqual(original['type'],'minmaxma')
        self.assertEqual(original['parameters']['buffer_ratio'],'1.001')
        self.assertEqual(original['parameters']['ma_ms'],10000)
        self.assertEqual(original['required_venues'],['bithumb','binance'])
        self.assertEqual(original['specification']['source_entrypoint'],'point_st')
        self.assertFalse(original['specification']['reference_trading_enabled'])
        self.assertNotIn('hedge_execution',original['parameters'])
        custom=self.registry.save(dict(name='minmaxma 짧은 MA',type='minmaxma',parameters={
            'ma_ms':1000,'buffer_ratio':'1.0001','reference_exchange':'okx'}))
        self.assertEqual(custom['required_venues'],['bithumb','okx'])
        self.assertEqual(custom['specification']['variant'],'okx_reference_variant')
        self.assertFalse(custom['executable'])
        with self.assertRaisesRegex(ValueError,'minmaxma'):self.registry.bind(custom['id'])
        for parameters in ({'buffer_ratio':'1'},{'buffer_ratio':'2'},{'ma_ms':0},{'ma_ms':True},
                           {'reference_exchange':'upbit'},{'hedge_execution':'market'},{'max_position_krw':'-1'}):
            with self.subTest(parameters=parameters),self.assertRaises(ValueError):
                self.registry.save(dict(name='잘못된 설정',type='minmaxma',parameters=parameters))
        self.assertEqual(self.registry.get('builtin_point2')['type'],'point2')

    def test_new_builtin_seeding_preserves_existing_strategy_versions(self):
        point=self.registry.get('builtin_point2')
        updated=self.registry.save(dict(name='내 Point',type='point2',parameters={'buffer_bps':'20'}),'builtin_point2',1)
        self.registry.archive('builtin_point2',True)
        # Simulate the database from before minmaxma was added.
        with self.registry.connect() as con:
            con.execute("DELETE FROM strategy_revisions WHERE strategy_id='builtin_minmaxma'")
            con.execute("DELETE FROM strategies WHERE id='builtin_minmaxma'")
        reopened=StrategyRegistry(self.temp.name)
        self.assertEqual(reopened.get('builtin_point2')['revision'],updated['revision'])
        self.assertEqual(reopened.get('builtin_point2')['name'],'내 Point')
        self.assertTrue(reopened.get('builtin_point2')['archived'])
        self.assertEqual(reopened.get('builtin_point2',1)['parameters'],point['parameters'])
        self.assertEqual(reopened.get('builtin_minmaxma')['revision'],1)
        self.assertEqual(len(StrategyRegistry(self.temp.name).list()),3)

    def test_revision_history_concurrent_edits_archive_and_restore(self):
        first=self.registry.save(maker(direction='buy'));original=deepcopy(first)
        second=self.registry.save(maker(direction='sell'),first['id'],1)
        self.assertEqual(second['revision'],2)
        self.assertEqual(self.registry.get(first['id'],1),original)
        with self.assertRaisesRegex(ValueError,'변경되었습니다'):self.registry.save(maker(),first['id'],1)
        self.registry.archive(first['id'],True)
        with self.assertRaisesRegex(ValueError,'보관'):self.registry.bind(first['id'],1)
        with self.assertRaisesRegex(ValueError,'복원'):self.registry.save(maker(),first['id'],2)
        self.registry.archive(first['id'],False)
        self.assertEqual(self.registry.bind(first['id'],1),original)
        self.assertEqual(self.registry.bind(first['id'])['revision'],2)

    def test_invalid_parameters_cannot_create_executable_point_or_arbitrary_code(self):
        cases=[dict(name='',type='point2'),dict(name='p',type='point2',executable=True),
               dict(name='p',type='python',code='print(1)'),dict(name='p',type='point2',parameters={'buffer_bps':'-1'}),
               dict(name='p',type='point2',parameters={'baseline_bps':'-9000','buffer_bps':'1000'}),
               dict(name='p',type='point2',parameters={'hedge_execution':'fake'}),
               maker(direction='all'),dict(name='m',type='single_exchange_maker',parameters={'max_quote_age_ms':True}),
               dict(name='m',type='single_exchange_maker',parameters={'max_quote_age_ms':10001}),
               dict(name='m',type='single_exchange_maker',parameters={'arbitrary':1})]
        for case in cases:
            with self.subTest(case=case),self.assertRaises(ValueError):self.registry.save(case)
        self.assertEqual(len(self.registry.list()),3)

    def test_registered_maker_direction_and_quote_age_change_actual_execution(self):
        for direction,expected in [('buy',{'buy'}),('sell',{'sell'}),('both',{'buy','sell'})]:
            profile=self.registry.save(maker(direction=direction))
            settings=config(auto_mm=True,initial_coin='2',latency_ms=0)
            settings['strategy']=profile
            engine=ReplayEngine(settings);start=engine.now
            engine.on_event(book(start));engine.start();engine.advance(start)
            self.assertEqual({o['side'] for o in engine.working()},expected)
            self.assertEqual(engine.mm.config.max_quote_age_ms,500)
            engine.advance(start+501)
            self.assertEqual(engine.working(),[])

    def test_run_freezes_selected_revision_and_invalid_strategy_creates_no_account(self):
        manager=ReplayManager(self.temp.name,SyntheticSource)
        self.addCleanup(manager.close)
        profile=manager.strategies.save(maker(direction='buy'))
        payload=dict(ticker='XYZ',start='2026-10-26T12:00:00+09:00',end='2026-10-26T12:00:00.120+09:00',
                     fast=True,quantity_step='1',min_notional='1',latency_ms=0,strategy_id=profile['id'],strategy_revision=1)
        run=manager.create(payload);manager.active.thread.join(2)
        original=manager.store.get(run['id'])
        self.assertEqual(original['config']['strategy']['id'],profile['id'])
        self.assertEqual(original['initial_config']['strategy']['parameters']['direction'],'buy')
        manager.strategies.save(maker(direction='sell'),profile['id'],1)
        self.assertEqual(manager.store.get(run['id']),original)
        repeat=manager.create(payload);manager.active.thread.join(2)
        self.assertEqual(manager.get(repeat['id'])['initial_config']['strategy']['revision'],1)
        count=len(manager.store.recent())
        for bad in [dict(strategy_id='builtin_point2'),dict(strategy_id='missing'),dict(strategy_revision=999),dict(strategy_revision=True)]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):manager.create(dict(payload,**bad))
        self.assertEqual(len(manager.store.recent()),count)

    def test_strategy_theory_is_consumed_with_provenance_and_frozen_at_fill(self):
        from test_replay import tick
        from oms_client.strategies.theory import TheoryOutput
        profile=self.registry.save(maker())
        settings=config(auto_mm=False,initial_coin='2',latency_ms=0)
        settings['strategy']=profile
        engine=ReplayEngine(settings);start=engine.now
        engine.on_event(book(start,bid=100,ask=102,quantity=0));engine.start()
        report=engine.valuation()
        self.assertEqual(report['theory']['price'],101)
        self.assertEqual(report['theory']['strategy_id'],profile['id'])
        self.assertEqual(report['account']['theory_unrealized_pnl'],2)
        # An adapter can publish its computed price; valuation consumes it without recalculating it.
        engine.accept_theory(TheoryOutput('105',start,start+100,'custom_strategy','custom reference'))
        engine.place('sell','102','1');engine.advance(start)
        engine.on_event(tick(start+1,102,1))
        frozen=deepcopy(engine.last_fills[0])
        self.assertEqual(frozen['theory']['price'],105)
        self.assertEqual(frozen['account']['theory_unrealized_pnl'],5)
        engine.on_event(book(start+2,bid=110,ask=112,quantity=0))
        self.assertEqual(engine.valuation()['theory']['price'],111)
        self.assertEqual(engine.last_fills[0],frozen)
        engine.advance(start+503)
        self.assertIsNone(engine.valuation()['theory'])
        self.assertIsNone(engine.valuation()['account']['theory_unrealized_pnl'])
        engine.accept_theory(TheoryOutput('120',start+504,start+600,'future'))
        self.assertIsNone(engine.valuation()['theory'])

    def test_missing_strategy_theory_never_falls_back_to_manual_fixed_price(self):
        payload=maker();payload['parameters']['theory_mode']='none'
        settings=config(initial_coin='2',theory_price='999');settings['strategy']=self.registry.save(payload)
        engine=ReplayEngine(settings);engine.on_event(book(engine.now));engine.start()
        self.assertIsNone(engine.valuation()['theory'])
        self.assertIsNone(engine.valuation()['account']['theory_unrealized_pnl'])
        manager=ReplayManager(self.temp.name,SyntheticSource)
        try:
            with self.assertRaisesRegex(ValueError,'고정 이론가'):
                manager.create(dict(ticker='XYZ',start='2026-10-26T12:00',end='2026-10-26T12:01',theory_price='999'))
            self.assertEqual(manager.store.recent(),[])
        finally:manager.close()



class StrategyApiTests(unittest.TestCase):
    def setUp(self):
        from oms_client.api import create_app
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app(dict(TESTING=True,STATE_DIR=self.temp.name,PASSWORD='strategy-password'))
        self.addCleanup(self.app.extensions['close_services'])
        self.client=self.app.test_client()
        page=self.client.get('/login').get_data(as_text=True)
        token=re.search(r'name="csrf_token" value="([^"]+)"',page)[1]
        self.client.post('/login',data={'csrf_token':token,'password':'strategy-password'})
        self.headers={'X-CSRF-Token':self.client.get('/api/session').json['csrf_token']}

    def test_auth_crud_revisions_and_archive(self):
        self.assertEqual(self.app.test_client().get('/api/strategies').status_code,401)
        self.assertEqual(self.client.post('/api/strategies',json=maker()).status_code,403)
        catalog=self.client.get('/api/strategies').json
        self.assertEqual(len(catalog['templates']),3)
        result=self.client.post('/api/strategies',json=maker(),headers=self.headers)
        self.assertEqual(result.status_code,201)
        identifier=result.json['id']
        update=self.client.post(f'/api/strategies/{identifier}/revisions',json=dict(maker(direction='buy'),expected_revision=1),headers=self.headers)
        self.assertEqual(update.status_code,201);self.assertEqual(update.json['revision'],2)
        self.assertEqual(self.client.get(f'/api/strategies/{identifier}?revision=1').json['parameters']['direction'],'both')
        self.assertEqual(self.client.get(f'/api/strategies/{identifier}?revision=-1').status_code,400)
        self.assertEqual(self.client.get(f'/api/strategies/{identifier}?revision=999').status_code,404)
        self.assertEqual(self.client.post(f'/api/strategies/{identifier}/archive',json={'archived':True},headers=self.headers).status_code,200)
        self.assertTrue(self.client.get(f'/api/strategies/{identifier}').json['archived'])
        point=self.client.post('/api/strategies',json={'name':'Point 실험','type':'point2'},headers=self.headers)
        self.assertEqual(point.status_code,201);self.assertFalse(point.json['executable'])
        minmax=self.client.post('/api/strategies',json={'name':'minmaxma 실험','type':'minmaxma','parameters':{'ma_ms':2000}},headers=self.headers)
        self.assertEqual(minmax.status_code,201)
        self.assertEqual(minmax.json['parameters']['ma_ms'],2000)
        self.assertEqual(minmax.json['specification']['source_entrypoint'],'point_st')
        self.assertFalse(minmax.json['executable'])
        response=self.client.get('/features/strategies/controller.js')
        self.assertEqual(response.status_code,200);response.close()

if __name__=='__main__':unittest.main()
