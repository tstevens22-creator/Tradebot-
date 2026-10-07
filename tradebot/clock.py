"""Injectable clocks so every timing path is deterministic under test."""
from __future__ import annotations

import time


class Clock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class SimClock(Clock):
    def __init__(self, start_ms: int = 1_790_000_000_000):
        self._t = start_ms

    def now_ms(self) -> int:
        return self._t

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("time cannot go backwards")
        self._t += ms
