"""Fixed public exchange WebSocket adapters and normalized best bid/ask."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid

from websockets.sync.client import connect

ENDPOINTS = {
    "bithumb": "wss://ws-api.bithumb.com/websocket/v1",
    "upbit": "wss://api.upbit.com/websocket/v1",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
}


def parse_quotes(exchange: str, message) -> list[dict]:
    """Normalize complete best-bid/ask snapshots, including Bithumb microseconds."""
    messages = message if isinstance(message, list) else [message]
    quotes = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        books = []
        if exchange == "okx":
            arg = item.get("arg") or {}
            instrument = str(arg.get("instId", ""))
            if arg.get("channel") not in {"books5", "bbo-tbt"} or not instrument.endswith("-USDT-SWAP"):
                continue
            for book in item.get("data") or []:
                try:
                    books.append((instrument[:-10], book["bids"][0][0], book["asks"][0][0], book["ts"]))
                except (KeyError, IndexError, TypeError):
                    continue
        elif exchange in {"bithumb", "upbit"}:
            code = str(item.get("code", ""))
            if item.get("type") != "orderbook" or not code.startswith("KRW-"):
                continue
            try:
                book = item["orderbook_units"][0]
                stamp = float(item["timestamp"])
                if exchange == "bithumb" and stamp > 1e14:
                    stamp /= 1000
                books.append((code[4:], book["bid_price"], book["ask_price"], stamp))
            except (KeyError, IndexError, TypeError, ValueError, OverflowError):
                continue
        for ticker, raw_bid, raw_ask, raw_stamp in books:
            try:
                bid, ask, stamp = float(raw_bid), float(raw_ask), float(raw_stamp)
                if not all(math.isfinite(v) for v in (bid, ask, stamp)) or bid <= 0 or ask < bid or stamp <= 0:
                    continue
                mid = bid / 2 + ask / 2
                if not math.isfinite(mid):
                    continue
                quotes.append({"exchange": exchange, "ticker": ticker, "bid": bid, "ask": ask, "mid": mid, "ts": int(stamp)})
            except (ValueError, TypeError, OverflowError):
                continue
    return quotes



def _subscription(exchange, legs):
    tickers = sorted({leg["ticker"] for leg in legs})
    if exchange == "okx":
        return {"op": "subscribe", "args": [{"channel": "books5", "instId": f"{ticker}-USDT-SWAP"} for ticker in tickers]}
    return [{"ticket": f"midprice-{uuid.uuid4().hex}"}, {"type": "orderbook", "codes": [f"KRW-{ticker}" for ticker in tickers]}, {"format": "DEFAULT"}]



class PublicQuoteFeed:
    def __init__(self, legs, on_quote, on_status, connector=connect):
        self.legs, self.on_quote, self.on_status = legs, on_quote, on_status
        self.connector = connector
        self._stop = threading.Event()
        self._threads = []
        self._sockets = {}
        self._lock = threading.Lock()

    def start(self):
        for exchange in sorted({leg["exchange"] for leg in self.legs}):
            thread = threading.Thread(target=self._run, args=(exchange,), name=f"midprice-feed-{exchange}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _status(self, exchange, state, message=""):
        if not self._stop.is_set():
            self.on_status({"exchange": exchange, "state": state, "message": message})

    def _run(self, exchange):
        legs = [leg for leg in self.legs if leg["exchange"] == exchange]
        expected = {leg["ticker"] for leg in legs}
        delay = 1
        while not self._stop.is_set():
            self._status(exchange, "connecting" if delay == 1 else "reconnecting")
            try:
                with self.connector(ENDPOINTS[exchange], open_timeout=8, close_timeout=1,
                                    ping_interval=20, ping_timeout=20, max_size=2**20, max_queue=16) as socket:
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self._sockets[exchange] = socket
                    socket.send(json.dumps(_subscription(exchange, legs)))
                    seen, previous = set(), {}
                    last_message, last_ping = time.monotonic(), time.monotonic()
                    delay = 1
                    while not self._stop.is_set():
                        try:
                            raw = socket.recv(timeout=1)
                        except TimeoutError:
                            now = time.monotonic()
                            if now - last_message > 35:
                                raise TimeoutError("거래소 응답을 기다리는 시간이 초과됐습니다.")
                            if now - last_ping >= 20:
                                socket.send("ping" if exchange == "okx" else "PING")
                                last_ping = now
                            continue
                        last_message = time.monotonic()
                        if raw in ("pong", "PONG", b"pong", b"PONG"):
                            continue
                        if raw in ("ping", "PING", b"ping", b"PING"):
                            socket.send("pong" if exchange == "okx" else "PONG")
                            continue
                        try:
                            message = json.loads(raw)
                        except (ValueError, UnicodeDecodeError, TypeError):
                            continue
                        if isinstance(message, dict) and (message.get("event") == "error" or message.get("error")):
                            error = message.get("error")
                            detail = error.get("message") if isinstance(error, dict) else message.get("msg") or str(error)
                            self._status(exchange, "error", str(detail or "거래소가 종목 구독을 거절했습니다.")[:300])
                            return
                        for quote in parse_quotes(exchange, message):
                            ticker = quote["ticker"]
                            if ticker not in expected or quote["ts"] < previous.get(ticker, 0):
                                continue
                            previous[ticker] = quote["ts"]
                            seen.add(ticker)
                            received = int(time.time() * 1000)
                            for leg in legs:
                                if leg["ticker"] == ticker and not self._stop.is_set():
                                    self.on_quote({**quote, "id": leg["id"], "receivedAt": received,
                                                   "currency": "KRW" if exchange in {"bithumb", "upbit"} else "USDT"})
                        self._status(exchange, "connected" if expected <= seen else "connecting")
            except Exception:
                # Details may contain proxy credentials; expose only a fixed public error.
                self._status(exchange, "reconnecting", "시세 연결이 끊겨 다시 연결하고 있습니다.")
            finally:
                with self._lock:
                    self._sockets.pop(exchange, None)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def stop(self):
        self._stop.set()
        with self._lock:
            sockets = list(self._sockets.values())
        for socket in sockets:
            try:
                socket.close()
            except Exception:
                pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.5)
