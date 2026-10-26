"""Historical USDT reporting basis; never changes KRW execution or reservations."""
from dataclasses import dataclass
from decimal import Decimal as D

ZERO = D(0)
FX_MAX_AGE_MS = 10_000

USDT_ACCOUNT_FIELDS = ('realized_pnl', 'unrealized_pnl', 'theory_unrealized_pnl', 'total_pnl',
                       'theory_total_pnl', 'cash', 'equity', 'fees_paid', 'cash_fx_pnl', 'equity_change_pnl')
USDT_CSV_FIELDS = (['fx_krw_per_usdt', 'fx_rate_decimal', 'fx_observed_at_ms', 'fx_server_time_ms', 'fx_source_id']
                   + ['usdt_price', 'usdt_fee', 'usdt_fill_realized_pnl']
                   + ['usdt_' + field for field in USDT_ACCOUNT_FIELDS])


def usdt_csv_values(report):
    """Only frozen report data; legacy rows intentionally export empty values."""
    usdt = report.get('usdt') or {}
    fill, account = usdt.get('fill') or {}, usdt.get('account') or {}
    fx = fill.get('fx') or {}
    return ([fx.get(key) for key in ('krw_per_usdt', 'rate_decimal', 'observed_at_ms', 'server_time_ms', 'source_id')]
            + [fill.get(key) for key in ('price', 'fee', 'realized_pnl')]
            + [account.get(key) for key in USDT_ACCOUNT_FIELDS])


def amount(value):
    return D(str(value))


def number(value):
    return float(value) if value is not None else None


def plus(left, right):
    return left + right if left is not None and right is not None else None


@dataclass(frozen=True)
class FxQuote:
    bid: D
    ask: D
    observed_ms: int
    server_ms: int
    source_id: str

    @property
    def rate(self):
        return self.bid / 2 + self.ask / 2

    def valid_at(self, now):
        return all(0 <= now - stamp <= FX_MAX_AGE_MS for stamp in (self.observed_ms, self.server_ms))

    @classmethod
    def from_public(cls, quote, now):
        try:
            if not isinstance(quote, dict) or quote.get('exchange', 'bithumb') != 'bithumb' or quote.get('ticker', 'USDT') != 'USDT':
                return None
            bid, ask = amount(quote['bid']), amount(quote['ask'])
            observed, server = quote['receivedAt'], quote['ts']
            if (not bid.is_finite() or not ask.is_finite() or not D('1e-18') <= bid <= ask <= D('1e18')
                    or any(type(value) not in (int, float) or int(value) != value for value in (observed, server))):
                return None
            result = cls(bid, ask, int(observed), int(server),
                         quote.get('source_id') or f'public:bithumb:USDT:{server}:{observed}')
            return result if result.valid_at(now) else None
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None

    def report(self):
        return dict(exchange='bithumb', ticker='USDT', basis='best_bid_ask_midprice',
                    krw_per_usdt=float(self.rate), rate_decimal=str(self.rate), bid=float(self.bid), ask=float(self.ask),
                    observed_at_ms=self.observed_ms, server_time_ms=self.server_ms, source_id=self.source_id)


