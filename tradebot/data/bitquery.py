"""Bitquery (streaming.bitquery.io): primary pump.fun lifecycle source.

Verified from Bitquery's docs corpus (docs.bitquery.io/llms-full.txt, 2026-10-07):
  * endpoint https://streaming.bitquery.io/graphql, ``Authorization: Bearer <token>``
  * pump.fun program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
  * creation: Instructions with Method in ["create","create_v2"] and the mint
    in Accounts
  * migration check: pump.fun Instructions with the mint in Accounts whose
    Logs include "Migrate" (successful transactions)
Both checks run in one request (GraphQL aliases).

Free trial is real-time only. Historical lookups need the archive/combined
dataset add-on, so on the trial an older token may look "not pump.fun".
Codex is the cross-check for that case.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from ..chains import SOLANA, STAGE_AMM, STAGE_BONDING_CURVE
from ..ratelimit import TokenBucket
from .feeds import FeedError, FeedReading, GraphQLClient

URL = "https://streaming.bitquery.io/graphql"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

STATUS_QUERY = """
query ($token: String) {
  Solana {
    created: Instructions(
      where: {Instruction: {Program: {Address: {is: "%(p)s"}, Method: {in: ["create", "create_v2"]}},
                            Accounts: {includes: {Address: {is: $token}}}},
              Transaction: {Result: {Success: true}}}
      limit: {count: 1}
    ) { Block { Time } Transaction { Signer } }
    migrated: Instructions(
      where: {Instruction: {Program: {Address: {is: "%(p)s"}},
                            Accounts: {includes: {Address: {is: $token}}},
                            Logs: {includes: {includes: "Migrate"}}},
              Transaction: {Result: {Success: true}}}
      limit: {count: 1}
    ) { Block { Time } }
  }
}
""" % {"p": PUMPFUN_PROGRAM}


def parse_status(data: Any, token: str, now_ms: int) -> FeedReading:
    try:
        sol = data["Solana"]
        created, migrated = sol["created"], sol["migrated"]
    except (KeyError, TypeError):
        raise FeedError("unexpected Bitquery response shape") from None
    if not isinstance(created, list) or not isinstance(migrated, list):
        raise FeedError("Bitquery fields are not lists")
    if not created:
        return FeedReading("bitquery", token, SOLANA, now_ms, stage=None, is_pumpfun=False)
    creator = ((created[0] or {}).get("Transaction") or {}).get("Signer")
    return FeedReading("bitquery", token, SOLANA, now_ms,
                       stage=STAGE_AMM if migrated else STAGE_BONDING_CURVE, is_pumpfun=True, creator=creator)


class BitqueryClient:
    def __init__(self, token: Optional[str] = None, bucket: Optional[TokenBucket] = None,
                 opener: Optional[Callable] = None, url: str = URL):
        tok = token if token is not None else os.environ.get("BITQUERY_API_KEY")
        self.gql = GraphQLClient(url, f"Bearer {tok}" if tok else None,
                                 bucket or TokenBucket(rate_per_s=2.0, capacity=4), opener=opener)

    def reading(self, token: str, now_ms: int) -> FeedReading:
        return parse_status(self.gql.query(STATUS_QUERY, {"token": token}), token, now_ms)
