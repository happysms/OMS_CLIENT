"""Bounded, cash-funded historical simulations using completed quote buckets."""
from __future__ import annotations

import math

import pandas as pd

from .research import data_service as ds


KEYS = {"exchange", "ticker", "start", "end", "interval", "strategy", "fast_window",
        "slow_window", "initial_cash", "fee_bps", "slippage_bps", "max_points", "close_on_end"}
MAX_TRADES = 20_000


def _amount(value, label, low, high):
    if isinstance(value, bool):
        raise ds.DataError(f"{label} 값을 확인해 주세요.")
    try:
        number = float(value)
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError("range")
        return number
    except (ValueError, TypeError, OverflowError) as exc:
        raise ds.DataError(f"{label}은 {low:g}~{high:g} 범위로 입력해 주세요.") from exc


def _config(payload):
    if not isinstance(payload, dict) or set(payload) - KEYS:
        raise ds.DataError("지원하는 백테스트 설정을 JSON 객체로 보내 주세요.")
    exchange, ticker = payload.get("exchange"), payload.get("ticker")
    if (not isinstance(exchange, str) or not ds.IDENTIFIER.fullmatch(exchange)
            or not isinstance(ticker, str) or not ds.IDENTIFIER.fullmatch(ticker)):
        raise ds.DataError("거래소와 종목을 선택해 주세요.")
    strategy = payload.get("strategy", "ma_cross")
    if not isinstance(strategy, str) or strategy not in {"ma_cross", "buy_hold"}:
        raise ds.DataError("전략은 ma_cross 또는 buy_hold로 선택해 주세요.")
    interval = payload.get("interval", "1min")
    if not isinstance(interval, str) or interval not in ds.INTERVALS:
        raise ds.DataError("지원하는 조회 간격을 선택해 주세요.")
    start = ds._date(payload.get("start"), "시작 시간")
    end = ds._date(payload.get("end"), "종료 시간")
    if start >= end or end - start > pd.Timedelta(days=ds.MAX_RANGE_DAYS):
        raise ds.DataError("백테스트 기간은 0일 초과 31일 이하여야 합니다.")
    step = ds.INTERVALS[interval]
    if (end.value // 1_000_000 - 1) // step - (start.value // 1_000_000) // step + 1 > ds.MAX_BUCKETS:
        raise ds.DataError("시간 구간이 너무 많습니다. 기간을 줄이거나 조회 간격을 늘려 주세요.")
    fast = ds._integer(payload.get("fast_window", 10), "단기 이동평균", 1, 100_000)
    slow = ds._integer(payload.get("slow_window", 30), "장기 이동평균", 2, 100_000)
    if fast >= slow:
        raise ds.DataError("단기 이동평균 기간은 장기 기간보다 작아야 합니다.")
    close = payload.get("close_on_end", True)
    if type(close) is not bool:
        raise ds.DataError("close_on_end는 true 또는 false로 입력해 주세요.")
    return {"exchange": exchange, "ticker": ticker, "start": start.isoformat(), "end": end.isoformat(),
            "interval": interval, "strategy": strategy, "fast_window": fast, "slow_window": slow,
            "initial_cash": _amount(payload.get("initial_cash", 1_000_000), "초기 자금", 0.000001, 1e18),
            "fee_bps": _amount(payload.get("fee_bps", 5), "수수료(bp)", 0, 1000),
            "slippage_bps": _amount(payload.get("slippage_bps", 2), "슬리피지(bp)", 0, 1000),
            "max_points": ds._integer(payload.get("max_points", 6000), "차트 최대 점 개수", 2, 6000),
            "close_on_end": close}


def _source(result, config):
    if not isinstance(result, dict):
        raise ds.DataError("호가 데이터 형식을 확인해 주세요.")
    full = result.get("full", result)
    if not isinstance(full, dict) or not isinstance(full.get("times"), list):
        raise ds.DataError("호가 데이터의 시각을 확인해 주세요.")
    times = full["times"]
    legs = full.get("legs")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        raise ds.DataError("백테스트는 하나의 가격 시리즈를 사용합니다.")
    leg = legs[0]
    if not 2 <= len(times) <= ds.MAX_BUCKETS:
        raise ds.DataError("완료된 호가 구간이 최소 2개 필요합니다. 기간과 조회 간격을 확인해 주세요.")
    if any(not isinstance(leg.get(column), list) or len(leg[column]) != len(times)
           for column in ("mid", "bid", "ask")):
        raise ds.DataError("호가 데이터의 길이가 서로 다릅니다.")
    previous = None
    for index, timestamp in enumerate(times):
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or int(timestamp) != timestamp
                or (previous is not None and timestamp <= previous)):
            raise ds.DataError("호가 시각은 중복 없는 오름차순이어야 합니다.")
        previous = timestamp
        values = [leg[column][index] for column in ("bid", "mid", "ask")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) or value <= 0 for value in values):
            raise ds.DataError("유효하지 않은 호가 가격이 있습니다.")
        if not values[0] <= values[1] <= values[2]:
            raise ds.DataError("매수·중간·매도 호가 순서가 올바르지 않습니다.")
    step = ds.INTERVALS[config["interval"]]
    start = ds._date(config["start"], "시작 시간").value // 1_000_000
    end = ds._date(config["end"], "종료 시간").value // 1_000_000
    # Partial boundary buckets contain only part of an interval's mean. Exclude
    # them rather than presenting them as fully observed execution periods.
    selected = [i for i, timestamp in enumerate(times) if start <= timestamp and timestamp + step <= end]
    if len(selected) < 2:
        raise ds.DataError("완료된 호가 구간이 최소 2개 필요합니다. 기간을 늘려 주세요.")
    return ([int(times[i]) + step for i in selected],
            {**leg, **{column: [float(leg[column][i]) for i in selected] for column in ("mid", "bid", "ask")}})


