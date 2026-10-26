"""Fixed-host signed REST adapters. No retry on any mutating request."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import OrderReport
    from .vault import Credentials

import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid


class ExchangeRejected(RuntimeError):
    pass


class ExchangeUncertain(RuntimeError):
    pass


def amount(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int, Decimal))
        or len(str(value)) > 80
    ):
        raise ValueError('가격·수량은 숫자 문자열로 입력하세요.')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('가격·수량 형식을 확인하세요.') from None
    if not number.is_finite() or not Decimal('0.000000000000000001') <= number <= Decimal(
        '1000000000000000000'
    ):
        raise ValueError('가격·수량은 0보다 큰 지원 범위 안의 값이어야 합니다.')
    plain = format(number, 'f')
    return plain.rstrip('0').rstrip('.') if '.' in plain else plain


def symbol(exchange, ticker):
    if (
        exchange not in ('bithumb', 'okx')
        or not isinstance(ticker, str)
        or (not re.fullmatch('[A-Z0-9]{1,20}', ticker))
    ):
        raise ValueError('빗썸·OKX 거래소와 코인 심볼을 확인하세요.')
    return 'KRW-' + ticker if exchange == 'bithumb' else ticker + '-USDT'


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def signed_request(credentials, method, path, params, *, now=None, nonce=None):
    now = time.time() if now is None else now
    query = urlencode(params)
    body = json.dumps(params, separators=(',', ':'), ensure_ascii=True) if method == 'POST' else ''
    request_path = path + ('?' + query if query and method != 'POST' else '')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    if credentials.exchange == 'bithumb':
        claims = dict(
            access_key=credentials.api_key,
            nonce=nonce or str(uuid.uuid4()),
            timestamp=int(now * 1000),
        )
        if params:
            claims.update(
                query_hash=hashlib.sha512(query.encode()).hexdigest(), query_hash_alg='SHA512'
            )
        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64(json.dumps(claims, separators=(',', ':')).encode())
        signature = _b64(
            hmac.new(
                credentials.secret.encode(), f'{header}.{payload}'.encode(), hashlib.sha256
            ).digest()
        )
        headers['Authorization'] = f'Bearer {header}.{payload}.{signature}'
        host = 'https://api.bithumb.com'
    else:
        stamp = (
            datetime.fromtimestamp(now, timezone.utc)
            .isoformat(timespec='milliseconds')
            .replace('+00:00', 'Z')
        )
        signature = base64.b64encode(
            hmac.new(
                credentials.secret.encode(),
                (stamp + method + request_path + body).encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        headers.update(
            {
                'OK-ACCESS-KEY': credentials.api_key,
                'OK-ACCESS-SIGN': signature,
                'OK-ACCESS-TIMESTAMP': stamp,
                'OK-ACCESS-PASSPHRASE': credentials.passphrase,
            }
        )
        host = 'https://openapi.okx.com'
    return (host + request_path, headers, body.encode() if body else None)


class NoRedirect(HTTPRedirectHandler):

    def redirect_request(self, *args, **kwargs):
        return None


class RestTransport:

    def request(self, credentials, method, path, params):
        url, headers, body = signed_request(credentials, method, path, params)
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with build_opener(NoRedirect()).open(request, timeout=6) as response:
                raw = response.read(2**20 + 1)
            if len(raw) > 2**20:
                raise ValueError('oversized response')
            data = json.loads(raw, parse_float=Decimal)
        except HTTPError as error:
            if error.code in {400, 401, 403, 404, 422}:
                raise ExchangeRejected(
                    f'거래소가 요청을 거절했습니다 (HTTP {error.code}). 키 권한·잔고·호가 단위를 확인하세요.'
                ) from None
            raise ExchangeUncertain(
                '거래소 응답을 확인하지 못했습니다. 주문을 다시 전송하지 말고 상태를 조회하세요.'
            ) from None
        except Exception:
            raise ExchangeUncertain(
                '거래소 응답을 확인하지 못했습니다. 주문을 다시 전송하지 말고 상태를 조회하세요.'
            ) from None
        if credentials.exchange == 'okx':
            if not isinstance(data, dict) or 'code' not in data:
                raise ExchangeUncertain('OKX 응답 형식을 확인하지 못했습니다.')
            rejected_codes = {
                '50101',
                '50103',
                '50104',
                '50105',
                '50106',
                '50107',
                '50108',
                '50109',
                '50110',
                '50111',
                '50112',
                '50113',
                '50114',
                '50115',
                '50119',
                '51000',
                '51001',
                '51006',
                '51008',
                '51010',
                '51020',
                '51119',
                '51121',
                '51131',
            }
            rows = data.get('data')
            codes = [] if str(data['code']) in {'0', '1', '2'} else [str(data['code'])]
            if isinstance(rows, list):
                if not all((isinstance(item, dict) for item in rows)):
                    raise ExchangeUncertain('OKX 응답 형식을 확인하지 못했습니다.')
                codes.extend(
                    (str(item['sCode']) for item in rows if str(item.get('sCode', '0')) != '0')
                )
            errors = codes
            if errors:
                if all((code in rejected_codes for code in errors)):
                    raise ExchangeRejected(
                        'OKX가 요청을 거절했습니다. 키·잔고·주문 단위를 확인하세요.'
                    )
                raise ExchangeUncertain('OKX 처리 결과가 불명확합니다. 주문 상태를 조회하세요.')
            if str(data['code']) != '0' or not isinstance(rows, list):
                raise ExchangeUncertain('OKX 응답 형식을 확인하지 못했습니다.')
            data = rows
        elif isinstance(data, dict) and data.get('error'):
            raise ExchangeRejected(
                '빗썸이 요청을 거절했습니다. 키 권한·잔고·주문 단위를 확인하세요.'
            )
        return data


class SpotBroker:

    def __init__(self, transport=None):
        self.transport = transport or RestTransport()

    def balances(self, key):
        if key.exchange == 'bithumb':
            data = self.transport.request(key, 'GET', '/v1/accounts', {})
            if not isinstance(data, list):
                raise ExchangeUncertain('잔고 응답을 확인하지 못했습니다.')
            return [
                dict(
                    currency=str(item['currency']),
                    available=str(item['balance']),
                    reserved=str(item['locked']),
                )
                for item in data
            ]
        data = self.transport.request(key, 'GET', '/api/v5/account/balance', {})
        return [
            dict(
                currency=str(item['ccy']),
                available=str(item.get('availBal', '')),
                reserved=str(item.get('frozenBal', '')),
            )
            for group in data
            for item in group.get('details', [])
        ]

    def place(self, key: Credentials, order: OrderReport) -> OrderReport:
        pair = symbol(key.exchange, order['ticker'])
        if key.exchange == 'bithumb':
            data = self.transport.request(
                key,
                'POST',
                '/v2/orders',
                dict(
                    market=pair,
                    side='bid' if order['side'] == 'buy' else 'ask',
                    price=order['price'],
                    volume=order['quantity'],
                    order_type='limit',
                    client_order_id=order['client_order_id'],
                ),
            )
            identifier = data.get('order_id') if isinstance(data, dict) else None
        else:
            data = self.transport.request(
                key,
                'POST',
                '/api/v5/trade/order',
                dict(
                    instId=pair,
                    tdMode='cash',
                    side=order['side'],
                    ordType='limit',
                    px=order['price'],
                    sz=order['quantity'],
                    clOrdId=order['client_order_id'],
                ),
            )
            identifier = data[0].get('ordId') if data else None
        if not isinstance(identifier, str) or not identifier:
            raise ExchangeUncertain('주문 접수 번호를 확인하지 못했습니다. 상태 조회가 필요합니다.')
        return dict(exchange_order_id=identifier, status='accepted')

    def cancel(self, key: Credentials, order: OrderReport) -> OrderReport:
        if key.exchange == 'bithumb':
            self.transport.request(
                key, 'DELETE', '/v2/order', {'client_order_id': order['client_order_id']}
            )
        else:
            self.transport.request(
                key,
                'POST',
                '/api/v5/trade/cancel-order',
                dict(
                    instId=symbol(key.exchange, order['ticker']), clOrdId=order['client_order_id']
                ),
            )
        return dict(status='cancel_requested')

    def order(self, key: Credentials, order: OrderReport) -> OrderReport:
        if key.exchange == 'bithumb':
            data = self.transport.request(
                key, 'GET', '/v1/order', {'client_order_id': order['client_order_id']}
            )
            if data.get('client_order_id') != order['client_order_id'] or data.get(
                'market'
            ) != symbol(key.exchange, order['ticker']):
                raise ExchangeUncertain('조회한 주문의 식별자가 일치하지 않습니다.')
            if 'executed_volume' not in data or 'remaining_volume' not in data:
                raise ExchangeUncertain('주문 수량 응답이 없습니다.')
            filled = str(data['executed_volume'])
            remaining = str(data['remaining_volume'])
            if not all(
                (
                    Decimal(value).is_finite() and 0 <= Decimal(value) <= Decimal(order['quantity'])
                    for value in (filled, remaining)
                )
            ):
                raise ExchangeUncertain('주문 수량 응답을 확인하지 못했습니다.')
            with localcontext() as context:
                context.prec = 200
                if Decimal(filled) + Decimal(remaining) > Decimal(order['quantity']):
                    raise ExchangeUncertain('주문 잔량과 누적 체결이 주문 수량을 초과했습니다.')
            state = {
                'wait': 'partial' if Decimal(filled) > 0 else 'live',
                'watch': 'live',
                'done': 'filled' if Decimal(filled) == Decimal(order['quantity']) else 'completed',
                'cancel': 'cancelled',
            }.get(data.get('state'), 'unknown')
            return dict(
                status=state,
                exchange_order_id=data.get('uuid'),
                filled=filled,
                remaining=remaining,
                fee=str(data.get('paid_fee', '')),
                average_price=None,
            )
        data = self.transport.request(
            key,
            'GET',
            '/api/v5/trade/order',
            dict(instId=symbol(key.exchange, order['ticker']), clOrdId=order['client_order_id']),
        )
        item = data[0] if data else {}
        if item.get('clOrdId') != order['client_order_id'] or item.get('instId') != symbol(
            key.exchange, order['ticker']
        ):
            raise ExchangeUncertain('조회한 주문의 식별자가 일치하지 않습니다.')
        if 'accFillSz' not in item:
            raise ExchangeUncertain('주문 체결 수량 응답이 없습니다.')
        filled = Decimal(str(item['accFillSz']))
        with localcontext() as context:
            context.prec = 200
            remaining = Decimal(order['quantity']) - filled
        if not filled.is_finite() or not 0 <= filled <= Decimal(order['quantity']):
            raise ExchangeUncertain('체결 수량 응답을 확인하지 못했습니다.')
        if item.get('state') == 'filled' and filled != Decimal(order['quantity']):
            raise ExchangeUncertain('전량 체결 상태와 누적 수량이 일치하지 않습니다.')
        return dict(
            status={
                'live': 'live',
                'partially_filled': 'partial',
                'filled': 'filled',
                'canceled': 'cancelled',
                'mmp_canceled': 'cancelled',
            }.get(item.get('state'), 'unknown'),
            exchange_order_id=item.get('ordId'),
            filled=str(filled),
            remaining=str(remaining),
            fee=str(item.get('fee', '')),
            fee_currency=str(item.get('feeCcy', '')),
            average_price=item.get('avgPx') or None,
        )
