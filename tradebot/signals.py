"""AI / strategy output boundary.

An AI may PROPOSE an entry. The proposal schema is deliberately tiny and
strict: unknown keys are rejected, so a model -- or a prompt-injected token
name -- cannot smuggle in stop widening, limit changes or "skip checks"
flags. Exits are never proposed by the AI. They come from deterministic
protection code only.

Token metadata (name, symbol, description) is untrusted text. It is never
passed to the risk gate, and it must be treated as data, never as
instructions, if it is shown to a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

ALLOWED_KEYS = {"proposal_id", "token", "usd_size", "expected_edge_bps", "strategy"}
_TOKEN_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58 Solana mint
_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")


class ProposalError(ValueError):
    pass


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    token: str
    usd_size: Decimal
    expected_edge_bps: Decimal
    strategy: str = "default"


def parse_proposal(raw: Any) -> TradeProposal:
    if not isinstance(raw, dict):
        raise ProposalError("proposal must be an object")
    extra = set(raw) - ALLOWED_KEYS
    if extra:
        raise ProposalError(f"proposal contains forbidden keys: {sorted(extra)}")
    try:
        pid = str(raw["proposal_id"])
        token = str(raw["token"])
        size = Decimal(str(raw["usd_size"]))
        edge = Decimal(str(raw["expected_edge_bps"]))
        strategy = str(raw.get("strategy", "default"))
    except (KeyError, InvalidOperation) as e:
        raise ProposalError(f"malformed proposal: {e!r}") from None
    if not _ID_RE.match(pid) or not _ID_RE.match(strategy):
        raise ProposalError("invalid proposal_id/strategy")
    if not _TOKEN_RE.match(token):
        raise ProposalError("token must be a base58 mint address")
    if not size.is_finite() or size <= 0:
        raise ProposalError("usd_size must be > 0")
    if not edge.is_finite():
        raise ProposalError("expected_edge_bps must be finite")
    return TradeProposal(pid, token, size, edge, strategy)
