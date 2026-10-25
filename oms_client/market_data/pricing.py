"""Shared price selection from native top-of-book/depth snapshots."""
from __future__ import annotations

import math

from .validation import DataError, integer

PRICE_BASES = frozenset({"mid", "bid", "ask", "buy_vwap", "sell_vwap"})
MAX_BOOK_DEPTH = 5


def normalize_price_options(leg: dict) -> dict:
    if not isinstance(leg, dict):
        raise DataError("가격 소스 옵션을 확인해 주세요.")
    basis = leg.get("price_basis", "mid")
    if not isinstance(basis, str) or basis not in PRICE_BASES:
        raise DataError("가격 기준은 중간가·매수호가·매도호가·매수 VWAP·매도 VWAP 중에서 선택해 주세요.")
    level = integer(leg.get("book_level", 1), "호가 단계", 1, MAX_BOOK_DEPTH)
    if basis == "mid" and level != 1:
        raise DataError("중간가는 매수·매도 1호가를 기준으로 계산합니다.")
    amount = None
    if basis in {"buy_vwap", "sell_vwap"}:
        raw = leg.get("liquidity_amount")
        if isinstance(raw, bool):
            raise DataError("VWAP 기준 금액은 0보다 큰 유한한 숫자로 입력해 주세요.")
        try:
            amount = float(raw)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("range")
        except (TypeError, ValueError, OverflowError):
            raise DataError("VWAP 기준 금액은 0보다 큰 유한한 숫자로 입력해 주세요.") from None
        level = 1
    return {"price_basis": basis, "book_level": level, "liquidity_amount": amount}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _levels(quote, side):
    name = "bids" if side == "bid" else "asks"
    raw = quote.get(name)
    if isinstance(raw, (list, tuple)) and raw:
        return list(raw[:MAX_BOOK_DEPTH])
    price, size = quote.get(side), quote.get(f"{side}_size")
    return [[price, size]] if price is not None else []


def select_quote_price(quote: dict, options: dict) -> dict:
    """Return no price for an incomplete/invalid fill; never return partial VWAP.

    Depth quantities must already be in base-asset units. Exchange contract
    multipliers and historical quote-notional conversion belong to adapters.
    """
    options = normalize_price_options(options)
    basis, level, target = (options[name] for name in ("price_basis", "book_level", "liquidity_amount"))
    result = {"value": None, "liquidity_available_notional": None,
              "liquidity_filled_quantity": None, "liquidity_levels_used": None, "reason": None}
    bids, asks = _levels(quote, "bid"), _levels(quote, "ask")
    try:
        bid, ask = _number(bids[0][0]), _number(asks[0][0])
    except (IndexError, TypeError):
        bid = ask = None
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return {**result, "reason": "invalid_best_quote"}
    if basis == "mid":
        return {**result, "value": bid / 2 + ask / 2}
    side = "ask" if basis in {"ask", "buy_vwap"} else "bid"
    raw = asks if side == "ask" else bids
    wanted = level if basis in {"bid", "ask"} else len(raw)
    previous, levels, depth_error = None, [], None
    for index in range(wanted):
        try:
            entry = raw[index]
            price = _number(entry[0])
            size = _number(entry[1]) if len(entry) > 1 else None
        except (IndexError, TypeError):
            depth_error = "missing_depth"
            break
        if basis in {"buy_vwap", "sell_vwap"} and ((price is None and size is None) or (price == 0 and size == 0)):
            depth_error = "missing_depth"
            break
        if price is None or price <= 0:
            depth_error = "invalid_depth"
            break
        if previous is not None and ((side == "ask" and price <= previous) or (side == "bid" and price >= previous)):
            depth_error = "unordered_depth"
            break
        if basis in {"buy_vwap", "sell_vwap"}:
            if size is None or size < 0 or not math.isfinite(price * size):
                depth_error = "invalid_quantity"
                break
            levels.append((price, size))
        previous = price
    if basis in {"bid", "ask"}:
        return {**result, "value": None if depth_error else previous, "reason": depth_error}
    if not levels:
        return {**result, "reason": depth_error or "missing_depth"}
    try:
        available = math.fsum(price * size for price, size in levels)
    except OverflowError:
        return {**result, "reason": "invalid_quantity"}
    if not math.isfinite(available):
        return {**result, "reason": "invalid_quantity"}
    remaining, fills, used = target, [], 0
    for price, size in levels:
        amount = min(remaining, price * size)
        if amount > 0:
            fills.append((amount, amount / price))
            remaining -= amount
            used += 1
        if remaining <= 0:
            break
    try:
        filled_quantity = math.fsum(size for _, size in fills)
    except OverflowError:
        return {**result, "reason": "invalid_quantity"}
    result.update(liquidity_available_notional=available, liquidity_filled_quantity=filled_quantity,
                  liquidity_levels_used=used)
    if available < target or not filled_quantity or not math.isfinite(filled_quantity):
        return {**result, "reason": depth_error or "insufficient_liquidity"}
    value = math.fsum(amount for amount, _ in fills) / filled_quantity
    return {**result, "value": value if math.isfinite(value) else None,
            "reason": None if math.isfinite(value) else "invalid_quantity"}
