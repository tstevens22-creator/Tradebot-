"""Position and P&L accounting from ACTUAL fills, net of fees and taxes."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional

from .models import Fill, Position, Side


class InvariantViolation(RuntimeError):
    pass


def utc_day(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Portfolio:
    starting_equity_usd: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    realized_by_day: dict[str, Decimal] = field(default_factory=dict)
    total_realized_usd: Decimal = Decimal(0)
    total_costs_usd: Decimal = Decimal(0)
    peak_equity_usd: Optional[Decimal] = None
    applied_fill_ids: set[str] = field(default_factory=set)

    def position(self, token: str) -> Position:
        return self.positions.setdefault(token, Position(token=token))

    def apply_fill(self, f: Fill) -> bool:
        """Idempotent and order-independent for buys. Returns False on duplicates."""
        if f.fill_id in self.applied_fill_ids:
            return False
        if f.qty <= 0 or f.price <= 0:
            raise InvariantViolation(f"non-positive fill {f.fill_id}")
        p = self.position(f.token)
        costs = f.fee_usd  # tax/LP fee are already embedded in the all-in fill price
        self.total_costs_usd += costs
        if f.side is Side.BUY:
            if not p.is_open:
                p.opened_ms = f.ts_exec_ms
                p.initial_qty = Decimal(0)
                p.scale_outs_done = set()
                p.high_water_price = f.price
                p.exit_failed = False
            p.qty += f.qty
            p.initial_qty += f.qty
            p.cost_usd += f.notional + costs
            p.entry_price = p.avg_cost
            p.high_water_price = max(p.high_water_price or f.price, f.price)
        else:
            if f.qty > p.qty:
                raise InvariantViolation(
                    f"oversell on {f.token}: sell {f.qty} > confirmed {p.qty} (fill {f.fill_id})"
                )
            cost_part = p.cost_usd * f.qty / p.qty
            pnl = f.notional - costs - cost_part
            p.qty -= f.qty
            p.cost_usd -= cost_part
            p.realized_pnl_usd += pnl
            self.total_realized_usd += pnl
            day = utc_day(f.ts_exec_ms)
            self.realized_by_day[day] = self.realized_by_day.get(day, Decimal(0)) + pnl
            if p.qty == 0:
                p.cost_usd = Decimal(0)
                p.opened_ms = None
        self.applied_fill_ids.add(f.fill_id)
        return True

    # ------------------------------------------------------------------
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    def exposure_usd(self) -> Decimal:
        return sum((p.cost_usd for p in self.open_positions()), Decimal(0))

    def realized_today(self, now_ms: int) -> Decimal:
        return self.realized_by_day.get(utc_day(now_ms), Decimal(0))

    def equity(self, liquidation_values: dict[str, Decimal]) -> Decimal:
        """Equity with open positions marked at ESTIMATED EXECUTABLE liquidation
        value, net of estimated exit costs. A missing mark counts as zero
        (conservative)."""
        unreal = Decimal(0)
        for p in self.open_positions():
            unreal += liquidation_values.get(p.token, Decimal(0)) - p.cost_usd
        return self.starting_equity_usd + self.total_realized_usd + unreal

    def update_peak(self, equity: Decimal) -> None:
        if self.peak_equity_usd is None or equity > self.peak_equity_usd:
            self.peak_equity_usd = equity

    def drawdown_pct(self, equity: Decimal) -> Decimal:
        peak = max(self.peak_equity_usd or self.starting_equity_usd, self.starting_equity_usd)
        return (peak - equity) / peak if peak > 0 else Decimal(0)

    @classmethod
    def from_fills(cls, starting_equity_usd: Decimal, fills: Iterable[Fill]) -> "Portfolio":
        pf = cls(starting_equity_usd=starting_equity_usd)
        for f in sorted(fills, key=lambda x: (x.ts_exec_ms, x.side is Side.SELL, x.fill_id)):
            pf.apply_fill(f)
        return pf