def _simulate(times, leg, config, strategy, *, record=True):
    cash, quantity, basis = config["initial_cash"], 0.0, 0.0
    fee_rate, slippage = config["fee_bps"] / 10_000, config["slippage_bps"] / 10_000
    mids = pd.Series(leg["mid"], dtype=float)
    fast = mids.rolling(config["fast_window"]).mean().to_numpy()
    slow = mids.rolling(config["slow_window"]).mean().to_numpy()
    equity, trades = [], []
    fees, realized, closed, wins, executions = 0.0, 0.0, 0, 0, 0

    def fill(index, side, reason):
        nonlocal cash, quantity, basis, fees, realized, closed, wins, executions
        if executions >= MAX_TRADES:
            raise ds.DataError(f"체결 횟수는 최대 {MAX_TRADES:,}회입니다. 기간을 줄이거나 조회 간격을 늘려 주세요.")
        timestamp = times[index]
        pnl = 0.0
        if side == "buy":
            price = leg["ask"][index] * (1 + slippage)
            size = cash / (price * (1 + fee_rate))
            if not math.isfinite(size) or size <= 0:
                raise ds.DataError("가격과 자금 규모로 유효한 주문 수량을 계산할 수 없습니다.")
            notional, basis = size * price, cash
            fee = notional * fee_rate
            cash = max(0.0, cash - notional - fee)
            quantity = size
        else:
            price, size = leg["bid"][index] * (1 - slippage), quantity
            notional = size * price
            fee = notional * fee_rate
            proceeds = notional - fee
            pnl = proceeds - basis
            cash += proceeds
            quantity, basis = 0.0, 0.0
            realized += pnl
            closed += 1
            wins += int(pnl > 0)
        fees += fee
        if not all(math.isfinite(value) for value in (price, size, fee, cash, fees, realized)):
            raise ds.DataError("가격과 자금 규모가 계산 범위를 초과합니다.")
        executions += 1
        if record:
            trades.append({"timestamp": timestamp, "side": side, "price": price, "quantity": size,
                           "fee": fee, "cash": cash, "realized_pnl": pnl, "reason": reason})

    for i in range(len(times)):
        desired = quantity > 0
        # Only a completed previous bucket can determine the next bucket's
        # order. In particular, the current execution price cannot set a signal.
        if i > 0:
            if strategy == "buy_hold":
                desired = True
            elif math.isfinite(slow[i - 1]):
                if fast[i - 1] > slow[i - 1]:
                    desired = True
                elif fast[i - 1] < slow[i - 1]:
                    desired = False
            if desired and quantity == 0:
                fill(i, "buy", "buy_hold" if strategy == "buy_hold" else "ma_cross_up")
            elif not desired and quantity > 0:
                fill(i, "sell", "ma_cross_down")
        if i == len(times) - 1 and config["close_on_end"] and quantity > 0:
            fill(i, "sell", "end_of_test")
        value = cash + quantity * leg["bid"][i]
        if not math.isfinite(value) or value < 0:
            raise ds.DataError("자산 평가값을 계산할 수 없습니다. 가격과 자금을 확인해 주세요.")
        equity.append(value)
    peak, drawdown = config["initial_cash"], 0.0
    for value in equity:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak * 100)
    metrics = {"initial_cash": config["initial_cash"], "final_equity": equity[-1],
               "return_pct": (equity[-1] / config["initial_cash"] - 1) * 100,
               "max_drawdown_pct": drawdown, "trades": executions, "closed_trades": closed,
               "win_rate_pct": wins / closed * 100 if closed else None,
               "fees_paid": fees, "realized_pnl": realized}
    if any(value is not None and not math.isfinite(value) for value in metrics.values()):
        raise ds.DataError("손익 지표가 계산 범위를 초과합니다. 가격과 자금을 확인해 주세요.")
    return equity, trades, metrics


