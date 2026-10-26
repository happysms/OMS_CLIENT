"""Point-in-time cash-spot valuation; never changes a ledger or realizes PnL."""

import math


def evaluate_positions(account, positions, marks, theories):
    valued = []
    for source in positions:
        position = dict(source)
        pair = (position['exchange'], position['ticker'])
        quote, theory = marks.get(pair), theories.get(pair, {})
        quantity, average = position['quantity'], position['avg_price']
        mark = quote['bid'] if quote else None
        theoretical = theory.get('price')
        position.update(
            id=f'{pair[0]}:{pair[1]}',
            mark_price=mark,
            market_value=quantity * mark if mark is not None else None,
            unrealized_pnl=quantity * (mark - average) if mark is not None else None,
            quote_time=quote['ts'] if quote else None,
            mark_quote=quote,
            theory_price=theoretical,
            theory=theory,
            theory_market_value=quantity * theoretical if theoretical is not None else None,
            theory_unrealized_pnl=(
                quantity * (theoretical - average) if theoretical is not None else None
            ),
        )
        position['theory_market_gap_pnl'] = (
            quantity * (theoretical - mark)
            if theoretical is not None and mark is not None
            else None
        )
        valued.append(position)

    def total(key):
        values = [p[key] for p in valued]
        return (
            math.fsum(values)
            if all(value is not None and math.isfinite(value) for value in values)
            else None
        )

    market, unrealized = total('market_value'), total('unrealized_pnl')
    theoretical, theory_unrealized = total('theory_market_value'), total('theory_unrealized_pnl')
    equity = account['cash'] + market if market is not None else None
    theory_equity = account['cash'] + theoretical if theoretical is not None else None
    result = {
        **account,
        'equity': equity,
        'unrealized_pnl': unrealized,
        'theory_equity': theory_equity,
        'theory_unrealized_pnl': theory_unrealized,
        'total_pnl': account['realized_pnl'] + unrealized if unrealized is not None else None,
        'theory_total_pnl': (
            account['realized_pnl'] + theory_unrealized if theory_unrealized is not None else None
        ),
        'return_pct': (
            (equity / account['initial_cash'] - 1) * 100
            if equity is not None and account['initial_cash']
            else None
        ),
    }
    return result, valued


def valid_theories(theories, now_ms, max_age_ms):
    result = {}
    for pair, source in theories.items():
        item = dict(source) if isinstance(source, dict) else {}
        price, stamp = item.get('price'), item.get('available_at_ms')
        references = [
            item[key] for key in ('okx_quote', 'fx_quote') if isinstance(item.get(key), dict)
        ]
        stale_reference = any(
            not isinstance(quote.get(key), (int, float))
            or not math.isfinite(quote[key])
            or now_ms - quote[key] > max_age_ms
            or (key == 'receivedAt' and quote[key] > now_ms)
            for quote in references
            for key in ('ts', 'receivedAt')
        )
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
            or isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or not 0 <= now_ms - stamp <= max_age_ms
            or stale_reference
        ):
            item.update(
                price=None,
                reason=item.get('reason') or '이론가 시각이 없거나 지연·미래 시각입니다.',
            )
        result[pair] = item
    return result
