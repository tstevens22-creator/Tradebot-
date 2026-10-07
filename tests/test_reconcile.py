from decimal import Decimal

from tradebot.models import Purpose
from tradebot.sim import TOKEN, Sim, paper_config
from tradebot.state_machine import OrderState


def test_restart_recovers_inflight_as_unknown(tmp_path):
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.venue.faults.confirm_delay_ms = 3000
    s.propose()
    bot = s.restart()  # crash while the entry tx is pending
    (o,) = [o for o in bot.engine.orders.values() if o.purpose is Purpose.ENTRY]
    assert o.state in (OrderState.SUBMITTED, OrderState.UNKNOWN)
    assert not s.propose().approved  # unresolved -> no new exposure
    s.run(4)
    assert o.state is OrderState.FILLED and s.position().qty > 0
    assert bot.reconcile_balances() == []


def test_crash_between_write_ahead_and_send(tmp_path):
    """Process dies after SUBMITTING is journaled but before the venue sees it."""
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)

    def die(order):
        raise SystemExit("crash")

    s.venue.submit_swap = die
    try:
        s.propose()
    except SystemExit:
        pass
    del s.venue.submit_swap
    bot = s.restart()
    (o,) = [o for o in bot.engine.orders.values() if o.purpose is Purpose.ENTRY]
    assert o.state is OrderState.UNKNOWN  # NOT resubmitted, NOT assumed failed
    s.run(70)  # past tx expiry
    assert o.state is OrderState.EXPIRED
    assert s.venue.submit_calls == []


def test_halt_survives_restart(tmp_path):
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.bot.halt("OPERATOR_TEST")
    s.restart()
    assert "OPERATOR_TEST" in s.bot.halts
    assert not s.propose().approved


def test_daily_loss_survives_restart(tmp_path):
    s = Sim(cfg=paper_config(daily_loss_limit_usd="25"), journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.propose(usd="150", edge="900")
    s.run(2)
    s.venue.shock(TOKEN, Decimal("-0.3"))
    s.run(3)
    realized = s.bot.portfolio.realized_today(s.clock.now_ms())
    assert realized < -25
    s.restart()
    assert s.bot.portfolio.realized_today(s.clock.now_ms()) == realized
    d = s.propose(usd="20")
    assert not d.approved and any("DAILY_LOSS" in r or "HALTED" in r for r in d.reasons)


def test_balance_mismatch_halts(tmp_path):
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.wallet[TOKEN] -= Decimal(1)  # e.g. external transfer / RPC disagreement
    s.restart()
    assert any(r.startswith("RECONCILE_MISMATCH") for r in s.bot.halts)


def test_resume_refused_while_hazards_remain():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.pools[TOKEN].sell_blocked = True
    s.venue.shock(TOKEN, Decimal("-0.1"))
    s.run(30)
    assert s.position().exit_failed
    assert s.bot.resume("op") != []
    assert s.bot.halted


def test_fills_not_double_counted_after_restart(tmp_path):
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.propose()
    s.run(2)
    q = s.position().qty
    s.restart()
    s.run(2)
    assert s.position().qty == q


def test_inflight_scale_out_not_repeated_after_restart(tmp_path):
    s = Sim(journal_path=str(tmp_path / "j.db"))
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.confirm_delay_ms = 3000
    s.venue.shock(TOKEN, Decimal("0.13"))  # crosses the 10% scale-out, below TP
    s.run(1)
    assert any(o.purpose is Purpose.SCALE_OUT and not o.is_terminal for o in s.bot.engine.orders.values())
    s.restart()
    s.venue.faults.confirm_delay_ms = 400
    s.run(6)
    scale_outs = [o for o in s.bot.engine.orders.values() if o.purpose is Purpose.SCALE_OUT]
    assert len(scale_outs) == 1 and 0 in s.position().scale_outs_done
