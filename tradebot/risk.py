"""Deterministic pre-trade risk gate. Fail-closed: unknown == reject.

Pure function of (config, context). No I/O, no LLM, no markout data. The
module dependency test asserts this module never imports ``markout``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .config import RiskConfig
from .models import BPS, MarketSnapshot, Quote, TokenSafety
from .portfolio import Portfolio
from .signals import TradeProposal


@dataclass(frozen=True)
class RiskContext:
    now_ms: int
    snapshot: Optional[MarketSnapshot]
    safety: Optional[TokenSafety]
    entry_quote: Optional[Quote]  # executable buy quote for the EXACT proposal size
    portfolio: Portfolio
    halted: bool
    token_paused: bool
    inflight_entry_usd: Decimal
    unresolved_orders_on_token: int
    last_stop_ms: Optional[int]
    est_network_fee_usd: Decimal  # entry + exit gas/priority estimate
    liquidation_values: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    approved: bool
    reasons: tuple[str, ...]
    decision_id: str
    config_hash: str
    usd_size: Decimal = Decimal(0)
    min_tokens_out: Decimal = Decimal(0)
    planned_loss_usd: Decimal = Decimal(0)
    stressed_loss_usd: Decimal = Decimal(0)


def authorize(cfg: RiskConfig, p: TradeProposal, ctx: RiskContext) -> Decision:
    r: list[str] = []
    snap, safety, q = ctx.snapshot, ctx.safety, ctx.entry_quote
    pf = ctx.portfolio

    # ---- global state ------------------------------------------------
    if ctx.halted:
        r.append("HALTED: trading is paused")
    if ctx.token_paused:
        r.append("TOKEN_PAUSED: circuit breaker active for token")
    if ctx.unresolved_orders_on_token:
        r.append("UNRESOLVED_ORDERS: reconcile in-flight/UNKNOWN orders first")
    if ctx.last_stop_ms is not None and ctx.now_ms - ctx.last_stop_ms < cfg.cooldown_after_stop_s * 1000:
        r.append("COOLDOWN: recent stop-out on token")

    # ---- data presence & freshness ----------------------------------
    if snap is None or snap.token != p.token:
        r.append("NO_DATA: no market snapshot")
    else:
        if snap.age_ms(ctx.now_ms) > cfg.max_data_age_ms or snap.age_ms(ctx.now_ms) < -cfg.max_data_age_ms:
            r.append(f"STALE_DATA: age {snap.age_ms(ctx.now_ms)}ms > {cfg.max_data_age_ms}ms")
        if snap.liquidity_usd is None or snap.liquidity_usd < cfg.min_liquidity_usd:
            r.append(f"LOW_LIQUIDITY: {snap.liquidity_usd} < {cfg.min_liquidity_usd}")
        rt = snap.round_trip_bps
        if rt is None or rt > cfg.max_spread_bps:
            r.append(f"WIDE_SPREAD: round-trip {rt} bps > {cfg.max_spread_bps}")
        if snap.priority_fee_lamports is None or snap.priority_fee_lamports > cfg.max_priority_fee_lamports:
            r.append(f"PRIORITY_FEE: {snap.priority_fee_lamports} > {cfg.max_priority_fee_lamports}")

    if q is None or q.token != p.token or q.qty <= 0 or q.usd != p.usd_size:
        r.append("NO_EXEC_QUOTE: need executable quote for the exact size")
    else:
        if ctx.now_ms - q.ts_ms > cfg.max_data_age_ms:
            r.append("STALE_QUOTE")
        if q.price_impact_bps > cfg.max_entry_price_impact_bps:
            r.append(f"PRICE_IMPACT: {q.price_impact_bps} > {cfg.max_entry_price_impact_bps} bps")

    # ---- token safety (meme-coin hazards) ----------------------------
    if safety is None or safety.token != p.token:
        r.append("NO_SAFETY_DATA")
    else:
        if safety.decimals is None:
            r.append("UNKNOWN_DECIMALS")
        if cfg.reject_mint_authority and safety.mint_authority is not False:
            r.append(f"MINT_AUTHORITY: {safety.mint_authority}")
        if cfg.reject_freeze_authority and safety.freeze_authority is not False:
            r.append(f"FREEZE_AUTHORITY: {safety.freeze_authority}")
        if safety.transfer_tax_bps is None or safety.transfer_tax_bps > cfg.max_transfer_tax_bps:
            r.append(f"TRANSFER_TAX: {safety.transfer_tax_bps}")
        if safety.top10_holder_pct is None or safety.top10_holder_pct > cfg.max_top10_holder_pct:
            r.append(f"HOLDER_CONCENTRATION: {safety.top10_holder_pct}")
        if cfg.require_sell_simulation and safety.sell_simulation_ok is not True:
            r.append(f"SELLABILITY_UNVERIFIED: {safety.sell_simulation_ok}")

    # ---- cost vs edge -----------------------------------------------
    size = p.usd_size
    rt_bps = snap.round_trip_bps if snap and snap.round_trip_bps is not None else None
    tax_bps = safety.transfer_tax_bps if safety and safety.transfer_tax_bps is not None else None
    cost_bps: Optional[Decimal] = None
    if rt_bps is not None and tax_bps is not None:
        cost_bps = rt_bps + 2 * tax_bps + ctx.est_network_fee_usd / size * BPS
        if p.expected_edge_bps < cost_bps + cfg.min_edge_buffer_bps:
            r.append(f"EDGE_BELOW_COST: edge {p.expected_edge_bps} < cost {cost_bps:.1f} + buffer {cfg.min_edge_buffer_bps}")

    # ---- sizing: planned vs stressed loss ---------------------------
    cost_frac = (cost_bps / BPS) if cost_bps is not None else Decimal(1)
    planned = size * (cfg.stop_loss_pct + cost_frac)
    stressed = size * min(Decimal(1), cfg.stop_loss_pct * cfg.stress_gap_multiplier + cost_frac)
    if planned > cfg.per_trade_risk_usd:
        r.append(f"PER_TRADE_RISK: planned loss {planned:.2f} > {cfg.per_trade_risk_usd}")
    if stressed > cfg.max_stressed_loss_usd:
        r.append(f"STRESSED_LOSS: {stressed:.2f} > {cfg.max_stressed_loss_usd}")

    # ---- exposure ----------------------------------------------------
    pos = pf.positions.get(p.token)
    pos_cost = pos.cost_usd if pos and pos.is_open else Decimal(0)
    if pos_cost + size > cfg.max_position_usd:
        r.append(f"POSITION_CAP: {pos_cost + size} > {cfg.max_position_usd}")
    total = pf.exposure_usd() + ctx.inflight_entry_usd + size
    if total > cfg.max_total_exposure_usd:
        r.append(f"TOTAL_EXPOSURE: {total} > {cfg.max_total_exposure_usd}")
    if (pos_cost + size) / cfg.max_total_exposure_usd > cfg.max_concentration_pct:
        r.append("CONCENTRATION: token share of total exposure budget too high")
    n_open = len(pf.open_positions()) + (0 if pos_cost > 0 else 1)
    if n_open > cfg.max_open_positions:
        r.append(f"MAX_OPEN_POSITIONS: {n_open} > {cfg.max_open_positions}")
    if pos and pos.is_open and not cfg.allow_average_down:
        bid = snap.bid.price if snap and snap.bid else None
        if bid is None or bid < pos.avg_cost:
            r.append("AVERAGE_DOWN_FORBIDDEN")
    if pos and pos.exit_failed:
        r.append("POSITION_EXIT_FAILED: token has an unprotected position")

    # ---- portfolio limits -------------------------------------------
    today = pf.realized_today(ctx.now_ms)
    remaining = cfg.daily_loss_limit_usd + today  # today is negative when losing
    if remaining <= 0:
        r.append(f"DAILY_LOSS_LIMIT: realized today {today}")
    elif planned > remaining:
        r.append(f"DAILY_LOSS_BUDGET: planned {planned:.2f} > remaining {remaining:.2f}")
    equity = pf.equity(ctx.liquidation_values)
    if pf.drawdown_pct(equity) >= cfg.max_drawdown_pct:
        r.append(f"MAX_DRAWDOWN: {pf.drawdown_pct(equity):.4f}")

    min_out = Decimal(0)
    if q is not None and q.qty > 0:
        min_out = q.qty * (1 - cfg.max_entry_slippage_bps / BPS)

    did = hashlib.sha256(f"{p.proposal_id}|{cfg.config_hash}|{ctx.now_ms}".encode()).hexdigest()[:16]
    return Decision(
        approved=not r,
        reasons=tuple(r),
        decision_id=did,
        config_hash=cfg.config_hash,
        usd_size=size if not r else Decimal(0),
        min_tokens_out=min_out,
        planned_loss_usd=planned,
        stressed_loss_usd=stressed,
    )
