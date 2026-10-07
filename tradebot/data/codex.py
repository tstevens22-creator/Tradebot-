"""Codex (graph.codex.io): launchpad stage, liquidity, holders, creator history.

Verified from the Codex docs (2026-10-07):
  * endpoint https://graph.codex.io/graphql, header ``Authorization: <API_KEY>``
    (no Bearer prefix)
  * network ids: Solana 1399811149, Base 8453
  * ``filterTokens(tokens: ["address:networkId"])`` returns known tokens and
    silently omits unindexed ones (-> unknown here)
  * ``token.launchpad {completed, migrated}``, ``liquidity``,
    ``token.top10HoldersPercent``, and ``token.creator {tokensCreatedCount,
    tokensMigratedCount}``
UNVERIFIED (no key here): the scale of ``top10HoldersPercent``. Interpreted
conservatively (see ``_top10_fraction``). Check on the first keyed call.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from ..chains import BASE, SOLANA, STAGE_AMM, STAGE_BONDING_CURVE, STAGE_MIGRATING
from ..ratelimit import TokenBucket
from .feeds import FeedError, FeedReading, GraphQLClient

URL = "https://graph.codex.io/graphql"
NETWORK_IDS = {SOLANA: 1399811149, BASE: 8453}

TOKENS_QUERY = """
query ($tokens: [String]) {
  filterTokens(tokens: $tokens) {
    results {
      liquidity
      token {
        address
        networkId
        creatorAddress
        top10HoldersPercent
        launchpad { launchpadName completed migrated graduationPercent migratedAt }
        creator { tokensCreatedCount tokensMigratedCount }
      }
    }
  }
}
"""


def _dec(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d >= 0 else None


def _top10_fraction(v: Any) -> Optional[Decimal]:
    """Codex calls it a percentage but never documents the scale. Values > 1
    are read as percent; values <= 1 as a fraction. A wrong guess can only
    OVERSTATE concentration (safe direction), never understate it."""
    d = _dec(v)
    if d is None:
        return None
    return d / 100 if d > 1 else d


def stage_from_launchpad(lp: Any) -> Optional[str]:
    if lp is None:
        return STAGE_AMM  # not a launchpad token: ordinary DEX pool(s)
    if not isinstance(lp, dict):
        return None
    if lp.get("migrated") is True:
        return STAGE_AMM
    if lp.get("completed") is True:
        return STAGE_MIGRATING  # curve filled, migration not confirmed yet
    if lp.get("completed") is False and lp.get("migrated") is False:
        return STAGE_BONDING_CURVE
    return None


def parse_tokens(data: Any, chain: str, now_ms: int) -> dict[str, FeedReading]:
    try:
        results = data["filterTokens"]["results"] or []
    except (KeyError, TypeError):
        raise FeedError("unexpected filterTokens shape") from None
    out: dict[str, FeedReading] = {}
    for r in results:
        tok = (r or {}).get("token") or {}
        addr = tok.get("address")
        if not addr or tok.get("networkId") != NETWORK_IDS[chain]:
            continue
        key = addr.lower() if chain == BASE else addr
        creator = tok.get("creator") or {}
        created, migrated = creator.get("tokensCreatedCount"), creator.get("tokensMigratedCount")
        out[key] = FeedReading(
            source="codex", token=key, chain=chain, ts_ms=now_ms,
            stage=stage_from_launchpad(tok["launchpad"]) if "launchpad" in tok else None,
            is_pumpfun=None, liquidity_usd=_dec(r.get("liquidity")),
            top10_holder_pct=_top10_fraction(tok.get("top10HoldersPercent")),
            creator=tok.get("creatorAddress"),
            creator_tokens_created=created if isinstance(created, int) and not isinstance(created, bool) else None,
            creator_tokens_migrated=migrated if isinstance(migrated, int) and not isinstance(migrated, bool) else None,
        )
    return out


class CodexClient:
    def __init__(self, api_key: Optional[str] = None, bucket: Optional[TokenBucket] = None,
                 opener: Optional[Callable] = None, url: str = URL):
        key = api_key if api_key is not None else os.environ.get("CODEX_API_KEY")
        self.gql = GraphQLClient(url, key or None, bucket or TokenBucket(rate_per_s=2.0, capacity=4), opener=opener)

    def readings(self, chain: str, tokens: list[str], now_ms: int) -> dict[str, FeedReading]:
        if not tokens:
            return {}
        if len(tokens) > 200:
            raise FeedError("Codex accepts at most 200 tokens per call")
        ids = [f"{t}:{NETWORK_IDS[chain]}" for t in tokens]
        return parse_tokens(self.gql.query(TOKENS_QUERY, {"tokens": ids}), chain, now_ms)
