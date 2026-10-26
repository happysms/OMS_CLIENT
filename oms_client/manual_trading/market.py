"""Public spot orderbook/trade subscriptions, independent of all private credentials."""
from collections import deque, OrderedDict
from decimal import Decimal, InvalidOperation
import json
import threading
import time
import uuid
from websockets.sync.client import connect
from .exchange import amount, symbol
ENDPOINTS = {'bithumb': 'wss://ws-api.bithumb.com/websocket/v1', 'okx': 'wss://ws.okx.com/ws/v5/public'}

def subscription(exchange, ticker):
    pair = symbol(exchange, ticker)
    if exchange == 'okx':
        return {'op': 'subscribe', 'args': [{'channel': kind, 'instId': pair} for kind in ('books5', 'trades')]}
    return [{'ticket': uuid.uuid4().hex}, {'type': 'orderbook', 'codes': [pair]}, {'type': 'trade', 'codes': [pair]}, {'format': 'DEFAULT'}]

def stamp(value):
    value = int(value)
    if value > 10 ** 14:
        value //= 1000
    if value <= 0:
        raise ValueError('timestamp')
    return value

def levels(raw, descending):
    result = []
    for row in raw[:5]:
        price = amount(str(row[0]))
        quantity = Decimal(str(row[1]))
        if not quantity.is_finite() or not 0 <= quantity <= Decimal('1e18'):
            raise ValueError('quantity')
        if result and (Decimal(price) >= Decimal(result[-1][0]) if descending else Decimal(price) <= Decimal(result[-1][0])):
            raise ValueError('depth order')
        result.append([price, format(quantity, 'f')])
    if not result:
        raise ValueError('empty book')
    return result

def parse_events(exchange, ticker, message, received_ms):
    pair = symbol(exchange, ticker)
    result = []
    try:
        if not isinstance(message, dict):
            return []
        if exchange == 'okx':
            arg = message.get('arg') or {}
            if arg.get('instId') != pair:
                return []
            for raw in message.get('data', []):
                if arg.get('channel') == 'books5':
                    result.append(dict(kind='book', time=stamp(raw['ts']), bids=levels(raw['bids'], True), asks=levels(raw['asks'], False)))
                elif arg.get('channel') == 'trades' and raw.get('side') in {'buy', 'sell'}:
                    result.append(dict(kind='tick', time=stamp(raw['ts']), id=str(raw['tradeId']), price=amount(raw['px']), quantity=amount(raw['sz']), side=raw['side']))
        elif message.get('code') == pair:
            if message.get('type') == 'orderbook':
                raw = message['orderbook_units']
                result.append(dict(kind='book', time=stamp(message['timestamp']), bids=levels([[item['bid_price'], item['bid_size']] for item in raw], True), asks=levels([[item['ask_price'], item['ask_size']] for item in raw], False)))
            elif message.get('type') == 'trade' and message.get('ask_bid') in {'ASK', 'BID'}:
                result.append(dict(kind='tick', time=stamp(message['trade_timestamp']), id=str(message['sequential_id']), price=amount(str(message['trade_price'])), quantity=amount(str(message['trade_volume'])), side='buy' if message['ask_bid'] == 'BID' else 'sell'))
    except (ValueError, TypeError, KeyError, IndexError, InvalidOperation, OverflowError):
        return []
    return [dict(event, received_ms=received_ms, exchange=exchange, ticker=ticker) for event in result if event['time'] <= received_ms + 5000 and (event['kind'] != 'book' or Decimal(event['bids'][0][0]) < Decimal(event['asks'][0][0]))]

