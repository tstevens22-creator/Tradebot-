"""Order execution: write-ahead journaling, idempotent submission, UNKNOWN
handling and reconciliation.

Invariants enforced here:
  E1  Every state change is journaled before the venue action it precedes.
  E2  A client_order_id is submitted at most once. A retry is a NEW order
      with a new id, and it is only allowed after the previous one is
      terminal.
  E3  A timeout or exception yields UNKNOWN, never FAILED.
  E4  UNKNOWN resolves only via venue status. NOT_FOUND counts as EXPIRED
      only once the tx expiry window has provably elapsed.
  E5  Fills are deduplicated by fill_id and applied to the portfolio exactly
      once.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, Optional

from .clock import Clock
from .journal import Journal
from .latency import LatencyMetrics
from .models import Fill, Side
from .portfolio import Portfolio
from .state_machine import Order, OrderState
from .venues.base import RateLimited, SwapVenue, VenueError


class DuplicateOrder(RuntimeError):
    pass


class ExecutionEngine:
    def __init__(self, venue: SwapVenue, journal: Journal, clock: Clock, portfolio: Portfolio,
                 latency: LatencyMetrics, on_fill: Optional[Callable[[Order, Fill], None]] = None):
        self.venue = venue
        self.journal = journal
        self.clock = clock
        self.portfolio = portfolio
        self.latency = latency
        self.orders: dict[str, Order] = {}
        self.on_fill = on_fill

    # ------------------------------------------------------------------
    def _save(self, o: Order) -> None:
        self.journal.record_order(self.clock.now_ms(), o)

    def submit(self, o: Order) -> Order:
        if o.client_order_id in self.orders:
            raise DuplicateOrder(o.client_order_id)
        o.created_ms = o.created_ms or self.clock.now_ms()
        self.orders[o.client_order_id] = o
        o.transition(OrderState.SUBMITTING)
        self._save(o)  # E1: write-ahead
        t0 = self.clock.now_ms()
        try:
            res = self.venue.submit_swap(o)
        except RateLimited as e:  # 429: request definitively NOT accepted
            o.submitted_ms = t0
            o.transition(OrderState.REJECTED)
            self._save(o)
            self.journal.append(self.clock.now_ms(), "SUBMIT_RATE_LIMITED",
                                {"cid": o.client_order_id, "error": repr(e)}, key=o.client_order_id)
            return o
        except VenueError as e:  # timeouts etc.: outcome ambiguous
            o.transition(OrderState.UNKNOWN)
            o.unknown_since_ms = self.clock.now_ms()
            o.submitted_ms = t0
            self._save(o)
            self.journal.append(self.clock.now_ms(), "SUBMIT_ERROR",
                                {"cid": o.client_order_id, "error": repr(e)}, key=o.client_order_id)
            return o
        except Exception as e:  # unexpected: still ambiguous, never swallowed
            o.transition(OrderState.UNKNOWN)
            o.unknown_since_ms = self.clock.now_ms()
            o.submitted_ms = t0
            self._save(o)
            self.journal.append(self.clock.now_ms(), "SUBMIT_EXCEPTION",
                                {"cid": o.client_order_id, "error": repr(e)}, key=o.client_order_id)
            raise
        self.latency.record("submit_ack", self.clock.now_ms() - t0)
        o.submitted_ms = t0
        if res.status == "ACCEPTED":
            o.tx_id = res.tx_id
            o.transition(OrderState.SUBMITTED)
        else:
            o.transition(OrderState.REJECTED)
        self._save(o)
        return o

    # ------------------------------------------------------------------
    def poll(self) -> None:
        for o in list(self.orders.values()):
            if not o.is_terminal:
                self.reconcile_order(o)

    def reconcile_order(self, o: Order) -> None:
        now = self.clock.now_ms()
        try:
            st = self.venue.get_tx_status(o.client_order_id)
        except VenueError as e:
            if o.state is not OrderState.UNKNOWN and o.state in (OrderState.SUBMITTED, OrderState.SUBMITTING):
                o.transition(OrderState.UNKNOWN)
                o.unknown_since_ms = now
                self._save(o)
            self.journal.append(now, "STATUS_ERROR", {"cid": o.client_order_id, "error": repr(e)},
                                key=o.client_order_id)
            return
        prev = o.state
        if st.status == "CONFIRMED" and st.fill is not None:
            self._apply_fill(o, st.fill)
            if o.state is not OrderState.FILLED:
                o.transition(OrderState.FILLED)
        elif st.status == "FAILED":
            o.transition(OrderState.FAILED)
        elif st.status == "EXPIRED":
            o.transition(OrderState.EXPIRED)
        elif st.status == "PENDING":
            if o.state is OrderState.UNKNOWN:
                o.transition(OrderState.SUBMITTED)
        elif st.status == "NOT_FOUND":
            sent = o.submitted_ms or o.created_ms
            if now - sent >= self.venue.tx_expiry_ms:
                if o.state is OrderState.SUBMITTING:
                    o.transition(OrderState.UNKNOWN)
                o.transition(OrderState.EXPIRED)  # provably cannot land any more
            elif o.state is not OrderState.UNKNOWN:
                o.transition(OrderState.UNKNOWN)  # not visible yet; may still land
                o.unknown_since_ms = now
        if o.state is not prev:
            self._save(o)

    def _apply_fill(self, o: Order, f: Fill) -> None:
        if not o.add_fill(f):
            return  # duplicate report
        self.journal.record_fill(self.clock.now_ms(), f)
        self.portfolio.apply_fill(f)
        if o.submitted_ms is not None:
            self.latency.record("submit_to_fill", f.ts_exec_ms - o.submitted_ms)
        if self.on_fill:
            self.on_fill(o, f)

    # ------------------------------------------------------------------
    def open_orders(self, token: Optional[str] = None) -> list[Order]:
        return [o for o in self.orders.values() if not o.is_terminal and (token is None or o.token == token)]

    def unresolved_count(self, token: str) -> int:
        return len(self.open_orders(token))

    def inflight_exit_qty(self, token: str) -> Decimal:
        return sum((o.qty - o.filled_qty for o in self.open_orders(token) if o.side is Side.SELL), Decimal(0))

    def inflight_entry_usd(self) -> Decimal:
        return sum((o.qty for o in self.open_orders() if o.side is Side.BUY), Decimal(0))

    def stuck_unknown(self, timeout_s: int) -> list[Order]:
        now = self.clock.now_ms()
        return [o for o in self.orders.values() if o.state is OrderState.UNKNOWN
                and o.unknown_since_ms is not None and now - o.unknown_since_ms > timeout_s * 1000]
