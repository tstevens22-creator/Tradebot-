"""Step 1: chain awareness (Solana + Base) and the post-graduation-only rule."""
from decimal import Decimal

import pytest

from tradebot.chains import BASE, SOLANA, STAGE_AMM, STAGE_BONDING_CURVE, STAGE_MIGRATING, normalize_address
from tradebot.config import ConfigError
from tradebot.models import Purpose
from tradebot.signals import ProposalError, parse_proposal
from tradebot.sim import BASE_TOKEN, TOKEN, Sim, paper_config


def reasons(d):
    return " ".join(d.reasons)


def ready(**cfg):
    s = Sim(cfg=paper_config(**cfg) if cfg else None)
    s.add_token(BASE_TOKEN, BASE)
    s.run(2)
    return s


# ---- addresses & proposals ------------------------------------------------
def test_chain_address_validation():
    assert normalize_address(SOLANA, TOKEN) == TOKEN
    with pytest.raises(ValueError):
        normalize_address(SOLANA, BASE_TOKEN)  # 0x address on the Solana path
    with pytest.raises(ValueError):
        normalize_address(BASE, TOKEN)  # base58 mint on the Base path
    with pytest.raises(ValueError):
        normalize_address("ethereum", BASE_TOKEN)  # unsupported chain
    with pytest.raises(ValueError):
        normalize_address(BASE, "0x1234")  # wrong length


def test_evm_checksum_case_maps_to_one_token():
    """One token must never be tracked as two positions (exposure-limit bypass)."""
    mixed = "0x4ED4E862860beD51a9570b96d89aF5E1B0Efefed"
    assert normalize_address(BASE, mixed) == normalize_address(BASE, mixed.lower()) == BASE_TOKEN


def test_chain_is_required_on_proposals():
    with pytest.raises(ProposalError):
        parse_proposal({"proposal_id": "x", "token": TOKEN, "usd_size": "10", "expected_edge_bps": "500"})
    p = parse_proposal({"proposal_id": "x", "chain": "base", "token": BASE_TOKEN.upper().replace("0X", "0x"),
                        "usd_size": "10", "expected_edge_bps": "500"})
    assert p.chain == BASE and p.token == BASE_TOKEN


def test_enabled_chains_config_validation():
    with pytest.raises(ConfigError):
        paper_config(enabled_chains=[])
    with pytest.raises(ConfigError):
        paper_config(enabled_chains=["solana", "ethereum"])
    with pytest.raises(ConfigError):
        paper_config(enabled_chains=["solana", "solana"])


# ---- risk gate ----------------------------------------------------------
def test_base_token_can_be_traded_in_paper():
    s = ready()
    d = s.propose(token=BASE_TOKEN)
    assert d.approved, d.reasons
    s.run(2)
    assert s.position(BASE_TOKEN).qty > 0


def test_chain_disabled():
    s = ready(enabled_chains=["solana"])
    assert "CHAIN_DISABLED" in reasons(s.propose(token=BASE_TOKEN))
    assert s.propose().approved


def test_no_venue_for_chain():
    s = ready()
    s.venue.chains = (SOLANA,)
    assert "NO_VENUE_FOR_CHAIN" in reasons(s.propose(token=BASE_TOKEN))


def test_chain_mismatch_between_proposal_and_data():
    s = ready()
    s.safety_overrides = {"chain": BASE}  # data claims the Solana token is on Base
    s.run(1)
    assert "CHAIN_MISMATCH" in reasons(s.propose())


@pytest.mark.parametrize("stage,tag", [
    (STAGE_BONDING_CURVE, "NOT_GRADUATED"),
    (STAGE_MIGRATING, "MIGRATING"),
    (None, "STAGE_UNKNOWN"),
])
def test_rejects_bonding_curve_migrating_and_unknown_stage(stage, tag):
    s = ready()
    s.stage[TOKEN] = stage
    s.run(1)
    d = s.propose()
    assert not d.approved and tag in reasons(d)


def test_graduated_token_accepted():
    s = ready()
    s.stage[TOKEN] = STAGE_BONDING_CURVE
    s.run(1)
    assert not s.propose().approved
    s.stage[TOKEN] = STAGE_AMM  # graduates to PumpSwap
    s.run(1)
    assert s.propose().approved


def test_exposure_cap_cross_chain():
    s = ready(max_total_exposure_usd="250", max_position_usd="200", max_concentration_pct="1")
    assert s.propose(usd="150").approved
    s.run(2)
    d = s.propose(usd="150", token=BASE_TOKEN)
    assert "TOTAL_EXPOSURE" in reasons(d)  # one cap across Solana and Base


# ---- migration while holding -----------------------------------------
def test_migration_triggers_vanish_and_blocks_recovery_until_amm():
    s = ready()
    s.propose()
    s.run(2)
    assert s.position().qty > 0
    s.stage[TOKEN] = STAGE_MIGRATING
    s.run(2)
    vanish = [p for *_, p in s.journal.events("VANISH")]
    assert vanish and "MIGRATION" in vanish[0]["triggers"]
    assert any(o.purpose is Purpose.EMERGENCY for o in s.bot.engine.orders.values())
    s.bot.breaker.ack(TOKEN)
    s.run(10)
    assert s.bot.breaker.is_tripped(TOKEN)  # still migrating: no recovery
    s.stage[TOKEN] = STAGE_AMM
    s.run(6)
    assert not s.bot.breaker.is_tripped(TOKEN)


def test_migration_of_other_token_does_not_exit_unrelated_position():
    s = ready()
    s.propose()
    s.run(2)
    s.stage[BASE_TOKEN] = STAGE_MIGRATING
    s.run(2)
    assert s.position().qty > 0  # Solana position untouched
    assert not s.propose(token=BASE_TOKEN).approved
