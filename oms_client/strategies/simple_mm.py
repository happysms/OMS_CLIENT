"""Bithumb best-bid/best-ask MM planner; no broker, clock or ledger side effects.

Inputs are one coherent observed account/order snapshot and an external-only
book. An executor must reserve and register intents atomically before the next
evaluation. This module does not simulate fills or connect to the paper API.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import re


ZERO = Decimal(0)


def _amount(value, name, *, positive=False, signed=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name}: use a decimal string, integer or Decimal")
    if len(str(value)) > 80:
        raise ValueError(f"{name}: amount is too large")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name}: invalid decimal") from None
    if (
        not number.is_finite()
        or number.copy_abs() > Decimal('1e18')
        or len(number.as_tuple().digits) > 28
        or not -18 <= number.as_tuple().exponent <= 18
        or (not signed and number < 0)
        or (positive and number <= 0)
    ):
        raise ValueError(f"{name}: invalid amount or precision")
    return number


def _label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError(f"{name}: invalid identifier")
    return value


def _ticker(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9]{1,35}', value.strip()):
        raise ValueError("ticker: enter the base coin symbol")
    ticker = value.strip().upper()
    if ticker == 'KRW':
        raise ValueError("ticker: KRW is the quote currency")
    return ticker


@dataclass(frozen=True)
class StartInputs:
    ticker: str
    initial_krw: Decimal = Decimal('1000000')
    initial_coin: Decimal = ZERO

    def __post_init__(self):
        object.__setattr__(self, 'ticker', _ticker(self.ticker))
        for name in ('initial_krw', 'initial_coin'):
            object.__setattr__(self, name, _amount(getattr(self, name), name))

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) - {
            'ticker',
            'initial_krw',
            'initial_coin',
        }:
            raise ValueError('Enter ticker, initial_krw and initial_coin')
        return cls(
            payload.get('ticker'),
            payload.get('initial_krw', '1000000'),
            payload.get('initial_coin', '0'),
        )

    def account_seed(self, name):
        """Build the proposed virtual-account creation payload, without saving it."""
        _label(name, 'name')
        costs = (
            [{'currency': self.ticker, 'avg_cost': None, 'cost_policy': 'start_mark'}]
            if self.initial_coin
            else []
        )
        return {
            'schema_version': 1,
            'name': name,
            'venue_configs': [
                {
                    'venue': 'bithumb',
                    'trading_enabled': True,
                    'account_mode': 'spot_cash',
                    'initial_state': {
                        'balances': [
                            {'currency': 'KRW', 'total': str(self.initial_krw)},
                            {'currency': self.ticker, 'total': str(self.initial_coin)},
                        ],
                        'spot_cost_basis': costs,
                        'swap_positions': [],
                    },
                }
            ],
        }


@dataclass(frozen=True)
class MMConfig:
    ticker: str
    quantity_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    maker_fee_bps: Decimal
    taker_fee_bps: Decimal
    fee_quantum: Decimal
    max_quote_age_ms: int = 1000

    def __post_init__(self):
        object.__setattr__(self, 'ticker', _ticker(self.ticker))
        for name in ('quantity_step', 'min_qty', 'min_notional', 'fee_quantum'):
            object.__setattr__(self, name, _amount(getattr(self, name), name, positive=True))
        for name in ('maker_fee_bps', 'taker_fee_bps'):
            value = _amount(getattr(self, name), name, signed=True)
            if value.copy_abs() > 1000:
                raise ValueError(f'{name}: fee outside supported range')
            object.__setattr__(self, name, value)
        if type(self.max_quote_age_ms) is not int or self.max_quote_age_ms < 0:
            raise ValueError('max_quote_age_ms: nonnegative integer required')


@dataclass(frozen=True)
class QuoteView:
    ticker: str
    version: str
    bid: Decimal
    ask: Decimal
    desktop_time_ms: int
    includes_own_orders: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'ticker', _ticker(self.ticker))
        _label(self.version, 'quote version')
        for name in ('bid', 'ask'):
            object.__setattr__(self, name, _amount(getattr(self, name), name))
        if type(self.desktop_time_ms) is not int or self.desktop_time_ms < 0:
            raise ValueError('desktop_time_ms: nonnegative integer required')
        if type(self.includes_own_orders) is not bool:
            raise ValueError('includes_own_orders: boolean required')


@dataclass(frozen=True)
class AccountView:
    account_id: str
    version: str
    free_krw: Decimal
    free_coin: Decimal
    synchronized: bool = True
    execution_known: bool = True

    def __post_init__(self):
        _label(self.account_id, 'account_id')
        _label(self.version, 'account version')
        for name in ('free_krw', 'free_coin'):
            object.__setattr__(self, name, _amount(getattr(self, name), name))
        if type(self.synchronized) is not bool or type(self.execution_known) is not bool:
            raise ValueError('account readiness flags must be boolean')


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    strategy_id: str
    side: str
    price: Decimal
    remaining_qty: Decimal
    phase: str = 'live'

    def __post_init__(self):
        _label(self.order_id, 'order_id')
        _label(self.strategy_id, 'strategy_id')
        if self.side not in {'buy', 'sell'} or self.phase not in {
            'live',
            'in_flight',
            'cancel_pending',
        }:
            raise ValueError('invalid order side or phase')
        for name in ('price', 'remaining_qty'):
            object.__setattr__(self, name, _amount(getattr(self, name), name, positive=True))


@dataclass(frozen=True)
class LimitIntent:
    client_order_id: str
    side: str
    price: Decimal
    quantity: Decimal
    reserve_krw: Decimal
    reserve_coin: Decimal
    tif: str = 'gtc'


@dataclass(frozen=True)
class MMPlan:
    keep_order_ids: tuple[str, ...] = ()
    cancel_order_ids: tuple[str, ...] = ()
    new_orders: tuple[LimitIntent, ...] = ()
    reasons: tuple[str, ...] = ()


class SimpleMM:
    def __init__(self, config: MMConfig, strategy_id='bithumb_simple_mm', theory_mode='none'):
        self.config = config
        self.strategy_id = _label(strategy_id, 'strategy_id')
        if theory_mode not in {'none', 'midprice'}:
            raise ValueError('지원하지 않는 메이커 이론가 방식입니다.')
        self.theory_mode = theory_mode

    def theoretical_quote(self, quote: QuoteView, *, now_ms: int):
        # This is a strategy output, separate from order generation and account valuation.
        from .theory import TheoryOutput

        if (
            self.theory_mode == 'none'
            or quote.ticker != self.config.ticker
            or quote.includes_own_orders
            or quote.bid <= 0
            or quote.ask <= quote.bid
            or not 0 <= now_ms - quote.desktop_time_ms <= self.config.max_quote_age_ms
        ):
            return None
        return TheoryOutput(
            (quote.bid + quote.ask) / 2,
            quote.desktop_time_ms,
            quote.desktop_time_ms + self.config.max_quote_age_ms,
            'strategy_best_bid_ask_midprice',
            '(best_bid + best_ask) / 2',
        )

    def plan(
        self,
        quote: QuoteView,
        account: AccountView,
        orders: tuple[WorkingOrder, ...],
        *,
        now_ms: int,
    ) -> MMPlan:
        """Orders must include ALL owners' working orders in this account/symbol.

        free balances already exclude all venue locks and unconfirmed local
        reservations at the same observed projection version. Output is a plan,
        never a fill. The executor must revalidate balances at order arrival.
        """
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError('now_ms: nonnegative integer required')
        if quote.ticker != self.config.ticker or quote.includes_own_orders:
            raise ValueError('Provide the selected ticker external-only book')
        if len({order.order_id for order in orders}) != len(orders):
            raise ValueError('Duplicate working order IDs')
        own = [order for order in orders if order.strategy_id == self.strategy_id]
        if not account.synchronized or not account.execution_known:
            return MMPlan(reasons=('account_not_ready',))
        if (
            quote.bid <= 0
            or quote.ask <= quote.bid
            or not 0 <= now_ms - quote.desktop_time_ms <= self.config.max_quote_age_ms
        ):
            return MMPlan(
                cancel_order_ids=tuple(sorted(o.order_id for o in own if o.phase == 'live')),
                reasons=('book_unavailable_cancel_live',),
            )
        keep, cancel, new, reasons = [], [], [], []
        with localcontext() as context:
            context.prec = 160
            for side, price in (('buy', quote.bid), ('sell', quote.ask)):
                current = [o for o in own if o.side == side]
                keep.extend(o.order_id for o in current if o.phase == 'live' and o.price == price)
                cancel.extend(o.order_id for o in current if o.phase == 'live' and o.price != price)
                if any(o.phase != 'live' or o.price != price for o in current):
                    reasons.append(f'{side}_await_order_confirmation')
                    continue
                opposite = [o for o in orders if o.side != side]
                if any((price >= o.price if side == 'buy' else price <= o.price) for o in opposite):
                    reasons.append(f'{side}_would_cross_own_account_order')
                    continue
                quantity, reserved = (
                    self._buy_size(account.free_krw, price)
                    if side == 'buy'
                    else (self._floor_qty(account.free_coin), ZERO)
                )
                if quantity < self.config.min_qty or quantity * price < self.config.min_notional:
                    reasons.append(f'{side}_below_order_minimum')
                    continue
                key = [
                    self.strategy_id,
                    account.account_id,
                    account.version,
                    quote.version,
                    side,
                    str(price),
                    str(quantity),
                    str(reserved),
                ]
                digest = hashlib.sha256(
                    json.dumps(key, separators=(',', ':')).encode()
                ).hexdigest()[:28]
                new.append(
                    LimitIntent(
                        'MM' + digest,
                        side,
                        price,
                        quantity,
                        reserved,
                        quantity if side == 'sell' else ZERO,
                    )
                )
        return MMPlan(tuple(sorted(keep)), tuple(sorted(cancel)), tuple(new), tuple(reasons))

    def _floor_qty(self, quantity):
        return (
            Fraction(quantity) // Fraction(self.config.quantity_step)
        ) * self.config.quantity_step

    def _buy_size(self, cash, price):
        # Exact rational comparisons avoid overspending at lot and fee boundaries.
        rate = Fraction(max(ZERO, self.config.maker_fee_bps, self.config.taker_fee_bps)) / 10000
        unit_cost = Fraction(price) * Fraction(self.config.quantity_step)
        quantum = Fraction(self.config.fee_quantum)

        def cost(lots):
            notional = unit_cost * lots
            fee_units = notional * rate / quantum
            fee_count = -(-fee_units.numerator // fee_units.denominator)
            return notional + fee_count * quantum

        budget = Fraction(cash)
        low, high = 0, budget // (unit_cost * (1 + rate))
        # The unrounded fee gives an upper bound. In the usual case it already
        # fits after rounding, so no binary search is needed. Keep the exact
        # search for small lots / large fee quanta where the bound overspends.
        total = cost(high)
        if total <= budget:
            low = high
        else:
            while low < high:
                middle = (low + high + 1) // 2
                if cost(middle) <= budget:
                    low = middle
                else:
                    high = middle - 1
            total = cost(low)
        reserve = Decimal(total.numerator) / Decimal(total.denominator)
        return low * self.config.quantity_step, reserve
