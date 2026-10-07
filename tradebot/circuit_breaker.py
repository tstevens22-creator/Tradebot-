"""'Vanish from the book': measurable, configurable market-invalidation triggers.

Triggers (per token, over a rolling ``vanish_window_s`` window):
  ADVERSE_MOVE      executable bid fell >= vanish_adverse_move_pct from window high
  SPREAD_EXPANSION  round-trip quote cost >= vanish_spread_bps
  LIQUIDITY_COLLAPSE liquidity fell >= vanish_liquidity_drop_pct from window high
  VOLATILITY        (max-min)/min of bid over window >= vanish_volatility_bps
  STALE_DATA        snapshot older than max_data_age_ms, or no executable bid
  SIGNAL_INVALIDATED explicit invalidation from the strategy

A trip latches. Recovery requires ``recovery_healthy_observations``
consecutive healthy snapshots AND, if configured, a manual operator ack.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .config import RiskConfig
from .models import BPS, MarketSnapshot


@dataclass
class _TokenState:
    hist: deque = field(default_factory=deque)  # (ts, bid_price, liquidity)
    tripped: bool = False
    triggers: tuple[str, ...] = ()
    tripped_ms: Optional[int] = None
    healthy_streak: int = 0
    acked: bool = False


class CircuitBreaker:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.tokens: dict[str, _TokenState] = {}

    def state(self, token: str) -> _TokenState:
        return self.tokens.setdefault(token, _TokenState())

    def check(self, snap: Optional[MarketSnapshot], token: str, now_ms: int) -> list[str]:
        c = self.cfg
        trig: list[str] = []
        st = self.state(token)
        if snap is None or snap.bid is None or abs(snap.age_ms(now_ms)) > c.max_data_age_ms:
            return ["STALE_DATA"]
        bid = snap.bid.price
        liq = snap.liquidity_usd
        st.hist.append((snap.ts_source_ms, bid, liq))
        horizon = now_ms - c.vanish_window_s * 1000
        while st.hist and st.hist[0][0] < horizon:
            st.hist.popleft()
        bids = [b for _, b, _ in st.hist]
        hi, lo = max(bids), min(bids)
        if hi > 0 and (hi - bid) / hi >= c.vanish_adverse_move_pct:
            trig.append("ADVERSE_MOVE")
        if lo > 0 and (hi - lo) / lo * BPS >= c.vanish_volatility_bps:
            trig.append("VOLATILITY")
        rt = snap.round_trip_bps
        if rt is None or rt >= c.vanish_spread_bps:
            trig.append("SPREAD_EXPANSION")
        liqs = [x for _, _, x in st.hist if x is not None]
        if liq is None:
            trig.append("STALE_DATA")
        elif liqs and max(liqs) > 0 and (max(liqs) - liq) / max(liqs) >= c.vanish_liquidity_drop_pct:
            trig.append("LIQUIDITY_COLLAPSE")
        return trig

    def observe(self, snap: Optional[MarketSnapshot], token: str, now_ms: int) -> list[str]:
        """Returns NEW triggers (empty if healthy or already tripped)."""
        trig = self.check(snap, token, now_ms)
        st = self.state(token)
        if trig:
            st.healthy_streak = 0
            if not st.tripped:
                st.tripped, st.triggers, st.tripped_ms, st.acked = True, tuple(trig), now_ms, False
                return trig
            return []
        if st.tripped:
            st.healthy_streak += 1
            if st.healthy_streak >= self.cfg.recovery_healthy_observations and (
                st.acked or not self.cfg.recovery_requires_manual_ack
            ):
                st.tripped, st.triggers = False, ()
                st.hist.clear()  # fresh baseline after recovery
        return []

    def invalidate_signal(self, token: str, now_ms: int) -> None:
        st = self.state(token)
        st.tripped, st.triggers, st.tripped_ms, st.acked, st.healthy_streak = (
            True, ("SIGNAL_INVALIDATED",), now_ms, False, 0)

    def ack(self, token: str) -> None:
        self.state(token).acked = True

    def is_tripped(self, token: str) -> bool:
        return self.state(token).tripped

    def any_tripped(self) -> bool:
        return any(s.tripped for s in self.tokens.values())
