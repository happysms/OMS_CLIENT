"""Bounded projection cache for immutable strategy inputs, owned by one replay."""

from ..strategies.simple_mm import WorkingOrder
from .orders import ReplayOrder


class StrategyOrderViews:
    def __init__(self):
        self._views: dict[str, tuple[tuple, WorkingOrder]] = {}

    def project(self, orders: list[ReplayOrder]) -> tuple[WorkingOrder, ...]:
        current = {}
        result = []
        for order in orders:
            signature = (
                order.id,
                order.owner,
                order.side,
                order.price,
                order.remaining,
                order.status,
            )
            previous = self._views.get(order.id)
            view = (
                previous[1]
                if previous is not None and previous[0] == signature
                else WorkingOrder(*signature)
            )
            current[order.id] = (signature, view)
            result.append(view)
        # Evict completed orders on every projection; never retain a run's full history.
        self._views = current
        return tuple(result)
