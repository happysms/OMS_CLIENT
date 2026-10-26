"""Live fair value from known, completed premium hours and fresh reference books.

This provider is deliberately not used for historical replay: present-day saved
hour revisions are not a historical point-in-time feature store.
"""
import math

from ..analysis.premiums import completed_end
from ..storage.premiums import PremiumStore


class PremiumTheoryProvider:
    def __init__(self, directory, quotes, validator, clock, days=7):
        if days not in {3, 7}:
            raise ValueError('Theory window must be 3 or 7 days')
        self.store, self.quotes = PremiumStore(directory), quotes
        self.validator, self.clock, self.days = validator, clock, days

    def get_many(self, pairs):
        tickers = sorted({ticker for exchange, ticker in pairs if exchange == 'bithumb'})
        references = self.quotes.get_many([('bithumb', 'USDT'), *[('okx', ticker) for ticker in tickers]], timeout=3) if tickers else {}
        now = int(self.clock() * 1000)
        end = completed_end(now)
        # Each returned value, coverage and provenance is frozen into its fill report.
        means = {item['ticker']: item['windows'][str(self.days)] for item in self.store.summary(end, tickers)['items']}
        result = {}
        for exchange, ticker in pairs:
            item = dict(price=None, basis=f'okx_usdt_premium_{self.days}d', available_at_ms=now,
                        window_end_ms=end, window_days=self.days)
            result[(exchange, ticker)] = item
            if exchange != 'bithumb':
                item['reason'] = '빗썸 종목의 이론가만 지원합니다.'
                continue
            window = means[ticker]
            item.update(mean_premium_pct=window['mean_pct'], samples=window['samples'], coverage_pct=window['coverage_pct'])
            if window['mean_pct'] is None:
                item['reason'] = '완료된 과거 괴리 표본이 없습니다.'
                continue
            try:
                okx = self.validator(references.get(('okx', ticker)), 'okx', ticker, now)
                fx = self.validator(references.get(('bithumb', 'USDT')), 'bithumb', 'USDT', now)
                if max(okx['receivedAt'], fx['receivedAt']) > now:
                    raise ValueError('Future reference receipt')
            except (ValueError, RuntimeError):
                item['reason'] = '신선한 OKX·USDT 기준 호가가 없습니다.'
                continue
            price = okx['mid'] * fx['mid'] * (1 + window['mean_pct'] / 100)
            if not math.isfinite(price) or price <= 0:
                item['reason'] = '이론가 계산 결과가 유효하지 않습니다.'
                continue
            item.update(price=price, okx_quote=okx, fx_quote=fx, reason=None,
                        available_at_ms=int(self.clock() * 1000))
        return result

    def close(self):
        if hasattr(self.quotes, 'close'):
            self.quotes.close()
