"""Bounded live formula sessions using public quote adapters."""
from __future__ import annotations

import ast
from collections import deque
import threading
import time
import uuid

import pandas as pd

from ..market_data.quotes import ENDPOINTS, PublicQuoteFeed
from ..market_data.validation import (DataError, IDENTIFIER, array, integer,
                                      iso_timestamp, number)
from .formulas import evaluate, formula_ids, parse_formula

MAX_SAMPLES = 3600
QUOTE_TTL_MS = 10_000


class _LiveSession:
    def __init__(self, identifier, payload, feed_factory, clock=time.time):
        self.id = identifier
        self.formula = payload["formula"]
        ids = formula_ids(self.formula)
        candidates = payload.get("legs")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
            raise DataError("실시간 소스를 A~D 중 1~4개 선택하세요.")
        by_id = {}
        for leg in candidates:
            if not isinstance(leg, dict) or not isinstance(leg.get("id"), str) or leg.get("id") not in {"A", "B", "C", "D"} or leg["id"] in by_id:
                raise DataError("실시간 소스의 이름은 중복 없는 A~D여야 합니다.")
            by_id[leg["id"]] = leg
        self.legs = []
        for identifier in ids:
            leg = by_id.get(identifier)
            if leg and leg.get("data_type", "price") != "price":
                raise DataError("펀딩비율·1분봉 소스는 저장된 데이터 조회와 과거 분석에서 지원합니다.")
            if not leg or leg.get("exchange") not in ENDPOINTS:
                raise DataError("실시간은 빗썸·업비트·OKX를 지원합니다. 소스의 거래소를 확인하세요.")
            ticker = leg.get("ticker")
            if not isinstance(ticker, str) or not IDENTIFIER.fullmatch(ticker.strip().upper()) or len(ticker) > 35:
                raise DataError("실시간 종목 코드를 확인하세요.")
            exchange = leg["exchange"]
            self.legs.append({"id": identifier, "exchange": exchange, "ticker": ticker.strip().upper(),
                              "currency": "KRW" if exchange in {"bithumb", "upbit"} else "USDT"})
        self.span = integer(payload.get("ewm_span", 60), "EMA 구간 수", 1, 1_000_000)
        self.tree = parse_formula(self.formula, set(ids))
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"MA", "ZSCORE", "RET", "DIFF"}:
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value >= MAX_SAMPLES:
                    raise DataError("실시간 수식의 조회 구간 수는 3,600 미만이어야 합니다.")
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
        result = evaluate(self.tree, values, index)
        smooth = result.ewm(span=self.span).mean()
        valid = result.dropna()
        mean, std = number(valid.mean()), number(valid.std())
        latest = number(valid.iloc[-1]) if len(valid) else None
        first = number(valid.iloc[0]) if len(valid) else None
        legs = []
        for leg in self.legs:
            legs.append({**leg, **{column: [sample["quotes"][leg["id"]][column] for sample in samples] for column in ("mid", "bid", "ask")}})
        output = {"times": list(index), "result": array(result), "ewm": array(smooth), "legs": legs, "mean": mean,
                  "stats": {"count": len(valid), "latest": latest, "mean": mean, "std": std, "first": first,
                            "min": number(valid.min()), "max": number(valid.max()),
                            "median": number(valid.median()),
                            "latest_minus_mean": number(latest - mean) if latest is not None and mean is not None else None,
                            "latest_zscore": number((latest - mean) / std) if std not in (None, 0) and latest is not None else None},
                  "meta": {"source": "live", "interval": "live", "formula": self.formula, "ewm_span": self.span,
                           "aligned_points": len(index), "returned_points": len(index),
                           "warnings": (["함수 계산에 필요한 표본이 아직 충분하지 않거나 계산 결과가 정의되지 않은 구간은 빈 값입니다."] if len(valid) < len(index) else []),
                           "window_basis": "aligned_samples", "undefined_points": len(index) - len(valid),
                           "start": iso_timestamp(index[0]), "end": iso_timestamp(index[-1]), "sample_interval_ms": 1000}}
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
            raise DataError("실시간 수식과 소스를 입력하세요.")
        if self.closed.is_set():
            raise DataError("실시간 서비스가 종료됐습니다.")
        identifier = uuid.uuid4().hex
        session = _LiveSession(identifier, payload, self.feed_factory)
        with self.lock:
            if sum(s.status == "running" for s in self.sessions.values()) >= self.max_active:
                raise DataError("실시간 분석은 동시에 2개까지 실행할 수 있습니다. 기존 연결을 종료하세요.")
            while len(self.sessions) >= self.max_retained:
                oldest = next((key for key, value in self.sessions.items() if value.status != "running"), None)
                if oldest is None:
                    raise DataError("실시간 분석 기록이 가득 찼습니다. 기존 연결을 종료하세요.")
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
