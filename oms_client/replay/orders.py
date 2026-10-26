"""Typed order state and per-run identity ownership, independent of the replay loop."""
from dataclasses import dataclass, fields
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

ZERO = Decimal(0)
WORKING_STATES = frozenset({'in_flight', 'live', 'cancel_pending', 'execution_unknown'})


@dataclass
class ReplayOrder:
    id: str
    owner: str
    side: str
    price: Decimal
    quantity: Decimal
    remaining: Decimal
    reserve_cash: Decimal
    created_at: int
    tif: str = 'post_only'
    stp: str = 'cancel_taker'
    expires_at_ms: int | None = None
    client_order_id: str | None = None
    status: str = 'in_flight'
    active_at: int | None = None
    filled: Decimal = ZERO
    fees: Decimal = ZERO
    maker_notional: Decimal = ZERO
    taker_notional: Decimal = ZERO
    maker_fees: Decimal = ZERO
    taker_fees: Decimal = ZERO
    maker_filled: Decimal = ZERO
    taker_filled: Decimal = ZERO
    cancel_on_ack: bool = False
    reason: str | None = None
    closed_at: int | None = None
    unknown_at: int | None = None
    replaces: str | None = None

    @property
    def working(self) -> bool:
        return self.status in WORKING_STATES

    def request_cancel(self, wait_for_ack: bool) -> bool:
        if self.status == 'execution_unknown':
            raise ValueError('체결 판정 불가 주문은 취소 확인만으로 위험을 해제할 수 없습니다.')
        if self.status not in {'live', 'in_flight', 'cancel_pending'}:
            raise ValueError('취소할 미체결 주문이 없습니다.')
        if self.status == 'cancel_pending':
            return False
        self.status = 'cancel_pending'
        self.cancel_on_ack = self.active_at is None and wait_for_ack
        return True

    def arrive(self, now: int) -> bool:
        if self.status not in {'in_flight', 'cancel_pending'}:
            raise ValueError('접수 가능한 주문 상태가 아닙니다.')
        self.active_at = now
        if self.status != 'cancel_pending':
            self.status = 'live'
            return False
        pending = self.cancel_on_ack
        self.cancel_on_ack = False
        return pending

    def mark_unknown(self, reason: str, now: int) -> None:
        if not self.working:
            raise ValueError('종료된 주문은 판정 불가 상태로 되돌릴 수 없습니다.')
        self.status = 'execution_unknown'
        self.reason = reason
        self.unknown_at = now

    def finish(self, status: str, reason: str, now: int) -> None:
        if status not in {'filled', 'cancelled', 'rejected'}:
            raise ValueError('종료 주문 상태를 확인하세요.')
        if status == 'filled' and self.remaining != 0:
            raise ValueError('미체결 잔량이 있어 전량체결로 종료할 수 없습니다.')
        if status == 'rejected' and self.filled:
            raise ValueError('이미 체결된 주문은 접수 거절로 변경할 수 없습니다.')
        self.status = status
        self.reserve_cash = ZERO
        self.reason = reason
        self.closed_at = now

    def role_totals(self, role: str) -> tuple[Decimal, Decimal]:
        if role == 'maker':
            return self.maker_notional, self.maker_fees
        if role == 'taker':
            return self.taker_notional, self.taker_fees
        raise ValueError('체결 역할은 maker 또는 taker여야 합니다.')

    def record_fill(self, quantity: Decimal, notional: Decimal, fee: Decimal, role: str) -> None:
        """Called by SpotLedger after validating the whole settlement."""
        if (quantity <= 0 or quantity > self.remaining or notional <= 0 or fee < 0
                or self.status not in {'in_flight', 'live', 'cancel_pending'}):
            raise ValueError('체결 수량·대금·비용·주문 상태를 확인하세요.')
        self.role_totals(role)
        self.remaining -= quantity
        self.filled += quantity
        self.fees += fee
        if role == 'maker':
            self.maker_notional += notional
            self.maker_fees += fee
            self.maker_filled += quantity
        else:
            self.taker_notional += notional
            self.taker_fees += fee
            self.taker_filled += quantity

    def quantity_state(self) -> dict:
        remaining = float(self.remaining)
        flying = self.status == 'in_flight' or (self.status == 'cancel_pending' and self.active_at is None)
        return dict(
            filled=float(self.filled), in_flight=remaining if flying else 0,
            live=remaining if self.status in {'live', 'cancel_pending'} and not flying else 0,
            cancelled=remaining if self.status == 'cancelled' else 0,
            rejected=remaining if self.status == 'rejected' else 0,
            unknown=remaining if self.status == 'execution_unknown' else 0,
        )

    def snapshot(self) -> dict:
        """Keep the existing JSON contract; internal optional state stays omitted."""
        optional = {'reason', 'closed_at', 'unknown_at', 'replaces'}
        result = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in optional and value is None:
                continue
            if field.name == 'cancel_on_ack' and not value:
                continue
            result[field.name] = float(value) if isinstance(value, Decimal) else value
        result['quantity_state'] = self.quantity_state()
        return result


