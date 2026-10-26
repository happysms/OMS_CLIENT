"""Cash-only paper ledger. Every fill uses a fresh server-side public quote.

This module has no account credentials, exchange order endpoints or live broker.
"""
from __future__ import annotations

import math
import json
import secrets
import threading
import time

from ..market_data import parquet
from ..market_data.validation import IDENTIFIER
from ..market_data.quotes import PublicQuoteFeed
from .reporting import evaluate_positions, valid_theories


EXCHANGES = {"bithumb", "upbit"}
MAX_QUOTE_AGE_MS = 10_000
MAX_POSITIONS = 12


def _finite(value, label, low=0, high=1e18, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{label} 값을 확인하세요.")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label} 값을 확인하세요.") from exc
    if not math.isfinite(number) or not low <= number <= high or (positive and number <= 0):
        raise ValueError(f"{label}은 유효한 양수 범위로 입력하세요.")
    return number


def _symbol(exchange, ticker):
    if not isinstance(exchange, str) or exchange not in EXCHANGES:
        raise ValueError("현재 모의투자는 빗썸·업비트의 KRW 현금 매매를 지원합니다.")
    if not isinstance(ticker, str) or not IDENTIFIER.fullmatch(ticker.strip()) or len(ticker) > 35:
        raise ValueError("종목 코드를 확인하세요.")
    return exchange, ticker.strip().upper()


def _fresh_quote(quote, exchange, ticker, now_ms):
    if not isinstance(quote, dict):
        raise RuntimeError("현재 호가를 받지 못했습니다. 다시 시도하세요.")
    bid = _finite(quote.get("bid"), "매수 호가", positive=True)
    ask = _finite(quote.get("ask"), "매도 호가", positive=True)
    stamp = _finite(quote.get("ts"), "호가 시각", positive=True)
    received = _finite(quote.get("receivedAt"), "수신 시각", positive=True)
    if ask < bid or stamp > now_ms + 5000 or received > now_ms + 5000:
        raise RuntimeError("호가의 가격·시각이 올바르지 않아 주문하지 않았습니다.")
    if now_ms - stamp > MAX_QUOTE_AGE_MS or now_ms - received > MAX_QUOTE_AGE_MS:
        raise RuntimeError("호가가 10초 이상 지연되어 주문하지 않았습니다. 다시 시도하세요.")
    if quote.get("exchange", exchange) != exchange or quote.get("ticker", ticker) != ticker:
        raise RuntimeError("선택한 종목과 시세가 일치하지 않습니다.")
    return {"exchange": exchange, "ticker": ticker, "bid": bid, "ask": ask,
            "mid": bid / 2 + ask / 2, "ts": int(stamp), "receivedAt": int(received)}


