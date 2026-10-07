"""Property-based tests and randomized fault injection for safety invariants."""
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tradebot.models import Fill, Side, from_atomic, to_atomic
from tradebot.portfolio import InvariantViolation, Portfolio
from tradebot.sim import TOKEN, Sim
from tradebot.state_machine import OrderState

dec = st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("1000000"), places=6)


@given(qty=dec, decimals=st.integers(0, 12))
def test_atomic_roundtrip_never_rounds_up(qty, decimals):
    units = to_atomic(qty, decimals)
    assert from_atomic(units, decimals) <= qty


def test_decimals_required():
    import pytest
    for bad in (None, -1, 19):
        with pytest.raises(ValueError):
            to_atomic(Decimal(1), bad)


@given(st.lists(st.tuples(dec, dec), min_size=1, max_size=8), st.randoms(use_true_random=False))
def test_fill_order_and_duplicates_irrelevant(buys, rnd):
    fills = [Fill(f"b{i}", f"c{i}", TOKEN, Side.BUY, q, p, Decimal("0.01"), Decimal(0), i, 0, "x")
             for i, (q, p) in enumerate(buys)]
    a = Portfolio(Decimal(1000))
    for f in fills:
        a.apply_fill(f)
    shuffled = fills + fills[: len(fills) // 2]
    rnd.shuffle(shuffled)
    b = Portfolio(Decimal(1000))
    for f in shuffled:
        b.apply_fill(f)
    assert a.positions[TOKEN].qty == b.positions[TOKEN].qty
    assert a.positions[TOKEN].cost_usd == b.positions[TOKEN].cost_usd


@given(buy=dec, sell=dec)
def test_never_oversell(buy, sell):
    pf = Portfolio(Decimal(1000))
    pf.apply_fill(Fill("b", "cb", TOKEN, Side.BUY, buy, Decimal(1), Decimal(0), Decimal(0), 0, 0, "x"))
    f = Fill("s", "cs", TOKEN, Side.SELL, sell, Decimal(1), Decimal(0), Decimal(0), 1, 0, "x")
    if sell > buy:
        try:
            pf.apply_fill(f)
            raise AssertionError("oversell accepted")
        except InvariantViolation:
            pass
    else:
        pf.apply_fill(f)
        assert pf.positions[TOKEN].qty >= 0


actions = st.lists(st.sampled_from([
    "tick", "tick", "tick", "propose", "shock_down", "shock_up", "gap_down", "rug", "timeouts_on",
    "timeouts_off", "fail_on", "fail_off", "feed_down", "feed_up", "restart", "honeypot", "fee_spike",
]), min_size=5, max_size=60)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(acts=actions, seed=st.integers(0, 10_000))
def test_random_fault_sequences_preserve_invariants(acts, seed, tmp_path_factory):
    path = str(tmp_path_factory.mktemp("fz") / "j.db")
    s = Sim(seed=seed, journal_path=path)
    s.run(2)
    f = s.venue.faults
    for a in acts:
        halted_before = s.bot.halted
        n_entries_before = sum(1 for o in s.bot.engine.orders.values() if o.side is Side.BUY)
        if a == "propose":
            s.propose(usd="100", edge="600")
            if halted_before:
                n_after = sum(1 for o in s.bot.engine.orders.values() if o.side is Side.BUY)
                assert n_after == n_entries_before, "entry submitted while halted"
        elif a == "shock_down":
            s.venue.shock(TOKEN, Decimal("-0.05"))
        elif a == "shock_up":
            s.venue.shock(TOKEN, Decimal("0.05"))
        elif a == "gap_down":
            s.venue.shock(TOKEN, Decimal("-0.4"))
        elif a == "rug":
            s.venue.remove_liquidity(TOKEN, Decimal("0.7"))
        elif a == "timeouts_on":
            f.submit_timeout_prob = 0.7
            f.status_timeout_prob = 0.3
        elif a == "timeouts_off":
            f.submit_timeout_prob = f.status_timeout_prob = 0.0
        elif a == "fail_on":
            f.tx_fail_prob = 0.8
        elif a == "fail_off":
            f.tx_fail_prob = 0.0
        elif a == "feed_down":
            s.feed_up = False
        elif a == "feed_up":
            s.feed_up = True
        elif a == "restart":
            s.restart()
        elif a == "honeypot":
            s.venue.pools[TOKEN].sell_blocked = True
        elif a == "fee_spike":
            f.priority_fee_lamports = 50_000_000
        else:
            s.step()

        # ---- invariants ------------------------------------------------
        pos = s.bot.portfolio.positions.get(TOKEN)
        qty = pos.qty if pos else Decimal(0)
        assert qty >= 0
        assert max(s.venue.landed_by_cid.values(), default=0) <= 1, "a client order executed twice"
        assert len(s.venue.submit_calls) == len(set(s.venue.submit_calls)), "client order id resubmitted"
        assert s.bot.portfolio.exposure_usd() <= s.cfg.max_total_exposure_usd
        if not s.bot.engine.open_orders(TOKEN):
            assert s.venue.wallet.get(TOKEN, Decimal(0)) == qty, "journal/venue position mismatch"
        snap = s.bot.snapshots.get(TOKEN)
        if qty > 0 and a == "tick" and (snap is None or snap.age_ms(s.clock.now_ms()) > s.cfg.max_data_age_ms):
            assert s.bot.halted, "held position with stale data but not halted"
        for o in s.bot.engine.orders.values():
            if o.state is OrderState.FILLED:
                assert o.fills
