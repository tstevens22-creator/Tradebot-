"""Venue interface for swap-based (DEX) execution."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol

from ..models import Fill, Quote
from ..state_machine import Order


class VenueError(RuntimeError):
    pass


class VenueTimeout(VenueError):
    """The outcome is AMBIGUOUS: the request may or may not have taken effect."""


class RateLimited(VenueError):
    pass


@dataclass(frozen=True)
class SubmitResult:
    status: str  # "ACCEPTED" | "REJECTED"
    tx_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class TxStatus:
    status: str  # "PENDING" | "CONFIRMED" | "FAILED" | "EXPIRED" | "NOT_FOUND"
    fill: Optional[Fill] = None
    reason: str = ""


class SwapVenue(Protocol):
    name: str
    is_simulated: bool
    tx_expiry_ms: int

    def submit_swap(self, order: Order) -> SubmitResult: ...
    def get_tx_status(self, client_order_id: str) -> TxStatus: ...
    def balance(self, token: str) -> Decimal: ...
    def quote_buy(self, token: str, usd_in: Decimal) -> Quote: ...
    def quote_sell(self, token: str, qty: Decimal) -> Quote: ...
    def network_fee_usd(self) -> Decimal: ...
