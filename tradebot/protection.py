"""Synthetic stop-loss / take-profit / trailing / max-hold evaluation.

AMM pools have no venue-native stops, so these are SYNTHETIC: they only work
while this process runs and has fresh data. See loss register D6.

Rules:
  * The trigger price defaults to the EXECUTABLE bid for the full position
    size, not an indicative price.
  * The stop is evaluated on the WORSE of price return and net P&L, so a
    favourable accounting basis can never suppress a protective exit.
  * Precedence: STOP > EMERGENCY > TRAIL > MAX_HOLD > TP > SCALE_OUT.
  * Hard profit floor: profit-taking exits (TP, scale-out, trailing stop)
    only fire when the CONSERVATIVE return (worse of price return and net
    P&L) is >= HARD_MIN_PROFIT_EXIT_PCT (30%). The floor never blocks
    protective exits (stop-loss, emergency/vanish, max hold).
  * Exit qty never exceeds confirmed qty minus qty already in flight.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .config import HARD_MIN_PROFIT_EXIT_PCT, RiskConfig
from .models import EXIT_PRECEDENCE, Position, Purpose


@dataclass(frozen=True)
class ExitIntent:
    token: str
    purpose: Purpose
    qty: Decimal
    reason: str
    trigger_price: Optional[Decimal]
    scale_index: Optional[int] = None


def evaluate_exits(
    cfg: RiskConfig,
    pos: Position,
    now_ms: int,
    trigger_price: Optional[Decimal],
    net_liquidation_usd: Optional[Decimal],
    inflight_exit_qty: Decimal,
) -> list[ExitIntent]:
    """Returns intents sorted strongest-first. Does not mutate the position
    (except the trailing high-water mark)."""
    if not pos.is_open:
        return []
    available = pos.qty - inflight_exit_qty
    if available <= 0:
        return []
    out: list[ExitIntent] = []

    # Max hold needs no price data.
    if cfg.max_holding_seconds is not None and pos.opened_ms is not None:
        if now_ms - pos.opened_ms >= cfg.max_holding_seconds * 1000:
            out.append(ExitIntent(pos.token, Purpose.MAX_HOLD, available, "max holding time reached", trigger_price))

    if trigger_price is not None and pos.avg_cost > 0:
        price_ret = trigger_price / pos.avg_cost - 1
        net_ret = (net_liquidation_usd / pos.cost_usd - 1) if (net_liquidation_usd is not None and pos.cost_usd > 0) else None
        worst = min(price_ret, net_ret) if net_ret is not None else price_ret

        if worst <= -cfg.stop_loss_pct:
            out.append(ExitIntent(pos.token, Purpose.STOP_LOSS, available,
                                  f"return {worst:.4f} <= -{cfg.stop_loss_pct}", trigger_price))

        profit_floor_met = worst >= HARD_MIN_PROFIT_EXIT_PCT

        if cfg.trailing_stop_pct is not None:
            hw = max(pos.high_water_price or trigger_price, trigger_price)
            pos.high_water_price = hw
            if profit_floor_met and trigger_price <= hw * (1 - cfg.trailing_stop_pct):
                out.append(ExitIntent(pos.token, Purpose.TRAILING_STOP, available,
                                      f"trail from {hw}", trigger_price))

        target_ret = price_ret if cfg.target_basis == "price_return" else net_ret
        if target_ret is not None and profit_floor_met:
            if target_ret >= cfg.take_profit_pct:
                out.append(ExitIntent(pos.token, Purpose.TAKE_PROFIT, available,
                                      f"{cfg.target_basis} {target_ret:.4f} >= {cfg.take_profit_pct}", trigger_price))
            for i, so in enumerate(cfg.scale_outs):
                if i in pos.scale_outs_done or target_ret < so.trigger_pct:
                    continue
                q = min(pos.initial_qty * so.fraction, available)
                if q > 0:
                    out.append(ExitIntent(pos.token, Purpose.SCALE_OUT, q,
                                          f"scale-out {i} at {so.trigger_pct}", trigger_price, scale_index=i))

    out.sort(key=lambda e: EXIT_PRECEDENCE[e.purpose])
    return out
