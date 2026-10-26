"""Historical multi-source analysis and research catalog defaults."""
from __future__ import annotations

import ast

import pandas as pd

from ..market_data import candles, funding, parquet
from ..market_data.parquet import bounds
from ..market_data.pricing import normalize_price_options
from ..market_data.validation import (DataError, INTERVALS, KST, MAX_RANGE_DAYS,
                                      MAX_BUCKETS, array, integer, number, parse_date)
from .formulas import FUNCTIONS, FUNCTION_NAMES, evaluate, formula_ids, parse_formula
from .sampling import select_points
from .sources import MAX_SOURCES, MAX_TOTAL_SOURCE_BUCKETS, SOURCE_IDS, source_map


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
        "candle_exchanges": candles.list_symbols(),
        "source_types": [
            {"id": "price", "label": "가격·호가", "formula_value": "values", "default_price_basis": "mid"},
            {"id": "funding", "label": "펀딩비율", "formula_value": "values", "unit": "%",
             "raw_unit": "ratio", "kind": "observed_rate", "live_supported": False},
            {"id": "candle", "label": "1분봉 종가", "formula_value": "values", "unit": "KRW",
             "kind": "completed_candle", "live_supported": False},
        ],
        "source_ids": list(SOURCE_IDS),
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
        "price_bases": ['mid', 'bid', 'ask', 'buy_vwap', 'sell_vwap'],
        "max_book_level": 5,
        "functions": [dict(item) for item in FUNCTIONS],
        "limits": {"max_range_days": MAX_RANGE_DAYS, "max_buckets": MAX_BUCKETS, "max_chart_points": 6000,
                   "max_sources": MAX_SOURCES, "max_total_source_buckets": MAX_TOTAL_SOURCE_BUCKETS},
    }



