"""Historical multi-source analysis and research catalog defaults."""
from __future__ import annotations

import ast

import pandas as pd

from ..market_data import funding, parquet
from ..market_data.parquet import bounds
from ..market_data.validation import (DataError, INTERVALS, KST, MAX_RANGE_DAYS,
                                      MAX_BUCKETS, array, integer, number, parse_date)
from .formulas import FUNCTIONS, FUNCTION_NAMES, evaluate, formula_ids, parse_formula
from .sampling import select_points


def catalog() -> dict:
    available = parquet.list_symbols()
    legs = [
        {"id": "A", "exchange": "bithumb", "ticker": "CT"},
        {"id": "B", "exchange": "okx", "ticker": "CT"},
        {"id": "C", "exchange": "bithumb", "ticker": "BTC"},
        {"id": "D", "exchange": "okx", "ticker": "BTC"},
    ]
    # Fall back to a shared symbol when the sample notebook's CT is unavailable.
    if not all(leg["ticker"] in available.get(leg["exchange"], []) for leg in legs):
        exchanges = list(available)
        if len(exchanges) >= 2:
            first, second = exchanges[:2]
            common = sorted(set(available[first]) & set(available[second]))
            if common:
                asset = next((x for x in common if x != "BTC"), common[0])
                reference = "BTC" if "BTC" in common else common[0]
                legs = [
                    {"id": "A", "exchange": first, "ticker": asset},
                    {"id": "B", "exchange": second, "ticker": asset},
                    {"id": "C", "exchange": first, "ticker": reference},
                    {"id": "D", "exchange": second, "ticker": reference},
                ]
            else:
                legs = []
        elif exchanges:
            legs = [{"id": "A", "exchange": exchanges[0], "ticker": available[exchanges[0]][0]}]
        else:
            legs = []
    now = pd.Timestamp.now(tz=KST).floor("min")
    start, end = now - pd.Timedelta(hours=6), now
    try:
        limits = [bounds(leg["exchange"], leg["ticker"]) for leg in legs]
        if limits:
            # End is exclusive: include the final millisecond in the file.
            end = (min(pd.Timestamp(item["end"]) for item in limits) + pd.Timedelta(milliseconds=1)).ceil("min")
            start = max(end - pd.Timedelta(hours=6), max(pd.Timestamp(item["start"]) for item in limits))
    except DataError:
        pass
    return {
        "exchanges": available,
        "funding_exchanges": funding.list_symbols(),
        "source_types": [
            {"id": "price", "label": "가격·호가", "formula_value": "mid"},
            {"id": "funding", "label": "펀딩비율", "formula_value": "values", "unit": "%",
             "raw_unit": "ratio", "kind": "observed_rate", "live_supported": False},
        ],
        "defaults": {
            "legs": legs,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "formula": "((A / B) / (C / D) - 1) * 100" if len(legs) == 4 else "A",
            "interval": "1min",
            "unit": "%" if len(legs) == 4 else ("KRW" if legs and legs[0]["exchange"] in {"bithumb", "upbit", "ls"} else "USDT"),
        },
        "data_root": str(parquet.data_root()),
        "intervals": INTERVALS.copy(),
        "functions": [dict(item) for item in FUNCTIONS],
        "limits": {"max_range_days": MAX_RANGE_DAYS, "max_buckets": MAX_BUCKETS, "max_chart_points": 6000},
    }