class SpotFeed:

    def __init__(self, exchange, ticker, on_event, on_status, connector=connect):
        self.exchange, self.ticker = (exchange, ticker)
        self.on_event, self.on_status = (on_event, on_status)
        self.connector = connector
        self.stopped = threading.Event()
        self.socket = None
        self.thread = threading.Thread(target=self.run, name='manual-spot-feed', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        delay = 1
        while not self.stopped.is_set():
            self.on_status('connecting')
            try:
                with self.connector(ENDPOINTS[self.exchange], open_timeout=6, close_timeout=1, ping_interval=20, ping_timeout=20, max_size=2 ** 20, max_queue=16) as socket:
                    self.socket = socket
                    if self.stopped.is_set():
                        break
                    socket.send(json.dumps(subscription(self.exchange, self.ticker)))
                    last = time.monotonic()
                    ping = last
                    delay = 1
                    while not self.stopped.is_set():
                        try:
                            raw = socket.recv(timeout=1)
                        except TimeoutError:
                            now = time.monotonic()
                            if now - last > 35:
                                raise TimeoutError()
                            if now - ping > 15:
                                socket.send('ping' if self.exchange == 'okx' else 'PING')
                                ping = now
                            continue
                        last = time.monotonic()
                        if raw in ('pong', 'PONG', b'pong', b'PONG'):
                            continue
                        if raw in ('ping', 'PING', b'ping', b'PING'):
                            socket.send('pong' if self.exchange == 'okx' else 'PONG')
                            continue
                        try:
                            message = json.loads(raw, parse_float=Decimal)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(message, dict) and (message.get('event') == 'error' or message.get('error')):
                            self.on_status('error')
                            return
                        for event in parse_events(self.exchange, self.ticker, message, int(time.time() * 1000)):
                            if not self.stopped.is_set():
                                self.on_event(event)
            except Exception:
                if not self.stopped.is_set():
                    self.on_status('reconnecting')
            finally:
                self.socket = None
            if self.stopped.wait(delay):
                break
            delay = min(30, delay * 2)

    def stop(self):
        self.stopped.set()
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(1.5)

class MarketSessions:

    def __init__(self, feed_factory=SpotFeed, clock=time.time):
        self.feed_factory, self.clock = (feed_factory, clock)
        self.lock = threading.RLock()
        self.change_lock = threading.Lock()
        self.sessions = {}
        self.feeds = {}
        self.generation = 0

    def subscribe(self, exchange, ticker):
        symbol(exchange, ticker)
        with self.change_lock:
            with self.lock:
                current = self.sessions.get(exchange)
                if current and current['ticker'] == ticker and (current['status'] != 'error'):
                    return
                self.generation += 1
                generation = self.generation
                previous = self.feeds.pop(exchange, None)
                self.sessions[exchange] = dict(ticker=ticker, status='connecting', book=None, ticks=deque(maxlen=300), seen=OrderedDict(), generation=generation)
            if previous:
                previous.stop()

            def event(value):
                with self.lock:
                    state = self.sessions.get(exchange)
                    if not state or state['generation'] != generation:
                        return
                    if value['kind'] == 'book':
                        if state['book'] and value['time'] < state['book']['time']:
                            return
                        state['book'] = value
                        state['status'] = 'connected'
                    else:
                        if value['id'] in state['seen']:
                            return
                        state['seen'][value['id']] = None
                        if len(state['seen']) > 2048:
                            state['seen'].popitem(last=False)
                        state['ticks'].append(value)

            def status(value):
                with self.lock:
                    state = self.sessions.get(exchange)
                    if not state or state['generation'] != generation:
                        return
                    state['status'] = value
                    state['book'] = None
            feed = self.feed_factory(exchange, ticker, event, status)
            with self.lock:
                self.feeds[exchange] = feed
            feed.start()

    def snapshot(self, exchange):
        now = int(self.clock() * 1000)
        with self.lock:
            state = self.sessions.get(exchange)
            if not state:
                return dict(status='disconnected', book=None, ticks=[], can_order=False)
            book = state['book']
            age = max(now - book['time'], now - book['received_ms']) if book else None
            fresh = bool(book and state['status'] == 'connected' and (-1000 <= now - book['time'] <= 2000) and (0 <= now - book['received_ms'] <= 2000))
            return dict(exchange=exchange, ticker=state['ticker'], status=state['status'], book=book, ticks=list(reversed(state['ticks'])), age_ms=age, can_order=fresh)

    def require_fresh(self, exchange, ticker):
        current = self.snapshot(exchange)
        if current.get('ticker') != ticker or not current['can_order']:
            raise ValueError('선택 종목의 호가가 없거나 2초 이상 지연되었습니다. 시세 연결 후 다시 주문하세요.')

    def close(self):
        with self.change_lock:
            with self.lock:
                feeds = list(self.feeds.values())
                self.feeds.clear()
                self.sessions.clear()
            for feed in feeds:
                feed.stop()
