"""Retrospective fill grading at +1/+5/+10 s (horizons come from config).

Strictly post-trade diagnostics: results go to the separate ``markouts``
table, and trading modules never import this module (enforced by
tests/test_architecture.py).

Definitions (frozen; part of the config hash via the horizons, tolerance and
threshold):
  signed markout bps = side * (benchmark_{t+h} - fill_price) / fill_price * 1e4
  side = +1 buy, -1 sell.
  Pool benchmark = EXECUTABLE quote for the SAME size at t+h:
    BUY fill  -> per-unit proceeds of selling fill.qty (liquidation value)
    SELL fill -> per-unit cost of re-buying fill.qty
  No midpoint is invented. Round-trip cost is therefore inside the markout,
  which makes it a conservative "could I get out at t+h" measure.
  The benchmark observation must lie in [t+h, t+h+tolerance]. Nothing is
  interpolated. Missing -> UNKNOWN.
"""
from __future__ import annotations

import copy
import statistics
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Callable, Iterable, Optional

from .config import RiskConfig
from .models import BPS, Fill, Purpose, Side
from .state_machine import Order


@dataclass(frozen=True)
class Benchmark:
    price: Decimal
    ts_ms: int
    liquidity_usd: Optional[Decimal]
    kind: str  # e.g. "pool_reserves_exact", "router_quote", "indicative"


BenchmarkFn = Callable[[str, Side, Decimal, int, int], Optional[Benchmark]]


@dataclass
class MarkoutRow:
    fill_id: str
    client_order_id: str
    token: str
    side: str
    venue: str
    strategy: str
    purpose: str
    horizon_s: int
    fill_ts_ms: int
    ts_uncertainty_ms: int
    fill_price: Decimal
    fill_qty: Decimal
    fee_usd: Decimal
    latency_submit_to_fill_ms: Optional[int]
    benchmark_price: Optional[Decimal]
    benchmark_ts_ms: Optional[int]
    benchmark_kind: Optional[str]
    benchmark_liquidity_usd: Optional[Decimal]
    markout_bps: Optional[Decimal]
    est_liquidation_value_usd: Optional[Decimal]
    data_status: str  # OK | UNKNOWN
    confidence: str  # HIGH | LOW | NONE
    execution_grade: str  # OK | ADVERSE | UNKNOWN (vs frozen threshold)
    rule_compliance: str  # COMPLIANT | VIOLATION
    violations: list[str] = field(default_factory=list)
    stop_deviation_bps: Optional[Decimal] = None
    config_hash: str = ""


def check_compliance(order: Order, fill: Fill, approved_decisions: set[str]) -> list[str]:
    v: list[str] = []
    if order.purpose is Purpose.ENTRY:
        if not order.decision_id or order.decision_id not in approved_decisions:
            v.append("ENTRY_WITHOUT_APPROVED_DECISION")
        if fill.qty < order.min_out:
            v.append("BUY_BELOW_MIN_OUT")
    else:
        if fill.side is not Side.SELL:
            v.append("EXIT_NOT_SELL")
        if fill.notional < order.min_out:
            v.append("SELL_BELOW_MIN_OUT")
    return v