class PublicQuoteProvider:
    """Bounded per-symbol subscriptions isolate rejected or delisted symbols."""
    def __init__(self, feed_factory=PublicQuoteFeed, max_symbols=32):
        self.feed_factory, self.max_symbols = feed_factory, max_symbols
        self.condition = threading.Condition()
        self.watch_lock = threading.Lock()
        self.watch = set()
        self.quotes, self.errors = {}, {}
        self.feeds = {}
        self.generation = 0
        self.closed = False

    def _ensure(self, pairs):
        with self.watch_lock:
            if self.closed:
                raise RuntimeError("모의투자 시세 서비스가 종료됐습니다.")
            wanted = self.watch | set(pairs)
            if len(wanted) > self.max_symbols:
                raise ValueError("현재 서버의 모의 시세 구독 한도에 도달했습니다.")
            generation = self.generation
            for pair in sorted(wanted - self.watch):
                exchange, ticker = pair
                legs = [{"id": f"{exchange}:{ticker}", "exchange": exchange, "ticker": ticker}]

                def on_quote(quote, key=pair):
                    with self.condition:
                        if self.closed or generation != self.generation or (quote["exchange"], quote["ticker"]) != key:
                            return
                        previous = self.quotes.get(key)
                        if previous and quote["ts"] < previous["ts"]:
                            return
                        self.quotes[key] = quote
                        self.errors.pop(key, None)
                        self.condition.notify_all()

                def on_status(status, key=pair):
                    with self.condition:
                        if self.closed or generation != self.generation:
                            return
                        if status["state"] in {"error", "reconnecting", "disconnected"}:
                            self.quotes.pop(key, None)
                        if status["state"] == "error":
                            self.errors[key] = status.get("message") or "종목 시세를 구독하지 못했습니다."
                        self.condition.notify_all()

                feed = self.feed_factory(legs, on_quote, on_status)
                self.feeds[pair] = feed
                self.watch.add(pair)
                feed.start()

    def get_many(self, pairs, timeout=3):
        pairs = list(dict.fromkeys(pairs))
        if not pairs:
            return {}
        self._ensure(pairs)
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                now = int(time.time() * 1000)
                fresh = {}
                for pair in pairs:
                    try:
                        fresh[pair] = _fresh_quote(self.quotes.get(pair), *pair, now)
                    except (ValueError, RuntimeError):
                        pass
                if len(fresh) == len(pairs) or self.closed or time.monotonic() >= deadline:
                    return fresh
                if all(pair in self.errors for pair in pairs if pair not in fresh):
                    return fresh
                self.condition.wait(min(.25, max(0, deadline - time.monotonic())))

    def get(self, exchange, ticker):
        quote = self.get_many([(exchange, ticker)], timeout=8).get((exchange, ticker))
        if quote is None:
            raise RuntimeError("신선한 실시간 호가를 받지 못해 주문하지 않았습니다. 종목과 연결 상태를 확인하세요.")
        return quote

    def close(self):
        with self.watch_lock:
            self.closed = True
            self.generation += 1
            for feed in self.feeds.values():
                feed.stop()
            with self.condition:
                self.quotes.clear()
                self.condition.notify_all()


