"""Simulated constant-product DEX with deterministic fault injection.

Simulation only: it never touches a real chain or third-party infrastructure.

Modelled realities:
  * Swaps are atomic: a swap confirms fully or fails (``min_out`` violated,
    sell blocked, etc.). No partial fills at the venue.
  * Broadcast transactions cannot be cancelled. They confirm or fail after
    ``confirm_delay_ms``, or never land and become EXPIRED after
    ``tx_expiry_ms``.
  * A submit TIMEOUT can still land on chain (``timeout_but_lands``).
  * Signatures are deterministic per client_order_id, so resubmitting the same
    signed tx is idempotent (it cannot execute twice).
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from ..clock import SimClock
from ..models import BPS, Fill, Quote, Side
from ..state_machine import Order
from .base import RateLimited, SubmitResult, TxStatus, VenueTimeout


@dataclass
class Pool:
    token_reserve: Decimal
    usd_reserve: Decimal
    fee_bps: Decimal = Decimal(25)
    tax_bps: Decimal = Decimal(0)
    sell_blocked: bool = False  # honeypot

    @property
    def spot(self) -> Decimal:
        return self.usd_reserve / self.token_reserve

    def buy_out(self, usd_in: Decimal) -> tuple[Decimal, Decimal]:
        """-> (tokens received after tax, tax tokens)."""
        dx = usd_in * (1 - self.fee_bps / BPS)
        gross = self.token_reserve * dx / (self.usd_reserve + dx)
        tax = gross * self.tax_bps / BPS
        return gross - tax, tax

    def sell_out(self, qty: Decimal) -> tuple[Decimal, Decimal]:
        """-> (USD received, tax tokens)."""
        tax = qty * self.tax_bps / BPS
        dq = (qty - tax) * (1 - self.fee_bps / BPS)
        return self.usd_reserve * dq / (self.token_reserve + dq), tax


@dataclass
class Faults:
    submit_timeout_prob: float = 0.0
    timeout_but_lands: bool = True
    status_timeout_prob: float = 0.0
    confirm_delay_ms: int = 400
    ack_delay_ms: int = 50
    tx_fail_prob: float = 0.0
    tx_drop_prob: float = 0.0  # never lands -> EXPIRED
    rate_limit_budget: Optional[int] = None  # None = unlimited
    network_fee_usd: Decimal = Decimal("0.01")
    fee_spike_multiplier: Decimal = Decimal(1)
    priority_fee_lamports: int = 10_000
    duplicate_status_reports: bool = False


@dataclass
class _Tx:
    order: Order
    tx_id: str
    submit_ms: int
    land_ms: Optional[int]  # None = dropped
    status: str = "PENDING"
    fill: Optional[Fill] = None
    reason: str = ""


class PaperDexVenue:
    name = "paper-dex"
    is_simulated = True
    chains: tuple[str, ...] = ("solana", "base")  # simulation stands in for Jupiter and CDP/0x

    def __init__(self, clock: SimClock, seed: int = 0, tx_expiry_ms: int = 60_000):
        self.clock = clock
        self.rng = random.Random(seed)
        self.pools: dict[str, Pool] = {}
        self.faults = Faults()
        self.tx_expiry_ms = tx_expiry_ms
        self.txs: dict[str, _Tx] = {}
        self.wallet: dict[str, Decimal] = {}
        self.submit_calls: list[str] = []  # audit trail for duplicate detection
        self.landed_by_cid: dict[str, int] = {}

    # -- setup / market events (scenario control) ------------------------
    def add_pool(self, token: str, pool: Pool) -> None:
        self.pools[token] = pool

    def shock(self, token: str, pct: Decimal) -> None:
        """External market move: scale USD reserve (price moves by ~pct)."""
        p = self.pools[token]
        p.usd_reserve *= (1 + pct)

    def remove_liquidity(self, token: str, frac: Decimal) -> None:
        p = self.pools[token]
        p.usd_reserve *= (1 - frac)
        p.token_reserve *= (1 - frac)

    # -- rate limiting --------------------------------------------------
    def _spend(self) -> None:
        f = self.faults
        if f.rate_limit_budget is not None:
            if f.rate_limit_budget <= 0:
                raise RateLimited("429 rate limit exhausted")
            f.rate_limit_budget -= 1

    # -- quotes ---------------------------------------------------------
    def quote_buy(self, token: str, usd_in: Decimal) -> Quote:
        self._spend()
        p = self.pools[token]
        out, _ = p.buy_out(usd_in)
        price = usd_in / out
        return Quote(token, Side.BUY, out, usd_in, (price / p.spot - 1) * BPS, self.clock.now_ms())

    def quote_sell(self, token: str, qty: Decimal) -> Quote:
        self._spend()
        p = self.pools[token]
        usd, _ = p.sell_out(qty)
        price = usd / qty
        return Quote(token, Side.SELL, qty, usd, (1 - price / p.spot) * BPS, self.clock.now_ms())

    def quote_sell_unmetered(self, token: str, qty: Decimal) -> Optional[Decimal]:
        """Simulation-only benchmark helper (no rate-limit spend)."""
        p = self.pools.get(token)
        if not p or qty <= 0:
            return None
        usd, _ = p.sell_out(qty)
        return usd / qty

    def network_fee_usd(self) -> Decimal:
        return self.faults.network_fee_usd * self.faults.fee_spike_multiplier

    def balance(self, token: str) -> Decimal:
        self._spend()
        return self.wallet.get(token, Decimal(0))

    # -- execution ------------------------------------------------------
    @staticmethod
    def _sig(cid: str) -> str:
        return "sig_" + hashlib.sha256(cid.encode()).hexdigest()[:24]

    def submit_swap(self, order: Order) -> SubmitResult:
        self._spend()
        self.submit_calls.append(order.client_order_id)
        self.clock.advance(self.faults.ack_delay_ms)
        tx_id = self._sig(order.client_order_id)
        if tx_id not in self.txs:  # identical signature -> cannot execute twice
            dropped = self.rng.random() < self.faults.tx_drop_prob
            land = None if dropped else self.clock.now_ms() + self.faults.confirm_delay_ms
            self.txs[tx_id] = _Tx(order=order, tx_id=tx_id, submit_ms=self.clock.now_ms(), land_ms=land)
        if self.rng.random() < self.faults.submit_timeout_prob:
            if not self.faults.timeout_but_lands:
                self.txs.pop(tx_id, None)
            raise VenueTimeout("submit timed out (outcome unknown)")
        return SubmitResult("ACCEPTED", tx_id)

    def process(self) -> None:
        """Advance pending txs whose landing time has passed."""
        now = self.clock.now_ms()
        for tx in self.txs.values():
            if tx.status != "PENDING":
                continue
            if tx.land_ms is None:
                if now - tx.submit_ms >= self.tx_expiry_ms:
                    tx.status = "EXPIRED"
                continue
            if now >= tx.land_ms:
                self._execute(tx)

    def _execute(self, tx: _Tx) -> None:
        o, p = tx.order, self.pools[tx.order.token]
        if self.rng.random() < self.faults.tx_fail_prob:
            tx.status, tx.reason = "FAILED", "simulated tx failure"
            return
        fee = self.network_fee_usd()
        if o.side is Side.BUY:
            out, tax = p.buy_out(o.qty)
            if out < o.min_out:
                tx.status, tx.reason = "FAILED", "slippage: out < min_out"
                return
            p.usd_reserve += o.qty * (1 - p.fee_bps / BPS)
            p.token_reserve -= out + tax
            self.wallet[o.token] = self.wallet.get(o.token, Decimal(0)) + out
            price = o.qty / out
            qty = out
            tax_usd = tax * price
        else:
            have = self.wallet.get(o.token, Decimal(0))
            if p.sell_blocked:
                tx.status, tx.reason = "FAILED", "transfer restricted (honeypot)"
                return
            if o.qty > have:
                tx.status, tx.reason = "FAILED", "insufficient balance"
                return
            usd, tax = p.sell_out(o.qty)
            if usd < o.min_out:
                tx.status, tx.reason = "FAILED", "slippage: out < min_out"
                return
            dq = (o.qty - tax) * (1 - p.fee_bps / BPS)
            p.token_reserve += dq
            p.usd_reserve -= usd
            self.wallet[o.token] = have - o.qty
            price = usd / o.qty
            qty = o.qty
            tax_usd = tax * price
        self.landed_by_cid[o.client_order_id] = self.landed_by_cid.get(o.client_order_id, 0) + 1
        tx.status = "CONFIRMED"
        tx.fill = Fill(
            fill_id=tx.tx_id, client_order_id=o.client_order_id, token=o.token, side=o.side,
            qty=qty, price=price, fee_usd=fee, tax_usd=tax_usd, ts_exec_ms=tx.land_ms or self.clock.now_ms(),
            ts_uncertainty_ms=400, venue=self.name,
        )

    def get_tx_status(self, client_order_id: str) -> TxStatus:
        self._spend()
        if self.rng.random() < self.faults.status_timeout_prob:
            raise VenueTimeout("status query timed out")
        tx = self.txs.get(self._sig(client_order_id))
        if tx is None:
            return TxStatus("NOT_FOUND")
        return TxStatus(tx.status, tx.fill, tx.reason)
