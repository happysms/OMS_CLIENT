"""One bounded replay worker; virtual time is independent of rendering and pacing."""
from copy import deepcopy
import json
import logging
import threading
import time
import uuid

from ..strategies.registry import StrategyRegistry, DEFAULT_ID
from ..storage.access import DATA_ACCESS
from .clock import PlaybackClock
from .engine import ReplayEngine, config_from
from .source import ReplaySource
from .store import ReplayStore, account_metadata

TERMINAL = {'finished','stopped','failed','interrupted'}
LOG = logging.getLogger(__name__)


class ReplaySession:
    def __init__(self, identifier, config, store, source_factory=ReplaySource):
        self.id, self.config, self.store = identifier, config, store
        self.initial_config = deepcopy(config)
        self.virtual_account = account_metadata(identifier, config, int(time.time()*1000))
        self.engine = ReplayEngine(config)
        self.source = source_factory(config)
        self.condition = threading.Condition(threading.RLock())
        self.clock = None
        self.status = 'loading'
        self.error = None
        self.fast = config['fast']
        self.speed = config['speed']
        self.paused = False
        self.stop_requested = False
        self.sequence = 0
        self.command_sequence = 0
        self.commands = []
        self._snapshot_json = '{}'
        self._publish()
        self.thread = threading.Thread(target=self._run, name='historical-replay', daemon=True)

    def _publish(self):
        self.sequence += 1
        snapshot = dict(id=self.id, status=self.status, error=self.error, config=dict(self.config), initial_config=self.initial_config, virtual_account=self.virtual_account,
                        speed=self.speed, fast=self.fast, paused=self.paused, revision=self.sequence,
                        total_events=self.source.total, skipped_events=self.source.skipped,
                        manifest=self.source.manifest, **self.engine.snapshot())
        snapshot['lag_ms'] = max(0, int(min(self.config['end_ms'], self.clock.now())-self.engine.now)) if self.clock and not self.fast else 0
        # The UI and the database expose the same committed state. Failed commits never leak fills.
        encoded = self.store.save(snapshot, self.engine.new_fills, self.commands, self.engine.new_order_events)
        self.engine.new_fills.clear(); self.commands.clear(); self.engine.new_order_events.clear()
        self._snapshot_json = encoded

    @property
    def snapshot(self):
        """Detached diagnostic view; committed state has a single JSON owner."""
        return self.get()

    def get(self):
        return json.loads(self.get_encoded())

    def get_encoded(self):
        with self.condition:
            # Immutable committed text is safe to read/decode outside the worker's lock.
            return self._snapshot_json

    def control(self, payload):
        if not isinstance(payload,dict): raise ValueError('제어 설정을 확인하세요.')
        action = payload.get('action')
        allowed = {'pause':set(), 'play':set(), 'stop':set(), 'pace':{'speed','fast'},
                   'order':{'side','price','quantity','tif','stp','expires_at_ms','client_order_id'},
                   'replace':{'order_id','price','total_quantity','tif'},'cancel_all':set(),
                   'cancel':{'order_id'}, 'auto_mm':{'enabled'}}
        if action not in allowed or set(payload)-allowed[action]-{'action'}:
            raise ValueError('지원하지 않는 재생 제어입니다.')
        with self.condition:
            if self.status in TERMINAL: raise ValueError('종료된 실행은 변경할 수 없습니다.')
            if action == 'stop':
                self.stop_requested = True
            elif self.status == 'loading':
                raise ValueError('원본 데이터를 불러온 뒤 제어할 수 있습니다.')
            elif action == 'pace':
                speed, fast = payload.get('speed', self.speed), payload.get('fast', self.fast)
                if type(speed) is not int or not 1<=speed<=32 or type(fast) is not bool:
                    raise ValueError('배속은 1~32이며 빠른 실행은 참/거짓이어야 합니다.')
                if self.fast != fast: self.clock.seek_anchor(self.engine.now)
                self.speed, self.fast = speed, fast
                self.clock.set(speed=speed)
            elif action in {'play','pause'}:
                self.paused = action == 'pause'
                self.clock.set(paused=self.paused)
                self.status = 'paused' if self.paused else 'running'
            elif action == 'order':
                self.engine.place(payload.get('side'), payload.get('price'), payload.get('quantity'),
                    **{k:payload[k] for k in ('tif','stp','expires_at_ms','client_order_id') if k in payload})
            elif action == 'replace':
                self.engine.replace(payload.get('order_id'),payload.get('price'),payload.get('total_quantity'),payload.get('tif'))
            elif action == 'cancel_all':
                for order in self.engine.working():
                    if order.status!='execution_unknown':self.engine.cancel(order.id)
            elif action == 'cancel':
                identifier = payload.get('order_id')
                if not isinstance(identifier,str): raise ValueError('주문 ID를 확인하세요.')
                self.engine.cancel(identifier)
            elif action == 'auto_mm':
                enabled = payload.get('enabled')
                if type(enabled) is not bool: raise ValueError('MM 설정을 확인하세요.')
                self.config['auto_mm'] = enabled
                if not enabled:
                    for order in self.engine.working():
                        if order.owner=='bithumb_simple_mm' and order.status!='execution_unknown': self.engine.cancel(order.id)
                self.engine.auto()
            self.command_sequence += 1
            self.commands.append(dict(sequence=self.command_sequence,virtual_time_ms=self.engine.now,**payload))
            try:
                if self.status != 'loading': self._publish()
            except Exception:
                self.stop_requested = True
                self.condition.notify_all()
                raise
            self.condition.notify_all()
            return json.loads(self._snapshot_json)

    @DATA_ACCESS.protect
    def _run(self):
        try:
            events = iter(self.source.load())
            event = next(events, None)
            # Warm-up quotes establish the initial coin cost basis; no warm-up fills.
            while event is not None and event['time'] <= self.config['start_ms']:
                if self.stop_requested: break
                self.engine.on_event(event)
                event = next(events, None)
            with self.condition:
                if self.stop_requested:
                    self.engine.finish('stopped'); self.status = 'stopped'; self._publish(); return
                self.engine.start()
                if self.engine.initial_equity is None:
                    raise ValueError(f'시작 시각 이전 {self.engine.mm.config.max_quote_age_ms}ms 안의 유효한 빗썸 호가가 필요합니다. 시작 시각을 조정하세요.')
                self.clock = PlaybackClock(self.config['start_ms'], self.speed)
                self.status = 'running'
                self._publish()
            last_publish = time.monotonic()
            while True:
                with self.condition:
                    if self.stop_requested:
                        self.engine.finish('stopped'); self.status = 'stopped'; self._publish(); break
                    if self.paused:
                        self.condition.wait(.1); continue
                    end = self.config['end_ms']
                    target = end if self.fast else min(end, int(self.clock.now()))
                    processed = 0
                    # Bounded chunks allow pause/speed/order commands even during batch runs.
                    while processed < 250:
                        due = self.engine.due()
                        next_time = min(event['time'] if event is not None else end,
                                        due if due is not None else end, end)
                        if next_time > target: break
                        if next_time >= end:
                            self.engine.advance(end)
                            self.source.verify()
                            self.engine.finish(); self.status = 'finished'; break
                        if event is not None and event['time'] == next_time:
                            self.engine.on_event(event)
                            event = next(events, None)
                        else:
                            self.engine.advance(next_time)
                        processed += 1
                    if self.status in TERMINAL:
                        self._publish(); break
                    if processed < 250:
                        self.engine.advance(target)
                    wall = time.monotonic()
                    if wall-last_publish >= .1:
                        self._publish(); last_publish=wall
                    # Releases the control lock even when fast mode remains busy.
                    self.condition.wait(.001 if processed == 250 or self.fast else .02)
        except Exception as exc:
            LOG.exception('Historical replay failed: %s', self.id)
            with self.condition:
                self.error = str(exc)
                self.status = 'failed'
                self.engine.finish('failed')
                try:
                    self._publish()
                except Exception:
                    # Keep only the last committed account and mark it incomplete.
                    failed = dict(self.get(), status='failed', error='저장에 실패했습니다. 마지막 저장 결과만 표시합니다.')
                    self._snapshot_json = json.dumps(failed, allow_nan=False)
        finally:
            self.source.close()


