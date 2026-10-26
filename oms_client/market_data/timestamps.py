"""Signed differences between recorded and exchange clocks, in milliseconds."""

import math

MIN_TIMESTAMP_MS = 946_684_800_000  # 2000-01-01 UTC; do not guess units.
MAX_TIMESTAMP_MS = 4_102_444_800_000  # 2100-01-01 UTC, exclusive.


def unmeasurable_clock_reason(exchange, data_kind):
    if data_kind == "funding" and exchange in {"okx", "hyperliquid"}:
        return "serverTime이 수집기에서 생성된 시각이므로 거래소와의 시간차를 측정할 수 없습니다."
    if exchange == "ls" and data_kind in {"ob", "tick"}:
        return "LS serverTime은 날짜 없는 시분초 또는 수집기 생성 시각이므로 거래소와의 시간차를 측정할 수 없습니다."
    return None


def timestamp_delta_ms(desktop, server):
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not MIN_TIMESTAMP_MS <= value < MAX_TIMESTAMP_MS
        for value in (desktop, server)
    ):
        return None
    return desktop - server


def timestamp_delta_sql(columns, exchange, data_kind):
    """Fixed-schema expression; only genuine paired epoch-ms clocks qualify."""
    if not {"desktopTime", "serverTime"}.issubset(columns) or unmeasurable_clock_reason(
        exchange, data_kind
    ):
        return "NULL::DOUBLE"
    desktop, server = 'try_cast("desktopTime" AS DOUBLE)', 'try_cast("serverTime" AS DOUBLE)'
    valid = " AND ".join(
        f"{field} >= {MIN_TIMESTAMP_MS} AND {field} < {MAX_TIMESTAMP_MS}"
        for field in (desktop, server)
    )
    return f"CASE WHEN {valid} THEN {desktop} - {server} ELSE NULL END"