class PaperTradingService:
    def __init__(self, store, quote_provider=None, clock=time.time, symbol_validator=None, theory_provider=None):
        self.store = store
        self.quote_provider = quote_provider if quote_provider is not None else PublicQuoteProvider()
        self.clock = clock
        self.symbol_validator = symbol_validator if symbol_validator is not None else parquet.validate_symbol
        self.lock = threading.RLock()
        self.theory_provider = theory_provider
        if theory_provider is None and quote_provider is None:
            from .theory import PremiumTheoryProvider
            # Reference subscriptions must not consume the execution feed's limit.
            self.theory_provider = PremiumTheoryProvider(store.directory, PublicQuoteProvider(), _fresh_quote, clock)
        with store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK(id=1), initial_cash REAL NOT NULL,
                    cash REAL NOT NULL, realized_pnl REAL NOT NULL,
                    quote_currency TEXT NOT NULL, fee_bps REAL NOT NULL, slippage_bps REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    exchange TEXT NOT NULL, ticker TEXT NOT NULL, quantity REAL NOT NULL,
                    avg_price REAL NOT NULL, PRIMARY KEY(exchange,ticker)
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL, exchange TEXT NOT NULL,
                    ticker TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL,
                    quantity REAL NOT NULL, fee REAL NOT NULL, cash REAL NOT NULL,
                    realized_pnl REAL NOT NULL, quote_time INTEGER NOT NULL,
                    status TEXT NOT NULL, client_order_id TEXT UNIQUE
                );
                INSERT OR IGNORE INTO paper_account VALUES (1,1000000,1000000,0,'KRW',5,2);
                CREATE TABLE IF NOT EXISTS paper_fill_reports (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    fill_id TEXT NOT NULL UNIQUE, report TEXT NOT NULL
                );
            """)
            # Old fills have no historical marks. Preserve their known execution
            # fields explicitly, without inventing past valuations from today's book.
            for row in connection.execute('SELECT o.* FROM paper_orders o LEFT JOIN paper_fill_reports r ON r.fill_id=o.id WHERE r.fill_id IS NULL ORDER BY o.timestamp,o.rowid').fetchall():
                report = dict(schema_version=1, valuation_status='legacy_unavailable', fill=dict(row), account=None, positions=[])
                connection.execute('INSERT OR IGNORE INTO paper_fill_reports(fill_id,report) VALUES (?,?)', (row['id'], json.dumps(report)))

    def _quote(self, exchange, ticker):
        provider = self.quote_provider
        quote = provider.get(exchange, ticker) if hasattr(provider, "get") else provider(exchange, ticker)
        return _fresh_quote(quote, exchange, ticker, int(self.clock() * 1000))

    def _valuation_inputs(self, pairs, overrides=None):
        marks = dict(overrides or {})
        missing = [pair for pair in pairs if pair not in marks]
        try:
            if hasattr(self.quote_provider, 'get_many'):
                marks.update(self.quote_provider.get_many(missing, timeout=3))
            else:
                for pair in missing:
                    try:
                        marks[pair] = self._quote(*pair)
                    except (RuntimeError, ValueError):
                        pass
        except (RuntimeError, ValueError):
            pass
        theories = {}
        if self.theory_provider:
            try:
                theories = self.theory_provider.get_many(pairs)
            except (RuntimeError, ValueError) as exc:
                theories = {pair: {'price': None, 'reason': str(exc)} for pair in pairs}
        return marks, theories

    @staticmethod
    def _marks_at(marks, now):
        valid = {}
        for pair, value in marks.items():
            try:
                quote = _fresh_quote(value, *pair, now)
                if quote['receivedAt'] <= now:
                    valid[pair] = quote
            except (ValueError, RuntimeError):
                pass
        return valid

    def fills(self, *, before=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500 or (before is not None and (type(before) is not int or before < 1)):
            raise ValueError('체결 조회 범위를 확인하세요.')
        with self.store.connection() as connection:
            rows = connection.execute('SELECT * FROM paper_fill_reports WHERE (? IS NULL OR sequence<?) ORDER BY sequence DESC LIMIT ?',
                                      (before, before, limit + 1)).fetchall()
            total = connection.execute('SELECT count(*) FROM paper_fill_reports').fetchone()[0]
        items = [dict(sequence=row['sequence'], **json.loads(row['report'])) for row in rows[:limit]]
        return dict(items=items, total=total, next_before=items[-1]['sequence'] if len(rows) > limit else None)

    def _fill_report(self, fill_id):
        with self.store.connection() as connection:
            row = connection.execute('SELECT sequence,report FROM paper_fill_reports WHERE fill_id=?', (fill_id,)).fetchone()
        return dict(sequence=row['sequence'], **json.loads(row['report'])) if row else None

    def snapshot(self, quotes_override=None):
        with self.lock, self.store.connection() as connection:
            account = dict(connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            account.pop("id", None)
            positions = [dict(row) for row in connection.execute("SELECT * FROM paper_positions ORDER BY exchange,ticker")]
            orders = [dict(row) for row in connection.execute("SELECT * FROM paper_orders ORDER BY timestamp DESC,rowid DESC LIMIT 100")]
        pairs = [(position["exchange"], position["ticker"]) for position in positions]
        marks, theories = self._valuation_inputs(pairs, quotes_override)
        now = int(self.clock() * 1000)
        marks = self._marks_at(marks, now)
        theories = valid_theories(theories, now, MAX_QUOTE_AGE_MS)
        account, positions = evaluate_positions(account, positions, marks, theories)
        valued = sum(position['mark_price'] is not None for position in positions)
        complete = valued == len(positions)
        equity = account['equity']
        fills = self.fills()
        return {"account": account, "positions": positions, "orders": orders, "equity": equity, "fills": fills["items"], "fill_count": fills["total"], "next_fill_before": fills["next_before"],
                "quote_status": {"state": "fresh" if complete else "partial" if valued else "unavailable",
                                 "message": "보유 종목이 없습니다." if not positions else "실시간 호가로 평가했습니다." if complete else "일부 실시간 호가가 없어 전체 평가액을 표시하지 않습니다.",
                                 "updated_at": now},
                "assumptions": ["KRW 현금 기반 매수·매도만 지원합니다.", "매수는 최우선 매도 호가, 매도는 최우선 매수 호가에 설정한 슬리피지·수수료를 적용합니다.",
                                "평균 매입가는 매수 수수료를 포함하며, 실제 거래소 주문·잔량 소진·시장 충격은 재현하지 않습니다."]}

    def place_order(self, payload):
        allowed = {"exchange", "ticker", "side", "quantity", "client_order_id"}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValueError("주문에는 거래소·종목·매수/매도·수량만 지정하세요. 가격은 서버가 결정합니다.")
        exchange, ticker = _symbol(payload.get("exchange"), payload.get("ticker"))
        exchange, ticker = self.symbol_validator(exchange, ticker)
        side = payload.get("side")
        if not isinstance(side, str) or side not in {"buy", "sell"}:
            raise ValueError("주문 방향은 buy 또는 sell이어야 합니다.")
        quantity = _finite(payload.get("quantity"), "주문 수량", high=1e12, positive=True)
        client_id = payload.get("client_order_id")
        if client_id is not None and (not isinstance(client_id, str) or not 1 <= len(client_id) <= 80):
            raise ValueError("주문 식별자는 1~80자 문자열이어야 합니다.")
        with self.lock, self.store.connection() as connection:
            existing = connection.execute("SELECT * FROM paper_orders WHERE client_order_id=?", (client_id,)).fetchone() if client_id else None
        if existing:
            if (existing["exchange"], existing["ticker"], existing["side"], existing["quantity"]) != (exchange, ticker, side, quantity):
                raise ValueError("같은 주문 식별자를 다른 주문에 사용할 수 없습니다.")
            return {**self.snapshot(), "order": dict(existing), "fill_report": self._fill_report(existing["id"]), "duplicate": True}
        with self.lock, self.store.connection() as connection:
            pairs = [(row['exchange'], row['ticker']) for row in connection.execute('SELECT exchange,ticker FROM paper_positions')]
        pairs = list(dict.fromkeys([*pairs, (exchange, ticker)]))
        marks, theories = self._valuation_inputs(pairs)
        quote = self._quote(exchange, ticker)
        marks[(exchange, ticker)] = quote
        now = int(self.clock() * 1000)
        with self.lock, self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = int(self.clock() * 1000)
            quote = _fresh_quote(quote, exchange, ticker, now)
            # The idempotency check belongs inside the write transaction too.
            existing = connection.execute("SELECT * FROM paper_orders WHERE client_order_id=?", (client_id,)).fetchone() if client_id else None
            if existing:
                if (existing["exchange"], existing["ticker"], existing["side"], existing["quantity"]) != (exchange, ticker, side, quantity):
                    raise ValueError("같은 주문 식별자를 다른 주문에 사용할 수 없습니다.")
                order = dict(existing)
            else:
                account = dict(connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
                position = connection.execute("SELECT * FROM paper_positions WHERE exchange=? AND ticker=?", (exchange, ticker)).fetchone()
                old_quantity = position["quantity"] if position else 0.0
                average = position["avg_price"] if position else 0.0
                slip = account["slippage_bps"] / 10000
                price = quote["ask"] * (1 + slip) if side == "buy" else quote["bid"] * (1 - slip)
                notional = quantity * price
                fee = notional * account["fee_bps"] / 10000
                if not all(math.isfinite(value) for value in (notional, fee)):
                    raise ValueError("주문 금액이 너무 큽니다.")
                realized = 0.0
                if side == "buy":
                    if account["cash"] < notional + fee:
                        raise RuntimeError("모의 계좌의 현금이 부족합니다.")
                    if not position and connection.execute("SELECT count(*) FROM paper_positions").fetchone()[0] >= MAX_POSITIONS:
                        raise RuntimeError("모의 계좌는 최대 12개 종목을 보유할 수 있습니다.")
                    new_quantity = old_quantity + quantity
                    new_average = (old_quantity * average + notional + fee) / new_quantity
                    cash = max(0.0, account["cash"] - notional - fee)
                    connection.execute("INSERT INTO paper_positions VALUES (?,?,?,?) ON CONFLICT(exchange,ticker) DO UPDATE SET quantity=excluded.quantity,avg_price=excluded.avg_price",
                                       (exchange, ticker, new_quantity, new_average))
                else:
                    if not position or quantity > old_quantity:
                        raise RuntimeError("보유 수량보다 많이 매도할 수 없습니다.")
                    notional = quantity * price
                    fee = notional * account["fee_bps"] / 10000
                    realized = notional - fee - quantity * average
                    cash = account["cash"] + notional - fee
                    remaining = old_quantity - quantity
                    if remaining <= 0:
                        connection.execute("DELETE FROM paper_positions WHERE exchange=? AND ticker=?", (exchange, ticker))
                    else:
                        connection.execute("UPDATE paper_positions SET quantity=? WHERE exchange=? AND ticker=?", (remaining, exchange, ticker))
                connection.execute("UPDATE paper_account SET cash=?,realized_pnl=realized_pnl+? WHERE id=1", (cash, realized))
                order = {"id": secrets.token_hex(12), "timestamp": now, "exchange": exchange, "ticker": ticker,
                         "side": side, "price": price, "quantity": quantity, "fee": fee, "cash": cash,
                         "realized_pnl": realized, "quote_time": quote["ts"], "status": "filled", "client_order_id": client_id}
                connection.execute("INSERT INTO paper_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(order.values()))
                after = dict(connection.execute('SELECT * FROM paper_account WHERE id=1').fetchone())
                after.pop('id', None)
                after['fees_paid'] = connection.execute('SELECT coalesce(sum(fee),0) FROM paper_orders').fetchone()[0]
                positions = [dict(row) for row in connection.execute('SELECT * FROM paper_positions ORDER BY exchange,ticker')]
                valuation, positions = evaluate_positions(after, positions, self._marks_at(marks, now),
                                                          valid_theories(theories, now, MAX_QUOTE_AGE_MS))
                report = dict(schema_version=1, valuation_status='recorded', as_of_ms=now,
                              fill=order, account=valuation, positions=positions,
                              before=dict(cash=account['cash'], quantity=old_quantity, avg_price=average),
                              execution_quote=quote,
                              execution_theory=valid_theories(theories, now, MAX_QUOTE_AGE_MS).get((exchange,ticker)),
                              mark_basis='best_bid', cost_basis='weighted_average_including_buy_fees')
                connection.execute('INSERT INTO paper_fill_reports(fill_id,report) VALUES (?,?)',
                                   (order['id'], json.dumps(report, ensure_ascii=False, allow_nan=False)))
        return {**self.snapshot({(exchange, ticker): quote}), "order": order, "fill_report": self._fill_report(order["id"])}

    def reset(self, payload=None):
        payload = {} if payload is None else payload
        if not isinstance(payload, dict) or set(payload) - {"initial_cash", "quote_currency", "fee_bps", "slippage_bps"}:
            raise ValueError("모의 계좌 초기화 조건을 확인하세요.")
        if payload.get("quote_currency", "KRW") != "KRW":
            raise ValueError("현재 모의 계좌의 기준 통화는 KRW입니다.")
        cash = _finite(payload.get("initial_cash", 1_000_000), "초기 현금", high=1e15, positive=True)
        fee = _finite(payload.get("fee_bps", 5), "수수료", high=1000)
        slip = _finite(payload.get("slippage_bps", 2), "슬리피지", high=1000)
        with self.lock, self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM paper_fill_reports")
            connection.execute("DELETE FROM paper_orders")
            connection.execute("DELETE FROM paper_positions")
            connection.execute("UPDATE paper_account SET initial_cash=?,cash=?,realized_pnl=0,quote_currency='KRW',fee_bps=?,slippage_bps=? WHERE id=1", (cash, cash, fee, slip))
        return self.snapshot()

    def close(self):
        if hasattr(self.quote_provider, "close"):
            self.quote_provider.close()
        if hasattr(self.theory_provider, 'close'):
            self.theory_provider.close()
