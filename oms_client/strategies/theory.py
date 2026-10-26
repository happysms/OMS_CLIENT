"""Strategy output contract for point-in-time theoretical valuation."""

from dataclasses import dataclass
from decimal import Decimal
from .simple_mm import _amount


@dataclass(frozen=True)
class TheoryOutput:
    price: Decimal
    observed_at_ms: int
    valid_until_ms: int
    basis: str
    formula: str = ''

    def __post_init__(self):
        object.__setattr__(
            self, 'price', _amount(self.price, 'strategy_theory_price', positive=True)
        )
        if (
            type(self.observed_at_ms) is not int
            or type(self.valid_until_ms) is not int
            or self.observed_at_ms < 0
            or self.valid_until_ms < self.observed_at_ms
        ):
            raise ValueError('전략 이론가 시각을 확인하세요.')
        if not isinstance(self.basis, str) or not self.basis or len(self.basis) > 120:
            raise ValueError('전략 이론가 계산 근거를 확인하세요.')
        if not isinstance(self.formula, str) or len(self.formula) > 500:
            raise ValueError('전략 이론가 계산식을 확인하세요.')

    def valid_at(self, now_ms):
        return self.observed_at_ms <= now_ms <= self.valid_until_ms

    def report(self):
        return dict(
            price=float(self.price),
            available_at_ms=self.observed_at_ms,
            valid_until_ms=self.valid_until_ms,
            basis=self.basis,
            formula=self.formula,
        )
