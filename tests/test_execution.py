from decimal import Decimal

import pytest

from tradebot.execution import DuplicateOrder
from tradebot.models import Fill, Purpose, Side
from tradebot.sim import TOKEN, Sim, paper_config
from tradebot.state_machine import IllegalTransition, NotCancellable, Order, OrderState


def entry_orders(s):
    return [o for o in s.bot.engine.orders.values() if o.purpose is Purpose.ENTRY]


def test_timeout_is_unknown_not_failed():
    s = Sim()
    s.run(2)
    s.venue.faults.submit_timeout_prob = 1.0  # timeout, but tx lands
    assert s.propose().approved
    (o,) = entry_orders(s)
    assert o.state is OrderState.UNKNOWN
    s.venue.faults.submit_timeout_prob = 0.0
    s.run(2)
    assert o.state is OrderState.FILLED
    assert s.position().qty > 0
    assert s.venue.landed_by_cid[o.client_order_id] == 1


def test_no_resubmit_while_unknown():
    s = Sim()
    s.run(2)
    s.venue.faults.submit_timeout_prob = 1.0
    s.propose()
    d = s.propose()
    assert not d.approved and any("UNRESOLVED_ORDERS" in r for r in d.reasons)
    assert len(s.venue.submit_calls) == 1


def test_unknown_never_landed_expires_only_after_window():
    s = Sim()
    s.run(2)
    s.venue.faults.submit_timeout_prob = 1.0
    s.venue.faults.timeout_but_lands = False
    s.propose()
    (o,) = entry_orders(s)
    s.venue.faults.submit_timeout_prob = 0.0
    s.run(30)
    assert o.state is OrderState.UNKNOWN  # still inside the expiry window
    s.run(35)
    assert o.state is OrderState.EXPIRED


def test_stuck_unknown_halts():
    cfg = paper_config(unknown_resolution_timeout_s=5)
    s = Sim(cfg=cfg)
    s.run(2)
    s.venue.faults.submit_timeout_prob = 1.0
    s.venue.faults.timeout_but_lands = False
    s.propose()
    s.venue.faults.submit_timeout_prob = 0.0
    s.run(10)
    assert any(r.startswith("UNKNOWN_UNRESOLVED") for r in s.bot.halts)


def test_pending_tx_cannot_be_cancelled():
    o = Order("c1", TOKEN, Side.BUY, Purpose.ENTRY, Decimal(1), Decimal(1), Decimal(1))
    o.transition(OrderState.SUBMITTING)
    o.transition(OrderState.SUBMITTED)
    with pytest.raises(NotCancellable):
        o.request_cancel()


def test_cancel_pending_is_not_cancelled_and_accepts_late_fill():
    o = Order("c2", TOKEN, Side.BUY, Purpose.ENTRY, Decimal(1), Decimal(1), Decimal(1), is_swap=False)
    o.transition(OrderState.SUBMITTING)
    o.transition(OrderState.SUBMITTED)
    o.request_cancel()
    assert o.state is OrderState.CANCEL_PENDING and not o.is_terminal
    f = Fill("f1", "c2", TOKEN, Side.BUY, Decimal(1), Decimal(1), Decimal(0), Decimal(0), 0, 0, "x")
    assert o.add_fill(f)
    o.transition(OrderState.FILLED)


def test_illegal_transitions():
    o = Order("c3", TOKEN, Side.BUY, Purpose.ENTRY, Decimal(1), Decimal(1), Decimal(1))
    with pytest.raises(IllegalTransition):
        o.transition(OrderState.FILLED)
    o.transition(OrderState.SUBMITTING)
    o.transition(OrderState.UNKNOWN)
    with pytest.raises(IllegalTransition):
        o.transition(OrderState.SUBMITTING)  # no blind resubmission of the same order


def test_duplicate_client_order_id_refused():
    s = Sim()
    o = Order("dup", TOKEN, Side.BUY, Purpose.ENTRY, Decimal(10), Decimal(0), Decimal(100))
    s.bot.engine.submit(o)
    with pytest.raises(DuplicateOrder):
        s.bot.engine.submit(Order("dup", TOKEN, Side.BUY, Purpose.ENTRY, Decimal(10), Decimal(0), Decimal(100)))


