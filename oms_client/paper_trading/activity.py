"""Execution-only turnover and volume; never counts orders, balances or marks."""
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal(0)


def decimal(value):
    return Decimal(str(value))


def nullable_sum(values):
    values = list(values)
    return sum(values, ZERO) if all(value is not None for value in values) else None


def number(value):
    return float(value) if value is not None else None


@dataclass(frozen=True)
class ExecutedTrade:
    exchange: str
    ticker: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    krw_per_usdt: Decimal | None = None

    def __post_init__(self):
        if self.side not in {'buy', 'sell'} or not self.exchange or not self.ticker:
            raise ValueError('체결 거래소·종목·방향을 확인하세요.')
        for value in (self.quantity, self.price, self.fee):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError('체결 수량·가격·수수료는 유효한 Decimal이어야 합니다.')
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError('체결 수량·가격은 양수여야 합니다.')
        rate = self.krw_per_usdt
        if rate is not None and (not isinstance(rate, Decimal) or not rate.is_finite() or rate <= 0):
            raise ValueError('체결 당시 환율을 확인하세요.')


@dataclass
class SideTotals:
    quantity: Decimal = ZERO
    notional: Decimal = ZERO
    fee: Decimal = ZERO
    usdt_notional: Decimal | None = ZERO
    usdt_fee: Decimal | None = ZERO
    count: int = 0

    def add(self, trade):
        notional = trade.price * trade.quantity
        rate = trade.krw_per_usdt
        self.quantity += trade.quantity
        self.notional += notional
        self.fee += trade.fee
        self.usdt_notional = nullable_sum((self.usdt_notional, notional / rate if rate else None))
        self.usdt_fee = nullable_sum((self.usdt_fee, trade.fee / rate if rate else None))
        self.count += 1

    def state(self):
        return {name: value if name == 'count' else str(value) if value is not None else None
                for name, value in vars(self).items()}

    @classmethod
    def restore(cls, state):
        return cls(**{name: value if name == 'count' else decimal(value) if value is not None else None
                      for name, value in state.items()})


@dataclass
class MarketActivity:
    buy: SideTotals = field(default_factory=SideTotals)
    sell: SideTotals = field(default_factory=SideTotals)


class TradingActivity:
    """Account-owned Decimal accumulators, bounded by traded market count."""
    def __init__(self, state=None):
        self.markets = {}
        for market in (state or {}).get('markets', []):
            self.markets[(market['exchange'], market['ticker'])] = MarketActivity(
                SideTotals.restore(market['buy']), SideTotals.restore(market['sell']))

    def record(self, trade: ExecutedTrade):
        market = self.markets.setdefault((trade.exchange, trade.ticker), MarketActivity())
        getattr(market, trade.side).add(trade)

    def state(self):
        return dict(markets=[dict(exchange=exchange, ticker=ticker, buy=market.buy.state(), sell=market.sell.state())
                             for (exchange, ticker), market in sorted(self.markets.items())])

    @staticmethod
    def _totals(markets, unit):
        notional = 'notional' if unit == 'KRW' else 'usdt_notional'
        fee = 'fee' if unit == 'KRW' else 'usdt_fee'
        buy = nullable_sum(getattr(m.buy, notional) for m in markets)
        sell = nullable_sum(getattr(m.sell, notional) for m in markets)
        return dict(buy_turnover=number(buy), sell_turnover=number(sell),
                    turnover=number(nullable_sum((buy, sell))),
                    fees_paid=number(nullable_sum(getattr(getattr(m, side), fee) for m in markets for side in ('buy', 'sell'))),
                    buy_fill_count=sum(m.buy.count for m in markets), sell_fill_count=sum(m.sell.count for m in markets),
                    fill_count=sum(m.buy.count + m.sell.count for m in markets))

    def report(self):
        result = {}
        for unit in ('KRW', 'USDT'):
            markets = []
            for (exchange, ticker), market in sorted(self.markets.items()):
                field_name = 'notional' if unit == 'KRW' else 'usdt_notional'
                row = dict(exchange=exchange, ticker=ticker, **self._totals([market], unit),
                           buy_quantity=float(market.buy.quantity), sell_quantity=float(market.sell.quantity),
                           traded_quantity=float(market.buy.quantity + market.sell.quantity))
                for side in ('buy', 'sell'):
                    totals = getattr(market, side)
                    value = getattr(totals, field_name)
                    row[side + '_vwap'] = number(value / totals.quantity) if value is not None and totals.quantity else None
                markets.append(row)
            totals = self._totals(list(self.markets.values()), unit)
            result[unit] = dict(quote_currency=unit, **totals, markets=markets,
                                status='complete' if totals['turnover'] is not None else 'missing_historical_fx')
        return result


def saved_trade(fill, report=None):
    """Reconstruct only from the execution and its frozen FX, never today's quote."""
    fx = (((report or {}).get('usdt') or {}).get('fill') or {}).get('fx') or {}
    rate = fx.get('rate_decimal', fx.get('krw_per_usdt'))
    return ExecutedTrade(fill['exchange'], fill['ticker'], fill['side'], decimal(fill['quantity']),
                         decimal(fill['price']), decimal(fill['fee']), decimal(rate) if rate is not None else None)


ACTIVITY_FIELDS = ('turnover', 'buy_turnover', 'sell_turnover', 'fees_paid',
                   'fill_count', 'buy_fill_count', 'sell_fill_count')
ACTIVITY_CSV_FIELDS = ['fill_notional_krw', 'fill_notional_usdt'] + [
    f'activity_{unit.lower()}_{key}' for unit in ('KRW', 'USDT') for key in ACTIVITY_FIELDS]


def activity_csv_values(report):
    fill = report.get('fill', report)
    notional = decimal(fill['price']) * decimal(fill['quantity'])
    usdt_fill = (report.get('usdt') or {}).get('fill') or {}
    activity = report.get('activity') or {}
    return [float(notional), usdt_fill.get('notional')] + [
        (activity.get(unit) or {}).get(key) for unit in ('KRW', 'USDT') for key in ACTIVITY_FIELDS]
