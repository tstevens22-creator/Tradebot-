"""Step 3: Bitquery (primary) + Codex (secondary) feeds and their cross-check.

No API keys exist in this environment, so responses are built from each
provider's DOCUMENTED schema (Codex filterTokens; Bitquery Solana.Instructions).
Field names were checked against the docs on 2026-10-07; real keyed calls must
re-verify them (docs/05_READINESS.md).
"""
import json
import urllib.error
from decimal import Decimal

import pytest

from tradebot.chains import BASE, SOLANA, STAGE_AMM, STAGE_BONDING_CURVE, STAGE_MIGRATING
from tradebot.data.bitquery import PUMPFUN_PROGRAM, STATUS_QUERY, BitqueryClient, parse_status
from tradebot.data.codex import NETWORK_IDS, CodexClient, parse_tokens, stage_from_launchpad
from tradebot.data.feeds import FeedError, FeedReading, apply_view, consensus
from tradebot.ratelimit import TokenBucket
from tradebot.sim import BASE_TOKEN, TOKEN, Sim, paper_config, safe_token
from tradebot.venues.base import RateLimited

NOW = 1_790_000_000_000


def codex_result(addr=TOKEN, net=NETWORK_IDS[SOLANA], launchpad="absent", liq="250000.5", top10=34.5,
                 created=3, migrated=2):
    tok = {"address": addr, "networkId": net, "creatorAddress": "Creator1111", "top10HoldersPercent": top10,
           "creator": {"tokensCreatedCount": created, "tokensMigratedCount": migrated}}
    if launchpad != "absent":
        tok["launchpad"] = launchpad
    return {"filterTokens": {"results": [{"liquidity": liq, "token": tok}]}}


def bq(created=True, migrated=True):
    return {"Solana": {
        "created": [{"Block": {"Time": "2026-10-07T00:00:00Z"}, "Transaction": {"Signer": "Dev111"}}] if created else [],
        "migrated": [{"Block": {"Time": "2026-10-07T01:00:00Z"}}] if migrated else []}}


# ---- Codex -------------------------------------------------------------
@pytest.mark.parametrize("lp,stage", [
    ({"migrated": True, "completed": True}, STAGE_AMM),
    ({"migrated": False, "completed": True}, STAGE_MIGRATING),
    ({"migrated": False, "completed": False}, STAGE_BONDING_CURVE),
    (None, STAGE_AMM),  # not a launchpad token
    ({"launchpadName": "Pump.fun"}, None),  # flags missing -> unknown
])
def test_codex_stage_mapping(lp, stage):
    assert stage_from_launchpad(lp) == stage


def test_codex_parse_reading():
    r = parse_tokens(codex_result(launchpad={"migrated": True, "completed": True}), SOLANA, NOW)[TOKEN]
    assert r.stage == STAGE_AMM and r.liquidity_usd == Decimal("250000.5")
    assert r.top10_holder_pct == Decimal("0.345")
    assert (r.creator_tokens_created, r.creator_tokens_migrated) == (2 + 1, 2)


def test_codex_missing_launchpad_field_is_unknown_not_amm():
    r = parse_tokens(codex_result(), SOLANA, NOW)[TOKEN]
    assert r.stage is None


def test_codex_top10_scale_is_conservative():
    assert parse_tokens(codex_result(top10=0.6), SOLANA, NOW)[TOKEN].top10_holder_pct == Decimal("0.6")
    assert parse_tokens(codex_result(top10=60), SOLANA, NOW)[TOKEN].top10_holder_pct == Decimal("0.6")


def test_codex_rejects_wrong_network_and_bad_counts():
    assert parse_tokens(codex_result(net=NETWORK_IDS[BASE]), SOLANA, NOW) == {}
    r = parse_tokens(codex_result(created=True, migrated="2"), SOLANA, NOW)[TOKEN]
    assert r.creator_tokens_created is None and r.creator_tokens_migrated is None
    with pytest.raises(FeedError):
        parse_tokens({"nope": 1}, SOLANA, NOW)


def test_codex_base_address_lowercased():
    mixed = "0x4ED4E862860beD51a9570b96d89aF5E1B0Efefed"
    out = parse_tokens(codex_result(addr=mixed, net=NETWORK_IDS[BASE], launchpad=None), BASE, NOW)
    assert BASE_TOKEN in out


# ---- Bitquery ----------------------------------------------------------
def test_bitquery_status():
    assert parse_status(bq(True, True), TOKEN, NOW).stage == STAGE_AMM
    r = parse_status(bq(True, False), TOKEN, NOW)
    assert r.stage == STAGE_BONDING_CURVE and r.is_pumpfun and r.creator == "Dev111"
    r = parse_status(bq(False, False), TOKEN, NOW)
    assert r.is_pumpfun is False and r.stage is None
    with pytest.raises(FeedError):
        parse_status({"Solana": {"created": None, "migrated": []}}, TOKEN, NOW)


def test_bitquery_query_uses_documented_filters():
    assert PUMPFUN_PROGRAM in STATUS_QUERY and '"create", "create_v2"' in STATUS_QUERY
    assert 'includes: "Migrate"' in STATUS_QUERY and "limit: {count: 1}" in STATUS_QUERY


# ---- GraphQL transport -------------------------------------------------
def opener_returning(payload, seen=None, http=None):
    def opener(req, t):
        if seen is not None:
            seen.append((dict(req.header_items()), json.loads(req.data)))
        if http:
            raise urllib.error.HTTPError(req.full_url, http, "x", {}, None)
        return json.dumps(payload).encode()
    return opener


