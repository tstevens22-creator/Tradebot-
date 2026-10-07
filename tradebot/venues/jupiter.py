"""Jupiter Swap API V2 (Solana): executable quotes + a paper venue on live prices.

Endpoint choice: ``GET /swap/v2/order`` (+ ``POST /swap/v2/execute`` for live,
NOT implemented). With /order, all Jupiter routers compete, and the quote
comes from the same path that would execute (checklist item 2). The older
v1 "Metis" API is unmaintained.

Facts from the V2 OpenAPI spec (verified 2026-10-07):
  * amounts are strings in the token's smallest unit
  * ``priceImpact`` is in percentage points (-0.1 == -0.1%)
  * ``feeBps`` is the total fee rate, collected in ``feeMint``
  * keyless access: 0.5 requests/second. A free API key (``x-api-key``)
    raises that limit, and is needed for real-time protection.

Accounting (conservative):
  * USD amounts are USDC (6 decimals). Every quote is USDC <-> token.
  * The docs don't say whether ``outAmount`` is net of the fee when the fee is
    taken in the output mint, so we deduct ``feeBps`` again in that case. This
    understates proceeds by at most the fee. Verify against
    ``totalOutputAmount`` on the first real fill.

Live execution (sign + /execute + on-chain reconciliation) is deliberately
absent: live trading is blocked until the owner authorizes it.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

from ..clock import Clock
from ..models import BPS, Fill, Quote, Side, from_atomic, to_atomic
from ..ratelimit import TokenBucket
from ..state_machine import Order
from .base import RateLimited, SubmitResult, TxStatus, VenueError, VenueTimeout

BASE_URL = "https://api.jup.ag/swap/v2"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = Decimal(1_000_000_000)


class JupiterError(VenueError):
    pass


@dataclass(frozen=True)
class OrderQuote:
    """Validated subset of a /order response."""
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int  # what we conservatively expect to receive (fee-adjusted)
    raw_out_amount: int
    price_impact_bps: Decimal
    fee_bps: Decimal
    fee_mint: Optional[str]
    router: Optional[str]
    request_id: Optional[str]
    latency_ms: int


def parse_order(body: dict, input_mint: str, output_mint: str, amount: int, latency_ms: int = 0) -> OrderQuote:
    """Validate a /order response. Anything inconsistent is an error: a quote
    for the wrong mint or amount must never be acted on."""
    if not isinstance(body, dict):
        raise JupiterError("order response is not an object")
    if body.get("error") or body.get("errorCode"):
        raise JupiterError(f"order error: {body.get('errorCode')} {body.get('error') or body.get('errorMessage')}")
    if body.get("inputMint") != input_mint or body.get("outputMint") != output_mint:
        raise JupiterError("order mints do not match request")
    try:
        in_amt = int(body["inAmount"])
        out_raw = int(body["outAmount"])
    except (KeyError, TypeError, ValueError):
        raise JupiterError("order amounts missing or not integers") from None
    if in_amt != amount:
        raise JupiterError(f"order inAmount {in_amt} != requested {amount}")
    if out_raw <= 0:
        raise JupiterError("order outAmount must be > 0 (no route)")
    impact = body.get("priceImpact")
    if not isinstance(impact, (int, float)) or isinstance(impact, bool):
        raise JupiterError("order priceImpact missing")
    fee_bps = Decimal(str(body.get("feeBps") or 0))
    fee_mint = body.get("feeMint")
    out_amt = out_raw
    if fee_mint == output_mint and fee_bps > 0:
        out_amt = int(Decimal(out_raw) * (1 - fee_bps / BPS))  # conservative; see module doc
    return OrderQuote(input_mint, output_mint, in_amt, out_amt, out_raw,
                      abs(Decimal(str(impact))) * 100, fee_bps, fee_mint,
                      body.get("router"), body.get("requestId"), latency_ms)


class JupiterClient:
    def __init__(self, api_key: Optional[str] = None, base_url: str = BASE_URL,
                 bucket: Optional[TokenBucket] = None, timeout_s: float = 3.0,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None,
                 now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000):
        self.api_key = api_key if api_key is not None else os.environ.get("JUPITER_API_KEY")
        # Keyless: 0.5 req/s. Keep one request in reserve for exits.
        self.bucket = bucket or (TokenBucket(rate_per_s=0.5, capacity=3, reserve=1) if not self.api_key
                                 else TokenBucket(rate_per_s=5.0, capacity=10, reserve=3))
        self.base_url, self.timeout_s = base_url, timeout_s
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())
        self.now_ms = now_ms

    def __repr__(self) -> str:
        return f"JupiterClient(base_url={self.base_url!r}, api_key={'***' if self.api_key else None})"

    def order(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
              priority: bool = False) -> OrderQuote:
        if amount <= 0:
            raise JupiterError("amount must be > 0")
        if not self.bucket.try_take(priority):
            raise RateLimited("Jupiter rate budget exhausted (local)")
        q = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint,
                                    "amount": str(amount), "slippageBps": str(slippage_bps)})
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        req = urllib.request.Request(f"{self.base_url}/order?{q}", headers=headers)
        t0 = self.now_ms()
        try:
            raw = self._open(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("Jupiter 429") from None
            raise JupiterError(f"Jupiter HTTP {e.code}") from None
        except (TimeoutError, OSError) as e:
            raise VenueTimeout(f"Jupiter request failed: {type(e).__name__}") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise JupiterError("invalid JSON from Jupiter") from None
        return parse_order(body, input_mint, output_mint, amount, self.now_ms() - t0)

    # -- USD <-> token helpers (USD == USDC) ------------------------------
    def quote_buy(self, mint: str, usd_in: Decimal, decimals: int, slippage_bps: int = 100,
                  priority: bool = False) -> tuple[Quote, OrderQuote]:
        oq = self.order(USDC_MINT, mint, to_atomic(usd_in, USDC_DECIMALS), slippage_bps, priority)
        qty = from_atomic(oq.out_amount, decimals)
        usd = from_atomic(oq.in_amount, USDC_DECIMALS)
        return Quote(mint, Side.BUY, qty, usd, oq.price_impact_bps, self.now_ms()), oq

    def quote_sell(self, mint: str, qty: Decimal, decimals: int, slippage_bps: int = 100,
                   priority: bool = False) -> tuple[Quote, OrderQuote]:
        atomic = to_atomic(qty, decimals)
        oq = self.order(mint, USDC_MINT, atomic, slippage_bps, priority)
        usd = from_atomic(oq.out_amount, USDC_DECIMALS)
        return Quote(mint, Side.SELL, from_atomic(atomic, decimals), usd, oq.price_impact_bps, self.now_ms()), oq


# ---------------------------------------------------------------------------
@dataclass
class _PaperTx:
    order: Order
    tx_id: str
    submit_ms: int
    land_ms: int
    status: str = "PENDING"
    fill: Optional[Fill] = None
    reason: str = ""


class JupiterPaperVenue:
    """Paper trading on LIVE Jupiter prices. No transaction is ever built,
    signed or sent.

    At the simulated landing time the venue re-quotes the exact size from
    Jupiter and fills at that real price, or FAILS if it is worse than
    ``min_out``. That models price drift between decision and landing. If the
    re-quote cannot be fetched (rate limit, outage), the tx stays PENDING and
    EXPIRES after ``tx_expiry_ms``, like a Solana tx whose blockhash lapsed.
    """

    name = "jupiter-paper"
    is_simulated = True  # real prices, simulated fills
    chains: tuple[str, ...] = ("solana",)

    def __init__(self, clock: Clock, client: JupiterClient, decimals_of: Callable[[str], int],
                 confirm_delay_ms: int = 1500, tx_expiry_ms: int = 90_000,
                 priority_fee_lamports: int = 50_000, base_fee_lamports: int = 5_000,
                 sol_usd: Optional[Callable[[], Optional[Decimal]]] = None):
        self.clock, self.client, self.decimals_of = clock, client, decimals_of
        self.confirm_delay_ms, self.tx_expiry_ms = confirm_delay_ms, tx_expiry_ms
        self.priority_fee_lamports, self.base_fee_lamports = priority_fee_lamports, base_fee_lamports
        self._sol_usd = sol_usd
        self._sol_cache: tuple[int, Optional[Decimal]] = (0, None)
        self.wallet: dict[str, Decimal] = {}
        self.txs: dict[str, _PaperTx] = {}
        self.last_order: Optional[OrderQuote] = None

    # -- quotes ---------------------------------------------------------
    def quote_buy(self, token: str, usd_in: Decimal) -> Quote:
        q, self.last_order = self.client.quote_buy(token, usd_in, self.decimals_of(token))
        return q

    def quote_sell(self, token: str, qty: Decimal) -> Quote:
        q, self.last_order = self.client.quote_sell(token, qty, self.decimals_of(token), priority=True)
        return q

    def sol_usd(self) -> Optional[Decimal]:
        if self._sol_usd:
            return self._sol_usd()
        ts, px = self._sol_cache
        if px is not None and self.clock.now_ms() - ts < 120_000:
            return px
        try:
            oq = self.client.order(SOL_MINT, USDC_MINT, 1_000_000_000, 50)
            px = from_atomic(oq.raw_out_amount, USDC_DECIMALS)
            self._sol_cache = (self.clock.now_ms(), px)
        except VenueError:
            pass  # keep the previous value; None if never fetched
        return px

    def network_fee_usd(self) -> Decimal:
        px = self.sol_usd()
        if px is None:
            return Decimal("0.05")  # conservative fallback when SOL price is unknown
        return Decimal(self.base_fee_lamports + self.priority_fee_lamports) / LAMPORTS_PER_SOL * px

    def balance(self, token: str) -> Decimal:
        return self.wallet.get(token, Decimal(0))

    # -- simulated execution ------------------------------------------
    def submit_swap(self, order: Order) -> SubmitResult:
        tx_id = "paper_" + order.client_order_id
        if tx_id not in self.txs:
            now = self.clock.now_ms()
            self.txs[tx_id] = _PaperTx(order, tx_id, now, now + self.confirm_delay_ms)
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

    def _land(self, tx: _PaperTx) -> None:
        o = tx.order
        try:
            if o.side is Side.BUY:
                q = self.quote_buy(o.token, o.qty)
                if q.qty < o.min_out:
                    tx.status, tx.reason = "FAILED", "slippage: tokens out < min_out"
                    return
                qty, price = q.qty, o.qty / q.qty
                self.wallet[o.token] = self.wallet.get(o.token, Decimal(0)) + qty
            else:
                have = self.wallet.get(o.token, Decimal(0))
                if o.qty > have:
                    tx.status, tx.reason = "FAILED", "insufficient balance"
                    return
                q = self.quote_sell(o.token, o.qty)
                if q.usd < o.min_out:
                    tx.status, tx.reason = "FAILED", "slippage: usd out < min_out"
                    return
                qty, price = o.qty, q.usd / o.qty
                self.wallet[o.token] = have - o.qty
        except VenueError:
            return  # stays PENDING; retried next process() until expiry
        tx.status = "CONFIRMED"
        tx.fill = Fill(fill_id=tx.tx_id, client_order_id=o.client_order_id, token=o.token, side=o.side,
                       qty=qty, price=price, fee_usd=self.network_fee_usd(), tax_usd=Decimal(0),
                       ts_exec_ms=self.clock.now_ms(), ts_uncertainty_ms=self.confirm_delay_ms,
                       venue=self.name)

    def get_tx_status(self, client_order_id: str) -> TxStatus:
        tx = self.txs.get("paper_" + client_order_id)
        if tx is None:
            return TxStatus("NOT_FOUND")
        return TxStatus(tx.status, tx.fill, tx.reason)
