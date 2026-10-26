"""Manual spot command boundary. Public data never creates an order intent."""

import threading
import time

from .exchange import SpotBroker, ExchangeRejected
from .journal import OrderJournal
from .market import MarketSessions
from .models import LimitOrderIntent, OrderReport
from .vault import CredentialVault


class OrderNotSubmitted(ValueError):
    """Validation failed before any new private order dispatch."""


class ManualTradingService:
    def __init__(self, directory, market=None, broker=None):
        self.vault = CredentialVault(directory)
        self.journal = OrderJournal(directory)
        self.market = market or MarketSessions()
        self.broker = broker or SpotBroker()
        self.lock = threading.RLock()

    def register_key(self, payload):
        with self.lock:
            return self.vault.add(payload)

    def verify_key(self, identifier):
        with self.lock:
            key = self.vault.get(identifier)
            balances = self.broker.balances(key)
            self.vault.verified(identifier)
            return dict(balances=balances, keys=self.vault.list())

    def delete_key(self, identifier):
        with self.lock:
            if self.journal.has_open(identifier):
                raise ValueError(
                    '미종결·미확인 주문이 있어 키를 삭제할 수 없습니다. 주문 상태를 확인하세요.'
                )
            self.vault.delete(identifier)

    def submit(self, payload) -> OrderReport:
        with self.lock:
            # Only this pre-dispatch boundary may certify that a new order was not sent.
            try:
                intent = LimitOrderIntent.from_payload(payload)
                key = self.vault.get(intent.key_id)
                if key.exchange != intent.exchange:
                    raise ValueError('키와 주문 거래소가 다릅니다.')
                if not any(
                    item['id'] == key.identifier and item['verified_ms']
                    for item in self.vault.list()
                ):
                    raise ValueError('잔고 조회로 API 키 연결을 먼저 확인하세요.')
                try:
                    existing = self.journal.get(key.identifier, intent.client_order_id)
                except ValueError:
                    existing = None
                if not existing:
                    self.market.require_fresh(key.exchange, intent.ticker)
                order, is_new = self.journal.reserve(key.identifier, intent.journal_body())
            except ValueError as error:
                raise OrderNotSubmitted(str(error)) from None
            # Existing IDs are returned after disconnection; never re-send an existing intent.
            if not is_new:
                return order
            try:
                changes = self.broker.place(key, order)
            except ExchangeRejected as error:
                changes = dict(status='rejected', error=str(error))
            except Exception:
                changes = dict(
                    status='unknown',
                    error='접수 여부 미확인. 같은 주문을 새 ID로 다시 보내지 말고 상태 조회를 실행하세요.',
                )
            return self.journal.update(key.identifier, intent.client_order_id, changes)

    def order_action(self, key_id, client_id, action) -> OrderReport:
        if action not in ('refresh', 'cancel'):
            raise ValueError('주문 작업을 확인하세요.')
        with self.lock:
            order = self.journal.get(key_id, client_id)
            if order['status'] in {'filled', 'completed', 'cancelled'} or (
                action == 'cancel' and order['status'] == 'rejected'
            ):
                return order
            key = self.vault.get(key_id)
            try:
                if action == 'refresh':
                    changes = self.broker.order(key, order)
                    changes['last_checked_ms'] = int(time.time() * 1000)
                else:
                    # A crash or timeout must never become a confirmed cancellation.
                    self.journal.update(key_id, client_id, {'status': 'cancel_requested'})
                    changes = self.broker.cancel(key, order)
                changes['error'] = None
            except Exception:
                changes = dict(
                    error='거래소 상태를 확인하지 못했습니다. 상태 조회를 다시 실행하세요.'
                )
                if action == 'cancel':
                    changes['status'] = 'unknown'
            return self.journal.update(key_id, client_id, changes)

    def snapshot(self, exchange):
        if exchange not in ('bithumb', 'okx'):
            raise ValueError('거래소를 선택하세요.')
        return dict(
            keys=self.vault.list(),
            market=self.market.snapshot(exchange),
            orders=self.journal.list(),
        )

    def close(self):
        self.market.close()
