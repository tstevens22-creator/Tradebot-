"""Explicit, immutable risk configuration.

Every risk field is REQUIRED. There are no silent defaults: a missing key is a
ConfigError, and the loader reports all missing keys in one batch. Optional
features (trailing stop, max hold) must still be present, with ``null`` meaning
"disabled".

The config is frozen. Its hash is stamped on every journal event, so the
risk regime behind each decision can be traced. Nothing at runtime -- the AI
included -- can mutate it.
"""
from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Any, Optional

from .chains import CHAINS

MODES = ("paper", "shadow", "live")
TRIGGER_SOURCES = ("executable_bid", "indicative")
TARGET_BASES = ("price_return", "net_pnl")
VANISH_POLICIES = ("exit_all", "exit_if_loss", "hold_protected")

# ---------------------------------------------------------------------------
# OWNER-MANDATED HARD LIMITS (2026-10-07). These are code constants, not config:
# no config file, AI proposal or runtime path can loosen them. Changing them
# needs a code change and review.
#   * Losses are cut at 10%: the stop may be tighter than 10%, never wider.
#   * Profit is never taken below +30%: take-profit, scale-outs and trailing
#     stops cannot sell until the position is up at least 30%.
HARD_MAX_STOP_LOSS_PCT = Decimal("0.10")
HARD_MIN_PROFIT_EXIT_PCT = Decimal("0.30")

# Fields that are optional *features* (null = disabled) but must still be explicit.
NULLABLE = {"trailing_stop_pct", "max_holding_seconds", "exit_failed_probe_interval_s"}

SECRET_HINTS = ("key", "secret", "private", "mnemonic", "seed", "password", "token_")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ScaleOut:
    trigger_pct: Decimal  # price return at which to scale out
    fraction: Decimal  # fraction of the ORIGINAL position to sell


@dataclass(frozen=True)
class RiskConfig:
    # --- mode -----------------------------------------------------------
    mode: str
    live_trading_authorized: bool

    # --- chains ---------------------------------------------------------
    enabled_chains: tuple[str, ...]

    # --- stop / target --------------------------------------------------
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    trigger_price_source: str
    target_basis: str
    trailing_stop_pct: Optional[Decimal]
    max_holding_seconds: Optional[int]
    scale_outs: tuple[ScaleOut, ...]

    # --- sizing / exposure ---------------------------------------------
    per_trade_risk_usd: Decimal
    max_position_usd: Decimal
    max_total_exposure_usd: Decimal
    max_concentration_pct: Decimal
    max_open_positions: int
    stress_gap_multiplier: Decimal
    max_stressed_loss_usd: Decimal
    allow_average_down: bool

    # --- portfolio ------------------------------------------------------
    daily_loss_limit_usd: Decimal
    max_drawdown_pct: Decimal
    starting_equity_usd: Decimal

    # --- market quality -------------------------------------------------
    min_liquidity_usd: Decimal
    max_spread_bps: Decimal
    max_entry_slippage_bps: Decimal
    max_entry_price_impact_bps: Decimal
    max_data_age_ms: int
    min_edge_buffer_bps: Decimal

    # --- token safety ---------------------------------------------------
    reject_mint_authority: bool
    reject_freeze_authority: bool
    max_transfer_tax_bps: Decimal
    max_top10_holder_pct: Decimal
    require_sell_simulation: bool
    require_creator_history: bool  # unknown creator history -> reject
    creator_max_tokens_created: int  # more launches than this = serial launcher -> reject
    creator_min_graduation_ratio: Decimal  # migrated/created below this -> reject
    feed_max_age_ms: int  # launchpad/creator feed readings older than this are unknown

    # --- chain fees -----------------------------------------------------
    max_priority_fee_lamports: int
    emergency_max_priority_fee_lamports: int

    # --- exit escalation ------------------------------------------------
    max_exit_attempts: int
    exit_retry_backoff_ms: int  # wait backoff * 2^(n-1) after the n-th failed exit attempt
    exit_failed_probe_interval_s: Optional[int]  # after exhaustion: one capped-slippage probe per interval; null = operator only
    exit_slippage_ladder_bps: tuple[Decimal, ...]
    emergency_max_slippage_bps: Decimal
    unknown_resolution_timeout_s: int
    cooldown_after_stop_s: int

    # --- "vanish" circuit breaker ---------------------------------------
    vanish_adverse_move_pct: Decimal
    vanish_window_s: int
    vanish_spread_bps: Decimal
    vanish_liquidity_drop_pct: Decimal
    vanish_volatility_bps: Decimal
    vanish_emergency_policy: str
    recovery_healthy_observations: int
    recovery_requires_manual_ack: bool

    # --- latency budgets ------------------------------------------------
    budget_detect_to_decision_ms: int
    budget_decision_to_submit_ms: int
    budget_exit_confirm_ms: int

    # --- markout grading (frozen before evaluation) ---------------------
    markout_horizons_s: tuple[int, ...]
    markout_benchmark_tolerance_ms: int
    markout_adverse_threshold_bps: Decimal

    # ------------------------------------------------------------------
    @property
    def config_hash(self) -> str:
        return hashlib.sha256(_canonical(self).encode()).hexdigest()[:16]

    def __post_init__(self) -> None:
        _validate(self)