def grade_fill(cfg: RiskConfig, fill: Fill, order: Order, bench: BenchmarkFn,
               approved_decisions: set[str], trigger_price: Optional[Decimal] = None) -> list[MarkoutRow]:
    violations = check_compliance(order, fill, approved_decisions)
    stop_dev = None
    if trigger_price and order.purpose in (Purpose.STOP_LOSS, Purpose.TRAILING_STOP, Purpose.EMERGENCY):
        stop_dev = (fill.price - trigger_price) / trigger_price * BPS  # negative = worse than trigger
    rows = []
    for h in cfg.markout_horizons_s:
        target = fill.ts_exec_ms + h * 1000
        b = bench(fill.token, fill.side, fill.qty, target, cfg.markout_benchmark_tolerance_ms)
        if b is not None and not (target <= b.ts_ms <= target + cfg.markout_benchmark_tolerance_ms):
            b = None  # defensive: never accept an out-of-window observation
        mk = None
        if b is not None:
            mk = fill.side.sign * (b.price - fill.price) / fill.price * BPS
        if mk is None:
            conf, grade = "NONE", "UNKNOWN"
        else:
            conf = "LOW" if (fill.ts_uncertainty_ms > h * 1000 // 2 or b.kind == "indicative") else "HIGH"
            grade = "ADVERSE" if mk <= -cfg.markout_adverse_threshold_bps else "OK"
        rows.append(MarkoutRow(
            fill_id=fill.fill_id, client_order_id=fill.client_order_id, token=fill.token, side=fill.side.name,
            venue=fill.venue, strategy=fill.strategy, purpose=order.purpose.name, horizon_s=h,
            fill_ts_ms=fill.ts_exec_ms, ts_uncertainty_ms=fill.ts_uncertainty_ms, fill_price=fill.price,
            fill_qty=fill.qty, fee_usd=fill.fee_usd,
            latency_submit_to_fill_ms=(fill.ts_exec_ms - order.submitted_ms) if order.submitted_ms else None,
            benchmark_price=b.price if b else None, benchmark_ts_ms=b.ts_ms if b else None,
            benchmark_kind=b.kind if b else None, benchmark_liquidity_usd=b.liquidity_usd if b else None,
            markout_bps=mk, est_liquidation_value_usd=(b.price * fill.qty if b and fill.side is Side.BUY else None),
            data_status="OK" if b else "UNKNOWN", confidence=conf, execution_grade=grade,
            rule_compliance="VIOLATION" if violations else "COMPLIANT", violations=violations,
            stop_deviation_bps=stop_dev, config_hash=cfg.config_hash,
        ))
    return rows


def summarize(rows: Iterable[MarkoutRow]) -> dict:
    by_h: dict[int, list[MarkoutRow]] = {}
    for r in rows:
        by_h.setdefault(r.horizon_s, []).append(r)
    out = {}
    for h, rs in sorted(by_h.items()):
        known = [float(r.markout_bps) for r in rs if r.markout_bps is not None]
        out[f"+{h}s"] = {
            "fills": len(rs),
            "unknown": sum(r.data_status == "UNKNOWN" for r in rs),
            "mean_bps": round(statistics.fmean(known), 2) if known else None,
            "median_bps": round(statistics.median(known), 2) if known else None,
            "worst_bps": round(min(known), 2) if known else None,
            "adverse": sum(r.execution_grade == "ADVERSE" for r in rs),
            "violations": sum(r.rule_compliance == "VIOLATION" for r in rs),
        }
    return out


def summarize_by_purpose(rows: Iterable[MarkoutRow]) -> dict:
    """Separates entry execution quality from exit quality (e.g. stops filled into a falling market)."""
    groups: dict[str, list[MarkoutRow]] = {}
    for r in rows:
        groups.setdefault(r.purpose, []).append(r)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def row_to_dict(r: MarkoutRow) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(r).items()}


class PoolBenchmarkRecorder:
    """Paper-mode benchmark: snapshots of pool reserves over time, so the exact
    executable price for ANY size can be computed afterwards. Live mode would
    instead record router quotes for a ladder of sizes."""

    def __init__(self, venue):
        self.venue = venue
        self.series: dict[str, list[tuple[int, object]]] = {}

    def record(self) -> None:
        now = self.venue.clock.now_ms()
        for t, p in self.venue.pools.items():
            self.series.setdefault(t, []).append((now, copy.copy(p)))

    def drop(self, token: str, start_ms: int, end_ms: int) -> None:
        """Simulate a data outage (observations missing)."""
        self.series[token] = [(ts, p) for ts, p in self.series.get(token, []) if not start_ms <= ts <= end_ms]

    def __call__(self, token: str, side: Side, qty: Decimal, target_ms: int, tol_ms: int) -> Optional[Benchmark]:
        for ts, pool in self.series.get(token, []):
            if target_ms <= ts <= target_ms + tol_ms:
                if side is Side.BUY:  # liquidation value of what we bought
                    usd, _ = pool.sell_out(qty)
                    price = usd / qty
                else:  # cost to re-buy what we sold
                    lo, hi = Decimal(0), qty * pool.spot * 10
                    for _ in range(60):  # invert buy_out by bisection
                        mid = (lo + hi) / 2
                        out, _ = pool.buy_out(mid)
                        lo, hi = (mid, hi) if out < qty else (lo, mid)
                    price = hi / qty
                return Benchmark(price, ts, pool.usd_reserve * 2, "pool_reserves_exact")
        return None
