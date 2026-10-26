"""No live credentials/network: signing fixtures and explicit manual intent lifecycle."""
import base64
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oms_client.api import create_app
from oms_client.manual_trading.vault import CredentialVault, Credentials
from oms_client.manual_trading.exchange import amount, signed_request, SpotBroker, ExchangeUncertain, ExchangeRejected
from oms_client.manual_trading.market import MarketSessions, parse_events, subscription
from oms_client.manual_trading.service import ManualTradingService
KEY = dict(exchange='bithumb', label='test only', api_key='fake-access-key-1234', secret='fake-secret-value-5678', passphrase='')
T = 1793062800000

class FakeMarket:

    def __init__(self):
        self.fresh = True
        self.subscriptions = []

    def require_fresh(self, exchange, ticker):
        if not self.fresh:
            raise ValueError('stale')

    def subscribe(self, exchange, ticker):
        self.subscriptions.append((exchange, ticker))

    def snapshot(self, exchange):
        return dict(status='disconnected', book=None, ticks=[], can_order=False)

    def close(self):
        pass

class FakeBroker:

    def __init__(self):
        self.places = []
        self.cancelled = []
        self.balance_calls = 0
        self.failure = None
        self.next_status = 'partial'

    def balances(self, key):
        self.balance_calls += 1
        return [dict(currency='KRW', available='10000', reserved='0')]

    def place(self, key, order):
        self.places.append(dict(order))
        if self.failure:
            raise self.failure
        return dict(status='accepted', exchange_order_id='remote-1')

    def cancel(self, key, order):
        self.cancelled.append(order)
        return dict(status='cancel_requested')

    def order(self, key, order):
        return dict(status=self.next_status, filled='0.5', remaining='0.5', exchange_order_id='remote-1')

class VaultTests(unittest.TestCase):

    def test_encrypted_storage_masked_metadata_and_no_secret_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = CredentialVault(directory)
            metadata = vault.add(KEY)
            self.assertNotIn('api_key', metadata)
            self.assertNotIn('secret', metadata)
            self.assertEqual(metadata['key_hint'], '••••1234')
            self.assertNotIn(KEY['secret'].encode(), vault.path.read_bytes())
            self.assertNotIn(KEY['api_key'].encode(), vault.path.read_bytes())
            self.assertEqual(vault.key_path.stat().st_mode & 511, 384)
            self.assertEqual(vault.path.stat().st_mode & 511, 384)
            restored = CredentialVault(directory).get(metadata['id'])
            self.assertEqual(restored.secret, KEY['secret'])
            self.assertNotIn(KEY['secret'], repr(restored))
            with self.assertRaises(ValueError):
                vault.add(KEY)
            with self.assertRaises(ValueError):
                vault.add(dict(KEY, exchange='okx'))
            with self.assertRaises(ValueError):
                vault.add(dict(KEY, exchange='okx', api_key='short', passphrase='pass'))
            vault.key_path.unlink()
            with self.assertRaises(RuntimeError):
                CredentialVault(directory)

