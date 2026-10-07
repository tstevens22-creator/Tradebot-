"""Core value types. All money and quantities are Decimal -- never float."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum
from typing import Optional

BPS = Decimal(10_000)


class Side(Enum):
    BUY = 1
    SELL = -1

    @property
    def sign(self) -> int:
        return self.value


class Purpose(Enum):
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    SCALE_OUT = "SCALE_OUT"
    TRAILING_STOP = "TRAILING_STOP"
    MAX_HOLD = "MAX_HOLD"
    EMERGENCY = "EMERGENCY"

    @property
    def is_exit(self) -> bool:
        return self is not Purpose.ENTRY

    @property
    def is_protective(self) -> bool:
        return self in (Purpose.STOP_LOSS, Purpose.TRAILING_STOP, Purpose.EMERGENCY, Purpose.MAX_HOLD)


# Precedence when several exit rules fire on the same tick. Lower = stronger.
# A profit target can never pre-empt a protective exit.
EXIT_PRECEDENCE = {
    Purpose.STOP_LOSS: 0,
    Purpose.EMERGENCY: 1,
    Purpose.TRAILING_STOP: 2,
    Purpose.MAX_HOLD: 3,
    Purpose.TAKE_PROFIT: 4,
    Purpose.SCALE_OUT: 5,
}


def to_atomic(qty: Decimal, decimals: int) -> int:
    """Token amount -> integer base units, rounding DOWN (never oversell)."""
    if decimals is None or not (0 <= decimals <= 18):
        raise ValueError(f"invalid token decimals: {decimals!r}")
    return int((qty * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def from_atomic(units: int, decimals: int) -> Decimal:
    if decimals is None or not (0 <= decimals <= 18):
        raise ValueError(f"invalid token decimals: {decimals!r}")
    return Decimal(units) / (Decimal(10) ** decimals)


@dataclass(frozen=True)
class Quote:
    """Executable quote for a specific size. For pools this comes from the
    router/pool maths -- it is NOT a midpoint."""
    token: str
    side: Side  # side WE would trade
    qty: Decimal  # tokens
    usd: Decimal  # USD paid (buy) or received (sell), before network fees
    price_impact_bps: Decimal
    ts_ms: int

    @property
    def price(self) -> Decimal:
        return self.usd / self.qty if self.qty else Decimal(0)


@dataclass(frozen=True)
class TokenSafety:
    """None means UNKNOWN; the risk gate treats UNKNOWN as unsafe."""
    token: str
    decimals: Optional[int]
    mint_authority: Optional[bool]
    freeze_authority: Optional[bool]
    transfer_tax_bps: Optional[Decimal]
    top10_holder_pct: Optional[Decimal]
    sell_simulation_ok: Optional[bool]
    ts_ms: int
    chain: Optional[str] = None  # "solana" | "base"; None = unknown -> reject
    venue_stage: Optional[str] = None  # chains.STAGES; None = unknown -> reject


@dataclass(frozen=True)
class MarketSnapshot:
    token: str
    ts_source_ms: int  # when the source says the data is from
    ts_received_ms: int
    liquidity_usd: Optional[Decimal]
    indicative_price: Optional[Decimal]  # e.g. Birdeye -- NOT executable
    bid: Optional[Quote]  # executable sell quote for the reference size
    ask: Optional[Quote]  # executable buy quote for the reference size
    priority_fee_lamports: Optional[int] = None

    def age_ms(self, now_ms: int) -> int:
        return now_ms - self.ts_source_ms

    @property
    def round_trip_bps(self) -> Optional[Decimal]:
        """Pool 'spread': cost of buying then immediately selling the reference
        size, from executable quotes. Documented proxy; no midpoint invented."""
        if not self.bid or not self.ask or self.ask.price == 0:
            return None
        return (self.ask.price - self.bid.price) / self.ask.price * BPS


@dataclass(frozen=True)
class Fill:
    """Accounting convention (swaps):
    * ``price`` is the ALL-IN effective price: USD paid / tokens received
      (buy) or USD received / tokens sent (sell). LP fees and transfer taxes
      are therefore already inside ``price``.
    * ``fee_usd`` is network cost (base fee + priority fee) and is added on top.
    * ``tax_usd`` is an informational breakdown of the transfer tax already
      embedded in ``price``. It is NOT added again.
    """
    fill_id: str
    client_order_id: str
    token: str
    side: Side
    qty: Decimal
    price: Decimal  # USD per token, actual execution
    fee_usd: Decimal  # gas + priority + LP/venue fees
    tax_usd: Decimal  # informational; embedded in price
    ts_exec_ms: int
    ts_uncertainty_ms: int  # e.g. slot-time uncertainty on chain
    venue: str
    strategy: str = "default"

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price


@dataclass
class Position:
    token: str
    qty: Decimal = Decimal(0)  # CONFIRMED tokens held
    cost_usd: Decimal = Decimal(0)  # remaining cost basis, incl. entry costs
    realized_pnl_usd: Decimal = Decimal(0)
    opened_ms: Optional[int] = None
    entry_price: Optional[Decimal] = None
    high_water_price: Optional[Decimal] = None
    initial_qty: Decimal = Decimal(0)
    scale_outs_done: set[int] = field(default_factory=set)
    exit_failed: bool = False  # set when exit escalation is exhausted -> unprotected

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    @property
    def avg_cost(self) -> Decimal:
        return self.cost_usd / self.qty if self.qty else Decimal(0)
