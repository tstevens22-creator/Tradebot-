"""Launchpad / market-structure feeds and their cross-check.

Owner decision (checklist 1f): Bitquery = primary, Codex = secondary.
  * Solana pump.fun tokens: Bitquery gives the stage (created by pump.fun?
    migrated?). Codex must not contradict it.
  * Everything else (non-pump.fun Solana tokens, all Base tokens): Codex is the
    only stage source. Bitquery's Base coverage is unverified.
  * Liquidity, holder concentration and creator history come from Codex.
Rules (fail closed):
  * primary errored or stale for a token it should cover -> stage unknown
  * the two feeds disagree on stage -> stage unknown (reason recorded)
  * any missing field -> None -> the risk gate rejects
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

from ..chains import BASE, SOLANA
from ..ratelimit import TokenBucket
from ..venues.base import RateLimited, VenueError, VenueTimeout


class FeedError(VenueError):
    pass


@dataclass(frozen=True)
class FeedReading:
    source: str
    token: str
    chain: str
    ts_ms: int
    stage: Optional[str] = None  # chains.STAGES or None (unknown / not covered)
    is_pumpfun: Optional[bool] = None
    liquidity_usd: Optional[Decimal] = None
    top10_holder_pct: Optional[Decimal] = None  # fraction 0..1
    creator: Optional[str] = None
    creator_tokens_created: Optional[int] = None
    creator_tokens_migrated: Optional[int] = None


@dataclass(frozen=True)
class FeedView:
    """Consensus the bot acts on. ``notes`` explains every downgrade to unknown."""
    token: str
    chain: str
    ts_ms: int
    stage: Optional[str]
    liquidity_usd: Optional[Decimal]
    top10_holder_pct: Optional[Decimal]
    creator_tokens_created: Optional[int]
    creator_tokens_migrated: Optional[int]
    notes: tuple[str, ...] = field(default_factory=tuple)


def consensus(token: str, chain: str, now_ms: int, max_age_ms: int,
              primary: Optional[FeedReading], secondary: Optional[FeedReading]) -> FeedView:
    notes: list[str] = []

    def fresh(r: Optional[FeedReading], name: str) -> Optional[FeedReading]:
        if r is None:
            notes.append(f"{name}: no reading")
            return None
        if now_ms - r.ts_ms > max_age_ms:
            notes.append(f"{name}: stale ({now_ms - r.ts_ms} ms)")
            return None
        return r

    p, s = fresh(primary, "bitquery"), fresh(secondary, "codex")
    stage: Optional[str] = None
    if chain == SOLANA:
        if p is None:
            notes.append("primary silent -> stage unknown")
        elif p.is_pumpfun:
            stage = p.stage
            if s is not None and s.stage is not None and s.stage != p.stage:
                notes.append(f"stage disagreement: bitquery={p.stage} codex={s.stage}")
                stage = None
        elif p.is_pumpfun is False:
            stage = s.stage if s else None  # not a pump.fun token: Codex covers other launchpads/pools
            if stage is None:
                notes.append("non-pump.fun token and no Codex stage")
        else:
            notes.append("primary could not classify token")
    elif chain == BASE:
        stage = s.stage if s else None  # Codex is the only verified Base source
        if stage is None:
            notes.append("no Codex stage for Base token")
    return FeedView(token, chain, now_ms, stage,
                    s.liquidity_usd if s else None, s.top10_holder_pct if s else None,
                    s.creator_tokens_created if s else None, s.creator_tokens_migrated if s else None,
                    tuple(notes))


# ---------------------------------------------------------------------------
class GraphQLClient:
    def __init__(self, url: str, auth_header: Optional[str], bucket: TokenBucket, timeout_s: float = 5.0,
                 opener: Optional[Callable[[urllib.request.Request, float], bytes]] = None):
        self.url, self.auth_header, self.bucket, self.timeout_s = url, auth_header, bucket, timeout_s
        self._open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t).read())

    def __repr__(self) -> str:
        return f"GraphQLClient(url={self.url!r}, auth=***)"

    def query(self, query: str, variables: Optional[dict] = None) -> Any:
        if not self.auth_header:
            raise FeedError("API key not configured")
        if not self.bucket.try_take():
            raise RateLimited("feed rate budget exhausted (local)")
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "content-type": "application/json", "Authorization": self.auth_header})
        try:
            raw = self._open(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited("feed 429") from None
            raise FeedError(f"feed HTTP {e.code}") from None
        except (TimeoutError, OSError) as e:
            raise VenueTimeout(f"feed request failed: {type(e).__name__}") from None
        try:
            resp = json.loads(raw)
        except ValueError:
            raise FeedError("invalid JSON from feed") from None
        if not isinstance(resp, dict) or resp.get("errors"):
            raise FeedError(f"GraphQL errors: {str(resp.get('errors') if isinstance(resp, dict) else resp)[:200]}")
        if not isinstance(resp.get("data"), dict):
            raise FeedError("GraphQL response without data")
        return resp["data"]


def apply_view(safety, view: FeedView):
    """Merge a feed consensus into on-chain TokenSafety. Holder concentration
    takes the WORSE (higher) of the on-chain and feed values."""
    import dataclasses
    tops = [x for x in (safety.top10_holder_pct, view.top10_holder_pct) if x is not None]
    return dataclasses.replace(
        safety, venue_stage=view.stage, top10_holder_pct=max(tops) if tops else None,
        creator_tokens_created=view.creator_tokens_created,
        creator_tokens_migrated=view.creator_tokens_migrated)