class SigningTests(unittest.TestCase):

    def test_bithumb_query_hash_and_signature_match_exact_body_parameters(self):
        key = Credentials('k', 'bithumb', KEY['api_key'], KEY['secret'])
        params = dict(market='KRW-BTC', side='bid', price='100', volume='0.1', order_type='limit', client_order_id='a' * 32)
        url, headers, body = signed_request(key, 'POST', '/v2/orders', params, now=123, nonce='nonce')
        self.assertEqual(url, 'https://api.bithumb.com/v2/orders')
        self.assertEqual(json.loads(body), params)
        header, payload, signature = headers['Authorization'][7:].split('.')
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        self.assertEqual(claims['timestamp'], 123000)
        from urllib.parse import urlencode
        self.assertEqual(claims['query_hash'], hashlib.sha512(urlencode(params).encode()).hexdigest())
        expected = base64.urlsafe_b64encode(hmac.new(KEY['secret'].encode(), (header + '.' + payload).encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
        self.assertEqual(signature, expected)

    def test_okx_signs_query_or_body_and_amount_never_rounds(self):
        key = Credentials('k', 'okx', 'test-api', 'test-secret', 'test-pass')
        url, headers, body = signed_request(key, 'GET', '/api/v5/trade/order', {'instId': 'BTC-USDT', 'clOrdId': 'a' * 32}, now=0)
        path = url.removeprefix('https://openapi.okx.com')
        expected = base64.b64encode(hmac.new(b'test-secret', ('1970-01-01T00:00:00.000ZGET' + path).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(headers['OK-ACCESS-SIGN'], expected)
        self.assertIsNone(body)
        self.assertEqual(amount('999999999999999999.123456789123456789'), '999999999999999999.123456789123456789')
        self.assertEqual(amount('0.01000'), '0.01')
        for invalid in [True, 0, 1.1, 'NaN', 'Infinity', '-1', '1e99']:
            with self.assertRaises(ValueError):
                amount(invalid)

    def test_spot_broker_ack_is_not_fill_and_partial_fills_from_actual_order_query(self):
        calls = []

        class Transport:
            result = {'order_id': 'remote'}

            def request(self, *args):
                calls.append(args)
                return self.result
        transport = Transport()
        broker = SpotBroker(transport)
        key = Credentials('k', 'bithumb', 'fake-key', 'fake-secret')
        order = dict(ticker='BTC', side='buy', price='100', quantity='1', client_order_id='b' * 32)
        self.assertEqual(broker.place(key, order)['status'], 'accepted')
        self.assertEqual(calls[-1][2], '/v2/orders')
        self.assertEqual(calls[-1][3]['order_type'], 'limit')
        transport.result = dict(client_order_id='b' * 32, market='KRW-BTC', state='wait', executed_volume='0.2', remaining_volume='0.8', uuid='remote')
        self.assertEqual(broker.order(key, order)['status'], 'partial')
        transport.result['client_order_id'] = 'wrong'
        with self.assertRaises(ExchangeUncertain):
            broker.order(key, order)
        transport.result = {}
        with self.assertRaises(ExchangeUncertain):
            broker.place(key, order)
        okx = Credentials('o', 'okx', 'fake-key', 'fake-secret', 'pass')
        transport.result = [{'ordId': 'o1'}]
        broker.place(okx, order)
        self.assertEqual(calls[-1][3]['instId'], 'BTC-USDT')
        self.assertEqual(calls[-1][3]['tdMode'], 'cash')

class MarketTests(unittest.TestCase):

    def test_public_subscriptions_and_spot_only_parsing(self):
        self.assertEqual([item['channel'] for item in subscription('okx', 'BTC')['args']], ['books5', 'trades'])
        self.assertNotIn('SWAP', json.dumps(subscription('okx', 'BTC')))
        raw = dict(arg={'channel': 'books5', 'instId': 'BTC-USDT'}, data=[dict(ts=str(T), bids=[['100', '2']], asks=[['101', '3']])])
        parsed = parse_events('okx', 'BTC', raw, T)[0]
        self.assertEqual(parsed['bids'][0], ['100', '2'])
        raw['arg']['instId'] = 'BTC-USDT-SWAP'
        self.assertEqual(parse_events('okx', 'BTC', raw, T), [])
        bithumb = dict(type='trade', code='KRW-BTC', trade_timestamp=T, sequential_id=123, trade_price=Decimal('100.00000001'), trade_volume=Decimal('0.000001'), ask_bid='ASK')
        tick = parse_events('bithumb', 'BTC', bithumb, T)[0]
        self.assertEqual(tick['side'], 'sell')
        self.assertEqual(tick['price'], '100.00000001')

    def test_generation_dedup_disconnect_and_stale_order_guard(self):
        created = []
        clock = [T / 1000]

        class Feed:

            def __init__(self, exchange, ticker, event, status):
                self.event = event
                self.status = status
                created.append(self)

            def start(self):
                pass

            def stop(self):
                pass
        market = MarketSessions(feed_factory=Feed, clock=lambda: clock[0])
        self.addCleanup(market.close)
        market.subscribe('bithumb', 'BTC')
        book = dict(kind='book', time=T, received_ms=T, bids=[['100', '1']], asks=[['101', '1']])
        created[0].event(book)
        market.require_fresh('bithumb', 'BTC')
        tick = dict(kind='tick', id='one', time=T, received_ms=T, price='100', quantity='1', side='buy')
        created[0].event(tick)
        created[0].event(tick)
        self.assertEqual(len(market.snapshot('bithumb')['ticks']), 1)
        clock[0] += 2.001
        with self.assertRaises(ValueError):
            market.require_fresh('bithumb', 'BTC')
        market.subscribe('bithumb', 'ETH')
        created[0].event(book)
        self.assertIsNone(market.snapshot('bithumb')['book'])
        created[-1].event(dict(book, time=int(clock[0] * 1000), received_ms=int(clock[0] * 1000)))
        created[-1].status('reconnecting')
        with self.assertRaises(ValueError):
            market.require_fresh('bithumb', 'ETH')

class ServiceTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.market = FakeMarket()
        self.broker = FakeBroker()
        self.service = ManualTradingService(self.temp.name, self.market, self.broker)
        self.addCleanup(self.service.close)
        self.key = self.service.register_key(KEY)
        self.payload = dict(key_id=self.key['id'], exchange='bithumb', ticker='BTC', side='buy', price='100', quantity='1', client_order_id='a' * 32, confirm_live=True)

    def test_public_only_until_explicit_verified_fresh_live_intent(self):
        self.service.snapshot('bithumb')
        self.assertEqual(self.broker.places, [])
        self.assertEqual(self.broker.balance_calls, 0)
        for payload in [self.payload, dict(self.payload, confirm_live=False), dict(self.payload, exchange='okx')]:
            with self.assertRaises(ValueError):
                self.service.submit(payload)
        self.service.verify_key(self.key['id'])
        self.market.fresh = False
        with self.assertRaises(ValueError):
            self.service.submit(self.payload)
        self.assertEqual(self.service.journal.list(), [])
        self.market.fresh = True
        result = self.service.submit(self.payload)
        self.assertEqual(result['status'], 'accepted')
        self.assertIsNone(result['filled'])
        self.assertEqual(self.service.submit(self.payload), result)
        self.assertEqual(len(self.broker.places), 1)
        with self.assertRaises(ValueError):
            self.service.submit(dict(self.payload, price='101'))
        with self.assertRaises(ValueError):
            self.service.delete_key(self.key['id'])

    def test_unknown_order_survives_restart_without_resubmit_then_reconciles(self):
        self.service.verify_key(self.key['id'])
        self.broker.failure = TimeoutError('must not expose secret')
        result = self.service.submit(self.payload)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('must not expose', json.dumps(result))
        restarted = ManualTradingService(self.temp.name, self.market, self.broker)
        self.assertEqual(restarted.submit(self.payload)['status'], 'unknown')
        self.assertEqual(len(self.broker.places), 1)
        with self.assertRaises(ValueError):
            restarted.submit(dict(self.payload, client_order_id='b' * 32))
        result = restarted.order_action(self.key['id'], 'a' * 32, 'refresh')
        self.assertEqual(result['status'], 'partial')
        self.assertGreater(result['last_checked_ms'], 0)
        checked = result['last_checked_ms']
        result = restarted.order_action(self.key['id'], 'a' * 32, 'cancel')
        self.assertEqual(result['status'], 'cancel_requested')
        self.assertEqual(result['last_checked_ms'], checked)
        self.broker.next_status = 'cancelled'
        self.assertEqual(restarted.order_action(self.key['id'], 'a' * 32, 'refresh')['status'], 'cancelled')
        restarted.delete_key(self.key['id'])
        self.assertEqual(restarted.vault.list(), [])

    def test_rejection_and_fill_regression_do_not_fabricate_success(self):
        self.service.verify_key(self.key['id'])
        self.broker.failure = ExchangeRejected('rejected')
        self.assertEqual(self.service.submit(self.payload)['status'], 'rejected')
        self.broker.failure = None
        self.service.submit(dict(self.payload, client_order_id='b' * 32))
        self.service.order_action(self.key['id'], 'b' * 32, 'refresh')
        before = self.service.journal.get(self.key['id'], 'b' * 32)
        with self.assertRaises(ValueError):
            self.service.journal.update(self.key['id'], 'b' * 32, {'filled': '0.1'})
        self.assertEqual(self.service.journal.get(self.key['id'], 'b' * 32), before)

class ManualApiTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker = FakeBroker()
        self.market = FakeMarket()
        self.app = create_app(dict(TESTING=True, MANUAL_DIRECT_TRADING_ENABLED=True, STATE_DIR=self.temp.name, PASSWORD='manual-test', MANUAL_TRADING_FACTORY=lambda directory: ManualTradingService(directory, self.market, self.broker)))
        self.addCleanup(self.app.extensions['close_services'])
        self.client = self.app.test_client()
        csrf = re.search('name="csrf_token" value="([^"]+)"', self.client.get('/login').text)[1]
        self.client.post('/login', data=dict(password='manual-test', csrf_token=csrf))
        self.headers = {'X-CSRF-Token': self.client.get('/api/session').json['csrf_token']}

    def test_remote_default_blocks_every_private_action_before_creating_service(self):
        self.app.config['MANUAL_DIRECT_TRADING_ENABLED'] = False
        capability = self.client.get('/api/session').json['capabilities']
        self.assertFalse(capability['manual_spot_orders'])
        self.assertTrue(capability['manual_market_view'])
        for path, method, body in [
            ('/api/manual/keys', 'post', KEY),
            ('/api/manual/keys/key/verify', 'post', {}),
            ('/api/manual/keys/key', 'delete', {}),
            ('/api/manual/orders', 'post', {}),
            ('/api/manual/orders/key/client/refresh', 'post', {}),
            ('/api/manual/orders/key/client/cancel', 'post', {}),
        ]:
            response = getattr(self.client, method)(path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, 409, path)
            self.assertEqual(response.json['code'], 'REMOTE_EXECUTION_REQUIRED')
        self.assertNotIn('manual_trading', self.app.extensions)
        self.assertEqual(self.broker.balance_calls, 0)
        self.assertEqual(self.broker.places, [])
        response = self.client.post('/api/manual/market', json={'exchange':'bithumb','ticker':'BTC'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.market.subscriptions, [('bithumb','BTC')])

    def test_auth_csrf_register_verify_market_order_cancel_and_secrets_never_return(self):
        self.assertEqual(self.app.test_client().get('/api/manual').status_code, 401)
        self.assertEqual(self.client.post('/api/manual/keys', json=KEY).status_code, 403)
        self.assertEqual(self.broker.balance_calls, 0)
        key = self.client.post('/api/manual/keys', json=KEY, headers=self.headers)
        self.assertEqual(key.status_code, 200, key.text)
        self.assertNotIn(KEY['secret'], key.text)
        self.assertNotIn(KEY['api_key'], key.text)
        identifier = key.json['id']
        check = self.client.post(f'/api/manual/keys/{identifier}/verify', json={}, headers=self.headers)
        self.assertEqual(check.status_code, 200, check.text)
        self.assertEqual(self.broker.balance_calls, 1)
        self.assertEqual(self.broker.places, [])
        self.assertEqual(self.client.post('/api/manual/market', json=dict(exchange='bithumb', ticker='BTC'), headers=self.headers).status_code, 200)
        payload = dict(key_id=identifier, exchange='bithumb', ticker='BTC', side='buy', price='100', quantity='1', client_order_id='a' * 32, confirm_live=True)
        self.market.fresh = False
        refused = self.client.post('/api/manual/orders', json=payload, headers=self.headers)
        self.assertEqual(refused.status_code, 400)
        self.assertTrue(refused.json['order_not_submitted'])
        self.assertEqual(self.broker.places, [])
        self.market.fresh = True
        order = self.client.post('/api/manual/orders', json=payload, headers=self.headers)
        self.assertEqual(order.status_code, 200, order.text)
        self.assertEqual(order.json['status'], 'accepted')
        self.assertEqual(self.client.post(f'/api/manual/orders/{identifier}/{'a' * 32}/cancel', json={}, headers=self.headers).json['status'], 'cancel_requested')
        snapshot = self.client.get('/api/manual')
        self.assertIn('no-store', snapshot.headers['Cache-Control'])
        self.assertNotIn(KEY['secret'], snapshot.text)
        asset = self.client.get('/features/manual_trading/controller.js')
        self.assertEqual(asset.status_code, 200)
        asset.close()
        self.assertEqual(self.client.get('/manual_trading/master.key').status_code, 404)

class TransportTests(unittest.TestCase):

    def test_uncertain_exchange_failures_are_not_rejected_or_retried(self):
        from unittest.mock import patch, MagicMock
        from urllib.error import HTTPError
        from oms_client.manual_trading.exchange import RestTransport, NoRedirect
        key = Credentials('o', 'okx', 'fake-key', 'fake-secret', 'pass')
        for response in [{'code': '50004', 'data': []}, {'code': '1', 'data': [{'sCode': '50004'}]}, {'code': '0', 'data': [{'sCode': '1', 'ordId': 'untrusted'}]}, {'code': '0', 'data': ['invalid']}, {'message': 'not an exchange acknowledgement'}]:
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            with patch('oms_client.manual_trading.exchange.build_opener', return_value=opener):
                with self.assertRaises(ExchangeUncertain):
                    RestTransport().request(key, 'POST', '/api/v5/trade/order', {})
            self.assertEqual(opener.open.call_count, 1)
        for code in [301, 429, 500, 504]:
            opener = MagicMock()
            opener.open.side_effect = HTTPError('https://example.invalid', code, 'secret raw error', {}, None)
            with patch('oms_client.manual_trading.exchange.build_opener', return_value=opener):
                with self.assertRaises(ExchangeUncertain) as context:
                    RestTransport().request(key, 'POST', '/api/v5/trade/order', {})
            self.assertNotIn('secret raw error', str(context.exception))
            self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_order_reports_reject_impossible_fill_quantities(self):

        class Transport:
            result = None

            def request(self, *args):
                return self.result
        transport = Transport()
        broker = SpotBroker(transport)
        order = dict(ticker='BTC', side='buy', price='100', quantity='1', client_order_id='c' * 32)
        transport.result = [dict(clOrdId='c' * 32, instId='BTC-USDT', state='filled', accFillSz='0.5')]
        with self.assertRaises(ExchangeUncertain):
            broker.order(Credentials('o', 'okx', 'fake-key', 'fake-secret', 'pass'), order)
        transport.result = dict(client_order_id='c' * 32, market='KRW-BTC', state='wait', executed_volume='0.6', remaining_volume='0.6')
        with self.assertRaises(ExchangeUncertain):
            broker.order(Credentials('b', 'bithumb', 'fake-key', 'fake-secret'), order)

class JournalVisibilityTests(unittest.TestCase):

    def test_old_open_orders_remain_visible_after_more_than_100_terminal_orders(self):
        from oms_client.manual_trading.journal import OrderJournal
        with tempfile.TemporaryDirectory() as directory:
            journal = OrderJournal(directory)
            body = dict(exchange='bithumb', ticker='BTC', side='buy', price='100', quantity='1', client_order_id='first-open-order')
            journal.reserve('key', body)
            journal.update('key', body['client_order_id'], {'status': 'accepted'})
            for i in range(105):
                item = dict(body, client_order_id=f'closed-{i}')
                journal.reserve('key', item)
                journal.update('key', item['client_order_id'], {'status': 'rejected'})
            self.assertEqual(len(journal.list()), 101)
            self.assertTrue(any((item['client_order_id'] == 'first-open-order' for item in journal.list())))
            self.assertTrue(journal.has_open('key'))
if __name__ == '__main__':
    unittest.main()