class UsdtLedger:
    """Weighted historical USDT costs and cash basis, isolated per account."""
    def __init__(self, state=None):
        state = state or {}
        self.initialized = state.get('initialized', False)
        self.initial_fx = state.get('initial_fx')
        self.costs = {key: amount(value) if value is not None else None for key, value in state.get('costs', {}).items()}
        for name in ('realized', 'fees', 'initial_equity', 'cash_basis'):
            value = state.get(name, '0' if name in {'realized', 'fees'} else None)
            setattr(self, name, amount(value) if value is not None else None)

    def initialize(self, cash, costs, fx, *, legacy=False):
        if self.initialized:
            return
        self.initialized = True
        self.initial_fx = fx.report() if fx and not legacy else None
        rate = fx.rate if fx and not legacy else None
        self.costs = {key: amount(cost) / rate if rate else None for key, cost in costs.items()}
        self.cash_basis = amount(cash) / rate if rate else None
        self.initial_equity = (amount(cash) + sum(map(amount, costs.values()), ZERO)) / rate if rate else None
        if legacy:
            self.realized = self.fees = None

    def record_fill(self, key, side, quantity, price, fee, before_quantity, fx):
        quantity, price, fee, before_quantity = map(amount, (quantity, price, fee, before_quantity))
        rate = fx.rate if fx else None
        notional = quantity * price
        fee_usdt = fee / rate if rate else None
        cost = self.costs.get(key, ZERO if before_quantity == 0 else None)
        realized = ZERO
        if side == 'buy':
            paid = (notional + fee) / rate if rate else None
            self.costs[key] = plus(cost, paid)
            cash_change = -paid if paid is not None else None
        else:
            allocated = (cost if quantity == before_quantity else cost * quantity / before_quantity) if cost is not None else None
            proceeds = (notional - fee) / rate if rate else None
            realized = proceeds - allocated if proceeds is not None and allocated is not None else None
            self.realized = plus(self.realized, realized)
            if quantity == before_quantity:
                self.costs.pop(key, None)
            else:
                self.costs[key] = cost - allocated if cost is not None else None
            cash_change = proceeds
        self.cash_basis = plus(self.cash_basis, cash_change)
        self.fees = plus(self.fees, fee_usdt)
        return dict(price=number(price / rate) if rate else None, fee=number(fee_usdt),
                    notional=number(notional / rate) if rate else None, realized_pnl=number(realized),
                    fx=fx.report() if fx else None)

    def valuation(self, account, positions, fx):
        rate = fx.rate if fx else None
        def convert(value):
            return amount(value) / rate if value is not None and rate else None
        valued, unrealized_values, theory_values = [], [], []
        for position in positions:
            key = f"{position['exchange']}:{position['ticker']}"
            quantity = amount(position['quantity'])
            cost = self.costs.get(key)
            market, theory = convert(position.get('market_value')), convert(position.get('theory_market_value'))
            unrealized = market - cost if market is not None and cost is not None else None
            theoretical = theory - cost if theory is not None and cost is not None else None
            unrealized_values.append(unrealized)
            theory_values.append(theoretical)
            valued.append(dict(position, avg_price=number(cost / quantity) if cost is not None and quantity else None,
                               mark_price=number(convert(position.get('mark_price'))), theory_price=number(convert(position.get('theory_price'))),
                               market_value=number(market), theory_market_value=number(theory),
                               unrealized_pnl=number(unrealized), theory_unrealized_pnl=number(theoretical),
                               theory_market_gap_pnl=number(theory - market) if theory is not None and market is not None else None))
        def total(values):
            return sum(values, ZERO) if all(v is not None for v in values) else None
        unrealized, theoretical = total(unrealized_values), total(theory_values)
        cash, equity = convert(account.get('cash')), convert(account.get('equity'))
        result = {key: number(convert(account.get(key))) for key in ('cash', 'free_cash', 'equity', 'theory_equity')}
        result.update(quote_currency='USDT', initial_cash=number(self.initial_equity), initial_equity=number(self.initial_equity),
                      realized_pnl=number(self.realized), fees_paid=number(self.fees), unrealized_pnl=number(unrealized),
                      theory_unrealized_pnl=number(theoretical), total_pnl=number(plus(self.realized, unrealized)),
                      theory_total_pnl=number(plus(self.realized, theoretical)),
                      cash_fx_pnl=number(cash - self.cash_basis) if cash is not None and self.cash_basis is not None else None,
                      equity_change_pnl=number(equity - self.initial_equity) if equity is not None and self.initial_equity is not None else None)
        return dict(account=result, positions=valued, fx=fx.report() if fx else None, initial_fx=self.initial_fx,
                    status='complete' if result['total_pnl'] is not None and fx else 'unavailable_or_partial',
                    basis='execution_time_fx_weighted_cost_v1')

    def state(self):
        return dict(initialized=self.initialized, initial_fx=self.initial_fx,
                    costs={key: str(value) if value is not None else None for key, value in self.costs.items()},
                    **{name: str(getattr(self, name)) if getattr(self, name) is not None else None
                       for name in ('realized', 'fees', 'cash_basis', 'initial_equity')})
