from decimal import Decimal

from tradebot.sim import TOKEN, Sim, paper_config


def reasons(d):
    return " ".join(d.reasons)


def ready(**cfg):
    s = Sim(cfg=paper_config(**cfg) if cfg else None)
    s.run(2)
    return s


def test_happy_path_approved():
    d = ready().propose()
    assert d.approved, d.reasons


def test_stale_data_rejected():
    s = ready()
    s.clock.advance(10_000)
    d = s.propose()
    assert not d.approved and "STALE_DATA" in reasons(d)


def test_rejects_when_costs_exceed_edge():
    d = ready().propose(edge="20")
    assert "EDGE_BELOW_COST" in reasons(d)


def test_rejects_high_impact():
    s = Sim(usd_reserve=Decimal(10_000), token_reserve=Decimal(100_000), cfg=paper_config(min_liquidity_usd="10000", max_spread_bps="600", vanish_spread_bps="900"))
    s.run(2)
    d = s.propose(usd="200", edge="2000")
    assert "PRICE_IMPACT" in reasons(d)


def test_stressed_loss_budget():
    d = ready(max_stressed_loss_usd="20", per_trade_risk_usd="20").propose(usd="100")
    assert "STRESSED_LOSS" in reasons(d)


def test_per_trade_risk():
    d = ready(per_trade_risk_usd="5", max_stressed_loss_usd="60").propose(usd="100")
    assert "PER_TRADE_RISK" in reasons(d)


def test_total_exposure_cap():
    s = ready(max_total_exposure_usd="250", max_position_usd="200", max_concentration_pct="1")
    assert s.propose(usd="150").approved
    s.run(2)
    d = s.propose(usd="150")
    assert "TOTAL_EXPOSURE" in reasons(d) or "POSITION_CAP" in reasons(d)


def test_inflight_entries_count_toward_exposure():
    s = ready(max_total_exposure_usd="250", max_position_usd="200", max_concentration_pct="1")
    s.venue.faults.confirm_delay_ms = 10_000
    assert s.propose(usd="150").approved
    d = s.propose(usd="150")  # first still pending
    assert not d.approved and "UNRESOLVED_ORDERS" in reasons(d) and "TOTAL_EXPOSURE" in reasons(d)


def test_priority_fee_cap():
    s = ready()
    s.venue.faults.priority_fee_lamports = 10_000_000
    s.run(1)
    assert "PRIORITY_FEE" in reasons(s.propose())


def test_token_safety_unknown_is_rejected():
    for field, bad, tag in [
        ("mint_authority", None, "MINT_AUTHORITY"),
        ("mint_authority", True, "MINT_AUTHORITY"),
        ("freeze_authority", True, "FREEZE_AUTHORITY"),
        ("transfer_tax_bps", Decimal(500), "TRANSFER_TAX"),
        ("transfer_tax_bps", None, "TRANSFER_TAX"),
        ("top10_holder_pct", Decimal("0.9"), "HOLDER_CONCENTRATION"),
        ("decimals", None, "UNKNOWN_DECIMALS"),
    ]:
        s = ready()
        s.safety_overrides = {field: bad}
        s.run(1)
        assert tag in reasons(s.propose()), (field, bad)


def test_rejects_unverified_sellability():
    s = ready()
    s.safety_overrides = {"sell_simulation_ok": None}
    s.run(1)
    assert "SELLABILITY_UNVERIFIED" in reasons(s.propose())


def test_no_average_down():
    s = ready(max_concentration_pct="1")
    assert s.propose(usd="80").approved
    s.run(2)
    s.venue.shock(TOKEN, Decimal("-0.04"))
    s.run(1)
    assert "AVERAGE_DOWN_FORBIDDEN" in reasons(s.propose(usd="80"))


def test_halted_blocks_everything():
    s = ready()
    s.bot.halt("TEST")
    assert "HALTED" in reasons(s.propose())


def test_duplicate_proposal_id_rejected():
    s = ready()
    raw = {"proposal_id": "same", "chain": "solana", "token": TOKEN, "usd_size": "50", "expected_edge_bps": "500"}
    assert s.bot.propose(raw).approved
    assert "DUPLICATE_PROPOSAL" in reasons(s.bot.propose(raw))


def _lose(s, usd="150", gap="-0.12"):
    assert s.propose(usd=usd, edge="900").approved
    s.run(2)
    s.venue.shock(TOKEN, Decimal(gap))
    s.run(3)
    s.clock.advance(400_000)  # past cooldown
    s.run(6)


def test_daily_loss_budget_limits_size_of_next_trade():
    s = ready(daily_loss_limit_usd="30", per_trade_risk_usd="20", max_stressed_loss_usd="60")
    _lose(s)
    today = s.bot.portfolio.realized_today(s.clock.now_ms())
    assert -30 < today < -10
    assert "DAILY_LOSS_BUDGET" in reasons(s.propose(usd="150"))  # planned risk > remaining budget
    assert s.propose(usd="30").approved  # a trade that fits the remaining budget is allowed


def test_daily_loss_limit_halts():
    s = ready(daily_loss_limit_usd="25", per_trade_risk_usd="20", max_stressed_loss_usd="60")
    _lose(s, gap="-0.3")
    assert s.bot.portfolio.realized_today(s.clock.now_ms()) <= -25
    assert "DAILY_LOSS_LIMIT" in s.bot.halts
    d = s.propose(usd="20")
    assert "DAILY_LOSS_LIMIT" in reasons(d) and "HALTED" in reasons(d)
