"""Birdeye market-data adapter (read-only).

Auth: ``X-API-KEY`` header (from the BIRDEYE_API_KEY env var, never config),
chain via ``x-chain``.

IMPORTANT LIMITS
  * Birdeye ``/defi/price`` is an aggregated INDICATIVE price, not an
    executable quote. It can feed signals and sanity checks, but stops and
    markouts must use executable router quotes for the actual size.
  * Response field names below follow Birdeye's public docs as found during
    development (``data.value``, ``data.updateUnixTime``, ``data.liquidity``;
    token_security: ``freezeable``, ``freezeAuthority``,
    ``transferFeeEnable``, ``top10HolderPercent``). VERIFY against the live
    API before relying on them. Any missing field maps to None (UNKNOWN), and
    the risk gate rejects UNKNOWN.
  * Mint authority, the exact transfer-fee bps, and sellability are NOT
    derived here. They must come from an on-chain read / sell simulation, so
    they default to None (-> gate rejects).
  * Token name/symbol/metadata are never returned: untrusted text stays out of
    the decision path.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from ..models import TokenSafety

BASE_URL = "https://public-api.birdeye.so"


class BirdeyeError(RuntimeError):
    pass


class TokenBucket:
    """Rate-limit budget. ``reserve`` tokens are held back for the exit path,
    so analytics can never starve protective exits of quota."""

    def __init__(self, rate_per_s: float, capacity: int, reserve: int = 0,
                 now: Callable[[], float] = time.monotonic):
        self.rate, self.capacity, self.reserve, self.now = rate_per_s, capacity, reserve, now
        self.tokens = float(capacity)
        self.t = now()
        self.lock = threading.Lock()

    def try_take(self, priority: bool = False) -> bool:
        with self.lock:
            n = self.now()
            self.tokens = min(self.capacity, self.tokens + (n - self.t) * self.rate)
            self.t = n
            floor = 0 if priority else self.reserve
            if self.tokens - 1 >= floor:
                self.tokens -= 1
                return True
            return False


@dataclass(frozen=True)
class IndicativePrice:
    token: str
    price: Optional[Decimal]
    liquidity_usd: Optional[Decimal]
    ts_source_ms: Optional[int]
    ts_received_ms: int


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def parse_price(token: str, body: dict, received_ms: int) -> IndicativePrice:
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), dict):
        raise BirdeyeError(f"unexpected price response: success={body.get('success') if isinstance(body, dict) else None}")
    d = body["data"]
    ts = d.get("updateUnixTime")
    return IndicativePrice(
        token=token,
        price=_dec(d.get("value")),
        liquidity_usd=_dec(d.get("liquidity")),
        ts_source_ms=int(ts) * 1000 if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None,
        ts_received_ms=received_ms,
    )


def parse_security(token: str, body: dict, decimals: Optional[int], received_ms: int,
                   chain: Optional[str] = None) -> TokenSafety:
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), dict):
        raise BirdeyeError("unexpected token_security response")
    d = body["data"]
    freeze: Optional[bool]
    if d.get("freezeable") is True or d.get("freezeAuthority"):
        freeze = True
    elif d.get("freezeable") is False and "freezeAuthority" in d and not d.get("freezeAuthority"):
        freeze = False
    else:
        freeze = None
    tax: Optional[Decimal] = Decimal(0) if d.get("transferFeeEnable") is False else None
    top10 = _dec(d.get("top10HolderPercent"))
    return TokenSafety(
        token=token, decimals=decimals, mint_authority=None, freeze_authority=freeze,
        transfer_tax_bps=tax, top10_holder_pct=top10, sell_simulation_ok=None, ts_ms=received_ms,
        chain=chain, venue_stage=None,  # stage comes from the launchpad feed (Bitquery/Codex), not Birdeye
    )


class BirdeyeClient:
    def __init__(self, api_key: Optional[str] = None, chain: str = "solana",
                 bucket: Optional[TokenBucket] = None, timeout_s: float = 2.0,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None):
        self.api_key = api_key or os.environ.get("BIRDEYE_API_KEY")
        if not self.api_key:
            raise BirdeyeError("BIRDEYE_API_KEY not set")
        self.chain = chain
        self.bucket = bucket or TokenBucket(rate_per_s=1.0, capacity=5, reserve=1)
        self.timeout_s = timeout_s
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())

    def __repr__(self) -> str:  # never leak the key into logs
        return f"BirdeyeClient(chain={self.chain!r}, api_key=***)"

    def _get(self, path: str, params: dict, priority: bool) -> dict:
        if not self.bucket.try_take(priority):
            raise BirdeyeError("rate budget exhausted (local)")
        url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "X-API-KEY": self.api_key, "x-chain": self.chain, "accept": "application/json"})
        try:
            raw = self._open(req, self.timeout_s)
        except Exception as e:  # network errors surface as BirdeyeError; caller treats data as stale
            raise BirdeyeError(f"request failed: {type(e).__name__}") from None
        try:
            return json.loads(raw)
        except ValueError:
            raise BirdeyeError("invalid JSON") from None

    def price(self, token: str, priority: bool = False) -> IndicativePrice:
        body = self._get("/defi/price", {"address": token, "include_liquidity": "true"}, priority)
        return parse_price(token, body, int(time.time() * 1000))

    def security(self, token: str, decimals: Optional[int]) -> TokenSafety:
        body = self._get("/defi/token_security", {"address": token}, priority=False)
        return parse_security(token, body, decimals, int(time.time() * 1000), chain=self.chain)
