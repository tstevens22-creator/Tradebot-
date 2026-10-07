"""Explicit order state machine.

Key rules:
  * A timeout means UNKNOWN, not FAILED. UNKNOWN must be reconciled before any
    resubmission.
  * A cancel request means CANCEL_PENDING, not CANCELLED. Fills are still
    accepted in CANCEL_PENDING.
  * A pending DEX swap transaction cannot be cancelled. It can only confirm,
    fail, or expire. ``cancel()`` on a swap order raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

from .models import Fill, Purpose, Side


class OrderState(Enum):
    NEW = "NEW"
    SUBMITTING = "SUBMITTING"  # journaled BEFORE send (write-ahead)
    SUBMITTED = "SUBMITTED"  # venue acked / tx broadcast, outcome pending
    UNKNOWN = "UNKNOWN"  # outcome ambiguous -> must reconcile
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"  # proven failure (e.g. tx reverted on chain)
    EXPIRED = "EXPIRED"  # proven never-landed (e.g. blockhash expired)


TERMINAL = {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.FAILED, OrderState.EXPIRED}

_ALLOWED: dict[OrderState, set[OrderState]] = {
    OrderState.NEW: {OrderState.SUBMITTING, OrderState.REJECTED},
    OrderState.SUBMITTING: {OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.REJECTED},
    OrderState.SUBMITTED: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.FAILED, OrderState.EXPIRED,
        OrderState.UNKNOWN, OrderState.CANCEL_PENDING, OrderState.REJECTED,
    },
    OrderState.UNKNOWN: {
        OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.FAILED,
        OrderState.EXPIRED, OrderState.REJECTED, OrderState.CANCELLED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCEL_PENDING, OrderState.UNKNOWN,
        OrderState.CANCELLED, OrderState.EXPIRED, OrderState.FAILED,
    },
    OrderState.CANCEL_PENDING: {
        OrderState.CANCELLED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.UNKNOWN,
        OrderState.CANCEL_PENDING,
    },
}


class IllegalTransition(RuntimeError):
    pass


class NotCancellable(RuntimeError):
    pass


@dataclass
class Order:
    client_order_id: str
    token: str
    side: Side
    purpose: Purpose
    qty: Decimal  # tokens for SELL; for BUY this is the USD amount in
    min_out: Decimal  # minimum tokens (BUY) or USD (SELL) -- slippage bound
    slippage_bps: Decimal
    is_swap: bool = True
    state: OrderState = OrderState.NEW
    tx_id: Optional[str] = None
    fills: dict[str, Fill] = field(default_factory=dict)
    created_ms: int = 0
    submitted_ms: Optional[int] = None
    unknown_since_ms: Optional[int] = None
    attempt: int = 0
    decision_id: Optional[str] = None  # links to the risk-gate authorization

    @property
    def filled_qty(self) -> Decimal:
        return sum((f.qty for f in self.fills.values()), Decimal(0))

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def transition(self, new: OrderState) -> None:
        if new not in _ALLOWED.get(self.state, set()):
            raise IllegalTransition(f"{self.client_order_id}: {self.state.value} -> {new.value}")
        self.state = new

    def add_fill(self, fill: Fill) -> bool:
        """Idempotent. Returns False for a duplicate fill id."""
        if fill.fill_id in self.fills:
            return False
        if fill.client_order_id != self.client_order_id:
            raise ValueError("fill does not belong to this order")
        self.fills[fill.fill_id] = fill
        return True

    def request_cancel(self) -> None:
        if self.is_swap:
            raise NotCancellable(
                "a broadcast swap transaction cannot be cancelled; it can only confirm, fail or expire"
            )
        self.transition(OrderState.CANCEL_PENDING)
