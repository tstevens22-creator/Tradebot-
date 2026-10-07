"""Read-only Solana JSON-RPC: token safety from the chain itself, plus
signature status for reconciliation. It never signs or sends anything.

Token safety (loss register C2-C4, C7):
  * classic SPL Token program: no transfer fee possible -> tax 0
  * Token-2022: extensions are inspected. Hazards that can trap or drain a
    holder are flagged, and the risk gate rejects any flagged token:
      PERMANENT_DELEGATE    an authority can move/burn tokens from any wallet
      TRANSFER_HOOK         a program runs on every transfer (can block sells)
      TRANSFER_HOOK_MUTABLE hook authority can install one after entry
      TRANSFER_FEE_MUTABLE  fee authority can raise the transfer fee after entry
      NON_TRANSFERABLE / DEFAULT_FROZEN / PAUSABLE
  * unknown token program -> everything unknown -> rejected

The public RPC (api.mainnet-beta.solana.com) rate-limits heavy calls such as
getTokenLargestAccounts (observed 2026-10-07). Production needs a paid RPC
(SOLANA_RPC_URL).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional

from ..chains import SOLANA
from ..models import TokenSafety
from ..ratelimit import TokenBucket
from ..venues.base import RateLimited, VenueError, VenueTimeout

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class RpcError(VenueError):
    pass


@dataclass(frozen=True)
class MintInfo:
    mint: str
    program: str
    decimals: int
    supply: int
    mint_authority: bool
    freeze_authority: bool
    transfer_tax_bps: Optional[Decimal]
    hazards: tuple[str, ...]


def parse_mint(mint: str, result: Any) -> MintInfo:
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        raise RpcError(f"mint account not found: {mint}")
    owner = value.get("owner")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        raise RpcError("account is not a parsed mint")
    info = parsed.get("info") or {}
    try:
        decimals = int(info["decimals"])
        supply = int(info["supply"])
    except (KeyError, TypeError, ValueError):
        raise RpcError("mint missing decimals/supply") from None
    hazards: list[str] = []
    tax: Optional[Decimal]
    if owner == SPL_TOKEN:
        tax = Decimal(0)  # the classic program has no transfer-fee mechanism
    elif owner == TOKEN_2022:
        tax = Decimal(0)
        for ext in info.get("extensions") or []:
            name, state = ext.get("extension"), ext.get("state") or {}
            if name == "transferFeeConfig":
                try:
                    tax = Decimal(max(int(state["olderTransferFee"]["transferFeeBasisPoints"]),
                                      int(state["newerTransferFee"]["transferFeeBasisPoints"])))
                except (KeyError, TypeError, ValueError):
                    tax = None
                if state.get("transferFeeConfigAuthority"):
                    hazards.append("TRANSFER_FEE_MUTABLE")
            elif name == "permanentDelegate" and state.get("delegate"):
                hazards.append("PERMANENT_DELEGATE")
            elif name == "transferHook":
                if state.get("programId"):
                    hazards.append("TRANSFER_HOOK")
                if state.get("authority"):
                    hazards.append("TRANSFER_HOOK_MUTABLE")
            elif name == "nonTransferable":
                hazards.append("NON_TRANSFERABLE")
            elif name == "defaultAccountState" and str(state.get("accountState", "")).lower() == "frozen":
                hazards.append("DEFAULT_FROZEN")
            elif name in ("pausableConfig", "pausable"):
                hazards.append("PAUSABLE")
    else:
        raise RpcError(f"unknown token program {owner}")
    return MintInfo(mint, owner, decimals, supply, info.get("mintAuthority") is not None,
                    info.get("freezeAuthority") is not None, tax, tuple(hazards))


def parse_top10_pct(result: Any, supply: int) -> Optional[Decimal]:
    """Share of supply in the 10 largest token accounts. Pool vaults and
    exchange wallets are included, so this OVERSTATES concentration
    (the conservative direction)."""
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, list) or supply <= 0:
        return None
    try:
        top = sorted((int(a["amount"]) for a in value), reverse=True)[:10]
    except (KeyError, TypeError, ValueError):
        return None
    return Decimal(sum(top)) / Decimal(supply)


def map_signature_status(entry: Any) -> str:
    """-> CONFIRMED | FAILED | NOT_FOUND | PENDING. NOT_FOUND only becomes
    EXPIRED once the blockhash's lastValidBlockHeight has passed (caller's job)."""
    if entry is None:
        return "NOT_FOUND"
    if entry.get("err") is not None:
        return "FAILED"
    if entry.get("confirmationStatus") in ("confirmed", "finalized"):
        return "CONFIRMED"
    return "PENDING"


class SolanaRpc:
    def __init__(self, url: Optional[str] = None, timeout_s: float = 4.0,
                 bucket: Optional[TokenBucket] = None,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None):
        self.url = url or os.environ.get("SOLANA_RPC_URL") or PUBLIC_RPC
        self.timeout_s = timeout_s
        self.bucket = bucket or TokenBucket(rate_per_s=2.0, capacity=4, reserve=1)
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())

    def __repr__(self) -> str:  # RPC URLs often embed API keys
        return "SolanaRpc(url=***)"

    def call(self, method: str, params: list, priority: bool = False) -> Any:
        if not self.bucket.try_take(priority):
            raise RateLimited("RPC rate budget exhausted (local)")
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"content-type": "application/json"})
        try:
            raw = self._open(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(f"RPC 429 on {method}") from None
            raise RpcError(f"RPC HTTP {e.code} on {method}") from None
        except (TimeoutError, OSError) as e:
            raise VenueTimeout(f"RPC {method} failed: {type(e).__name__}") from None
        try:
            resp = json.loads(raw)
        except ValueError:
            raise RpcError("invalid JSON from RPC") from None
        err = resp.get("error") if isinstance(resp, dict) else None
        if err:
            if isinstance(err, dict) and err.get("code") == 429:
                raise RateLimited(f"RPC 429 on {method}")
            raise RpcError(f"RPC error on {method}: {err}")
        return resp.get("result")

    def mint_info(self, mint: str) -> MintInfo:
        return parse_mint(mint, self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}]))

    def token_safety(self, mint: str, now_ms: int, venue_stage: Optional[str] = None) -> TokenSafety:
        """On-chain safety. Sellability is NOT proven here (sell_simulation_ok
        is None), and the stage must come from the launchpad feed."""
        mi = self.mint_info(mint)
        try:
            top10 = parse_top10_pct(self.call("getTokenLargestAccounts", [mint]), mi.supply)
        except VenueError:
            top10 = None  # unknown -> the gate rejects
        return TokenSafety(token=mint, decimals=mi.decimals, mint_authority=mi.mint_authority,
                           freeze_authority=mi.freeze_authority, transfer_tax_bps=mi.transfer_tax_bps,
                           top10_holder_pct=top10, sell_simulation_ok=None, ts_ms=now_ms,
                           chain=SOLANA, venue_stage=venue_stage, hazards=mi.hazards)

    def signature_statuses(self, signatures: list[str]) -> list[str]:
        res = self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}], priority=True)
        value = res.get("value") if isinstance(res, dict) else None
        if not isinstance(value, list) or len(value) != len(signatures):
            raise RpcError("malformed getSignatureStatuses response")
        return [map_signature_status(v) for v in value]

    def block_height(self) -> int:
        return int(self.call("getBlockHeight", [], priority=True))