def analyze(payload: dict) -> dict:
    """Analyze valid common time buckets, then sample only the returned chart.

    Time-series function periods count chronological *common samples*, not wall
    time. Missing buckets are never filled. MA and ZSCORE require a full window;
    DIFF and RET require an earlier sample, and EMA uses pandas adjust=True.
    """
    if not isinstance(payload, dict):
        raise DataError("조회 조건을 JSON 객체로 보내 주세요.")
    requested = payload.get("legs")
    if not isinstance(requested, list) or not 1 <= len(requested) <= 4:
        raise DataError("분석 시리즈를 1~4개 선택해 주세요.")
    requested_by_id = {}
    for leg in requested:
        if (not isinstance(leg, dict) or not isinstance(leg.get("id"), str)
                or leg["id"] not in {"A", "B", "C", "D"} or leg["id"] in requested_by_id):
            raise DataError("분석 시리즈 이름은 중복 없는 A~D로 지정해 주세요.")
        requested_by_id[leg["id"]] = leg
    default_formula = "((A / B) / (C / D) - 1) * 100" if len(requested_by_id) == 4 else "A"
    formula = payload.get("formula", default_formula)
    tree = parse_formula(formula, set(requested_by_id))
    referenced = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and node.id in requested_by_id}
    available = parquet.list_symbols()
    funding_available = None
    legs = []
    for identifier, leg in requested_by_id.items():
        if identifier not in referenced:
            continue
        data_type = leg.get("data_type", "price")
        if not isinstance(data_type, str) or data_type not in {"price", "funding"}:
            raise DataError("분석 데이터 종류는 가격·호가 또는 펀딩비율로 선택해 주세요.")
        if data_type == "funding":
            if funding_available is None:
                funding_available = funding.list_symbols()
            exchange, ticker = funding.validate_symbol(leg.get("exchange"), leg.get("ticker"), funding_available)
        else:
            exchange, ticker = parquet.validate_symbol(leg.get("exchange"), leg.get("ticker"), available)
        legs.append({"id": leg["id"], "exchange": exchange, "ticker": ticker, "data_type": data_type})
    start, end = parse_date(payload.get("start"), "시작 시간"), parse_date(payload.get("end"), "종료 시간")
    if start >= end:
        raise DataError("종료 시간은 시작 시간보다 뒤여야 합니다.")
    if end - start > pd.Timedelta(days=MAX_RANGE_DAYS):
        raise DataError(f"한 번에 조회할 수 있는 기간은 최대 {MAX_RANGE_DAYS}일입니다.")
    interval = payload.get("interval", "1min")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("조회 간격은 1s, 10s, 1min, 5min, 1h 중에서 선택해 주세요.")
    price_mode = payload.get("price_mode", "mean")
    if not isinstance(price_mode, str) or price_mode not in {"mean", "last"}:
        raise DataError("조회 방식은 마지막 관측값 또는 평균으로 선택해 주세요.")
    step = INTERVALS[interval]
    buckets = (int(end.value // 1_000_000) - 1) // step - int(start.value // 1_000_000) // step + 1
    if buckets > MAX_BUCKETS:
        raise DataError(f"예상 시간 구간이 {buckets:,}개입니다. 소스당 최대 {MAX_BUCKETS:,}개까지 조회할 수 있으니 기간을 줄이거나 조회 간격을 늘려 주세요.")
    max_points = integer(payload.get("max_points", 4000), "차트 최대 점 개수", 2, 6000)
    span = integer(payload.get("ewm_span", 60), "EMA span", 1, 1_000_000)
    include_full = payload.get("include_full_data", False)
    if type(include_full) is not bool:
        raise DataError("include_full_data는 true 또는 false로 지정해 주세요.")

    loaded, rows_read = {}, 0
    for leg in legs:
        key = (leg["data_type"], leg["exchange"], leg["ticker"])
        if key not in loaded:
            reader = funding.read_buckets if leg["data_type"] == "funding" else parquet.read_buckets
            if price_mode == "last":
                frame, count = reader(*key[1:], start, end, INTERVALS[interval], price_mode="last")
            else:
                frame, count = reader(*key[1:], start, end, INTERVALS[interval])
            loaded[key] = frame
            rows_read += count
    index = None
    for frame in loaded.values():
        index = frame.index if index is None else index.intersection(frame.index)
    index = index.sort_values()
    if index.empty:
        raise DataError("모든 분석 시리즈가 함께 존재하는 시간 구간이 없습니다. 기간·종목·조회 간격을 바꿔 주세요. 누락 시각을 이전 값으로 채우지 않습니다.")

    values = {leg["id"]: loaded[(leg["data_type"], leg["exchange"], leg["ticker"])].loc[
        index, "value" if leg["data_type"] == "funding" else "mid"] for leg in legs}
    result = evaluate(tree, values, index)
    valid = result.dropna()
    smooth = result.ewm(span=span).mean()
    warnings = []
    has_funding = any(leg["data_type"] == "funding" for leg in legs)
    if has_funding:
        warnings.append("펀딩 시리즈의 수식 값은 관측 펀딩비율(%)입니다. 계좌의 실제 지급·수취 금액이 아니며, 거래소·종목별 정산 주기를 환산하거나 합산하지 않습니다.")
        if price_mode == "mean":
            warnings.append("펀딩비율은 구간 평균이며 관측 시각·서버 시각·다음 정산 잔여 초는 해당 구간의 마지막 원본 관측값 기준입니다.")
    missing = int(result.isna().sum())
    if missing:
        warnings.append(f"함수의 초기 구간, 0으로 나누기 또는 유효하지 않은 계산 결과 {missing:,}개는 빈 값으로 표시합니다.")
    functions_used = sorted({node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)})
    if functions_used:
        warnings.append(f"함수의 기간은 공통 {'관측값' if has_funding else '호가'}가 존재하는 표본 개수입니다. 누락 시각을 채우지 않으며 실제 경과 시간과 다를 수 있습니다.")
    available_points = len(set().union(*(set(frame.index) for frame in loaded.values())))
    if available_points > len(index):
        warnings.append(f"일부 {'관측값' if has_funding else '가격'}이 없는 시간 구간 {available_points - len(index):,}개는 제외했습니다. 이전 값으로 채우지 않습니다.")
    selection = select_points(result, max_points)
    if len(index) > max_points:
        warnings.append(f"차트는 {len(index):,}개 중 {len(selection):,}개를 표시합니다. 통계와 EMA는 표시 전 전체 구간으로 계산했습니다.")
    first, latest = (number(valid.iloc[0]), number(valid.iloc[-1])) if len(valid) else (None, None)
    mean = number(valid.mean())
    change = number((latest / first - 1) * 100) if first not in (None, 0) and latest is not None else None
    std = number(valid.std())
    full_legs, output_legs = [], []
    for leg in legs:
        frame = loaded[(leg["data_type"], leg["exchange"], leg["ticker"])].loc[index]
        if leg["data_type"] == "funding":
            def funding_leg(source):
                return {**leg, "unit": "%", "values": array(source["value"]),
                        "funding_rate": array(source["funding_rate"]),
                        "source_times": [int(value) for value in source["source_time"]],
                        "server_times": [int(value) if pd.notna(value) else None for value in source["server_time"]],
                        "next_funding_in_seconds": array(source["next_funding_in_seconds"])}
            output_legs.append(funding_leg(frame.iloc[selection]))
            if include_full:
                full_legs.append(funding_leg(frame))
            continue
        info = {**leg, "currency": "KRW" if leg["exchange"] in {"bithumb", "upbit", "ls"} else "USDT"}
        output_leg = {**info, **{column: array(frame.iloc[selection][column]) for column in ["mid", "bid", "ask"]}}
        if price_mode == "last":
            output_leg["quote_times"] = [int(value) for value in frame.iloc[selection]["quote_time"]]
        output_legs.append(output_leg)
        if include_full:
            full_leg = {**info, **{column: array(frame[column]) for column in ["mid", "bid", "ask"]}}
            if price_mode == "last":
                full_leg["quote_times"] = [int(value) for value in frame["quote_time"]]
            full_legs.append(full_leg)
    correlations = pd.DataFrame(values, index=index).corr()
    output = {
        "times": [int(value) for value in index[selection]],
        "legs": output_legs,
        "result": array(result.iloc[selection]),
        "ewm": array(smooth.iloc[selection]),
        "mean": mean,
        "stats": {"count": int(len(valid)), "latest": latest, "mean": mean, "std": std,
                  "min": number(valid.min()), "max": number(valid.max()), "first": first, "change_pct": change,
                  "median": number(valid.median()), "q05": number(valid.quantile(.05)), "q95": number(valid.quantile(.95)),
                  "positive_rate": number((valid > 0).mean()) if len(valid) else None,
                  "latest_minus_mean": number(latest - mean) if latest is not None and mean is not None else None,
                  "latest_zscore": number((latest - mean) / std) if latest is not None and mean is not None and std not in (None, 0) else None,
                  "latest_time": int(valid.index[-1]) if len(valid) else None},
        "correlations": {left: {right: number(correlations.loc[left, right]) for right in correlations.columns}
                         for left in correlations.index},
        "meta": {"start": start.tz_convert(KST).isoformat(), "end": end.tz_convert(KST).isoformat(),
                 "interval": interval, "price_mode": price_mode, "formula": formula, "source": "parquet", "rows_read": rows_read,
                 "aligned_points": len(index), "returned_points": len(selection), "warnings": warnings, "ewm_span": span,
                 "functions": functions_used, "window_basis": "aligned_samples", "undefined_points": missing},
    }
    if include_full:
        output["full"] = {"times": [int(value) for value in index], "legs": full_legs,
                          "result": array(result), "ewm": array(smooth)}
    return output