def test_write_ahead_journal_before_send():
    s = Sim()
    s.run(2)
    seen = []
    orig = s.venue.submit_swap

    def spy(order):
        states = [p["state"] for _, _, t, k, p in s.journal.events("ORDER") if k == order.client_order_id]
        seen.append(states)
        return orig(order)

    s.venue.submit_swap = spy
    s.propose()
    assert seen and seen[0] == ["SUBMITTING"]


def test_slippage_failure_is_terminal_and_not_retried_for_entries():
    s = Sim()
    s.run(2)
    s.propose()
    s.venue.shock(TOKEN, Decimal("0.5"))  # price jumps before landing -> min_out violated
    s.run(2)
    (o,) = entry_orders(s)
    assert o.state is OrderState.FAILED
    assert len(s.venue.submit_calls) == 1
    assert s.position() is None or s.position().qty == 0


def test_exit_escalation_is_bounded():
    s = Sim(cfg=paper_config(exit_failed_probe_interval_s=None))
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.tx_fail_prob = 1.0
    s.venue.shock(TOKEN, Decimal("-0.10"))
    s.run(40)
    sells = [o for o in s.bot.engine.orders.values() if o.side is Side.SELL]
    assert len(sells) == s.cfg.max_exit_attempts
    slips = [o.slippage_bps for o in sorted(sells, key=lambda o: o.attempt)]
    assert slips == sorted(slips) and max(slips) <= s.cfg.emergency_max_slippage_bps
    assert s.position().exit_failed
    assert any(r.startswith("EXIT_FAILED") for r in s.bot.halts)


def test_rate_limited_submit_is_rejected_not_unknown():
    s = Sim()
    s.run(2)
    s.venue.faults.rate_limit_budget = 1  # quote succeeds, submit gets 429
    s.propose()
    (o,) = entry_orders(s)
    assert o.state is OrderState.REJECTED


def test_rate_limited_exits_stay_protected_or_halt():
    """Regression for the 2026-10-07 adversarial finding: exits blocked by rate
    limits left a below-stop position neither exited nor halted."""
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.rate_limit_budget = 0
    s.venue.shock(TOKEN, Decimal("-0.12"))
    s.run(6)
    p = s.position()
    assert p.qty == 0 or p.exit_failed or s.bot.halted
    assert any(r.startswith(("EXIT_QUOTES_UNAVAILABLE", "EXIT_FAILED")) for r in s.bot.halts)


def test_exit_backoff_spreads_attempts():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.tx_fail_prob = 1.0
    s.venue.shock(TOKEN, Decimal("-0.10"))
    s.run(3)
    sells = sorted((o for o in s.bot.engine.orders.values() if o.side is Side.SELL), key=lambda o: o.created_ms)
    gaps = [b.created_ms - a.created_ms for a, b in zip(sells, sells[1:])]
    assert gaps == sorted(gaps)  # exponential backoff, non-decreasing


def test_probe_recovers_after_transient_failure():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.tx_fail_prob = 1.0
    s.venue.shock(TOKEN, Decimal("-0.10"))
    s.run(20)
    assert s.position().exit_failed and s.position().qty > 0
    s.venue.faults.tx_fail_prob = 0.0  # congestion clears
    s.run(35)  # one probe interval
    assert s.position().qty == 0 and not s.position().exit_failed
    assert any(r.startswith("EXIT_FAILED") for r in s.bot.halts)  # still needs operator review


def test_operator_only_policy_never_probes():
    s = Sim(cfg=paper_config(exit_failed_probe_interval_s=None))
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.faults.tx_fail_prob = 1.0
    s.venue.shock(TOKEN, Decimal("-0.10"))
    s.run(20)
    n = len([o for o in s.bot.engine.orders.values() if o.side is Side.SELL])
    s.venue.faults.tx_fail_prob = 0.0
    s.run(120)
    assert len([o for o in s.bot.engine.orders.values() if o.side is Side.SELL]) == n
