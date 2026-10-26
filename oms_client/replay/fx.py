"""Causal, independent FX cursor; reference quotes never trigger strategy events."""

from ..paper_trading.currency import FxQuote


class AsOfFxQuotes:
    def __init__(self, events=()):
        self.events = iter(events)
        self.pending = None
        self.latest = None
        self.finished = False
        self.error = None
        self.now = None

    def at(self, now):
        if self.now is not None and now < self.now:
            return None
        self.now = now
        try:
            while True:
                if self.pending is None and not self.finished:
                    self.pending = next(self.events, None)
                    self.finished = self.pending is None
                if self.pending is None or self.pending['time'] > now:
                    break
                event, self.pending = self.pending, None
                self.latest = FxQuote.from_public(
                    dict(
                        bid=event['bids'][0]['price'],
                        ask=event['asks'][0]['price'],
                        ts=event.get('server_time'),
                        receivedAt=event['time'],
                        source_id=event['source_id'],
                    ),
                    now,
                )
        except Exception:
            self.finished, self.latest = True, None
            self.error = 'USDT 원본 환율을 읽지 못했습니다.'
        return self.latest if self.latest and self.latest.valid_at(now) else None
