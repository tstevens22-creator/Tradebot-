"""Latency measurements with p50/p95/p99 (nearest-rank) and budget misses."""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


def percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = max(1, math.ceil(p / 100 * len(s)))
    return s[k - 1]


@dataclass
class LatencyMetrics:
    budgets_ms: dict[str, int] = field(default_factory=dict)
    samples: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    misses: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record(self, name: str, ms: float) -> None:
        self.samples[name].append(ms)
        b = self.budgets_ms.get(name)
        if b is not None and ms > b:
            self.misses[name] += 1

    def summary(self) -> dict[str, dict]:
        out = {}
        for name, v in sorted(self.samples.items()):
            out[name] = {
                "n": len(v),
                "p50": percentile(v, 50),
                "p95": percentile(v, 95),
                "p99": percentile(v, 99),
                "max": max(v) if v else None,
                "budget_ms": self.budgets_ms.get(name),
                "budget_misses": self.misses.get(name, 0),
            }
        return out
