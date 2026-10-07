"""0x Swap API v2 (AllowanceHolder) on Base: executable quotes + paper venue.

Why 0x directly rather than Coinbase CDP's Swap API: CDP routes through 0x,
but its SDK sets token approvals for you. Calling 0x directly lets the bot
enforce EXACT-amount approvals to an allowlisted spender (loss register F5).

Verified from 0x docs (2026-10-07):
  * GET https://api.0x.org/swap/allowance-holder/price  (indicative)
    GET .../allowance-holder/quote  (firm; for live, NOT implemented)
  * headers ``0x-api-key`` + ``0x-version: v2``; chainId 8453 = Base
  * response: buyAmount, minBuyAmount, sellAmount, liquidityAvailable, gas,
    gasPrice, totalNetworkFee (wei), fees.zeroExFee {amount, token},
    issues.allowance.spender, tokenMetadata.{buyToken,sellToken}.{buyTaxBps,sellTaxBps}
  * AllowanceHolder on Cancun chains incl. Base:
    0x0000000000001fF3684f28c67538d4D072C22734 (approve THIS, never Settler)
Conservative accounting: when the 0x fee is taken in the buy token, it is
deducted from buyAmount again (the docs don't say whether buyAmount is net).
0x returns no price-impact figure, so impact is measured against a small
probe quote of the same pair.
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
from ..state_machine import Order
from .base import RateLimited, VenueError, VenueTimeout
from .live_quote_paper import LiveQuotePaperVenue

BASE_URL = "https://api.0x.org"
BASE_CHAIN_ID = 8453
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_DECIMALS = 6
WETH_BASE = "0x4200000000000000000000000000000000000006"
ALLOWANCE_HOLDER = "0x0000000000001ff3684f28c67538d4d072c22734"  # Cancun chains (incl. Base)
SPENDER_ALLOWLIST = frozenset({ALLOWANCE_HOLDER})
APPROVE_GAS = 60_000  # estimate for an ERC-20 approve() on Base
WEI = Decimal(10) ** 18


class ZeroExError(VenueError):
    pass


@dataclass(frozen=True)
class ZxPrice:
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int  # conservative (fee-adjusted) amount we expect
    raw_buy_amount: int
    min_buy_amount: Optional[int]
    total_network_fee_wei: Optional[int]
    gas_price_wei: Optional[int]
    buy_tax_bps: Optional[Decimal]  # of the token being bought
    sell_tax_bps: Optional[Decimal]  # of the token being bought, when later sold
    spender: Optional[str]
    latency_ms: int


def _int(v) -> Optional[int]:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _bps(v) -> Optional[Decimal]:
    i = _int(v)
    return Decimal(i) if i is not None and i >= 0 else None


def parse_price(body: dict, sell_token: str, buy_token: str, sell_amount: int, latency_ms: int = 0) -> ZxPrice:
    if not isinstance(body, dict):
        raise ZeroExError("price response is not an object")
    if body.get("liquidityAvailable") is not True:
        raise ZeroExError("no liquidity available for this pair/size")
    if str(body.get("sellToken", "")).lower() != sell_token or str(body.get("buyToken", "")).lower() != buy_token:
        raise ZeroExError("price tokens do not match request")
    if _int(body.get("sellAmount")) != sell_amount:
        raise ZeroExError("price sellAmount does not match request")
    raw = _int(body.get("buyAmount"))
    if raw is None or raw <= 0:
        raise ZeroExError("buyAmount missing or zero")
    spender = ((body.get("issues") or {}).get("allowance") or {}).get("spender")
    if spender is not None and str(spender).lower() not in SPENDER_ALLOWLIST:
        raise ZeroExError(f"unexpected allowance spender {spender}: refusing (approval would not be allowlisted)")
    fee = (body.get("fees") or {}).get("zeroExFee") or {}
    buy = raw
    if str(fee.get("token", "")).lower() == buy_token and _int(fee.get("amount")):
        buy = max(0, raw - _int(fee.get("amount")))  # conservative: see module doc
    meta = ((body.get("tokenMetadata") or {}).get("buyToken")) or {}
    return ZxPrice(sell_token, buy_token, sell_amount, buy, raw, _int(body.get("minBuyAmount")),
                   _int(body.get("totalNetworkFee")), _int(body.get("gasPrice")),
                   _bps(meta.get("buyTaxBps")), _bps(meta.get("sellTaxBps")),
                   str(spender).lower() if spender else None, latency_ms)


class ZeroExClient:
    def __init__(self, api_key: Optional[str] = None, base_url: str = BASE_URL,
                 bucket: Optional[TokenBucket] = None, timeout_s: float = 3.0,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None,
                 now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000, rps: Optional[float] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("ZEROX_API_KEY")
        env_rps = os.environ.get("ZEROX_RPS")
        self.rps = rps if rps is not None else (float(env_rps) if env_rps else 1.0)  # plan limit unverified: be modest
        self.bucket = bucket or TokenBucket(rate_per_s=self.rps * 0.9, capacity=max(3, int(self.rps * 3)), reserve=1)
        self.base_url, self.timeout_s = base_url, timeout_s
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())
        self.now_ms = now_ms
        self.rate_limited_count = 0
        self._probe: dict[str, tuple[int, Decimal]] = {}

    def __repr__(self) -> str:
        return f"ZeroExClient(api_key={'***' if self.api_key else None})"

    def price(self, sell_token: str, buy_token: str, sell_amount: int, slippage_bps: int = 100,
              priority: bool = False) -> ZxPrice:
        if not self.api_key:
            raise ZeroExError("ZEROX_API_KEY not configured (0x requires a key)")
        if sell_amount <= 0:
            raise ZeroExError("sellAmount must be > 0")
        if not self.bucket.try_take(priority):
            raise RateLimited("0x rate budget exhausted (local)")
        q = urllib.parse.urlencode({"chainId": BASE_CHAIN_ID, "sellToken": sell_token, "buyToken": buy_token,
                                    "sellAmount": str(sell_amount), "slippageBps": str(slippage_bps)})
        req = urllib.request.Request(f"{self.base_url}/swap/allowance-holder/price?{q}", headers={
            "0x-api-key": self.api_key, "0x-version": "v2", "accept": "application/json"})
        t0 = self.now_ms()
        try:
            raw = self._open(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.rate_limited_count += 1
                raise RateLimited("0x 429") from None
            raise ZeroExError(f"0x HTTP {e.code}") from None
        except (TimeoutError, OSError) as e:
            raise VenueTimeout(f"0x request failed: {type(e).__name__}") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise ZeroExError("invalid JSON from 0x") from None
        return parse_price(body, sell_token, buy_token, sell_amount, self.now_ms() - t0)

    def _impact_bps(self, sell_token: str, buy_token: str, px_size: Decimal, sell_decimals: int) -> Decimal:
        """Impact vs a small probe trade of the same pair (cached 10 s)."""
        key = f"{sell_token}>{buy_token}"
        cached = self._probe.get(key)
        if cached is None or self.now_ms() - cached[0] > 10_000:
            probe_amt = 10 ** sell_decimals if sell_token == USDC_BASE else None  # $1 probe for buys
            if probe_amt is None:
                return Decimal(0)  # sells: impact is reflected in the full-size quote itself
            p = self.price(sell_token, buy_token, probe_amt)
            cached = (self.now_ms(), Decimal(p.buy_amount) / Decimal(p.sell_amount))
            self._probe[key] = cached
        px_probe = cached[1]
        return max(Decimal(0), (px_probe - px_size) / px_probe * BPS) if px_probe > 0 else Decimal(0)

    def quote_buy(self, token: str, usd_in: Decimal, decimals: int, priority: bool = False) -> tuple[Quote, ZxPrice]:
        amt = to_atomic(usd_in, USDC_DECIMALS)
        p = self.price(USDC_BASE, token, amt, priority=priority)
        impact = self._impact_bps(USDC_BASE, token, Decimal(p.buy_amount) / Decimal(amt), USDC_DECIMALS)
        return Quote(token, Side.BUY, from_atomic(p.buy_amount, decimals), usd_in, impact, self.now_ms()), p

    def quote_sell(self, token: str, qty: Decimal, decimals: int, priority: bool = False) -> tuple[Quote, ZxPrice]:
        amt = to_atomic(qty, decimals)
        p = self.price(token, USDC_BASE, amt, priority=priority)
        return Quote(token, Side.SELL, from_atomic(amt, decimals), from_atomic(p.buy_amount, USDC_DECIMALS),
                     Decimal(0), self.now_ms()), p


class ZeroExPaperVenue(LiveQuotePaperVenue):
    """Paper trading on LIVE 0x Base prices, with EVM approval discipline:
    before each swap the venue records an approval of EXACTLY the sell amount
    to the allowlisted AllowanceHolder, and the allowance is consumed after.
    Approval gas is charged as a cost. Nothing is signed or sent."""

    name = "zeroex-paper"
    chains: tuple[str, ...] = ("base",)

    def __init__(self, clock: Clock, client: ZeroExClient, decimals_of: Callable[[str], int],
                 confirm_delay_ms: int = 2500, tx_expiry_ms: int = 120_000,
                 eth_usd: Optional[Callable[[], Optional[Decimal]]] = None):
        super().__init__(clock, confirm_delay_ms, tx_expiry_ms)
        self.client, self.decimals_of = client, decimals_of
        self._eth_usd = eth_usd
        self._eth_cache: tuple[int, Optional[Decimal]] = (0, None)
        self.last_price: Optional[ZxPrice] = None
        self.allowances: dict[tuple[str, str], int] = {}  # (token, spender) -> atomic allowance
        self.approvals: list[dict] = []  # audit trail

    def quote_buy(self, token: str, usd_in: Decimal) -> Quote:
        q, self.last_price = self.client.quote_buy(token, usd_in, self.decimals_of(token))
        return q

    def quote_sell(self, token: str, qty: Decimal) -> Quote:
        q, self.last_price = self.client.quote_sell(token, qty, self.decimals_of(token), priority=True)
        return q

    def eth_usd(self) -> Optional[Decimal]:
        if self._eth_usd:
            return self._eth_usd()
        ts, px = self._eth_cache
        if px is not None and self.clock.now_ms() - ts < 120_000:
            return px
        try:
            p = self.client.price(WETH_BASE, USDC_BASE, 10 ** 18)
            px = from_atomic(p.raw_buy_amount, USDC_DECIMALS)
            self._eth_cache = (self.clock.now_ms(), px)
        except VenueError:
            pass
        return px

    def _wei_to_usd(self, wei: Optional[int], fallback: str) -> Decimal:
        px = self.eth_usd()
        if wei is None or px is None:
            return Decimal(fallback)
        return Decimal(wei) / WEI * px

    def network_fee_usd(self) -> Decimal:
        lp = self.last_price
        return self._wei_to_usd(lp.total_network_fee_wei if lp else None, "0.10")

    # -- approval discipline (loss register F5) --------------------------
    def _sell_side(self, o: Order) -> tuple[str, int]:
        if o.side is Side.BUY:
            return USDC_BASE, to_atomic(o.qty, USDC_DECIMALS)
        return o.token, to_atomic(o.qty, self.decimals_of(o.token))

    def _before_swap(self, o: Order) -> Optional[str]:
        token, amount = self._sell_side(o)
        key = (token, ALLOWANCE_HOLDER)
        current = self.allowances.get(key, 0)
        if current != 0:
            return f"stale allowance {current} on {token}: refusing to stack approvals"
        self.allowances[key] = amount  # approve EXACTLY the sell amount
        self.approvals.append({"token": token, "spender": ALLOWANCE_HOLDER, "amount": amount,
                               "cid": o.client_order_id, "ts": self.clock.now_ms()})
        return None

    def _after_swap(self, o: Order, ok: bool) -> None:
        token, _ = self._sell_side(o)
        # Filled: the swap consumed the allowance. Failed: we revoke it (approve 0).
        self.allowances[(token, ALLOWANCE_HOLDER)] = 0

    def _extra_fee_usd(self, o: Order) -> Decimal:
        gp = self.last_price.gas_price_wei if self.last_price else None
        return self._wei_to_usd(APPROVE_GAS * gp if gp else None, "0.02")