@dataclass(frozen=True)
class ReplacementRequest:
    price: Decimal
    total: Decimal
    tif: str

    def snapshot(self) -> dict:
        return dict(price=str(self.price), total=str(self.total), tif=self.tif)


class OrderBook:
    """Own order IDs, retry identity, completed IDs and pending replacements."""
    def __init__(self):
        self._orders: dict[str, ReplayOrder] = {}
        self.orders: Mapping[str, ReplayOrder] = MappingProxyType(self._orders)
        self._requests: dict[str, tuple[tuple, str]] = {}
        self._completed: set[str] = set()
        self.collection_pending = False
        self._replacements: dict[str, ReplacementRequest] = {}
        self.sequence = 0

    def retry(self, client_id: str, fingerprint: tuple) -> str | None:
        previous = self._requests.get(client_id)
        if previous is None:
            return None
        if previous[0] != fingerprint:
            raise ValueError('동일 클라이언트 주문 ID의 요청 내용이 다릅니다.')
        return previous[1]

    def submit(self, *, side: str, price: Decimal, quantity: Decimal, owner: str,
               tif: str, stp: str, expires_at_ms: int | None, client_order_id: str | None,
               reserve_cash: Decimal, now: int, fingerprint: tuple) -> ReplayOrder:
        self.sequence += 1
        identifier = f'O{self.sequence:08d}'
        order = ReplayOrder(identifier, owner, side, price, quantity, quantity, reserve_cash, now,
                            tif=tif, stp=stp, expires_at_ms=expires_at_ms, client_order_id=client_order_id)
        self._orders[identifier] = order
        if client_order_id is not None:
            self._requests[client_order_id] = (fingerprint, identifier)
        return order

    def working(self) -> list[ReplayOrder]:
        return [order for order in self._orders.values() if order.working]

    def completed(self, identifier: str) -> bool:
        return identifier in self._completed

    def finish(self, order: ReplayOrder, status: str, reason: str, now: int) -> bool:
        if self.completed(order.id):
            return False
        order.finish(status, reason, now)
        if status == 'filled' or reason != 'requested':
            self._replacements.pop(order.id, None)
        self._completed.add(order.id)
        self.collection_pending = True
        return True

    def has_replacement(self, identifier: str) -> bool:
        return identifier in self._replacements

    def queue_replacement(self, identifier: str, request: ReplacementRequest) -> None:
        self._replacements[identifier] = request

    def pop_replacement(self, identifier: str) -> ReplacementRequest | None:
        return self._replacements.pop(identifier, None)

    def collect_completed(self) -> set[str]:
        active = {order.id for order in self.working()}
        for identifier in list(self._orders):
            if identifier not in active:
                del self._orders[identifier]
        self.collection_pending = False
        return active
