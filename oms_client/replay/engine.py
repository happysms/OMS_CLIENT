"""Deterministic spot MM replay. Pacing and UI never participate in matching."""

from collections import deque
from decimal import Decimal
from typing import Mapping

from ..market_data.validation import DataError
from ..paper_trading.reporting import evaluate_positions
from ..paper_trading.currency import UsdtLedger
from ..paper_trading.activity import TradingActivity, ExecutedTrade
from ..strategies.simple_mm import (
    AccountView,
    MMConfig,
    QuoteView,
    SimpleMM,
    _amount,
)
from .configuration import config_from  # Public import retained for API/test callers.
from .ledger import FeeSchedule, SpotLedger
from .limit_execution import CancelAction, ExecutionDepth, LimitOrderPlanner, STPS, TIFS
from .order_guard import FeedDelayGuard
from .orders import OrderBook, ReplayOrder, ReplacementRequest
from .queue_cancel import QueueCancellation
from .scheduler import EventScheduler
from .strategy_view import StrategyOrderViews

D = Decimal
ZERO = D(0)


class ReplayEngine:
    """Coordinate domain objects, market queues, strategy decisions and reports.

    Order state, account settlement and scheduled-event ownership live in the
    composed objects. The service serializes commands and owns all external IO.
    """

    def __init__(self, config: dict, fx_provider=None):
        self.config = config
        strategy = config.get('strategy')
        if strategy and (
            strategy.get('type') != 'single_exchange_maker' or not strategy.get('executable')
        ):
            raise ValueError('이 실행기에서 지원하지 않는 전략입니다.')
        self.strategy_parameters = (
            strategy['parameters'] if strategy else {'direction': 'both', 'max_quote_age_ms': 1000}
        )
        self.guard = FeedDelayGuard(self.strategy_parameters)
        self.queue_cancel = QueueCancellation(config)
        self.queue_tape = {}
        self.queue_consumed = {}
        self.depth = ExecutionDepth()
        self.limit_planner = LimitOrderPlanner(D(config['quantity_step']))
        self.order_book = OrderBook()
        self.scheduler = EventScheduler()
        self.strategy_orders = StrategyOrderViews()
        self.new_order_events = []
        self.order_event_count = 0
        self.end_orders = []
        self.strategy_theory = None
        self.now = config['start_ms']
        self.fee_schedule = FeeSchedule.from_config(config)
        self.account = SpotLedger(
            D(config['initial_krw']), D(config['initial_coin']), self.fee_schedule
        )
        self.usdt_ledger = UsdtLedger()
        self.activity = TradingActivity()
        self.fx_provider = fx_provider or (lambda now: None)
        self.books = {}
        self.queues = {}
        self.queue_known = {}
        self.depletion = {}
        self.event_sequence = 0
        self.fill_count = 0
        self.version = 0
        self.last_ticks = deque(maxlen=100)
        self.last_fills = deque(maxlen=100)
        self.new_fills = []
        self.order_log = deque(maxlen=100)
        self.closed = False
        reserve_fee = self.fee_schedule.reserve_bps
        self.mm = SimpleMM(
            MMConfig(
                ticker=config['ticker'],
                quantity_step=D(config['quantity_step']),
                min_qty=D(config['quantity_step']),
                min_notional=D(config['min_notional']),
                maker_fee_bps=reserve_fee,
                taker_fee_bps=reserve_fee,
                fee_quantum=D('0.00000001'),
                max_quote_age_ms=self.strategy_parameters['max_quote_age_ms'],
            ),
            theory_mode=self.strategy_parameters.get('theory_mode', 'none'),
        )

    @property
    def orders(self) -> Mapping[str, ReplayOrder]:
        return self.order_book.orders

    @property
    def order_sequence(self) -> int:
        return self.order_book.sequence

    @property
    def cash(self) -> Decimal:
        return self.account.cash

    @property
    def coin(self) -> Decimal:
        return self.account.coin

    @property
    def cost(self) -> Decimal:
        return self.account.cost

    @property
    def realized(self) -> Decimal:
        return self.account.realized

    @property
    def fees(self) -> Decimal:
        return self.account.fees

    @property
    def initial_equity(self) -> Decimal | None:
        return self.account.initial_equity

    def working(self) -> list[ReplayOrder]:
        return self.order_book.working()

    def free(self) -> tuple[Decimal, Decimal]:
        return self.account.available(self.working())

    def _schedule(self, at, kind, identifier):
        self.scheduler.schedule(at, kind, identifier)

    def _visible(self, side, price):
        book = self.books.get('bithumb', {})
        for level in book.get('bids' if side == 'buy' else 'asks', []):
            if D(str(level['price'])) == price:
                return (
                    max(
                        ZERO,
                        D(str(level['quantity'])) - self.depth.self_debt.get((side, price), ZERO),
                    )
                    if level['quantity'] is not None
                    else None
                )
        # Inside the observed range (including spread improvement), no row means zero.
        levels = book.get('bids' if side == 'buy' else 'asks', [])
        opposite = book.get('asks' if side == 'buy' else 'bids', [])
        if levels and opposite:
            boundary = None
            for level in opposite:
                value = self.depth.available(
                    'sell' if side == 'buy' else 'buy', D(str(level['price'])), level['quantity']
                )
                if value is None or value > 0:
                    boundary = D(str(level['price']))
                    break
            edge = D(str(opposite[-1]['price']))
            within_own = (
                price >= D(str(levels[-1]['price']))
                if side == 'buy'
                else price <= D(str(levels[-1]['price']))
            )
            within_opposite = (
                (price < boundary if side == 'buy' else price > boundary)
                if boundary is not None
                else price <= edge if side == 'buy' else price >= edge
            )
            if within_own and within_opposite:
                return ZERO
        return None

    def place(
        self,
        side,
        price,
        quantity,
        owner='manual',
        tif='post_only',
        stp='cancel_taker',
        expires_at_ms=None,
        client_order_id=None,
    ):
        if self.closed or self.initial_equity is None:
            raise ValueError('유효한 초기 호가가 도착한 뒤 주문할 수 있습니다.')
        if tif not in TIFS or stp not in STPS:
            raise ValueError('지원하지 않는 지정가 조건 또는 STP입니다.')
        if (tif == 'gtd') != (expires_at_ms is not None):
            raise ValueError('GTD 주문에는 만료 시각이 필요합니다.')
        if expires_at_ms is not None and (
            type(expires_at_ms) is not int or expires_at_ms <= self.now
        ):
            raise ValueError('만료는 현재 가상 시각 이후의 정수 ms여야 합니다.')
        price = _amount(price, 'price', positive=True)
        quantity = _amount(quantity, 'quantity', positive=True)
        fingerprint = (
            side,
            str(price.normalize()),
            str(quantity.normalize()),
            owner,
            tif,
            stp,
            expires_at_ms,
        )
        if client_order_id is not None:
            if (
                not isinstance(client_order_id, str)
                or not 1 <= len(client_order_id) <= 80
                or any((ord(c) < 32 for c in client_order_id))
            ):
                raise ValueError('클라이언트 주문 ID를 확인하세요.')
            previous = self.order_book.retry(client_order_id, fingerprint)
            if previous is not None:
                return previous
        working = self.working()
        if any((o.status == 'execution_unknown' for o in working)):
            raise ValueError('체결 판정 불가 잔량이 있어 신규 주문을 보류합니다.')
        if not self.guard.check(self.now, self.mm.config.max_quote_age_ms):
            self.guard.blocked_submissions += 1
            raise ValueError('시세 시간차 보호로 신규 주문을 차단했습니다: ' + self.guard.reason)
        if len(working) >= 100:
            raise ValueError('미체결 주문은 최대 100개입니다.')
        if self.order_sequence >= 100000:
            raise DataError('주문 10만 건 한도입니다. 기간을 줄여 주세요.')
        if side not in {'buy', 'sell'}:
            raise ValueError('주문 방향을 확인하세요.')
        step = D(self.config['quantity_step'])
        price_step = D(self.config.get('price_step', '0'))
        if (
            quantity % step
            or quantity * price < D(self.config['min_notional'])
            or (price_step and price % price_step)
        ):
            raise ValueError('모형의 최소 금액·수량 단위를 확인하세요.')
        cash, coin = self.account.available(working)
        reserve = self.fee_schedule.reserve_buy(price, quantity, tif) if side == 'buy' else ZERO
        if reserve > cash or (side == 'sell' and quantity > coin):
            raise ValueError('예약되지 않은 가상 잔고가 부족합니다.')
        order = self.order_book.submit(
            side=side,
            price=price,
            quantity=quantity,
            owner=owner,
            tif=tif,
            stp=stp,
            expires_at_ms=expires_at_ms,
            client_order_id=client_order_id,
            reserve_cash=reserve,
            now=self.now,
            fingerprint=fingerprint,
        )
        identifier = order.id
        self.version += 1
        self._schedule(self.now + self.config['latency_ms'], 'activate', identifier)
        if expires_at_ms is not None:
            self._schedule(expires_at_ms, 'expire', identifier)
        return identifier

    def cancel(self, identifier):
        if self.order_book.completed(identifier):
            return
        order = self.orders.get(identifier)
        if order is None:
            raise ValueError('취소할 미체결 주문이 없습니다.')
        if not order.request_cancel(self.config.get('cancel_ack_policy') == 'on_ack'):
            return
        self.version += 1
        if not order.cancel_on_ack:
            self._schedule(self.now + self.config['latency_ms'], 'cancel', identifier)

    def _terminal(self, order: ReplayOrder, status, reason):
        if not self.order_book.finish(order, status, reason, self.now):
            return
        self.version += 1
        self.order_event_count += 1
        report = dict(self._public_order(order), sequence=self.order_event_count)
        self.order_log.append(report)
        self.new_order_events.append(report)

    def _public_order(self, order: ReplayOrder) -> dict:
        result = order.snapshot()
        key = (order.side, order.price)
        result['queue_known'] = self.queue_known.get(key, False)
        if not result['queue_known'] or order.active_at is None:
            result.update(
                queue_ahead=None,
                queue_external_ahead=None,
                queue_external_behind=None,
                queue_front_cancel_probability=None,
            )
            return result
        ahead = ZERO
        for node in self.queues.get(key, []):
            if node['kind'] == 'own' and node['id'] == order.id:
                break
            ahead += (
                node['qty']
                if node['kind'] == 'external'
                else self.orders[node['id']].remaining if node['id'] in self.orders else ZERO
            )
        result['queue_ahead'] = (
            float(ahead) if result['queue_known'] and order.active_at is not None else None
        )
        position = self.queue_cancel.position(self.queues.get(key, []), order.id)
        result.update(
            {
                k: v if result['queue_known'] and order.active_at is not None else None
                for k, v in position.items()
            }
        )
        return result

    def _activate(self, order: ReplayOrder) -> None:
        if order.status not in {'in_flight', 'cancel_pending'}:
            return
        # A local guard cannot recall an already transmitted order. It can only
        # send a cancellation, which keeps its normal latency and fill race.
        book = self.books.get('bithumb')
        if not book or self.now - book['time'] > self.mm.config.max_quote_age_ms:
            self._terminal(order, 'rejected', 'stale_book')
            return
        if order.expires_at_ms is not None and self.now >= order.expires_at_ms:
            self._terminal(order, 'cancelled', 'expired')
            return
        plan = self.limit_planner.plan(order, book, self.depth, self.working())
        outcome, reason = (plan.outcome, plan.reason)
        if order.tif == 'fok' and outcome != 'filled':
            if outcome == 'unknown':
                self._unknown(order, reason)
            else:
                self._terminal(order, 'cancelled', 'fok_unfilled')
            return
        if self.fill_count + plan.fill_count > 100000:
            raise DataError('체결 10만 건 한도입니다. 기간을 줄여 주세요.')
        for action in plan.actions:
            if isinstance(action, CancelAction):
                self._terminal(self.orders[action.order_id], 'cancelled', 'self_trade_prevention')
            else:
                target, size = (action.price, action.quantity)
                self.depth.consume('sell' if order.side == 'buy' else 'buy', target, size, 'taker')
                opposite_key = ('sell' if order.side == 'buy' else 'buy', target)
                self.queue_cancel.consume_external(self.queues.get(opposite_key, []), size)
                self._fill(
                    order, size, dict(source_id=book['source_id']), price=target, role='taker'
                )
        if outcome == 'filled':
            return
        if outcome == 'unknown':
            self._unknown(order, reason)
            return
        if outcome in {'reject', 'cancel'}:
            self._terminal(order, 'rejected' if outcome == 'reject' else 'cancelled', reason)
            return
        key = (order.side, order.price)
        nodes = self.queues.setdefault(key, [])
        visible = self._visible(*key)
        self.queue_known.setdefault(key, visible is not None)
        if visible is not None and self.queue_cancel.enabled:
            if not any((n['kind'] == 'own' for n in nodes)):
                extra = max(
                    ZERO,
                    visible
                    - max(self.queue_tape.get(key, ZERO), self.depth.public_debt.get(key, ZERO)),
                )
                if extra:
                    nodes.append(dict(kind='external', qty=extra))
        elif visible is not None:
            represented = sum((node['qty'] for node in nodes if node['kind'] == 'external'), ZERO)
            extra = max(ZERO, visible - self.depletion.get(key, ZERO) - represented)
            if extra:
                nodes.append(dict(kind='external', qty=extra))
        nodes.append(dict(kind='own', id=order.id))
        if order.arrive(self.now):
            self._schedule(self.now + self.config['latency_ms'], 'cancel', order.id)
        self.version += 1

    def _unknown(self, order: ReplayOrder, reason: str) -> None:
        order.mark_unknown(reason, self.now)
        self.version += 1

    def replace(self, identifier, price, total_quantity, tif=None):
        order = self.orders.get(identifier)
        if not order or order.status not in {'live', 'in_flight', 'cancel_pending'}:
            raise ValueError('교체할 주문을 확인하세요.')
        if self.order_book.has_replacement(identifier):
            raise ValueError('이미 교체를 기다리는 주문입니다.')
        price = _amount(price, 'price', positive=True)
        total = _amount(total_quantity, 'total_quantity', positive=True)
        kind = tif or order.tif
        if kind not in TIFS:
            raise ValueError('지정가 조건을 확인하세요.')
        if kind == 'gtd' and order.expires_at_ms is None:
            raise ValueError('GTD 변경은 새 만료 시각을 갖춘 신규 주문으로 제출하세요.')
        price_step = D(self.config.get('price_step', '0'))
        if (
            total % D(self.config['quantity_step'])
            or total * price < D(self.config['min_notional'])
            or (price_step and price % price_step)
        ):
            raise ValueError('교체 주문의 가격·수량·최소금액을 확인하세요.')
        if total < order.filled:
            raise ValueError('총수량은 이미 체결된 수량 이상이어야 합니다.')
        if price == order.price and total == order.quantity and (kind == order.tif):
            return
        self.order_book.queue_replacement(identifier, ReplacementRequest(price, total, kind))
        self.cancel(identifier)

    def _replacement(self, order: ReplayOrder) -> None:
        requested = self.order_book.pop_replacement(order.id)
        if not requested:
            return
        remaining = requested.total - order.filled
        if remaining <= 0:
            return
        try:
            new_id = self.place(
                order.side,
                requested.price,
                str(remaining),
                owner=order.owner,
                tif=requested.tif,
                stp=order.stp,
                expires_at_ms=order.expires_at_ms if requested.tif == 'gtd' else None,
            )
            self.orders[new_id].replaces = order.id
        except ValueError as exc:
            self.order_event_count += 1
            report = dict(
                sequence=self.order_event_count,
                id=order.id,
                status='replace_failed',
                closed_at=self.now,
                reason=str(exc),
                requested=requested.snapshot(),
            )
            self.order_log.append(report)
            self.new_order_events.append(report)

    def due(self) -> int | None:
        return self.scheduler.next_time()

    def _gc(self):
        active = self.order_book.collect_completed()
        for key in list(self.queues):
            nodes = [n for n in self.queues[key] if n['kind'] == 'external' or n['id'] in active]
            if self.queue_cancel.enabled:
                self.queue_cancel.compact(nodes)
            if any((n['kind'] == 'own' for n in nodes)):
                self.queues[key] = nodes
            else:
                del self.queues[key]
                self.queue_known.pop(key, None)
        book = self.books.get('bithumb', {})
        visible = {
            (side, D(str(level['price'])))
            for side, field in [('buy', 'bids'), ('sell', 'asks')]
            for level in book.get(field, [])
        }
        self.depletion = {
            key: value
            for key, value in self.depletion.items()
            if key in self.queues or key in visible
        }

    def advance(self, timestamp: int) -> None:
        processed = False
        while event := self.scheduler.pop_due(timestamp):
            processed = True
            self.now = event.at_ms
            kind, identifier = (event.kind, event.identifier)
            if kind == 'stale':
                if self.books.get('bithumb', {}).get('time') == identifier:
                    self.auto()
                continue
            order = self.orders.get(identifier)
            if order is None:
                continue
            if kind == 'activate':
                self._activate(order)
            elif kind == 'expire' and order.status in {'live', 'in_flight', 'cancel_pending'}:
                self._terminal(order, 'cancelled', 'expired')
            elif kind == 'cancel' and order.status == 'cancel_pending':
                self._terminal(order, 'cancelled', 'requested')
                self._replacement(order)
            self.auto()
        self.now = timestamp
        # Nothing can change queue membership between events without a terminal
        # order or a dispatched timer. on_event still collects after every market event.
        if processed or self.order_book.collection_pending:
            self._gc()

    def on_event(self, event):
        self.advance(event['time'])
        self.event_sequence += 1
        if event['kind'] == 'book':
            previous = self.books.get(event['venue'])
            self.books[event['venue']] = {k: v for k, v in event.items() if k != '_key'}
            if event['venue'] == 'bithumb':
                self.depth.book()
                self.guard.observe(
                    event['time'], event.get('server_time'), self.mm.config.max_quote_age_ms
                )
                self.accept_theory(
                    self.mm.theoretical_quote(
                        QuoteView(
                            self.config['ticker'],
                            str(self.event_sequence),
                            str(event['bids'][0]['price']),
                            str(event['asks'][0]['price']),
                            event['time'],
                        ),
                        now_ms=self.now,
                    )
                )
                if self.queue_cancel.enabled:
                    self._update_queue_book(previous, event)
                elif previous:
                    for side, field in [('buy', 'bids'), ('sell', 'asks')]:
                        old = {D(str(x['price'])): x['quantity'] for x in previous[field]}
                        for level in event[field]:
                            key = (side, D(str(level['price'])))
                            before = old.get(key[1])
                            after = level['quantity']
                            if after is not None:
                                if before is None or after > before:
                                    self.depletion[key] = ZERO
                                elif after < before:
                                    # A smaller snapshot acknowledges already observed trades.
                                    # Never subtract a consumed trade twice for a later order.
                                    self.depletion[key] = max(
                                        ZERO, self.depletion.get(key, ZERO) - D(str(before - after))
                                    )
                if self.initial_equity is None and self.now >= self.config['start_ms']:
                    self._initialize_account(D(str(event['bids'][0]['price'])))
                for key, nodes in self.queues.items() if not self.queue_cancel.enabled else []:
                    visible = self._visible(*key)
                    if visible is not None and (not self.queue_known.get(key)):
                        nodes.insert(0, dict(kind='external', qty=visible))
                        self.queue_known[key] = True
                self._schedule(self.now + self.mm.config.max_quote_age_ms + 1, 'stale', self.now)
        else:
            self.last_ticks.append({k: v for k, v in event.items() if k != '_key'})
            if self.now >= self.config['start_ms'] and event['venue'] == 'bithumb':
                if self.queue_cancel.enabled:
                    key = ('sell' if event['qty'] > 0 else 'buy', D(str(event['price'])))
                    if key in self.queues or self._visible(*key) is not None:
                        self.queue_tape[key] = self.queue_tape.get(key, ZERO) + abs(
                            D(str(event['qty']))
                        )
                self._match(event)
        if self.now >= self.config['start_ms']:
            self.auto()
        self._gc()

    def _update_queue_book(self, previous, event):
        old = {
            (side, D(str(level['price']))): level['quantity']
            for side, field in [('buy', 'bids'), ('sell', 'asks')]
            for level in (previous or {}).get(field, [])
        }
        for key in sorted(self.queues):
            nodes = self.queues[key]
            visible = self._visible(*key)
            before = old.get(key)
            if visible is None:
                # Depth truncation / missing quantities are not cancellations.
                self.queue_known[key] = False
                continue
            if before is None or not self.queue_known.get(key):
                nodes[:] = [dict(kind='external', qty=visible)] + [
                    n for n in nodes if n['kind'] == 'own'
                ]
                self.queue_known[key] = True
                self.queue_cancel.compact(nodes)
                continue
            # Exact-price tape and virtual price-priority consumption overlap: use max, not sum.
            traded = max(self.queue_tape.get(key, ZERO), self.queue_consumed.get(key, ZERO))
            expected = max(ZERO, D(str(before)) - self.depth.self_debt.get(key, ZERO) - traded)
            inferred = max(ZERO, expected - visible)
            added = max(ZERO, visible - expected)
            identifiers = [n['id'] for n in nodes if n['kind'] == 'own'] if inferred else []
            positions = self.queue_cancel.positions(nodes, identifiers) if inferred else {}
            applied = self.queue_cancel.cancel(nodes, inferred) if inferred else ZERO
            after_cancel = self.queue_cancel.positions(nodes, identifiers) if inferred else {}
            if added:
                nodes.append(dict(kind='external', qty=added))
            # Own simulated fills displace historical external fills; reconcile the latter
            # without treating them as cancellations or spending a tick on another own fill.
            external = sum((n['qty'] for n in nodes if n['kind'] == 'external'), ZERO)
            reconciled = self.queue_cancel.consume_external(nodes, max(ZERO, external - visible))
            self.queue_cancel.compact(nodes)
            if inferred:
                self.queue_cancel.count += 1
                self.queue_cancel.cancelled += applied
                self.queue_cancel.events.append(
                    dict(
                        timestamp=self.now,
                        side=key[0],
                        price=float(key[1]),
                        previous_quantity=str(before),
                        visible_quantity=str(visible),
                        trade_quantity=str(traded),
                        inferred_cancel_quantity=str(inferred),
                        applied_cancel_quantity=str(applied),
                        added_quantity=str(added),
                        trade_reconciled_quantity=str(reconciled),
                        positions_before=positions,
                        positions_after_cancel=after_cancel,
                        random_draws=self.queue_cancel.draws,
                    )
                )
        self.queue_tape.clear()
        self.queue_consumed.clear()
        self.depletion.clear()

    def start(self):
        self.advance(self.config['start_ms'])
        book = self.books.get('bithumb')
        if (
            self.initial_equity is None
            and book
            and (self.now - book['time'] <= self.mm.config.max_quote_age_ms)
        ):
            self._initialize_account(D(str(book['bids'][0]['price'])))
        self.auto()

    def _initialize_account(self, bid):
        self.account.initialize(bid)
        costs = {f"bithumb:{self.config['ticker']}": self.cost} if self.coin else {}
        self.usdt_ledger.initialize(self.cash, costs, self._fx_quote())

    def _fx_quote(self):
        quote = self.fx_provider(self.now)
        return quote if quote and quote.valid_at(self.now) else None

    def auto(self):
        if self.closed or self.now < self.config['start_ms']:
            return
        if not self.guard.check(self.now, self.mm.config.max_quote_age_ms):
            if self.guard.cancel_working:
                for order in self.working():
                    if order.status not in {'cancel_pending', 'execution_unknown'}:
                        self.cancel(order.id)
            return
        if not self.config['auto_mm'] or self.initial_equity is None:
            return
        working = self.working()
        if any((o.status == 'execution_unknown' for o in working)):
            return
        book = self.books.get('bithumb')
        if not book:
            return
        cash, coin = self.account.available(working)
        view = QuoteView(
            self.config['ticker'],
            str(self.event_sequence),
            str(book['bids'][0]['price']),
            str(book['asks'][0]['price']),
            book['time'],
        )
        account = AccountView(
            'replay',
            str(self.version),
            cash if self.strategy_parameters['direction'] != 'sell' else ZERO,
            coin if self.strategy_parameters['direction'] != 'buy' else ZERO,
        )
        orders = self.strategy_orders.project(working)
        plan = self.mm.plan(view, account, orders, now_ms=self.now)
        for identifier in plan.cancel_order_ids:
            self.cancel(identifier)
        for intent in plan.new_orders:
            if len(self.working()) < 100:
                self.place(intent.side, intent.price, intent.quantity, 'bithumb_simple_mm')

    def _match(self, tick):
        if not tick.get('direction_known', False):
            return
        side = 'sell' if tick['qty'] > 0 else 'buy'
        price = D(str(tick['price']))
        budget = abs(D(str(tick['qty'])))
        visible = {
            D(str(level['price'])): level['quantity']
            for level in self.books.get('bithumb', {}).get('asks' if side == 'sell' else 'bids', [])
        }
        candidates = set(self.queues) | {(side, p) for p in visible}
        keys = sorted(
            (
                key
                for key in candidates
                if key[0] == side and (key[1] <= price if side == 'sell' else key[1] >= price)
            ),
            key=lambda key: key[1],
            reverse=side == 'buy',
        )
        for key in keys:
            if budget <= 0:
                break
            nodes = self.queues.get(key, [])
            if not any(
                (
                    n['kind'] == 'own' and self.orders[n['id']].status in {'live', 'cancel_pending'}
                    for n in nodes
                )
            ):
                available = self.depth.available(side, key[1], visible.get(key[1]))
                if available is None:
                    break
                used = min(budget, available)
                budget -= used
                self.depth.consume(side, key[1], used, 'maker')
                continue
            if not self.queue_known.get(key):
                break
            # A tick tied with activation may have happened before our arrival.
            if not any(
                (
                    n['kind'] == 'own'
                    and self.orders[n['id']].active_at is not None
                    and (self.orders[n['id']].active_at < self.now)
                    for n in nodes
                )
            ):
                return
            while nodes and budget > 0:
                node = nodes[0]
                if node['kind'] == 'external':
                    used = min(budget, node['qty'])
                    budget -= used
                    node['qty'] -= used
                    self.depletion[key] = self.depletion.get(key, ZERO) + used
                    self.depth.consume(side, key[1], used, 'maker')
                    if self.queue_cancel.enabled:
                        self.queue_consumed[key] = self.queue_consumed.get(key, ZERO) + used
                    if node['qty'] == 0:
                        nodes.pop(0)
                    continue
                order = self.orders[node['id']]
                if order.status not in {'live', 'cancel_pending'} or order.active_at is None:
                    nodes.pop(0)
                    continue
                if order.active_at >= self.now:
                    return
                size = min(order.remaining, budget)
                # Split fills are rounded down to the configured coin lot.
                step = D(self.config['quantity_step'])
                size = size // step * step
                if not size:
                    return
                self._fill(order, size, tick)
                budget -= size
                self.depletion[key] = self.depletion.get(key, ZERO) + size
                self.depth.consume(side, key[1], size, 'maker')
                if self.queue_cancel.enabled:
                    self.queue_consumed[key] = self.queue_consumed.get(key, ZERO) + size
                if order.remaining == 0:
                    nodes.pop(0)

    def _fill(
        self,
        order: ReplayOrder,
        size: Decimal,
        tick: dict,
        price: Decimal | None = None,
        role: str = 'maker',
    ) -> None:
        if self.fill_count >= 100000:
            raise DataError('체결 10만 건 한도입니다. 기간을 줄여 주세요.')
        queue_position = self._public_order(order)
        price = order.price if price is None else price
        before_quantity = self.coin
        fx = self._fx_quote()
        settlement = self.account.settle(order, size, price, role)
        self.activity.record(
            ExecutedTrade(
                'bithumb',
                self.config['ticker'],
                order.side,
                size,
                price,
                settlement.fee,
                fx.rate if fx else None,
            )
        )
        usdt_fill = self.usdt_ledger.record_fill(
            f"bithumb:{self.config['ticker']}",
            order.side,
            size,
            price,
            settlement.fee,
            before_quantity,
            fx,
        )
        fee, realized = (settlement.fee, settlement.realized_pnl)
        if order.remaining == 0:
            self._terminal(
                order, 'filled', 'trade_tick' if role == 'maker' else 'arrival_liquidity'
            )
        self.version += 1
        self.fill_count += 1
        valuation = self.valuation()
        report = dict(
            sequence=self.fill_count,
            fill_id=f'F{self.fill_count:08d}',
            order_id=order.id,
            timestamp=self.now,
            side=order.side,
            price=float(price),
            limit_price=float(order.price),
            liquidity=role,
            tif=order.tif,
            quantity=float(size),
            fee=float(fee),
            realized_pnl=float(realized),
            remaining=float(order.remaining),
            source_tick=tick['source_id'],
            account=valuation['account'],
            activity=valuation['activity'],
            positions=valuation['positions'],
            theory=valuation['theory'],
            order_guard=self.guard.snapshot(self.now),
            queue_position={
                key: value for key, value in queue_position.items() if key.startswith('queue_')
            },
            queue_model=self.queue_cancel.summary(),
            usdt=dict(valuation['usdt'], fill=usdt_fill),
        )
        self.last_fills.append(report)
        self.new_fills.append(report)

    def accept_theory(self, output):
        # Future strategy adapters publish through this method; account code never invents a price.
        from ..strategies.theory import TheoryOutput

        if output is not None and (not isinstance(output, TheoryOutput)):
            raise ValueError('전략은 TheoryOutput 또는 None을 반환해야 합니다.')
        self.strategy_theory = output if output is not None and output.valid_at(self.now) else None

    def valuation(self):
        book = self.books.get('bithumb')
        pair = ('bithumb', self.config['ticker'])
        marks = (
            {pair: dict(bid=book['bids'][0]['price'], ts=book['time'])}
            if book and 0 <= self.now - book['time'] <= self.mm.config.max_quote_age_ms
            else {}
        )
        positions = (
            [
                dict(
                    exchange='bithumb',
                    ticker=pair[1],
                    quantity=float(self.coin),
                    avg_price=float(self.cost / self.coin),
                )
            ]
            if self.coin
            else []
        )
        theory = None
        if self.strategy_theory and self.strategy_theory.valid_at(self.now):
            profile = self.config.get('strategy', {})
            theory = dict(
                self.strategy_theory.report(),
                strategy_id=profile.get('id', self.mm.strategy_id),
                strategy_revision=profile.get('revision'),
                engine_version=profile.get('engine_version'),
            )
        elif not self.config.get('strategy') and self.config['theory_price']:
            # Compatibility for the old pure engine; registered runs use strategy outputs only.
            theory = dict(price=float(self.config['theory_price']), basis='legacy_fixed_input')
        theories = {pair: theory} if theory else {}
        account, positions = evaluate_positions(
            dict(
                cash=float(self.cash),
                initial_cash=float(
                    self.initial_equity if self.initial_equity is not None else self.cash
                ),
                realized_pnl=float(self.realized),
                fees_paid=float(self.fees),
            ),
            positions,
            marks,
            theories,
        )
        cash, coin = self.free()
        account.update(
            free_cash=float(cash),
            free_coin=float(coin),
            coin=float(self.coin),
            initial_equity=float(self.initial_equity) if self.initial_equity is not None else None,
        )
        return dict(
            account=account,
            positions=positions,
            theory=theory,
            activity=self.activity.report(),
            usdt=self.usdt_ledger.valuation(account, positions, self._fx_quote()),
        )

    def finish(self, reason='end_of_run'):
        self.closed = True
        self.end_orders = [self._public_order(o) for o in self.working()]
        if self.config.get('end_order_policy', 'cancel') == 'cancel':
            for order in self.working():
                if order.status != 'execution_unknown':
                    self._terminal(order, 'cancelled', reason)

    def snapshot(self) -> dict:
        return dict(
            virtual_time_ms=self.now,
            order_guard=self.guard.snapshot(self.now),
            guard_events=list(reversed(self.guard.events)),
            order_event_count=self.order_event_count,
            end_orders=self.end_orders,
            execution_profile='spot-limit-v1',
            uncertain=any((o.status == 'execution_unknown' for o in self.working())),
            queue_model=self.queue_cancel.summary(),
            queue_cancel_events=list(reversed(self.queue_cancel.events)),
            hedge_timing=None,
            books=self.books,
            orders=[self._public_order(o) for o in self.working()],
            order_events=list(reversed(self.order_log)),
            ticks=list(reversed(self.last_ticks)),
            fills=list(reversed(self.last_fills)),
            fill_count=self.fill_count,
            event_count=self.event_sequence,
            **self.valuation(),
        )
