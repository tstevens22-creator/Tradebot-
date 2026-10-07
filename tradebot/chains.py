"""Chains, token lifecycle stages and per-chain address validation.

Owner decisions (2026-10-07): trade Solana and Base; pump.fun tokens only
after graduation. Execution venues: Jupiter (Solana) and CDP/0x (Base). Only
the paper venue exists today.
"""
from __future__ import annotations

import re

SOLANA = "solana"
BASE = "base"
CHAINS = (SOLANA, BASE)

# Where a token's liquidity lives. Fed by the data adapter (Bitquery primary,
# Codex cross-check). Anything other than AMM is ineligible for entry, and
# None (unknown) is rejected like every other unknown.
STAGE_BONDING_CURVE = "bonding_curve"  # pump.fun pre-graduation
STAGE_MIGRATING = "migrating"  # graduation in progress: route may be broken
STAGE_AMM = "amm"  # graduated (PumpSwap) or a normal DEX pool
STAGES = (STAGE_BONDING_CURVE, STAGE_MIGRATING, STAGE_AMM)

_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58 mint
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_address(chain: str, address: str) -> str:
    """Validate an address for its chain and return its canonical form.

    EVM addresses are case-insensitive (checksum casing is cosmetic), so they
    are lower-cased. Otherwise one token could be tracked as two positions and
    slip past exposure limits. Solana base58 is case-sensitive and kept as is.
    """
    if chain == SOLANA:
        if not _SOLANA_RE.match(address):
            raise ValueError("not a Solana base58 mint address")
        return address
    if chain == BASE:
        if not _EVM_RE.match(address):
            raise ValueError("not a 0x-prefixed 20-byte EVM address")
        return address.lower()
    raise ValueError(f"unsupported chain {chain!r}; expected one of {CHAINS}")
