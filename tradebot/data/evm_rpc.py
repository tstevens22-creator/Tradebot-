"""Read-only EVM JSON-RPC (Base) for token safety. It never signs or sends.

Checks (all fail-closed):
  * decimals()              -> required
  * owner() (Ownable)       -> zero address = renounced; non-zero = an owner
                               who may be able to mint, pause or blacklist
                               (treated as mint AND freeze authority);
                               empty "0x" result = no owner() function -> UNKNOWN
  * upgradeable proxy       -> EIP-1967 implementation slot, or the legacy
                               zeppelinos slot (used by e.g. Base USDC);
                               non-zero = UPGRADEABLE_PROXY hazard (logic can
                               be swapped for a honeypot after entry)
Transfer taxes come from 0x tokenMetadata. Holders, stage and creator come
from Codex. Sellability needs a real sell simulation (live step).
Verified against real Base RPC responses, 2026-10-07 (tests/fixtures/base).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..chains import BASE, normalize_address
from ..models import TokenSafety
from ..ratelimit import TokenBucket
from ..venues.base import RateLimited, VenueError, VenueTimeout

PUBLIC_BASE_RPC = "https://mainnet.base.org"
SEL_DECIMALS = "0x313ce567"
SEL_OWNER = "0x8da5cb5b"
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
ZOS_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"


class EvmRpcError(VenueError):
    pass


def word_to_int(hexstr: Any) -> Optional[int]:
    if not isinstance(hexstr, str) or not hexstr.startswith("0x") or len(hexstr) <= 2:
        return None  # "0x" = empty return (function absent)
    try:
        return int(hexstr, 16)
    except ValueError:
        return None


def word_to_address(hexstr: Any) -> Optional[str]:
    v = word_to_int(hexstr)
    return None if v is None else "0x" + format(v, "040x")


@dataclass(frozen=True)
class EvmTokenInfo:
    token: str
    decimals: Optional[int]
    owner: Optional[str]  # None = unknown (no owner() or call failed)
    owner_renounced: Optional[bool]
    is_proxy: bool
    hazards: tuple[str, ...]


def interpret(token: str, decimals_hex: Any, owner_hex: Any, slot_1967: Any, slot_zos: Any) -> EvmTokenInfo:
    dec = word_to_int(decimals_hex)
    if dec is not None and not (0 <= dec <= 36):
        dec = None
    owner = word_to_address(owner_hex)
    renounced = None if owner is None else int(owner, 16) == 0
    proxy = any((word_to_int(s) or 0) != 0 for s in (slot_1967, slot_zos))
    return EvmTokenInfo(token, dec, owner, renounced, proxy, ("UPGRADEABLE_PROXY",) if proxy else ())


class EvmRpc:
    def __init__(self, url: Optional[str] = None, timeout_s: float = 4.0, bucket: Optional[TokenBucket] = None,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None):
        self.url = url or os.environ.get("BASE_RPC_URL") or PUBLIC_BASE_RPC
        self.timeout_s = timeout_s
        self.bucket = bucket or TokenBucket(rate_per_s=4.0, capacity=8, reserve=1)
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())

    def __repr__(self) -> str:
        return "EvmRpc(url=***)"

    def call(self, method: str, params: list) -> Any:
        if not self.bucket.try_take():
            raise RateLimited("EVM RPC budget exhausted (local)")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json"})
        try:
            raw = self._open(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("EVM RPC 429") from None
            raise EvmRpcError(f"EVM RPC HTTP {e.code}") from None
        except (TimeoutError, OSError) as e:
            raise VenueTimeout(f"EVM RPC {method} failed: {type(e).__name__}") from None
        try:
            resp = json.loads(raw)
        except ValueError:
            raise EvmRpcError("invalid JSON from EVM RPC") from None
        if resp.get("error"):
            return None  # reverted eth_call etc. -> unknown
        return resp.get("result")

    def token_info(self, token: str) -> EvmTokenInfo:
        token = normalize_address(BASE, token)
        return interpret(
            token,
            self.call("eth_call", [{"to": token, "data": SEL_DECIMALS}, "latest"]),
            self.call("eth_call", [{"to": token, "data": SEL_OWNER}, "latest"]),
            self.call("eth_getStorageAt", [token, EIP1967_IMPL_SLOT, "latest"]),
            self.call("eth_getStorageAt", [token, ZOS_IMPL_SLOT, "latest"]),
        )

    def token_safety(self, token: str, now_ms: int, transfer_tax_bps=None, venue_stage=None) -> TokenSafety:
        info = self.token_info(token)
        authority = None if info.owner_renounced is None else not info.owner_renounced
        return TokenSafety(token=info.token, decimals=info.decimals, mint_authority=authority,
                           freeze_authority=authority, transfer_tax_bps=transfer_tax_bps, top10_holder_pct=None,
                           sell_simulation_ok=None, ts_ms=now_ms, chain=BASE, venue_stage=venue_stage,
                           hazards=info.hazards)
