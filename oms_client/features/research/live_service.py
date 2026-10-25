"""Bounded server-side public quote collection and live analysis.

Only fixed public exchange endpoints are used. There is no trading or account API.
"""
from __future__ import annotations

import ast
from collections import deque
import json
import math
import os
import threading
import time
import uuid

import pandas as pd
from websockets.sync.client import connect

from . import data_service as ds


ENDPOINTS = {
    "bithumb": "wss://ws-api.bithumb.com/websocket/v1",
    "upbit": "wss://api.upbit.com/websocket/v1",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
}
MAX_SAMPLES = 3600
QUOTE_TTL_MS = 10_000


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


class _LiveSession:
    def __init__(self, identifier, payload, feed_factory, clock=time.time):
        self.id = identifier
        self.formula = payload["formula"]
        ids = ds.formula_ids(self.formula)
        candidates = payload.get("legs")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
            raise ds.DataError("실시간 소스를 A~D 중 1~4개 선택하세요.")
        by_id = {}
        for leg in candidates:
            if not isinstance(leg, dict) or not isinstance(leg.get("id"), str) or leg.get("id") not in {"A", "B", "C", "D"} or leg["id"] in by_id:
                raise ds.DataError("실시간 소스의 이름은 중복 없는 A~D여야 합니다.")
            by_id[leg["id"]] = leg
        self.legs = []
        for identifier in ids:
            leg = by_id.get(identifier)
            if not leg or leg.get("exchange") not in ENDPOINTS:
                raise ds.DataError("실시간은 빗썸·업비트·OKX를 지원합니다. 소스의 거래소를 확인하세요.")
            ticker = leg.get("ticker")
            if not isinstance(ticker, str) or not ds.IDENTIFIER.fullmatch(ticker.strip().upper()) or len(ticker) > 35:
                raise ds.DataError("실시간 종목 코드를 확인하세요.")
            exchange = leg["exchange"]
            self.legs.append({"id": identifier, "exchange": exchange, "ticker": ticker.strip().upper(),
                              "currency": "KRW" if exchange in {"bithumb", "upbit"} else "USDT"})
        self.span = ds._integer(payload.get("ewm_span", 60), "EMA 구간 수", 1, 1_000_000)
        self.tree = ds._formula_tree(self.formula, set(ids))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"MA", "ZSCORE", "RET", "DIFF"}:
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value >= MAX_SAMPLES:
                    raise ds.DataError("실시간 수식의 조회 구간 수는 3,600 미만이어야 합니다.")
        self.clock = clock
        self.last_access = time.monotonic()
        self.quotes, self.connections = {}, {exchange: {"exchange": exchange, "state": "connecting", "message": ""} for exchange in {l["exchange"] for l in self.legs}}
        self.samples = deque(maxlen=MAX_SAMPLES)
        self.result = None
        self.status = "running"
        self.error = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.feed = feed_factory(self.legs, self._quote, self._connection_status)
        self.thread = threading.Thread(target=self._sample_loop, name=f"midprice-live-{self.id[:8]}", daemon=True)

    def start(self):
        self.feed.start()
        self.thread.start()

    def _quote(self, quote):
        with self.lock:
            if self.status != "running":
                return
            previous = self.quotes.get(quote["id"])
            if previous and quote["ts"] < previous["ts"]:
                return
            self.quotes[quote["id"]] = quote

    def _connection_status(self, status):
        with self.lock:
            if self.status != "running":
                return
            exchange = status["exchange"]
            self.connections[exchange] = status
            if status["state"] in {"error", "reconnecting", "disconnected"}:
                self.quotes = {key: quote for key, quote in self.quotes.items() if quote["exchange"] != exchange}

    def _missing(self, now):
        missing = []
        for leg in self.legs:
            quote = self.quotes.get(leg["id"])
            if (not quote or now - quote["ts"] > QUOTE_TTL_MS or now - quote["receivedAt"] > QUOTE_TTL_MS
                    or quote["ts"] > now + 5000 or self.connections[leg["exchange"]]["state"] != "connected"):
                missing.append({key: leg[key] for key in ("id", "exchange", "ticker")})
        return missing

    def _tick(self):
        with self.lock:
            now = int(self.clock() * 1000)
            pruned = False
            while self.samples and self.samples[0]["time"] < now - 3_600_000:
                self.samples.popleft()
                pruned = True
            if not self._missing(now) and (not self.samples or now > self.samples[-1]["time"]):
                self.samples.append({"time": now, "quotes": {leg["id"]: dict(self.quotes[leg["id"]]) for leg in self.legs}})
            elif not pruned:
                return
            if not self.samples:
                self.result = None
                return
            samples = list(self.samples)
        index = pd.Index([sample["time"] for sample in samples])
        values = {leg["id"]: pd.Series([sample["quotes"][leg["id"]]["mid"] for sample in samples], index=index) for leg in self.legs}
        result = ds._evaluate(self.tree, values, index)
        smooth = result.ewm(span=self.span).mean()
        valid = result.dropna()
        mean, std = ds._number(valid.mean()), ds._number(valid.std())
        latest = ds._number(valid.iloc[-1]) if len(valid) else None
        first = ds._number(valid.iloc[0]) if len(valid) else None
        legs = []
        for leg in self.legs:
            legs.append({**leg, **{column: [sample["quotes"][leg["id"]][column] for sample in samples] for column in ("mid", "bid", "ask")}})
        output = {"times": list(index), "result": ds._array(result), "ewm": ds._array(smooth), "legs": legs, "mean": mean,
                  "stats": {"count": len(valid), "latest": latest, "mean": mean, "std": std, "first": first,
                            "min": ds._number(valid.min()), "max": ds._number(valid.max()),
                            "median": ds._number(valid.median()),
                            "latest_minus_mean": ds._number(latest - mean) if latest is not None and mean is not None else None,
                            "latest_zscore": ds._number((latest - mean) / std) if std not in (None, 0) and latest is not None else None},
                  "meta": {"source": "live", "interval": "live", "formula": self.formula, "ewm_span": self.span,
                           "aligned_points": len(index), "returned_points": len(index),
                           "warnings": (["함수 계산에 필요한 표본이 아직 충분하지 않거나 계산 결과가 정의되지 않은 구간은 빈 값입니다."] if len(valid) < len(index) else []),
                           "window_basis": "aligned_samples", "undefined_points": len(index) - len(valid),
                           "start": ds._iso(index[0]), "end": ds._iso(index[-1]), "sample_interval_ms": 1000}}
        with self.lock:
            if self.status == "running":
                self.result = output

    def _sample_loop(self):
        try:
            while not self.stop_event.wait(1):
                self._tick()
        except Exception:
            with self.lock:
                self.error = "실시간 계산을 완료하지 못했습니다. 수식과 소스를 확인하고 다시 시작하세요."
                self.status = "failed"
            self.feed.stop()

    def snapshot(self):
        with self.lock:
            if self.status == "running":
                self.last_access = time.monotonic()
            errors = [status["message"] for status in self.connections.values() if status["state"] == "error" and status.get("message")]
            return {"id": self.id, "status": self.status, "result": self.result,
                    "quotes": [dict(q) for q in self.quotes.values()], "connections": [dict(s) for s in self.connections.values()],
                    "missing": self._missing(int(self.clock() * 1000)), "error": self.error or (errors[0] if errors else None)}

    def stop(self):
        with self.lock:
            self.status = "stopped"
            self.last_access = time.monotonic()
            self.stop_event.set()
        self.feed.stop()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.lock:
            for exchange in self.connections:
                self.connections[exchange] = {"exchange": exchange, "state": "disconnected", "message": ""}


