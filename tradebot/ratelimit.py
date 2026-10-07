"""Rate-limit budgeting shared by all external APIs."""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """Rate-limit budget. ``reserve`` tokens are held back for the exit path,
    so analytics can never starve protective exits of quota."""

    def __init__(self, rate_per_s: float, capacity: int, reserve: int = 0,
                 now: Callable[[], float] = time.monotonic):
        self.rate, self.capacity, self.reserve, self.now = rate_per_s, capacity, reserve, now
        self.tokens = float(capacity)
        self.t = now()
        self.lock = threading.Lock()

    def try_take(self, priority: bool = False) -> bool:
        with self.lock:
            n = self.now()
            self.tokens = min(self.capacity, self.tokens + (n - self.t) * self.rate)
            self.t = n
            floor = 0 if priority else self.reserve
            if self.tokens - 1 >= floor:
                self.tokens -= 1
                return True
            return False
