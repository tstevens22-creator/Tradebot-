"""Deterministic simulation harness (paper mode only, isolated)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from .bot import Bot
from .clock import SimClock
from .config import RiskConfig, from_dict
from .journal import Journal
from .markout import PoolBenchmarkRecorder
from .models import MarketSnapshot, Quote, Side, TokenSafety
from .venues.paper_dex import PaperDexVenue, Pool

TOKEN = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

PAPER_CONFIG: dict[str, Any] = {
    "mode": "paper", "live_trading_authorized": False,
    "stop_loss_pct": "0.08", "take_profit_pct": "0.20", "trigger_price_source": "executable_bid",
    "target_basis": "net_pnl", "trailing_stop_pct": "0.10", "max_holding_seconds": 3600,
    "scale_outs": [["0.10", "0.5"]],
    "per_trade_risk_usd": "20", "max_position_usd": "200", "max_total_exposure_usd": "600",
    "max_concentration_pct": "0.5", "max_open_positions": 5, "stress_gap_multiplier": "3",
    "max_stressed_loss_usd": "60", "allow_average_down": False,
    "daily_loss_limit_usd": "60", "max_drawdown_pct": "0.15", "starting_equity_usd": "1000",
    "min_liquidity_usd": "50000", "max_spread_bps": "300", "max_entry_slippage_bps": "100",
    "max_entry_price_impact_bps": "150", "max_data_age_ms": 3000, "min_edge_buffer_bps": "50",
    "reject_mint_authority": True, "reject_freeze_authority": True, "max_transfer_tax_bps": "0",
    "max_top10_holder_pct": "0.5", "require_sell_simulation": True,
    "max_priority_fee_lamports": 100_000, "emergency_max_priority_fee_lamports": 1_000_000,
    "max_exit_attempts": 4, "exit_retry_backoff_ms": 500, "exit_failed_probe_interval_s": 30, "exit_slippage_ladder_bps": [100, 300, 600, 1000],
    "emergency_max_slippage_bps": "1500", "unknown_resolution_timeout_s": 90, "cooldown_after_stop_s": 300,
    "vanish_adverse_move_pct": "0.15", "vanish_window_s": 60, "vanish_spread_bps": "800",
    "vanish_liquidity_drop_pct": "0.4", "vanish_volatility_bps": "3000", "vanish_emergency_policy": "exit_all",
    "recovery_healthy_observations": 5, "recovery_requires_manual_ack": True,
    "budget_detect_to_decision_ms": 250, "budget_decision_to_submit_ms": 250, "budget_exit_confirm_ms": 5000,
    "markout_horizons_s": [1, 5, 10], "markout_benchmark_tolerance_ms": 1000, "markout_adverse_threshold_bps": "50",
}


def paper_config(**overrides: Any) -> RiskConfig:
    d = dict(PAPER_CONFIG)
    d.update(overrides)
    return from_dict(d)


def safe_token(token: str, ts: int, **kw: Any) -> TokenSafety:
    base = dict(token=token, decimals=6, mint_authority=False, freeze_authority=False,
                transfer_tax_bps=Decimal(0), top10_holder_pct=Decimal("0.2"), sell_simulation_ok=True, ts_ms=ts)
    base.update(kw)
    return TokenSafety(**base)


class Sim:
    REF_USD = Decimal(100)

    def __init__(self, cfg: Optional[RiskConfig] = None, seed: int = 0, journal_path: str = ":memory:",
                 token_reserve: Decimal = Decimal(1_000_000), usd_reserve: Decimal = Decimal(100_000),
                 decision_delay_ms: int = 5):
        self.cfg = cfg or paper_config()
        self.clock = SimClock()
        self.venue = PaperDexVenue(self.clock, seed=seed)
        self.venue.add_pool(TOKEN, Pool(token_reserve, usd_reserve))
        self.journal_path = journal_path
        self.journal = Journal(journal_path, self.cfg.config_hash)
        self.bot = Bot(self.cfg, self.venue, self.journal, self.clock)
        self.recorder = PoolBenchmarkRecorder(self.venue)
        self.decision_delay_ms = decision_delay_ms
        self.safety_overrides: dict[str, Any] = {}
        self.feed_up = True

    def snapshot(self, token: str = TOKEN) -> MarketSnapshot:
        p = self.venue.pools[token]
        now = self.clock.now_ms()
        out, _ = p.buy_out(self.REF_USD)
        ask = Quote(token, Side.BUY, out, self.REF_USD, (self.REF_USD / out / p.spot - 1) * 10_000, now)
        usd, _ = p.sell_out(out)
        bid = Quote(token, Side.SELL, out, usd, (1 - usd / out / p.spot) * 10_000, now)
        return MarketSnapshot(token, now, now, p.usd_reserve * 2, p.spot, bid, ask,
                              priority_fee_lamports=self.venue.faults.priority_fee_lamports)

    def step(self, ms: int = 1000, token: str = TOKEN) -> None:
        self.clock.advance(ms)
        self.venue.process()
        if self.feed_up:
            self.bot.ingest(self.snapshot(token), safe_token(token, self.clock.now_ms(), **self.safety_overrides))
        self.recorder.record()
        self.clock.advance(self.decision_delay_ms)
        self.bot.tick()

    def run(self, seconds: int, token: str = TOKEN) -> None:
        for _ in range(seconds):
            self.step(1000, token)

    _n = 0

    def propose(self, usd: str = "100", edge: str = "500", token: str = TOKEN, **extra: Any):
        Sim._n += 1
        raw = {"proposal_id": f"p{Sim._n}", "token": token, "usd_size": usd, "expected_edge_bps": edge}
        raw.update(extra)
        return self.bot.propose(raw)

    def restart(self) -> Bot:
        """Simulate a crash + restart: in-memory state discarded, rebuilt from journal + venue."""
        if self.journal_path == ":memory:":
            j = self.journal  # same DB handle stands in for the persisted file
        else:
            self.journal.close()
            j = Journal(self.journal_path, self.cfg.config_hash)
            self.journal = j
        self.bot = Bot.recover(self.cfg, self.venue, j, self.clock)
        return self.bot

    def position(self, token: str = TOKEN):
        return self.bot.portfolio.positions.get(token)
