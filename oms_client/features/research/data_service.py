"""Local order-book analysis, with no exchange or database connections."""
from __future__ import annotations

import ast
import math
import os
from pathlib import Path
import re
import threading
from contextlib import contextmanager

import duckdb
import numpy as np
import pandas as pd


KST = "Asia/Seoul"
INTERVALS = {"1s": 1_000, "10s": 10_000, "1min": 60_000, "5min": 300_000, "1h": 3_600_000}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_RANGE_DAYS = 31
MAX_BUCKETS = 250_000
FUNCTIONS = [
    {"name": "MA", "syntax": "MA(A, 60)", "description": "최근 n개 공통 표본의 이동평균"},
    {"name": "EMA", "syntax": "EMA(A, 60)", "description": "span=n 지수이동평균 (adjust=True)"},
    {"name": "DIFF", "syntax": "DIFF(A, 1)", "description": "n개 표본 전 대비 차이"},
    {"name": "RET", "syntax": "RET(A, 1)", "description": "n개 표본 전 대비 수익률 (%)"},
    {"name": "ZSCORE", "syntax": "ZSCORE(A, 60)", "description": "최근 n개 표본의 평균·표본 표준편차 기준 z-score"},
    {"name": "ABS", "syntax": "ABS(A - B)", "description": "절댓값"},
]
FUNCTION_NAMES = {item["name"] for item in FUNCTIONS}


class DataError(ValueError):
    """A user-correctable request or source-data error."""


class QueryTimeout(DataError):
    """A bounded analytical query exceeded its configured time budget."""


def _root() -> Path:
    return Path(os.environ.get("DASHBOARD_DATA_ROOT", "/data")).resolve()


def _connection():
    threads = _integer(os.environ.get("DASHBOARD_THREADS", "2"), "DuckDB 스레드 수", 1, 32)
    memory = os.environ.get("DASHBOARD_MEMORY_LIMIT", "256MB")
    if not re.fullmatch(r"[1-9]\d*(?:\.\d+)?\s*(?:KB|MB|GB|TB)", memory, re.IGNORECASE):
        raise DataError("DASHBOARD_MEMORY_LIMIT는 256MB, 1GB와 같이 지정해 주세요.")
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET threads = ?", [threads])
        con.execute("SET memory_limit = ?", [memory])
    except Exception:
        con.close()
        raise
    return con


