"""Shared paper venue on LIVE executable quotes (Jupiter on Solana, 0x on Base).

No transaction is ever built, signed or sent. At the simulated landing time
the venue re-quotes the exact size and fills at that real price, or FAILS if
the price is worse than ``min_out``. That models drift between decision and
landing. If the re-quote can't be fetched (rate limit, outage), the tx stays
PENDING and EXPIRES after ``tx_expiry_ms``.

Subclasses provide ``quote_buy``, ``quote_sell`` and ``network_fee_usd``, and
may override ``_before_swap`` (e.g. EVM exact-amount approvals) and
``_extra_fee_usd``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..clock import Clock
from ..models import Fill, Quote, Side
from ..state_machine import Order
from .base import SubmitResult, TxStatus, VenueError


@dataclass
class PaperTx:
    order: Order
    tx_id: str
    submit_ms: int
    land_ms: int
    status: str = "PENDING"
    fill: Optional[Fill] = None
    reason: str = ""


class LiveQuotePaperVenue:
    name = "live-quote-paper"
    is_simulated = True  # real prices, simulated fills
    chains: tuple[str, ...] = ()

    def __init__(self, clock: Clock, confirm_delay_ms: int, tx_expiry_ms: int):
        self.clock, self.confirm_delay_ms, self.tx_expiry_ms = clock, confirm_delay_ms, tx_expiry_ms
        self.wallet: dict[str, Decimal] = {}
        self.txs: dict[str, PaperTx] = {}

    # -- subclass API ---------------------------------------------------
    def quote_buy(self, token: str, usd_in: Decimal) -> Quote:
        raise NotImplementedError

    def quote_sell(self, token: str, qty: Decimal) -> Quote:
        raise NotImplementedError

    def network_fee_usd(self) -> Decimal:
        raise NotImplementedError

    def _before_swap(self, order: Order) -> Optional[str]:
        """Return a failure reason to abort the swap, or None to proceed."""
        return None

    def _extra_fee_usd(self, order: Order) -> Decimal:
        return Decimal(0)

    def _after_swap(self, order: Order, ok: bool) -> None:
        pass

    # -- SwapVenue ------------------------------------------------------
    def balance(self, token: str) -> Decimal:
        return self.wallet.get(token, Decimal(0))

    def submit_swap(self, order: Order) -> SubmitResult:
        tx_id = "paper_" + order.client_order_id
        if tx_id not in self.txs:  # same client id -> same tx (idempotent)
            now = self.clock.now_ms()
            self.txs[tx_id] = PaperTx(order, tx_id, now, now + self.confirm_delay_ms)
        return SubmitResult("ACCEPTED", tx_id)

    def process(self) -> None:
        now = self.clock.now_ms()
        for tx in self.txs.values():
            if tx.status != "PENDING":
                continue
            if now - tx.submit_ms >= self.tx_expiry_ms:
                tx.status, tx.reason = "EXPIRED", "no landing quote before expiry"
                continue
            if now >= tx.land_ms:
                self._land(tx)

    def _land(self, tx: PaperTx) -> None:
        o = tx.order
        try:
            reason = self._before_swap(o)
            if reason:
                tx.status, tx.reason = "FAILED", reason
                return
            if o.side is Side.BUY:
                q = self.quote_buy(o.token, o.qty)
                if q.qty < o.min_out:
                    tx.status, tx.reason = "FAILED", "slippage: tokens out < min_out"
                    self._after_swap(o, False)
                    return
                qty, price = q.qty, o.qty / q.qty
                self.wallet[o.token] = self.wallet.get(o.token, Decimal(0)) + qty
            else:
                have = self.wallet.get(o.token, Decimal(0))
                if o.qty > have:
                    tx.status, tx.reason = "FAILED", "insufficient balance"
                    self._after_swap(o, False)
                    return
                q = self.quote_sell(o.token, o.qty)
                if q.usd < o.min_out:
                    tx.status, tx.reason = "FAILED", "slippage: usd out < min_out"
                    self._after_swap(o, False)
                    return
                qty, price = o.qty, q.usd / o.qty
                self.wallet[o.token] = have - o.qty
        except VenueError:
            self._after_swap(o, False)  # e.g. revoke the EVM approval; re-approved exactly on retry
            return  # stays PENDING; retried next process() until expiry
        self._after_swap(o, True)
        tx.status = "CONFIRMED"
        tx.fill = Fill(fill_id=tx.tx_id, client_order_id=o.client_order_id, token=o.token, side=o.side,
                       qty=qty, price=price, fee_usd=self.network_fee_usd() + self._extra_fee_usd(o),
                       tax_usd=Decimal(0), ts_exec_ms=self.clock.now_ms(), ts_uncertainty_ms=self.confirm_delay_ms,
                       venue=self.name)

    def get_tx_status(self, client_order_id: str) -> TxStatus:
        tx = self.txs.get("paper_" + client_order_id)
        if tx is None:
            return TxStatus("NOT_FOUND")
        return TxStatus(tx.status, tx.fill, tx.reason)
