"""Deterministic virtual events; no wall clock, order mutation or persistence."""

from dataclasses import dataclass, field
import heapq


@dataclass(frozen=True, order=True)
class ScheduledEvent:
    at_ms: int
    sequence: int
    kind: str = field(compare=False)
    identifier: str | int = field(compare=False)


class EventScheduler:
    def __init__(self):
        self._events: list[ScheduledEvent] = []
        self._sequence = 0

    def schedule(self, at_ms: int, kind: str, identifier: str | int) -> None:
        self._sequence += 1
        heapq.heappush(self._events, ScheduledEvent(at_ms, self._sequence, kind, identifier))

    def next_time(self) -> int | None:
        return self._events[0].at_ms if self._events else None

    def pop_due(self, through_ms: int) -> ScheduledEvent | None:
        if self._events and self._events[0].at_ms <= through_ms:
            return heapq.heappop(self._events)
        return None