class ReplayManager:
    def __init__(self, directory, source_factory=ReplaySource):
        self.store = ReplayStore(directory)
        self.strategies = StrategyRegistry(directory)
        self.source_factory = source_factory
        self.lock = threading.Lock()
        self.active = None
        self.closed = False

    def create(self, payload):
        if not isinstance(payload,dict): raise ValueError('실행 설정을 확인하세요.')
        payload=dict(payload)
        identifier=payload.pop('strategy_id',DEFAULT_ID)
        revision=payload.pop('strategy_revision',None)
        config = config_from(payload)
        if config['theory_price'] is not None:
            raise ValueError('이론가는 등록 전략의 출력으로 받습니다. 계좌의 고정 이론가 입력을 비워 주세요.')
        try:
            config['strategy'] = self.strategies.bind(identifier,revision)
        except KeyError:
            raise ValueError('선택한 전략 또는 버전이 없습니다. 전략 목록을 새로고침하세요.') from None
        with self.lock:
            if self.closed: raise ValueError('재생 서비스가 종료 중입니다.')
            if self.active and self.active.status not in TERMINAL:
                raise ValueError('실행 중인 시뮬레이션을 중지하거나 완료한 뒤 시작하세요.')
            session = ReplaySession(uuid.uuid4().hex, config, self.store, self.source_factory)
            self.active = session
            session.thread.start()
            return session.get()

    def get(self, identifier):
        with self.lock: active = self.active
        return active.get() if active and active.id == identifier else self.store.get(identifier)

    def get_encoded(self, identifier):
        with self.lock: active = self.active
        return active.get_encoded() if active and active.id == identifier else self.store.get_encoded(identifier)

    def control(self, identifier, payload):
        with self.lock: active = self.active
        if not active or active.id != identifier:
            self.store.get(identifier)
            raise ValueError('종료되거나 중단된 실행입니다. 새 실행을 시작하세요.')
        return active.control(payload)

    def close(self):
        with self.lock:
            self.closed = True
            active = self.active
        if active and active.thread.is_alive():
            with active.condition:
                active.stop_requested = True
                active.condition.notify_all()
            active.thread.join(timeout=3)
