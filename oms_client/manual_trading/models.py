"""Immutable order intent and typed exchange reports at component boundaries."""

from dataclasses import dataclass
from decimal import Decimal
import re
from typing import TypedDict

from .exchange import amount, symbol


class OrderReport(TypedDict, total=False):
    key_id: str
    client_order_id: str
    exchange: str
    ticker: str
    side: str
    price: str
    quantity: str
    status: str
    exchange_order_id: str
    filled: str | None
    remaining: str
    fee: str
    fee_currency: str
    average_price: str | None
    error: str | None
    created_ms: int
    updated_ms: int
    last_checked_ms: int


@dataclass(frozen=True)
class LimitOrderIntent:
    key_id: str
    exchange: str
    ticker: str
    side: str
    price: Decimal
    quantity: Decimal
    client_order_id: str

    @classmethod
    def from_payload(cls, payload):
        fields = {
            'key_id',
            'exchange',
            'ticker',
            'side',
            'price',
            'quantity',
            'client_order_id',
            'confirm_live',
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or payload.get('confirm_live') is not True
        ):
            raise ValueError('실제 계좌 주문 확인과 모든 주문 항목을 입력하세요.')
        if payload['side'] not in ('buy', 'sell'):
            raise ValueError('매수 또는 매도를 선택하세요.')
        if not isinstance(payload['key_id'], str):
            raise ValueError('등록된 API 키를 선택하세요.')
        if not isinstance(payload['client_order_id'], str) or not re.fullmatch(
            '[A-Za-z0-9]{16,32}', payload['client_order_id']
        ):
            raise ValueError('주문 요청 ID를 확인하세요.')
        symbol(payload['exchange'], payload['ticker'])
        return cls(
            key_id=payload['key_id'],
            exchange=payload['exchange'],
            ticker=payload['ticker'],
            side=payload['side'],
            price=Decimal(amount(payload['price'])),
            quantity=Decimal(amount(payload['quantity'])),
            client_order_id=payload['client_order_id'],
        )

    def journal_body(self) -> OrderReport:
        return dict(
            exchange=self.exchange,
            ticker=self.ticker,
            side=self.side,
            price=amount(self.price),
            quantity=amount(self.quantity),
            client_order_id=self.client_order_id,
        )
