"""Read-only fill inspection: immutable reports and bounded, as-of market context."""
from dataclasses import dataclass
import heapq
import logging

from ..market_data.validation import DataError
from ..storage.access import DATA_ACCESS
from .source import ReplaySource

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextWindow:
    start_ms: int
    end_ms: int
    center_ms: int

    def points(self):
        return sorted({*range(self.start_ms, self.end_ms, 1000), self.center_ms, self.end_ms - 1})


def book_frames(events, window):
    """Use the final observed book at or before each sample, never a future book."""
    points = window.points()
    frames, latest, index, count = [], None, 0, 0
    for event in events:
        count += 1
        while index < len(points) and points[index] < event['time']:
            frames.append(dict(time=points[index], book=latest))
            index += 1
        latest = {key: value for key, value in event.items() if key != '_key'}
    while index < len(points):
        frames.append(dict(time=points[index], book=latest))
        index += 1
    return dict(frames=frames, event_count=count)


def nearby_ticks(events, window, trigger_id, limit=1000):
    count, trigger = 0, None

    def candidates():
        nonlocal count, trigger
        for event in events:
            if not window.start_ms <= event['time'] < window.end_ms:
                continue
            count += 1
            clean = {key: value for key, value in event.items() if key != '_key'}
            if event['source_id'] == trigger_id:
                trigger = clean
            yield clean

    # A bounded heap retains the nearest observations, not just the first second of a busy market.
    selected = heapq.nsmallest(limit, candidates(), key=lambda event: abs(event['time'] - window.center_ms))
    if trigger and not any(item['source_id'] == trigger_id for item in selected):
        selected = selected[:limit - 1] + [trigger]
    selected.sort(key=lambda event: event['time'])
    return dict(items=selected, event_count=count, truncated=count > limit, trigger_found=trigger is not None)


class ContextMarketReader:
    def __init__(self, source_factory=ReplaySource):
        self.source_factory = source_factory

    @DATA_ACCESS.protect
    def read(self, snapshot, fill, window):
        original = snapshot.get('initial_config') or snapshot['config']
        config = dict(original, start_ms=window.start_ms, end_ms=window.end_ms)
        # Five seconds is only a visible last-known book seed, never an assumption of liquidity.
        config['strategy'] = {'parameters': {'max_quote_age_ms': 1000}}
        expected = {item['path']: item for item in snapshot.get('manifest', [])}
        markets = []
        for venue in original.get('venues', ['bithumb']):
            market = dict(venue=venue, frames=[], ticks=[], streams={})
            for kind in ('book', 'tick'):
                source = self.source_factory(config)
                try:
                    events = source.open_context_stream(venue, kind, max_events=100000)
                    matched = True
                    for item in source.manifest:
                        previous = expected.get(item['path'])
                        if previous is None:
                            matched = False
                        elif (previous['size'], previous['mtime_ns']) != (item['size'], item['mtime_ns']):
                            raise DataError('실행 당시와 원본 파일이 달라 해당 구간을 표시하지 않습니다.')
                    data = book_frames(events, window) if kind == 'book' else nearby_ticks(events, window, fill.get('source_tick'))
                    source.verify()
                    market['frames' if kind == 'book' else 'ticks'] = data.pop('frames' if kind == 'book' else 'items')
                    market['streams'][kind] = dict(status='ok', provenance='matched' if matched and source.manifest else 'unverified',
                                                   skipped=source.skipped, **data)
                except Exception as exc:
                    if not isinstance(exc, (DataError, OSError, ValueError)):
                        LOG.exception('Fill inspection market read failed: %s %s', venue, kind)
                    message = str(exc) if isinstance(exc, DataError) else '원본을 읽을 수 없습니다. 파일 보관 상태를 확인하세요.'
                    market['streams'][kind] = dict(status='unavailable', message=message)
                finally:
                    source.close()
            markets.append(market)
        return markets


class FillContextService:
    def __init__(self, store, market_reader=None):
        self.store = store
        self.market_reader = market_reader or ContextMarketReader()

    def get(self, identifier, sequence):
        context = self.store.fill_context(identifier, sequence)
        snapshot = context.pop('snapshot')
        window = ContextWindow(context['start_ms'], context['end_ms'], context['fill']['timestamp'])
        config = snapshot.get('initial_config') or snapshot['config']
        return dict(run_id=identifier, ticker=config['ticker'], checkpoint_ms=snapshot['virtual_time_ms'],
                    run_start_ms=config['start_ms'], run_end_ms=config['end_ms'],
                    max_quote_age_ms=config.get('strategy', {}).get('parameters', {}).get('max_quote_age_ms', 1000),
                    **context, markets=self.market_reader.read(snapshot, context['fill'], window))