class LiveAnalysisManager:
    def __init__(self, feed_factory=PublicQuoteFeed, idle_timeout=120, max_active=2, max_retained=8, start_reaper=True):
        self.feed_factory, self.idle_timeout, self.max_active = feed_factory, idle_timeout, max_active
        self.max_retained = max(max_retained, max_active)
        self.sessions = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.reaper = None
        if start_reaper:
            self.reaper = threading.Thread(target=self._reap_loop, name="midprice-live-cleanup", daemon=True)
            self.reaper.start()

    def start(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("formula"), str):
            raise ds.DataError("실시간 수식과 소스를 입력하세요.")
        if self.closed.is_set():
            raise ds.DataError("실시간 서비스가 종료됐습니다.")
        identifier = uuid.uuid4().hex
        session = _LiveSession(identifier, payload, self.feed_factory)
        with self.lock:
            if sum(s.status == "running" for s in self.sessions.values()) >= self.max_active:
                raise ds.DataError("실시간 분석은 동시에 2개까지 실행할 수 있습니다. 기존 연결을 종료하세요.")
            while len(self.sessions) >= self.max_retained:
                oldest = next((key for key, value in self.sessions.items() if value.status != "running"), None)
                if oldest is None:
                    raise ds.DataError("실시간 분석 기록이 가득 찼습니다. 기존 연결을 종료하세요.")
                self.sessions.pop(oldest)
            self.sessions[identifier] = session
        try:
            session.start()
        except Exception:
            with self.lock:
                self.sessions.pop(identifier, None)
            session.stop()
            raise
        return identifier

    def get(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
        if session is None:
            raise KeyError(identifier)
        return session.snapshot()

    def stop(self, identifier):
        with self.lock:
            session = self.sessions.get(identifier)
        if session is None:
            raise KeyError(identifier)
        session.stop()

    def _reap_loop(self):
        while not self.closed.wait(15):
            now = time.monotonic()
            with self.lock:
                stale = [key for key, session in self.sessions.items() if now - session.last_access > self.idle_timeout]
                sessions = [self.sessions.pop(key) for key in stale]
            for session in sessions:
                session.stop()

    def close(self):
        self.closed.set()
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.stop()
        if self.reaper and self.reaper is not threading.current_thread():
            self.reaper.join(timeout=1)