# ---------------------------------------------------------------------------
def _canonical(cfg: RiskConfig) -> str:
    def enc(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, ScaleOut):
            return [str(v.trigger_pct), str(v.fraction)]
        if isinstance(v, tuple):
            return [enc(x) for x in v]
        return v

    return json.dumps({f.name: enc(getattr(cfg, f.name)) for f in fields(cfg)}, sort_keys=True)


def _validate(c: RiskConfig) -> None:
    errs: list[str] = []

    def req(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    req(c.mode in MODES, f"mode must be one of {MODES}")
    req(len(c.enabled_chains) > 0 and all(ch in CHAINS for ch in c.enabled_chains)
        and len(set(c.enabled_chains)) == len(c.enabled_chains),
        f"enabled_chains must be a non-empty list drawn from {CHAINS}")
    req(c.trigger_price_source in TRIGGER_SOURCES, f"trigger_price_source must be one of {TRIGGER_SOURCES}")
    req(c.target_basis in TARGET_BASES, f"target_basis must be one of {TARGET_BASES}")
    req(c.vanish_emergency_policy in VANISH_POLICIES, f"vanish_emergency_policy must be one of {VANISH_POLICIES}")

    pos = [
        "stop_loss_pct", "take_profit_pct", "per_trade_risk_usd", "max_position_usd",
        "max_total_exposure_usd", "max_concentration_pct", "stress_gap_multiplier",
        "max_stressed_loss_usd", "daily_loss_limit_usd", "max_drawdown_pct",
        "starting_equity_usd", "min_liquidity_usd", "max_spread_bps",
        "max_entry_slippage_bps", "max_entry_price_impact_bps", "max_data_age_ms",
        "max_exit_attempts", "emergency_max_slippage_bps", "vanish_adverse_move_pct",
        "vanish_window_s", "vanish_spread_bps", "vanish_liquidity_drop_pct",
        "vanish_volatility_bps", "recovery_healthy_observations", "max_open_positions",
        "unknown_resolution_timeout_s", "markout_benchmark_tolerance_ms",
        "creator_max_tokens_created", "feed_max_age_ms",
    ]
    for name in pos:
        req(getattr(c, name) > 0, f"{name} must be > 0")

    req(c.stop_loss_pct <= HARD_MAX_STOP_LOSS_PCT,
        f"stop_loss_pct must be <= {HARD_MAX_STOP_LOSS_PCT} (hard limit: losses cut at 10%)")
    req(c.take_profit_pct >= HARD_MIN_PROFIT_EXIT_PCT,
        f"take_profit_pct must be >= {HARD_MIN_PROFIT_EXIT_PCT} (hard limit: no profit-taking below 30%)")
    req(all(s.trigger_pct >= HARD_MIN_PROFIT_EXIT_PCT for s in c.scale_outs),
        f"scale_out triggers must be >= {HARD_MIN_PROFIT_EXIT_PCT} (hard limit: no profit-taking below 30%)")
    req(c.stress_gap_multiplier >= 1, "stress_gap_multiplier must be >= 1")
    req(c.max_concentration_pct <= 1, "max_concentration_pct is a fraction <= 1")
    req(0 <= c.creator_min_graduation_ratio <= 1, "creator_min_graduation_ratio is a fraction in [0,1]")
    req(c.max_drawdown_pct < 1, "max_drawdown_pct is a fraction < 1")
    if c.trailing_stop_pct is not None:
        req(0 < c.trailing_stop_pct < 1, "trailing_stop_pct must be in (0,1) or null")
    if c.exit_failed_probe_interval_s is not None:
        req(c.exit_failed_probe_interval_s > 0, "exit_failed_probe_interval_s must be > 0 or null")
    req(c.exit_retry_backoff_ms >= 0, "exit_retry_backoff_ms must be >= 0")
    if c.max_holding_seconds is not None:
        req(c.max_holding_seconds > 0, "max_holding_seconds must be > 0 or null")

    # Conflicting-rule checks.
    req(c.max_position_usd <= c.max_total_exposure_usd, "max_position_usd must be <= max_total_exposure_usd")
    req(c.per_trade_risk_usd <= c.daily_loss_limit_usd, "per_trade_risk_usd must be <= daily_loss_limit_usd")
    req(c.max_stressed_loss_usd >= c.per_trade_risk_usd, "max_stressed_loss_usd must be >= per_trade_risk_usd")
    req(c.emergency_max_priority_fee_lamports >= c.max_priority_fee_lamports,
        "emergency priority fee cap must be >= normal cap")

    ladder = c.exit_slippage_ladder_bps
    req(len(ladder) >= 1, "exit_slippage_ladder_bps must have >= 1 step")
    req(list(ladder) == sorted(ladder), "exit_slippage_ladder_bps must be non-decreasing")
    req(all(x <= c.emergency_max_slippage_bps for x in ladder),
        "every exit ladder step must be <= emergency_max_slippage_bps (no silent relaxation)")

    total = sum((s.fraction for s in c.scale_outs), Decimal(0))
    req(total <= 1, "scale_out fractions must sum to <= 1")
    req(all(s.trigger_pct > 0 and 0 < s.fraction <= 1 for s in c.scale_outs), "scale_out values out of range")
    req(all(s.trigger_pct < c.take_profit_pct for s in c.scale_outs),
        "scale_out triggers must be below take_profit_pct")

    req(tuple(sorted(c.markout_horizons_s)) == c.markout_horizons_s and len(c.markout_horizons_s) > 0,
        "markout_horizons_s must be sorted and non-empty")

    if c.mode == "live":
        req(c.live_trading_authorized, "mode=live requires live_trading_authorized=true")
        req(c.trigger_price_source == "executable_bid",
            "live mode forbids indicative trigger prices (must use executable_bid)")

    if errs:
        raise ConfigError("Invalid risk config:\n  - " + "\n  - ".join(errs))


# ---------------------------------------------------------------------------
_DECIMAL_FIELDS = {f.name for f in fields(RiskConfig) if "Decimal" in str(f.type)}
_BOOL_FIELDS = {f.name for f in fields(RiskConfig) if str(f.type) == "bool"}
_INT_FIELDS = {f.name for f in fields(RiskConfig) if str(f.type) in ("int", "Optional[int]")}


def from_dict(raw: dict[str, Any]) -> RiskConfig:
    names = [f.name for f in fields(RiskConfig)]
    missing = [n for n in names if n not in raw]
    unknown = [k for k in raw if k not in names]
    secrets = [k for k in raw if any(h in k.lower() for h in SECRET_HINTS)]
    problems = []
    if missing:
        problems.append("missing required settings: " + ", ".join(missing))
    if unknown:
        problems.append("unknown settings (typo?): " + ", ".join(unknown))
    if secrets:
        problems.append("secrets must come from the environment, never config: " + ", ".join(secrets))
    if problems:
        raise ConfigError("\n".join(problems))

    vals: dict[str, Any] = {}
    for n in names:
        v = raw[n]
        if v is None:
            if n not in NULLABLE:
                raise ConfigError(f"{n} may not be null")
            vals[n] = None
        elif n == "scale_outs":
            vals[n] = tuple(ScaleOut(Decimal(str(t)), Decimal(str(f))) for t, f in v)
        elif n == "exit_slippage_ladder_bps":
            vals[n] = tuple(Decimal(str(x)) for x in v)
        elif n == "enabled_chains":
            vals[n] = tuple(str(x) for x in v)
        elif n == "markout_horizons_s":
            vals[n] = tuple(int(x) for x in v)
        elif n in _DECIMAL_FIELDS:
            if isinstance(v, float):
                v = repr(v)
            vals[n] = Decimal(str(v))
        elif n in _BOOL_FIELDS:
            if not isinstance(v, bool):
                raise ConfigError(f"{n} must be true/false")
            vals[n] = v
        elif n in _INT_FIELDS:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(f"{n} must be an integer")
            vals[n] = v
        else:
            vals[n] = v
    return RiskConfig(**vals)


def load(path: str) -> RiskConfig:
    with open(path, "rb") as fh:
        return from_dict(tomllib.load(fh))


LIVE_ACK_ENV = "TRADEBOT_LIVE_ACK"


def assert_live_allowed(cfg: RiskConfig) -> None:
    """Live trading requires config authorization AND an operator env ack of
    the exact config hash. No live venue adapter exists yet, so this always
    ends in refusal: the gate is in place for when one is added."""
    if cfg.mode != "live":
        return
    if os.environ.get(LIVE_ACK_ENV) != cfg.config_hash:
        raise ConfigError(f"live mode requires {LIVE_ACK_ENV}={cfg.config_hash}")
    raise ConfigError("no live venue adapter is implemented; live trading is unavailable")