def analyze(payload: dict) -> dict:
    """Analyze valid common time buckets, then sample only the returned chart.

    Time-series function periods count chronological *common samples*, not wall
    time. Missing buckets are never filled. MA and ZSCORE require a full window;
    DIFF and RET require an earlier sample, and EMA uses pandas adjust=True.
    """
    if not isinstance(payload, dict):
        raise DataError("조회 조건을 JSON 객체로 보내 주세요.")
    requested_by_id = source_map(payload.get("legs"))
    default_formula = "((A / B) / (C / D) - 1) * 100" if len(requested_by_id) == 4 else "A"
    formula = payload.get("formula", default_formula)
    tree = parse_formula(formula, set(requested_by_id))
    referenced = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and node.id in requested_by_id}
    available = parquet.list_symbols()
    funding_available = candle_available = None
    legs = []
    for identifier, leg in requested_by_id.items():
        if identifier not in referenced:
            continue
        data_type = leg.get("data_type", "price")
        if not isinstance(data_type, str) or data_type not in {"price", "funding", "candle"}:
            raise DataError("분석 데이터 종류는 가격·호가, 펀딩비율 또는 1분봉 종가로 선택해 주세요.")
        if data_type != 'price' and {'price_basis', 'book_level', 'liquidity_amount', 'liquidity_currency'} & set(leg):
            raise DataError('호가 가격 기준과 금액은 실제 호가 소스에서 설정해 주세요.')
        if data_type == "funding":
            if funding_available is None:
                funding_available = funding.list_symbols()
            exchange, ticker = funding.validate_symbol(leg.get("exchange"), leg.get("ticker"), funding_available)
        elif data_type == "candle":
            if candle_available is None:
                candle_available = candles.list_symbols()
            exchange, ticker = candles.validate_symbol(leg.get("exchange"), leg.get("ticker"), candle_available)
        else:
            exchange, ticker = parquet.validate_symbol(leg.get("exchange"), leg.get("ticker"), available)
        options = normalize_price_options(leg) if data_type == 'price' else {}
        legs.append({"id": leg["id"], "exchange": exchange, "ticker": ticker, "data_type": data_type, **options})
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
    if any(leg['data_type'] == 'candle' for leg in legs) and step < candles.MINUTE_MS:
        raise DataError('1분봉 종가는 1분 이상 간격으로 조회해 주세요.')
    buckets = (int(end.value // 1_000_000) - 1) // step - int(start.value // 1_000_000) // step + 1
    if buckets > MAX_BUCKETS:
        raise DataError(f"예상 시간 구간이 {buckets:,}개입니다. 소스당 최대 {MAX_BUCKETS:,}개까지 조회할 수 있으니 기간을 줄이거나 조회 간격을 늘려 주세요.")
    max_points = integer(payload.get("max_points", 4000), "차트 최대 점 개수", 2, 6000)
    span = integer(payload.get("ewm_span", 60), "EMA span", 1, 1_000_000)
    include_full = payload.get("include_full_data", False)
    if type(include_full) is not bool:
        raise DataError("include_full_data는 true 또는 false로 지정해 주세요.")

    source_keys = {}
    for leg in legs:
        key = (leg["data_type"], leg["exchange"], leg["ticker"])
        if leg['data_type'] == 'price':
            key += (leg['price_basis'], leg['book_level'], leg['liquidity_amount'])
        source_keys[leg['id']] = key
    if buckets * len(legs) > MAX_TOTAL_SOURCE_BUCKETS:
        raise DataError(f"수식에 참조한 소스 수 × 시간 구간 수가 {MAX_TOTAL_SOURCE_BUCKETS:,}개를 초과합니다. 기간·소스 수를 줄이거나 조회 간격을 늘려 주세요.")
    loaded, rows_read = {}, 0
    for leg in legs:
        key = source_keys[leg['id']]
        if key not in loaded:
            reader = {'funding': funding.read_buckets, 'candle': candles.read_buckets,
                      'price': parquet.read_buckets}[leg['data_type']]
            if leg['data_type'] == 'price' and leg['price_basis'] != 'mid':
                frame, count = reader(leg['exchange'], leg['ticker'], start, end, INTERVALS[interval],
                                      price_mode=price_mode, price_basis=leg['price_basis'],
                                      book_level=leg['book_level'], liquidity_amount=leg['liquidity_amount'])
            elif price_mode == "last":
                frame, count = reader(leg['exchange'], leg['ticker'], start, end, INTERVALS[interval], price_mode="last")
            else:
                frame, count = reader(leg['exchange'], leg['ticker'], start, end, INTERVALS[interval])
            loaded[key] = frame
            rows_read += count
    index = None
    for frame in loaded.values():
        index = frame.index if index is None else index.intersection(frame.index)
    index = index.sort_values()
    if index.empty:
        raise DataError("모든 분석 시리즈가 함께 존재하는 시간 구간이 없습니다. 기간·종목·조회 간격을 바꿔 주세요. 누락 시각을 이전 값으로 채우지 않습니다.")

    values = {}
    for leg in legs:
        frame = loaded[source_keys[leg['id']]]
        column = 'value' if 'value' in frame.columns else 'mid'
        values[leg['id']] = frame.loc[index, column]
    result = evaluate(tree, values, index)
    valid = result.dropna()
    smooth = result.ewm(span=span).mean()
    warnings = []
    for leg in legs:
        if leg['data_type'] != 'price' or leg['price_basis'] == 'mid':
            continue
        frame = loaded[source_keys[leg['id']]]
        missing_prices = int(frame.loc[index, 'value'].isna().sum())
        if leg['price_basis'] in {'buy_vwap', 'sell_vwap'}:
            side = '매도 호가를 소진하는 매수' if leg['price_basis'] == 'buy_vwap' else '매수 호가를 소진하는 매도'
            quote_currency = 'KRW' if leg['exchange'] in {'bithumb', 'upbit', 'ls'} else 'USDT'
            warnings.append(f"{leg['id']}: {side} 유동성호가, 목표 {leg['liquidity_amount']:g} {quote_currency}. 저장된 최대 5호가 안에서 전액 체결되는 경우의 평균 가격이며 수수료·추가 시장 충격은 포함하지 않습니다.")
            if frame.attrs.get('quantity_basis') == 'base_reconstructed_from_quote_notional':
                warnings.append(f"{leg['id']}: 기존 수집기의 정수 절삭 호가 금액에서 수량을 환산한 근사값입니다.")
        else:
            side = '매수' if leg['price_basis'] == 'bid' else '매도'
            warnings.append(f"{leg['id']}: {side} {leg['book_level']}호가를 분석 값으로 사용합니다. 별도 bid/ask/mid 컬럼은 최우선 1호가 원본입니다.")
        if missing_prices:
            warnings.append(f"{leg['id']}: 호가 단계 또는 유동성이 부족한 {missing_prices:,}개 구간의 선택 가격은 빈 값입니다.")
        excluded_snapshots = frame.attrs.get('pricing_missing_rows', 0)
        if excluded_snapshots:
            warnings.append(f"{leg['id']}: 원본 스냅샷 {excluded_snapshots:,}개에서 선택 가격을 계산하지 못했습니다. CSV의 유효·누락 스냅샷 수를 확인하세요.")
        if price_mode == 'mean':
            warnings.append(f"{leg['id']}: 스냅샷마다 선택 가격을 먼저 계산한 뒤 유효 가격을 구간 평균합니다. 계산되지 않는 스냅샷은 평균에서 제외합니다.")
            if leg['price_basis'] in {'buy_vwap', 'sell_vwap'}:
                warnings.append(f"{leg['id']}: CSV의 유동성·체결 수량·사용 단계 진단값은 미충족 행의 부분 체결도 포함한 원본 진단값 평균입니다. 선택 가격 평균의 전액 체결 수량을 뜻하지 않습니다.")
    has_funding = any(leg["data_type"] == "funding" for leg in legs)
    if any(leg['data_type'] == 'candle' for leg in legs):
        warnings.append('1분봉 소스는 REST로 받은 완료 분봉 종가입니다. 실제 틱·매수매도 1호가·호가 잔량을 복원한 값이 아닙니다. 완성시각으로 집계 구간을 배정하고 결과 시각은 구간 시작을 표시합니다. 5분·1시간 구간 전체값은 그 구간 종료 후 확정되며 마지막 원본 완성시각은 별도로 제공합니다. 체결 없는 분은 채우지 않습니다.')
        if price_mode == 'mean':
            warnings.append('1분봉 수식 값은 구간 내 종가 평균이며 OHLCV·거래대금·분봉 시각은 그 구간의 마지막 원본 1분봉입니다.')
    if any(frame.attrs.get("source_kind") == "orderbook_1s" for frame in loaded.values()):
        warnings.append("USDT 1초 호가는 수집 시점의 최신 매수·매도 1호가와 실제 USDT 수량입니다. 거래소 갱신 사이에는 같은 호가가 반복될 수 있으며, 원본 행의 시각은 실제 채택 시각입니다. 거래소·수신 시각은 원본 CSV에 보존합니다.")
        if price_mode == "mean":
            warnings.append("USDT 수량은 가격과 동일한 유효 원본 행들을 구간 평균한 값입니다.")
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
        frame = loaded[source_keys[leg['id']]].loc[index]
        if leg['data_type'] == 'candle':
            def candle_leg(source):
                return {**leg, 'unit': 'KRW', 'currency': 'KRW', 'price_unit': 'KRW',
                        'quantity_unit': 'USDT', 'source_kind': 'rest_1m', 'candle_interval_ms': candles.MINUTE_MS,
                        'values': array(source['value']),
                        'timestamp_delta_ms': [None] * len(source),
                        'timestamp_delta_unavailable_reason': '1분봉에는 기록·서버 시각 쌍이 없습니다.',
                        **{field: array(source[field]) for field in candles.FIELDS},
                        'candle_times': [int(value) for value in source['candle_time']],
                        'available_times': [int(value) for value in source['available_time']]}
            output_legs.append(candle_leg(frame.iloc[selection]))
            if include_full:
                full_legs.append(candle_leg(frame))
            continue
        if leg["data_type"] == "funding":
            def funding_leg(source):
                return {**leg, "unit": "%", "values": array(source["value"]),
                        "funding_rate": array(source["funding_rate"]),
                        "timestamp_delta_ms": array(source['timestamp_delta_ms']) if 'timestamp_delta_ms' in source else [None] * len(source),
                        "timestamp_delta_unavailable_reason": source.attrs.get('timestamp_delta_unavailable_reason'),
                        "source_times": [int(value) for value in source["source_time"]],
                        "server_times": [int(value) if pd.notna(value) else None for value in source["server_time"]],
                        "next_funding_in_seconds": array(source["next_funding_in_seconds"])}
            output_legs.append(funding_leg(frame.iloc[selection]))
            if include_full:
                full_legs.append(funding_leg(frame))
            continue
        info = {**leg, "currency": "KRW" if leg["exchange"] in {"bithumb", "upbit", "ls"} else "USDT",
                "timestamp_delta_unavailable_reason": frame.attrs.get('timestamp_delta_unavailable_reason')}
        columns = ["mid", "bid", "ask"]
        if 'value' in frame.columns:
            columns.append('value')
        for name in ('timestamp_delta_ms', 'liquidity_available_notional', 'liquidity_filled_quantity', 'liquidity_levels_used', 'value_rows', 'missing_value_rows'):
            if name in frame.columns:
                columns.append(name)
        for name in ('liquidity_currency', 'quantity_basis', 'depth_limit'):
            if name in frame.attrs:
                info[name] = frame.attrs[name]
        if (frame.attrs.get("source_kind") == "orderbook_1s"
                and frame.attrs.get("quantity_unit") == "USDT"
                and {"bid_size", "ask_size"}.issubset(frame.columns)):
            info.update({name: frame.attrs[name] for name in ["source_kind", "sample_interval_ms", "quantity_unit", "price_unit", "quote_time_basis"]})
            columns.extend(["bid_size", "ask_size"])
        output_leg = {**info, **{column: array(frame.iloc[selection][column]) for column in columns}}
        if 'value' in output_leg:
            output_leg['values'] = output_leg.pop('value')
        for old, new in (('value_rows', 'pricing_valid_rows'), ('missing_value_rows', 'pricing_missing_rows')):
            if old in output_leg:
                output_leg[new] = output_leg.pop(old)
        if price_mode == "last":
            output_leg["quote_times"] = [int(value) for value in frame.iloc[selection]["quote_time"]]
        output_legs.append(output_leg)
        if include_full:
            full_leg = {**info, **{column: array(frame[column]) for column in columns}}
            if 'value' in full_leg:
                full_leg['values'] = full_leg.pop('value')
            for old, new in (('value_rows', 'pricing_valid_rows'), ('missing_value_rows', 'pricing_missing_rows')):
                if old in full_leg:
                    full_leg[new] = full_leg.pop(old)
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
    if any(leg['data_type'] == 'candle' for leg in legs):
        output['meta'].update(candle_time_basis='completion', bucket_time_basis='start', bucket_interval_ms=step)
    if include_full:
        output["full"] = {"times": [int(value) for value in index], "legs": full_legs,
                          "result": array(result), "ewm": array(smooth)}
    return output