def run_backtest(payload, data_provider=None):
    """Run a funded long-only strategy; the provider accepts an analyze payload."""
    config = _config(payload)
    request = {key: config[key] for key in ("start", "end", "interval")}
    request.update(legs=[{"id": "A", "exchange": config["exchange"], "ticker": config["ticker"]}],
                   formula="A", include_full_data=True, max_points=6000)
    source = (data_provider or ds.analyze)(request)
    times, leg = _source(source, config)
    values, trades, metrics = _simulate(times, leg, config, config["strategy"])
    benchmark, _, _ = _simulate(times, leg, config, "buy_hold", record=False)
    series = pd.Series(values, dtype=float)
    selection = ds._selection(series, config["max_points"]).tolist()
    smooth = series.ewm(span=60, adjust=True).mean()
    chart_times = [times[i] for i in selection]
    chart_values = [values[i] for i in selection]
    sampled_leg = {**leg, **{column: [leg[column][i] for i in selection] for column in ("mid", "bid", "ask")}}
    assumptions = [
        "이전 완료 구간의 중간가 이동평균으로 신호를 만들고 다음 호가 구간에서 체결합니다. 초기 신호 준비 전에는 현금을 보유합니다.",
        "체결가는 다음 구간 전체의 평균 매도호가(매수)·평균 매수호가(매도)입니다. 시각은 구간 종료이며 실제 주문장 체결을 재현하지 않습니다.",
        "구간 평균은 해당 구간이 종료된 뒤 알 수 있으므로 결과는 구간 평균 체결을 가정한 연구용 시뮬레이션입니다.",
        "모든 매수·매도에 수수료와 불리한 방향의 슬리피지를 적용합니다. 현금 전액 범위에서 매수하며 수수료도 현금에 포함합니다.",
        "현금과 단일 종목의 매수 보유만 지원합니다. 공매도·레버리지·펀딩·주문 수량 단위·최소 주문·유동성 제한은 반영하지 않습니다.",
        "평가는 구간 평균 매수호가 기준입니다. 매수 후 보유 비교선에도 같은 다음 구간 진입과 비용·종료 청산 조건을 적용합니다.",
        "누락 구간과 시작·종료의 미완료 구간은 채우지 않습니다. 이동평균 기간은 시간 길이가 아닌 유효한 표본 수입니다.",
    ]
    if config["exchange"] not in {"bithumb", "upbit", "ls"}:
        assumptions.append("파생상품 호가도 현물과 같은 전액 현금 매수 보유의 가격 참고 자료로만 사용합니다. 실제 선물·파생상품 손익이 아닙니다.")
    if config["close_on_end"]:
        assumptions.append("마지막 완료 구간 평균 매수호가에 비용을 적용해 잔여 보유분을 청산합니다.")
    else:
        assumptions.append("종료 시 보유분은 청산하지 않고 평균 매수호가로 평가합니다. 미실현 손익은 실현 손익과 구분합니다.")
    warnings = list(source.get("meta", {}).get("warnings", []))
    if len(selection) < len(times):
        warnings.append("차트는 축소 표시하며 지표와 거래 내역은 전체 완료 구간으로 계산합니다.")
    stats = {"count": len(times), "first": values[0], "latest": values[-1], "mean": ds._number(series.mean()),
             "std": ds._number(series.std()), "min": min(values), "max": max(values), "median": ds._number(series.median()),
             "change_pct": metrics["return_pct"], "latest_time": times[-1]}
    return {"times": chart_times, "legs": [sampled_leg], "result": chart_values,
            "ewm": [ds._number(smooth.iloc[i]) for i in selection], "mean": stats["mean"], "stats": stats,
            "equity": {"times": chart_times, "values": chart_values, "benchmark": [benchmark[i] for i in selection]},
            "metrics": metrics, "trades": trades, "assumptions": assumptions, "config": config,
            "meta": {**source.get("meta", {}), "interval": config["interval"], "formula": config["strategy"],
                     "start": config["start"], "end": config["end"], "aligned_points": len(times),
                     "returned_points": len(selection), "warnings": warnings, "mode": "backtest",
                     "execution_model": "next_bucket_mean", "timestamp_basis": "bucket_end"}}