@contextmanager
def _query_deadline(con):
    try:
        seconds = float(os.environ.get("DASHBOARD_QUERY_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(seconds) or not 0.01 <= seconds <= 3600:
            raise ValueError("range")
    except (TypeError, ValueError) as exc:
        raise DataError("DASHBOARD_QUERY_TIMEOUT_SECONDS는 0.01~3600초로 지정해 주세요.") from exc
    timed_out = threading.Event()

    def interrupt():
        timed_out.set()
        con.interrupt()

    timer = threading.Timer(seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
        if timed_out.is_set():
            raise QueryTimeout("조회 제한 시간을 초과했습니다. 기간을 줄이거나 조회 간격을 늘려 주세요.")
    except duckdb.InterruptException as exc:
        if timed_out.is_set():
            raise QueryTimeout("조회 제한 시간을 초과했습니다. 기간을 줄이거나 조회 간격을 늘려 주세요.") from exc
        raise
    finally:
        timer.cancel()
        timer.join()


def _within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, RuntimeError):
        return False


def _exchanges() -> dict[str, list[str]]:
    root = _root()
    if not root.is_dir():
        return {}
    found = {}
    for exchange in sorted(root.iterdir()):
        ob = exchange / "ob"
        if not IDENTIFIER.fullmatch(exchange.name) or not ob.is_dir() or not _within(ob, root):
            continue
        symbols = sorted(p.name for p in ob.iterdir()
                         if p.is_dir() and IDENTIFIER.fullmatch(p.name) and _within(p, ob))
        if symbols:
            found[exchange.name] = symbols
    return found


def _validate_symbol(exchange, ticker, available=None) -> tuple[str, str]:
    if not isinstance(exchange, str) or not isinstance(ticker, str):
        raise DataError("거래소와 종목을 선택해 주세요.")
    ticker = ticker.strip()
    available = _exchanges() if available is None else available
    if exchange not in available:
        raise DataError(f"선택한 거래소·종목의 호가 데이터가 없습니다: {exchange} / {ticker}")
    lookup = {symbol.upper(): symbol for symbol in available[exchange]}
    normalized = lookup.get(ticker.upper())
    if normalized is None:
        raise DataError(f"선택한 거래소·종목의 호가 데이터가 없습니다: {exchange} / {ticker}")
    return exchange, normalized


def _iso(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(KST).isoformat()


def bounds(exchange: str, ticker: str) -> dict:
    """Return actual timestamp bounds from the first and last available files."""
    exchange, ticker = _validate_symbol(exchange, ticker)
    directory = _root() / exchange / "ob" / ticker
    files = sorted(p for p in directory.glob("*/*/*/*.parquet") if _within(p, directory))
    if not files:
        raise DataError(f"{exchange} {ticker}의 parquet 파일이 없습니다.")
    con = _connection()
    try:
        start = end = None
        with _query_deadline(con):
            for path in files:
                start = con.execute("SELECT min(desktopTime) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                if start is not None:
                    break
            for path in reversed(files):
                end = con.execute("SELECT max(desktopTime) FROM read_parquet(?)", [str(path)]).fetchone()[0]
                if end is not None:
                    break
    except duckdb.Error as exc:
        raise DataError("호가 파일의 시간 범위를 읽지 못했습니다. parquet 파일을 확인해 주세요.") from exc
    finally:
        con.close()
    if start is None or end is None:
        raise DataError(f"{exchange} {ticker}의 호가 파일이 비어 있습니다.")
    return {"start": _iso(int(start)), "end": _iso(int(end)), "exchange": exchange, "ticker": ticker}


def catalog() -> dict:
    available = _exchanges()
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
        "defaults": {
            "legs": legs,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "formula": "((A / B) / (C / D) - 1) * 100" if len(legs) == 4 else "A",
            "interval": "1min",
            "unit": "%" if len(legs) == 4 else ("KRW" if legs and legs[0]["exchange"] in {"bithumb", "upbit", "ls"} else "USDT"),
        },
        "data_root": str(_root()),
        "intervals": INTERVALS.copy(),
        "functions": [dict(item) for item in FUNCTIONS],
        "limits": {"max_range_days": MAX_RANGE_DAYS, "max_buckets": MAX_BUCKETS, "max_chart_points": 6000},
    }


def _date(value, label: str) -> pd.Timestamp:
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


def _integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise DataError(f"{name}은 {low}~{high} 사이 정수로 입력해 주세요.")
    try:
        result = int(value)
        if float(value) != result or not low <= result <= high:
            raise ValueError("range")
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise DataError(f"{name}은 {low}~{high} 사이 정수로 입력해 주세요.") from exc


def _formula_tree(formula, ids: set[str]):
    if not isinstance(formula, str) or not formula.strip() or len(formula) > 256:
        raise DataError("조합식을 256자 이내로 입력해 주세요.")
    try:
        tree = ast.parse(formula, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise DataError("조합식 문법을 확인해 주세요. 예: (A / B) / (C / D)") from exc
    if sum(1 for _ in ast.walk(tree)) > 96:
        raise DataError("조합식이 너무 복잡합니다.")

    def validate(node):
        if isinstance(node, ast.Expression):
            validate(node.body)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            validate(node.left)
            validate(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            validate(node.operand)
        elif isinstance(node, ast.Name) and node.id in ids:
            return
        elif isinstance(node, ast.Call):
            if (not isinstance(node.func, ast.Name) or node.func.id not in FUNCTION_NAMES
                    or node.keywords or len(node.args) != (1 if node.func.id == "ABS" else 2)):
                raise DataError("지원하는 함수는 MA, EMA, DIFF, RET, ZSCORE, ABS입니다. 함수의 인자도 확인해 주세요.")
            validate(node.args[0])
            if node.func.id != "ABS":
                window = node.args[1]
                if not isinstance(window, ast.Constant) or type(window.value) is not int or not 1 <= window.value <= 1_000_000:
                    raise DataError("함수 기간은 1~1,000,000 사이의 정수 상수로 지정해 주세요.")
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
            try:
                if not math.isfinite(float(node.value)):
                    raise ValueError("nonfinite")
            except (ValueError, OverflowError) as exc:
                raise DataError("조합식의 숫자가 너무 큽니다.") from exc
        else:
            raise DataError("조합식은 선택한 A~D와 숫자, 사칙연산, 괄호 및 지원하는 함수만 사용할 수 있습니다.")

    validate(tree)
    if not any(isinstance(node, ast.Name) and node.id in ids for node in ast.walk(tree)):
        raise DataError("조합식에 가격 시리즈 A~D를 하나 이상 포함해 주세요.")
    return tree


def formula_ids(formula: str) -> list[str]:
    """Validate a safe formula and list its source IDs, excluding function names."""
    tree = _formula_tree(formula, {"A", "B", "C", "D"})
    return sorted({node.id for node in ast.walk(tree)
                   if isinstance(node, ast.Name) and node.id in {"A", "B", "C", "D"}})


def _evaluate(tree, values: dict[str, pd.Series], index: pd.Index) -> pd.Series:
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Constant):
            return pd.Series(float(node.value), index=index)
        if isinstance(node, ast.Call):
            operand = visit(node.args[0]).replace([np.inf, -np.inf], np.nan)
            name = node.func.id
            if name == "ABS":
                return operand.abs()
            window = node.args[1].value
            if name == "MA":
                return operand.rolling(window, min_periods=window).mean()
            if name == "EMA":
                return operand.ewm(span=window, adjust=True).mean()
            if name == "DIFF":
                return operand - operand.shift(window)
            if name == "RET":
                previous = operand.shift(window)
                return (operand / previous.mask(previous == 0) - 1) * 100
            rolling = operand.rolling(window, min_periods=window)
            std = rolling.std(ddof=1)
            return (operand - rolling.mean()) / std.mask(std == 0)
        if isinstance(node, ast.UnaryOp):
            operand = visit(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        left, right = visit(node.left), visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right.mask(right == 0)

    with np.errstate(all="ignore"):
        return visit(tree).replace([np.inf, -np.inf], np.nan)


def _files(exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    files = []
    last = (end - pd.Timedelta(nanoseconds=1)).floor("h")
    first = start.floor("h")
    for day in pd.date_range(first.floor("D"), last.floor("D"), freq="D"):
        directory = _root() / exchange / "ob" / ticker / day.strftime("%Y/%m/%d")
        parent = _root() / exchange / "ob" / ticker
        if not directory.is_dir() or not _within(directory, parent):
            continue
        for path in sorted(directory.glob("*.parquet")):
            if not _within(path, parent):
                continue
            if path.stem.isdigit() and 0 <= int(path.stem) <= 23:
                hour = day + pd.Timedelta(hours=int(path.stem))
                if not first <= hour <= last:
                    continue
            files.append(str(path))
    return files


def _load(exchange: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp, step: int):
    paths = _files(exchange, ticker, start, end)
    if not paths:
        raise DataError(f"선택한 기간에 {exchange} {ticker} 호가 파일이 없습니다.")
    con = _connection()
    try:
        with _query_deadline(con):
            frame = con.execute(
                """
                SELECT CAST(floor(desktopTime / $step) AS BIGINT) * $step AS time,
                       avg(CAST(b1_p AS DOUBLE)) AS bid,
                       avg(CAST(a1_p AS DOUBLE)) AS ask,
                       avg(CAST(b1_p AS DOUBLE) / 2.0 + CAST(a1_p AS DOUBLE) / 2.0) AS mid,
                       count(*) AS source_rows
                FROM read_parquet($paths)
                WHERE desktopTime >= $start AND desktopTime < $end
                  AND isfinite(CAST(b1_p AS DOUBLE)) AND isfinite(CAST(a1_p AS DOUBLE))
                  AND b1_p > 0 AND a1_p >= b1_p
                GROUP BY time ORDER BY time
                """,
                {"paths": paths, "start": int(start.value // 1_000_000), "end": int(end.value // 1_000_000), "step": step},
            ).df()
    except duckdb.Error as exc:
        raise DataError(f"{exchange} {ticker} 호가 파일을 읽지 못했습니다. parquet 스키마를 확인해 주세요.") from exc
    finally:
        con.close()
    if frame.empty:
        raise DataError(f"선택한 기간에 {exchange} {ticker}의 유효한 매수·매도 호가가 없습니다.")
    return frame.set_index("time"), int(frame["source_rows"].sum())


def _number(value):
    return float(value) if value is not None and pd.notna(value) and math.isfinite(float(value)) else None


def _array(series: pd.Series) -> list:
    return [_number(value) for value in series.to_numpy()]


def _selection(result: pd.Series, limit: int) -> np.ndarray:
    if len(result) <= limit:
        return np.arange(len(result))
    selected = set(np.linspace(0, len(result) - 1, limit, dtype=int))
    protected = {0, len(result) - 1}
    finite = np.flatnonzero(np.isfinite(result.to_numpy()))
    if len(finite):
        values = result.to_numpy()[finite]
        for index in (int(finite[np.argmin(values)]), int(finite[np.argmax(values)])):
            if len(protected) < limit:
                selected.add(index)
                protected.add(index)
    while len(selected) > limit:
        choices = [i for i in sorted(selected) if i not in protected]
        # Remove the point nearest another retained point, preserving endpoints
        # and the global extrema. Full data is retained separately for exports.
        ordered = sorted(selected)
        positions = {value: pos for pos, value in enumerate(ordered)}
        remove = min(choices, key=lambda i: min(i - ordered[positions[i] - 1], ordered[positions[i] + 1] - i))
        selected.remove(remove)
    return np.array(sorted(selected), dtype=int)


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
        raise DataError("가격 시리즈를 1~4개 선택해 주세요.")
    requested_by_id = {}
    for leg in requested:
        if (not isinstance(leg, dict) or not isinstance(leg.get("id"), str)
                or leg["id"] not in {"A", "B", "C", "D"} or leg["id"] in requested_by_id):
            raise DataError("가격 시리즈 이름은 중복 없는 A~D로 지정해 주세요.")
        requested_by_id[leg["id"]] = leg
    default_formula = "((A / B) / (C / D) - 1) * 100" if len(requested_by_id) == 4 else "A"
    formula = payload.get("formula", default_formula)
    tree = _formula_tree(formula, set(requested_by_id))
    referenced = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and node.id in requested_by_id}
    available = _exchanges()
    legs = []
    for identifier, leg in requested_by_id.items():
        if identifier not in referenced:
            continue
        exchange, ticker = _validate_symbol(leg.get("exchange"), leg.get("ticker"), available)
        legs.append({"id": leg["id"], "exchange": exchange, "ticker": ticker})
    start, end = _date(payload.get("start"), "시작 시간"), _date(payload.get("end"), "종료 시간")
    if start >= end:
        raise DataError("종료 시간은 시작 시간보다 뒤여야 합니다.")
    if end - start > pd.Timedelta(days=MAX_RANGE_DAYS):
        raise DataError(f"한 번에 조회할 수 있는 기간은 최대 {MAX_RANGE_DAYS}일입니다.")
    interval = payload.get("interval", "1min")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("조회 간격은 1s, 10s, 1min, 5min, 1h 중에서 선택해 주세요.")
    step = INTERVALS[interval]
    buckets = (int(end.value // 1_000_000) - 1) // step - int(start.value // 1_000_000) // step + 1
    if buckets > MAX_BUCKETS:
        raise DataError(f"예상 시간 구간이 {buckets:,}개입니다. 소스당 최대 {MAX_BUCKETS:,}개까지 조회할 수 있으니 기간을 줄이거나 조회 간격을 늘려 주세요.")
    max_points = _integer(payload.get("max_points", 4000), "차트 최대 점 개수", 2, 6000)
    span = _integer(payload.get("ewm_span", 60), "EMA span", 1, 1_000_000)
    include_full = payload.get("include_full_data", False)
    if type(include_full) is not bool:
        raise DataError("include_full_data는 true 또는 false로 지정해 주세요.")

    loaded, rows_read = {}, 0
    for leg in legs:
        key = (leg["exchange"], leg["ticker"])
        if key not in loaded:
            frame, count = _load(*key, start, end, INTERVALS[interval])
            loaded[key] = frame
            rows_read += count
    index = None
    for frame in loaded.values():
        index = frame.index if index is None else index.intersection(frame.index)
    index = index.sort_values()
    if index.empty:
        raise DataError("모든 가격 시리즈가 함께 존재하는 시간 구간이 없습니다. 기간·종목·조회 간격을 바꿔 주세요.")

    values = {leg["id"]: loaded[(leg["exchange"], leg["ticker"])].loc[index, "mid"] for leg in legs}
    result = _evaluate(tree, values, index)
    valid = result.dropna()
    smooth = result.ewm(span=span).mean()
    warnings = []
    missing = int(result.isna().sum())
    if missing:
        warnings.append(f"함수의 초기 구간, 0으로 나누기 또는 유효하지 않은 계산 결과 {missing:,}개는 빈 값으로 표시합니다.")
    functions_used = sorted({node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)})
    if functions_used:
        warnings.append("함수의 기간은 공통 호가가 존재하는 표본 개수입니다. 누락 시각을 채우지 않으며 실제 경과 시간과 다를 수 있습니다.")
    available_points = len(set().union(*(set(frame.index) for frame in loaded.values())))
    if available_points > len(index):
        warnings.append(f"일부 가격이 없는 시간 구간 {available_points - len(index):,}개는 제외했습니다. 이전 가격으로 채우지 않습니다.")
    selection = _selection(result, max_points)
    if len(index) > max_points:
        warnings.append(f"차트는 {len(index):,}개 중 {len(selection):,}개를 표시합니다. 통계와 EMA는 표시 전 전체 구간으로 계산했습니다.")
    first, latest = (_number(valid.iloc[0]), _number(valid.iloc[-1])) if len(valid) else (None, None)
    mean = _number(valid.mean())
    change = _number((latest / first - 1) * 100) if first not in (None, 0) and latest is not None else None
    std = _number(valid.std())
    full_legs, output_legs = [], []
    for leg in legs:
        frame = loaded[(leg["exchange"], leg["ticker"])].loc[index]
        info = {**leg, "currency": "KRW" if leg["exchange"] in {"bithumb", "upbit", "ls"} else "USDT"}
        output_legs.append({**info, **{column: _array(frame.iloc[selection][column]) for column in ["mid", "bid", "ask"]}})
        if include_full:
            full_legs.append({**info, **{column: _array(frame[column]) for column in ["mid", "bid", "ask"]}})
    correlations = pd.DataFrame(values, index=index).corr()
    output = {
        "times": [int(value) for value in index[selection]],
        "legs": output_legs,
        "result": _array(result.iloc[selection]),
        "ewm": _array(smooth.iloc[selection]),
        "mean": mean,
        "stats": {"count": int(len(valid)), "latest": latest, "mean": mean, "std": std,
                  "min": _number(valid.min()), "max": _number(valid.max()), "first": first, "change_pct": change,
                  "median": _number(valid.median()), "q05": _number(valid.quantile(.05)), "q95": _number(valid.quantile(.95)),
                  "positive_rate": _number((valid > 0).mean()) if len(valid) else None,
                  "latest_minus_mean": _number(latest - mean) if latest is not None and mean is not None else None,
                  "latest_zscore": _number((latest - mean) / std) if latest is not None and mean is not None and std not in (None, 0) else None,
                  "latest_time": int(valid.index[-1]) if len(valid) else None},
        "correlations": {left: {right: _number(correlations.loc[left, right]) for right in correlations.columns}
                         for left in correlations.index},
        "meta": {"start": start.tz_convert(KST).isoformat(), "end": end.tz_convert(KST).isoformat(),
                 "interval": interval, "formula": formula, "source": "parquet", "rows_read": rows_read,
                 "aligned_points": len(index), "returned_points": len(selection), "warnings": warnings, "ewm_span": span,
                 "functions": functions_used, "window_basis": "aligned_samples", "undefined_points": missing},
    }
    if include_full:
        output["full"] = {"times": [int(value) for value in index], "legs": full_legs,
                          "result": _array(result), "ewm": _array(smooth)}
    return output


def export_leg(exchange: str, ticker: str, start: str, end: str, interval: str = "5min") -> dict:
    """Return a compact historical series for a portable static snapshot.

    Unlike chart responses, exports retain every interval, including 10,080+
    one-minute buckets for seven days. No prices are carried into empty buckets.
    """
    exchange, ticker = _validate_symbol(exchange, ticker)
    start_dt, end_dt = _date(start, "시작 시간"), _date(end, "종료 시간")
    if start_dt >= end_dt or end_dt - start_dt > pd.Timedelta(days=7):
        raise DataError("내보내기 기간은 0일 초과 7일 이하여야 합니다.")
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise DataError("지원하지 않는 내보내기 간격입니다.")
    frame, rows = _load(exchange, ticker, start_dt, end_dt, INTERVALS[interval])
    return {
        "exchange": exchange, "ticker": ticker,
        "currency": "KRW" if exchange in {"bithumb", "upbit", "ls"} else "USDT",
        "times": [int(value) for value in frame.index],
        **{column: _array(frame[column]) for column in ["mid", "bid", "ask"]},
        "meta": {"start": start_dt.tz_convert(KST).isoformat(), "end": end_dt.tz_convert(KST).isoformat(),
                 "interval": interval, "source": "parquet", "rows_read": rows,
                 "aligned_points": len(frame), "returned_points": len(frame), "warnings": []},
    }
