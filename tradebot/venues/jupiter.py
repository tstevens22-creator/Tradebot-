"""Jupiter Swap API V2 (Solana): executable quotes + a paper venue on live prices.

Endpoint choice: ``GET /swap/v2/order`` (+ ``POST /swap/v2/execute`` for live,
NOT implemented). With /order, all Jupiter routers compete, and the quote
comes from the same path that would execute (checklist item 2). The older
v1 "Metis" API is unmaintained.

Facts from the V2 OpenAPI spec (verified 2026-10-07):
  * amounts are strings in the token's smallest unit
  * ``priceImpact`` is in percentage points (-0.1 == -0.1%)
  * ``feeBps`` is the total fee rate, collected in ``feeMint``
  * rate limits per ORGANISATION, 60 s sliding window (docs/portal/rate-limits):
    keyless 0.5 rps, Free key 1 rps, Developer 10, Launch 50, Pro 150.
    /execute has a separate bucket. On 429, ``x-ratelimit-reset`` (unix
    seconds) says when a slot frees: we stop calling until then.

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
from ..models import BPS, Quote, Side, from_atomic, to_atomic
from ..ratelimit import TokenBucket
from .base import RateLimited, VenueError, VenueTimeout
from .live_quote_paper import LiveQuotePaperVenue

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
                 now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 rps: Optional[float] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("JUPITER_API_KEY")
        # Plan limit: JUPITER_RPS overrides (e.g. 10 for Developer); else keyless 0.5 / Free key 1.
        env_rps = os.environ.get("JUPITER_RPS")
        self.rps = rps if rps is not None else (float(env_rps) if env_rps else (1.0 if self.api_key else 0.5))
        # Budget 90% of the plan to stay clear of the limit; keep one request for exits.
        self.bucket = bucket or TokenBucket(rate_per_s=self.rps * 0.9, capacity=max(3, int(self.rps * 3)), reserve=1)
        self.blocked_until_ms = 0
        self.rate_limited_count = 0
        self.base_url, self.timeout_s = base_url, timeout_s
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())
        self.now_ms = now_ms

    def __repr__(self) -> str:
        return f"JupiterClient(base_url={self.base_url!r}, api_key={'***' if self.api_key else None})"

    def order(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int,
              priority: bool = False) -> OrderQuote:
        if amount <= 0:
            raise JupiterError("amount must be > 0")
        if self.now_ms() < self.blocked_until_ms:
            raise RateLimited("Jupiter 429 backoff in effect")
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
                self.rate_limited_count += 1
                reset = (e.headers.get("x-ratelimit-reset") if e.headers is not None else None)
                try:
                    wait_until = int(float(reset) * 1000)
                except (TypeError, ValueError):
                    wait_until = self.now_ms() + 1000  # documented fallback: fixed 1 s
                self.blocked_until_ms = max(self.blocked_until_ms, min(wait_until, self.now_ms() + 60_000))
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
class JupiterPaperVenue(LiveQuotePaperVenue):
    """Paper trading on LIVE Jupiter prices (see LiveQuotePaperVenue)."""

    name = "jupiter-paper"
    chains: tuple[str, ...] = ("solana",)

    def __init__(self, clock: Clock, client: JupiterClient, decimals_of: Callable[[str], int],
                 confirm_delay_ms: int = 1500, tx_expiry_ms: int = 90_000,
                 priority_fee_lamports: int = 50_000, base_fee_lamports: int = 5_000,
                 sol_usd: Optional[Callable[[], Optional[Decimal]]] = None):
        super().__init__(clock, confirm_delay_ms, tx_expiry_ms)
        self.client, self.decimals_of = client, decimals_of
        self.priority_fee_lamports, self.base_fee_lamports = priority_fee_lamports, base_fee_lamports
        self._sol_usd = sol_usd
        self._sol_cache: tuple[int, Optional[Decimal]] = (0, None)
        self.last_order: Optional[OrderQuote] = None

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
