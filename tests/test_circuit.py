from decimal import Decimal

from tradebot.models import Purpose, Side
from tradebot.sim import TOKEN, Sim, paper_config


def events(s, t):
    return [p for _, _, ty, _, p in s.journal.events(t)]


def test_liquidity_collapse_triggers_vanish():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.remove_liquidity(TOKEN, Decimal("0.6"))
    s.run(2)
    v = events(s, "VANISH")
    assert v and "LIQUIDITY_COLLAPSE" in v[0]["triggers"]
    assert s.bot.halted
    assert any(o.purpose is Purpose.EMERGENCY for o in s.bot.engine.orders.values())
    assert s.position().qty == 0
    assert not s.propose().approved


def test_stale_data_vanish():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.feed_up = False
    s.run(5)
    v = events(s, "VANISH")
    assert v and v[0]["triggers"] == ["STALE_DATA"]
    assert s.bot.halted


def test_spread_expansion():
    s = Sim(cfg=paper_config(vanish_spread_bps="60", max_spread_bps="55"))
    s.venue.pools[TOKEN].fee_bps = Decimal(40)
    s.run(2)
    assert "SPREAD_EXPANSION" in events(s, "VANISH")[0]["triggers"]


def test_recovery_requires_streak_and_ack():
    s = Sim()
    s.run(2)
    s.bot.breaker.invalidate_signal(TOKEN, s.clock.now_ms())
    s.bot.halt(f"VANISH:{TOKEN}", "auto")
    s.run(10)
    assert s.bot.breaker.is_tripped(TOKEN)  # streak met, but no ack
    s.bot.breaker.ack(TOKEN)
    s.run(1)
    assert not s.bot.breaker.is_tripped(TOKEN)
    assert not s.bot.halted


def test_hold_protected_policy_keeps_position_but_blocks_entries():
    s = Sim(cfg=paper_config(vanish_emergency_policy="hold_protected"))
    s.run(2)
    s.propose()
    s.run(2)
    s.bot.breaker.invalidate_signal(TOKEN, s.clock.now_ms())
    s.bot.halt(f"VANISH:{TOKEN}", "auto")
    s.run(2)
    assert s.position().qty > 0
    assert not s.propose().approved


def test_late_fill_after_vanish_is_exited():
    s = Sim()
    s.run(2)
    s.venue.faults.confirm_delay_ms = 3500  # entry still pending when market turns
    s.propose()
    s.bot.breaker.invalidate_signal(TOKEN, s.clock.now_ms())
    s.bot._vanish(TOKEN, ["SIGNAL_INVALIDATED"])
    assert events(s, "ENTRY_NOT_CANCELLABLE")
    s.venue.faults.confirm_delay_ms = 400
    s.run(8)
    buys = [o for o in s.bot.engine.orders.values() if o.side is Side.BUY]
    assert buys[0].fills  # the late fill happened
    assert s.position().qty == 0  # and was exited by the emergency policy


def test_emergency_exit_banks_profit_below_30pct_before_collapse():
    """Owner decision 2026-10-07: protective exits may sell a position that is up
    less than 30%, because banking profit before a collapse matters more than the
    profit floor."""
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.shock(TOKEN, Decimal("0.15"))
    s.run(65)  # +~14%: held (below the 30% floor), past the volatility window
    assert s.position().qty > 0
    s.venue.remove_liquidity(TOKEN, Decimal("0.5"))  # liquidity collapsing
    s.run(3)
    p = s.position()
    assert p.qty == 0, "emergency exit must not be blocked by the 30% profit floor"
    assert p.realized_pnl_usd > 0  # profit banked before the collapse
    assert any(o.purpose is Purpose.EMERGENCY and o.fills for o in s.bot.engine.orders.values())


def test_estimated_liquidity_swings_do_not_trigger_collapse():
    """Live-paper finding 2026-10-07: impact-implied liquidity swung ~40% as Jupiter's
    router changed, falsely tripping LIQUIDITY_COLLAPSE. Estimates must not trip it."""
    import dataclasses
    from tradebot.circuit_breaker import CircuitBreaker
    s = Sim()
    cb = CircuitBreaker(s.cfg)
    snap = s.snapshot()
    now = s.clock.now_ms()
    est = dataclasses.replace(snap, liquidity_is_estimate=True)
    assert cb.observe(est, TOKEN, now) == []
    assert cb.observe(dataclasses.replace(est, liquidity_usd=est.liquidity_usd * Decimal("0.4")), TOKEN, now) == []
    cb2 = CircuitBreaker(s.cfg)
    cb2.observe(snap, TOKEN, now)
    real_drop = cb2.observe(dataclasses.replace(snap, liquidity_usd=snap.liquidity_usd * Decimal("0.4")), TOKEN, now)
    assert "LIQUIDITY_COLLAPSE" in real_drop  # real readings still trip it
