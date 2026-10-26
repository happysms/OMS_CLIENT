"""Spot settlement owns cash/cost/fees, without market data, time, UI or storage."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Iterable

from ..market_data.validation import DataError
from .orders import ReplayOrder

ZERO = Decimal(0)
FEE_QUANTUM = Decimal('0.00000001')


@dataclass(frozen=True)
class FeeSchedule:
    maker_bps: Decimal
    taker_bps: Decimal

    def __post_init__(self):
        for rate in (self.maker_bps, self.taker_bps):
            if not isinstance(rate, Decimal) or not rate.is_finite() or not 0 <= rate <= 1000:
                raise ValueError('수수료는 0~1000bp의 유효한 Decimal 값이어야 합니다.')

    @classmethod
    def from_config(cls, config: dict) -> 'FeeSchedule':
        return cls(Decimal(config.get('maker_fee_bps', config['fee_bps'])),
                   Decimal(config.get('taker_fee_bps', config['fee_bps'])))

    @property
    def reserve_bps(self) -> Decimal:
        return max(self.maker_bps, self.taker_bps)

    def cumulative_fee(self, notional: Decimal, role: str | None = None) -> Decimal:
        rates = {'maker': self.maker_bps, 'taker': self.taker_bps, None: self.reserve_bps}
        return (notional * rates[role] / 10000).quantize(FEE_QUANTUM, rounding=ROUND_CEILING)

    def reserve_buy(self, price: Decimal, quantity: Decimal, tif: str) -> Decimal:
        # Independent role ceilings can exceed one aggregate ceiling by one quantum.
        mixed_roles = quantity and tif in {'gtc', 'gtd'} and self.maker_bps > 0 and self.taker_bps > 0
        extra = FEE_QUANTUM if mixed_roles else ZERO
        return price * quantity + self.cumulative_fee(price * quantity) + extra


@dataclass(frozen=True)
class FillSettlement:
    price: Decimal
    quantity: Decimal
    fee: Decimal
    realized_pnl: Decimal
    role: str


@dataclass
class SpotLedger:
    cash: Decimal
    coin: Decimal
    fee_schedule: FeeSchedule
    cost: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    initial_equity: Decimal | None = None

    def initialize(self, bid: Decimal) -> None:
        if self.initial_equity is None:
            self.cost = self.coin * bid
            self.initial_equity = self.cash + self.cost

    def available(self, orders: Iterable[ReplayOrder]) -> tuple[Decimal, Decimal]:
        orders = [order for order in orders if order.working]
        cash = self.cash - sum((order.reserve_cash for order in orders), ZERO)
        coin = self.coin - sum((order.remaining for order in orders if order.side == 'sell'), ZERO)
        return cash, coin

    def settle(self, order: ReplayOrder, quantity: Decimal, price: Decimal, role: str) -> FillSettlement:
        """Validate first, then commit the account and order delta together."""
        if (quantity <= 0 or quantity > order.remaining or price <= 0
                or order.status not in {'in_flight', 'live', 'cancel_pending'}
                or order.side not in {'buy', 'sell'}):
            raise DataError('체결 수량·가격·주문 상태를 확인하세요.')
        if (order.side == 'buy' and price > order.price) or (order.side == 'sell' and price < order.price):
            raise DataError('지정가 한도를 벗어난 체결입니다.')
        previous_notional, previous_fee = order.role_totals(role)
        notional = price * quantity
        fee = self.fee_schedule.cumulative_fee(previous_notional + notional, role) - previous_fee
        if fee < 0:
            raise DataError('누적 수수료 대사가 일치하지 않습니다.')
        realized = ZERO
        if order.side == 'buy':
            if notional + fee > self.cash or notional + fee > order.reserve_cash:
                raise DataError('가상계좌 예약금 대사가 일치하지 않습니다.')
            remaining_reserve = min(
                order.reserve_cash - notional - fee,
                self.fee_schedule.reserve_buy(order.price, order.remaining - quantity, order.tif),
            )
            self.cash -= notional + fee
            self.coin += quantity
            self.cost += notional + fee
            order.reserve_cash = remaining_reserve
        else:
            if quantity > self.coin:
                raise DataError('가상계좌 코인 잔고 대사가 일치하지 않습니다.')
            allocated = self.cost if quantity == self.coin else self.cost * quantity / self.coin
            realized = notional - fee - allocated
            self.coin -= quantity
            self.cost -= allocated
            self.cash += notional - fee
            self.realized += realized
        self.fees += fee
        order.record_fill(quantity, notional, fee, role)
        return FillSettlement(price, quantity, fee, realized, role)
