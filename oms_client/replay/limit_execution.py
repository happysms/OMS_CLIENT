"""Explicit synthetic spot execution profile; not a native exchange rule claim."""
from decimal import Decimal as D
import json
from pathlib import Path

TIFS = ('post_only', 'gtc', 'ioc', 'fok', 'gtd')
STPS = ('cancel_taker', 'cancel_maker', 'cancel_both')


def capabilities():
    cases=json.loads(Path(__file__).with_name('limit_case_matrix.json').read_text())
    return dict(profile='spot-limit-v1',execution_venues=['bithumb'],tifs=list(TIFS),stp=list(STPS),
                amendments='cancel_confirm_then_new',notifications='synchronous_truth',
                unsupported=['okx_execution','futures','reduce_only','native_amend','stop_limit','oco','iceberg','auction','liquidation','delayed_notifications'],
                counts={status:sum(c['status']==status for c in cases) for status in ('implemented','model_assumption','partial','unsupported')},cases=cases)


class ExecutionDepth:
    """Counterfactual self consumption survives repeated/increased raw snapshots."""
    def __init__(self):
        self.self_debt = {}
        self.public_debt = {}

    def book(self):
        # Snapshot-after-tape reconciliation is an explicit feed-order assumption.
        self.public_debt.clear()

    def available(self, side, price, raw):
        if raw is None:
            return None
        key = (side, price)
        return max(D(0), D(str(raw)) - self.self_debt.get(key, D(0)) - self.public_debt.get(key, D(0)))

    def consume(self, side, price, quantity, role):
        debt = self.self_debt if role == 'taker' else self.public_debt
        key = (side, price)
        debt[key] = debt.get(key, D(0)) + quantity


def plan_limit(order, book, depth, working, step):
    """Pure dry run: FOK can decide before any ledger, STP, or depth mutation."""
    buy = order['side'] == 'buy'
    opposite = 'sell' if buy else 'buy'
    levels = book['asks' if buy else 'bids']
    limit = order['price']
    crosses = lambda price: price <= limit if buy else price >= limit
    raw = {D(str(level['price'])): level['quantity'] for level in levels}
    makers = {}
    for other in working:
        if other['id'] != order['id'] and other['side'] == opposite and other['active_at'] is not None and other['status'] in {'live','cancel_pending'}:
            makers.setdefault(other['price'], []).append(other['id'])
    if order['tif'] == 'post_only':
        if crosses(D(str(levels[0]['price']))):
            return [], 'reject', 'post_only_cross'
        if any(crosses(price) for price in makers):
            return [], 'reject', 'self_cross'
        return [], 'rest', None
    remaining = order['remaining']
    actions = []
    for price in sorted(set(raw) | set(makers), reverse=not buy):
        if not crosses(price):
            break
        # Profile: STP precedes external liquidity at the same price; better
        # external prices may already have executed. Never synthesize a self fill.
        if price in makers:
            if order['stp'] in {'cancel_maker','cancel_both'}:
                actions.extend(('cancel', identifier, None) for identifier in makers[price])
            if order['stp'] != 'cancel_maker':
                return actions, 'cancel', 'self_trade_prevention'
        if price not in raw:
            continue
        available = depth.available(opposite, price, raw[price])
        if available is None:
            return actions, 'unknown', 'missing_depth_quantity'
        size = min(remaining, (available // step) * step)
        if size:
            actions.append(('fill', price, size))
            remaining -= size
        if not remaining:
            return actions, 'filled', None
    last = D(str(levels[-1]['price']))
    beyond = limit > last if buy else limit < last
    if beyond:
        return actions, 'unknown', 'depth_exhausted'
    if order['tif'] in {'ioc','fok'}:
        return actions, 'cancel', 'ioc_remainder' if order['tif']=='ioc' else 'fok_unfilled'
    return actions, 'rest', None
