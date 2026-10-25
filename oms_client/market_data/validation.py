"""Shared quote types, request bounds and JSON-safe value conversion."""
from __future__ import annotations

import math
import re

import pandas as pd

KST = "Asia/Seoul"
INTERVALS = {"1s": 1_000, "10s": 10_000, "1min": 60_000, "5min": 300_000, "1h": 3_600_000}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_RANGE_DAYS = 31
MAX_BUCKETS = 250_000


class DataError(ValueError):
    """A user-correctable request or source-data error."""



class QueryTimeout(DataError):
    """A bounded analytical query exceeded its configured time budget."""



def iso_timestamp(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(KST).isoformat()



def parse_date(value, label: str) -> pd.Timestamp:
    if not isinstance(value, str) or not value.strip():
        raise DataError(f"{label}을 입력해 주세요.")
    try:
        parsed = pd.Timestamp(value)
        if pd.isna(parsed):
            raise ValueError("NaT")
        parsed = (parsed.tz_localize(KST) if parsed.tzinfo is None else parsed).tz_convert("UTC")
        _ = parsed.value  # Validate the nanosecond range used by query conversion.
        return parsed
    except (ValueError, TypeError, OverflowError) as exc:
        raise DataError(f"{label} 형식이 올바르지 않습니다. 한국 시간 기준 날짜·시간을 입력해 주세요.") from exc



def integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise DataError(f"{name}은 {low}~{high} 사이 정수로 입력해 주세요.")
    try:
        result = int(value)
        if float(value) != result or not low <= result <= high:
            raise ValueError("range")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataError(f"{name}은 {low}~{high} 사이 정수로 입력해 주세요.") from exc



def number(value):
    return float(value) if value is not None and pd.notna(value) and math.isfinite(float(value)) else None



def array(series: pd.Series) -> list:
    return [number(value) for value in series.to_numpy()]
