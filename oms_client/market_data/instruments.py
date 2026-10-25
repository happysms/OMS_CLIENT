"""Bounded public instrument metadata needed to interpret contract quantities."""
from __future__ import annotations

import json
import math
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .validation import DataError, IDENTIFIER

MAX_RESPONSE_BYTES = 1024 * 1024
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
TICKER = re.compile(r"[A-Z0-9]{1,35}")
LOOKUP_ERROR = "OKX 계약 단위를 확인하지 못했습니다. 확인되지 않은 수량으로 평균 체결가를 계산하지 않습니다."


def _positive_number(value):
    if isinstance(value, bool):
        raise ValueError("Invalid contract value")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Invalid contract value")
    return number


def okx_contract_value(ticker: str, opener=urlopen) -> float:
    """Return base coins per contract for one verified linear USDT swap.

    OKX order-book sizes for swaps are contract counts. For supported linear
    swaps, base quantity is size * ctVal and quote notional is price * size *
    ctVal. Unfamiliar multipliers fail closed instead of assuming one.
    https://www.okx.com/docs-v5/en/#public-data-rest-api-get-instruments
    """
    if (not isinstance(ticker, str) or not IDENTIFIER.fullmatch(ticker)
            or not TICKER.fullmatch(ticker)):
        raise DataError("OKX 계약 단위를 확인할 종목을 다시 선택해 주세요.")
    instrument_id = f"{ticker}-USDT-SWAP"
    query = urlencode({"instType": "SWAP", "instId": instrument_id})
    request = Request(f"{OKX_INSTRUMENTS_URL}?{query}", headers={
        "Accept": "application/json", "User-Agent": "OMS-Client-Instruments/1.0",
    })
    try:
        with opener(request, timeout=8) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Oversized instrument response")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("code") != "0":
            raise ValueError("Instrument query failed")
        records = payload.get("data")
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("Ambiguous instrument response")
        instrument = records[0]
        if (not isinstance(instrument, dict) or instrument.get("instId") != instrument_id
                or instrument.get("instType") != "SWAP" or instrument.get("ctType") != "linear"
                or instrument.get("ctValCcy") != ticker or instrument.get("settleCcy") != "USDT"):
            raise ValueError("Unsupported contract units")
        contract_value = _positive_number(instrument.get("ctVal"))
        if _positive_number(instrument.get("ctMult")) != 1:
            raise ValueError("Unsupported contract multiplier")
        return contract_value
    except Exception as error:
        if isinstance(error, HTTPError):
            try:
                error.close()
            except Exception:
                pass
        # Network exceptions can contain proxy credentials, URLs and upstream
        # response messages. None of those belong in dashboard errors or logs.
        raise DataError(LOOKUP_ERROR) from None