def bucket():
    return TokenBucket(1000, 1000)


def test_auth_headers_per_provider():
    seen = []
    CodexClient(api_key="CK", bucket=bucket(), opener=opener_returning({"data": codex_result()}, seen)) \
        .readings(SOLANA, [TOKEN], NOW)
    assert seen[0][0]["Authorization"] == "CK"  # Codex: no Bearer prefix
    assert seen[0][1]["variables"] == {"tokens": [f"{TOKEN}:1399811149"]}
    seen.clear()
    BitqueryClient(token="BQ", bucket=bucket(), opener=opener_returning({"data": bq()}, seen)).reading(TOKEN, NOW)
    assert seen[0][0]["Authorization"] == "Bearer BQ"
    assert seen[0][1]["variables"] == {"token": TOKEN}


def test_transport_errors():
    with pytest.raises(FeedError):
        CodexClient(api_key="", bucket=bucket()).readings(SOLANA, [TOKEN], NOW)  # no key
    with pytest.raises(FeedError):
        CodexClient(api_key="k", bucket=bucket(),
                    opener=opener_returning({"errors": [{"message": "bad field"}]})).readings(SOLANA, [TOKEN], NOW)
    with pytest.raises(RateLimited):
        BitqueryClient(token="k", bucket=bucket(), opener=opener_returning({}, http=429)).reading(TOKEN, NOW)
    c = CodexClient(api_key="SECRETKEY", bucket=bucket())
    assert "SECRETKEY" not in repr(c.gql)
    with pytest.raises(FeedError):
        c.readings(SOLANA, [TOKEN] * 201, NOW)


# ---- consensus -----------------------------------------------------------
def rd(source, stage, pump=None, ts=NOW, **kw):
    return FeedReading(source, TOKEN, SOLANA, ts, stage=stage, is_pumpfun=pump, **kw)


def test_consensus_agree():
    v = consensus(TOKEN, SOLANA, NOW, 30_000, rd("bitquery", STAGE_AMM, True),
                  rd("codex", STAGE_AMM, liquidity_usd=Decimal(9e5), creator_tokens_created=2,
                     creator_tokens_migrated=1))
    assert v.stage == STAGE_AMM and v.liquidity_usd == Decimal(9e5) and v.notes == ()


def test_feed_disagreement_blocks_entry():
    v = consensus(TOKEN, SOLANA, NOW, 30_000, rd("bitquery", STAGE_AMM, True), rd("codex", STAGE_BONDING_CURVE))
    assert v.stage is None and "disagreement" in v.notes[0]


def test_primary_silent_or_stale_blocks():
    assert consensus(TOKEN, SOLANA, NOW, 30_000, None, rd("codex", STAGE_AMM)).stage is None
    stale = rd("bitquery", STAGE_AMM, True, ts=NOW - 60_000)
    assert consensus(TOKEN, SOLANA, NOW, 30_000, stale, rd("codex", STAGE_AMM)).stage is None


def test_secondary_missing_does_not_override_primary_stage():
    v = consensus(TOKEN, SOLANA, NOW, 30_000, rd("bitquery", STAGE_AMM, True), None)
    assert v.stage == STAGE_AMM and v.liquidity_usd is None  # but no liquidity -> gate rejects anyway


def test_non_pumpfun_token_uses_codex_and_base_codex_only():
    assert consensus(TOKEN, SOLANA, NOW, 30_000, rd("bitquery", None, False),
                     rd("codex", STAGE_AMM)).stage == STAGE_AMM
    base = FeedReading("codex", BASE_TOKEN, BASE, NOW, stage=STAGE_AMM)
    assert consensus(BASE_TOKEN, BASE, NOW, 30_000, None, base).stage == STAGE_AMM
    assert consensus(BASE_TOKEN, BASE, NOW, 30_000, None, None).stage is None


def test_apply_view_takes_worse_holder_concentration():
    s = safe_token(TOKEN, NOW, top10_holder_pct=Decimal("0.2"))
    v = consensus(TOKEN, SOLANA, NOW, 30_000, rd("bitquery", STAGE_AMM, True),
                  rd("codex", STAGE_AMM, top10_holder_pct=Decimal("0.45"), creator_tokens_created=4,
                     creator_tokens_migrated=1))
    out = apply_view(s, v)
    assert out.top10_holder_pct == Decimal("0.45") and out.creator_tokens_created == 4


# ---- creator-reputation gate (checklist 15b; thresholds are placeholders) --
@pytest.mark.parametrize("created,migrated,tag", [
    (None, None, "CREATOR_UNKNOWN"),
    (45, 30, "CREATOR_SERIAL_LAUNCHER"),
    (10, 1, "CREATOR_LOW_GRADUATION"),
])
def test_rejects_serial_rugger_creator(created, migrated, tag):
    s = Sim()
    s.safety_overrides = {"creator_tokens_created": created, "creator_tokens_migrated": migrated}
    s.run(2)
    d = s.propose()
    assert not d.approved and any(tag in r for r in d.reasons)


def test_good_creator_and_optional_history():
    s = Sim()
    s.safety_overrides = {"creator_tokens_created": 5, "creator_tokens_migrated": 2}
    s.run(2)
    assert s.propose().approved
    s2 = Sim(cfg=paper_config(require_creator_history=False))
    s2.safety_overrides = {"creator_tokens_created": None, "creator_tokens_migrated": None}
    s2.run(2)
    assert s2.propose().approved
