"""Safe AST formula validation and aligned-series evaluation."""
from __future__ import annotations

import ast
import math

import numpy as np
import pandas as pd

from ..market_data.validation import DataError

FUNCTIONS = [
    {"name": "MA", "syntax": "MA(A, 60)", "description": "최근 n개 공통 표본의 이동평균"},
    {"name": "EMA", "syntax": "EMA(A, 60)", "description": "span=n 지수이동평균 (adjust=True)"},
    {"name": "DIFF", "syntax": "DIFF(A, 1)", "description": "n개 표본 전 대비 차이"},
    {"name": "RET", "syntax": "RET(A, 1)", "description": "n개 표본 전 대비 수익률 (%)"},
    {"name": "ZSCORE", "syntax": "ZSCORE(A, 60)", "description": "최근 n개 표본의 평균·표본 표준편차 기준 z-score"},
    {"name": "ABS", "syntax": "ABS(A - B)", "description": "절댓값"},
]
FUNCTION_NAMES = {item["name"] for item in FUNCTIONS}


def parse_formula(formula, ids: set[str]):
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
    tree = parse_formula(formula, {"A", "B", "C", "D"})
    return sorted({node.id for node in ast.walk(tree)
                   if isinstance(node, ast.Name) and node.id in {"A", "B", "C", "D"}})



def evaluate(tree, values: dict[str, pd.Series], index: pd.Index) -> pd.Series:
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
