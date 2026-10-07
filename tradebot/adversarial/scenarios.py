"""Adversarial scenarios. SIMULATION ONLY: no network, no real funds, no
third-party infrastructure.

Each scenario returns evidence: invariant violations, financial impact
(planned vs realized loss), residual exposure and journal excerpts.
"""
from __future__ import annotations

import random
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from ..markout import grade_fill
from ..models import Side
from ..sim import TOKEN, Sim, paper_config
from ..state_machine import OrderState


@dataclass
class Result:
    name: str
    description: str
    violations: list[str] = field(default_factory=list)
    realized_pnl_usd: Decimal = Decimal(0)
    planned_loss_usd: Decimal = Decimal(0)
    residual_qty: Decimal = Decimal(0)
    unprotected: bool = False
    halts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)  # financial-impact warnings (not invariant breaks)
    latency: dict = field(default_factory=dict)  # name -> raw samples (ms)
    latency_budgets: dict = field(default_factory=dict)
    markouts: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def check_invariants(s: Sim) -> list[str]:
    v = []
    pos = s.position()
    qty = pos.qty if pos else Decimal(0)
    if qty < 0:
        v.append("I1 negative position (oversell)")
    if max(s.venue.landed_by_cid.values(), default=0) > 1:
        v.append("I2 a client order executed more than once")
    if len(s.venue.submit_calls) != len(set(s.venue.submit_calls)):
        v.append("I3 client_order_id resubmitted")
    if s.bot.portfolio.exposure_usd() > s.cfg.max_total_exposure_usd:
        v.append("I4 total exposure above cap")
    if not s.bot.engine.open_orders(TOKEN) and s.venue.wallet.get(TOKEN, Decimal(0)) != qty:
        v.append("I5 journal position != venue balance with no orders in flight")
    snap = s.bot.snapshots.get(TOKEN)
    if qty > 0 and (snap is None or snap.age_ms(s.clock.now_ms()) > s.cfg.max_data_age_ms) and not s.bot.halted:
        v.append("I6 held position on stale data without halt")
    return v


class Tracker:
    """Runs invariant checks after every step and records entries while halted."""

    def __init__(self, s: Sim):
        self.s, self.violations = s, []

    def step(self, n: int = 1) -> None:
        for _ in range(n):
            self.s.step()
            for x in check_invariants(self.s):
                if x not in self.violations:
                    self.violations.append(x)

    def propose(self, **kw):
        halted = self.s.bot.halted
        before = len([o for o in self.s.bot.engine.orders.values() if o.side is Side.BUY])
        d = self.s.propose(**kw)
        after = len([o for o in self.s.bot.engine.orders.values() if o.side is Side.BUY])
        if halted and after > before:
            self.violations.append("I7 entry submitted while halted")
        return d


def _finish(s: Sim, t: Tracker, r: Result, planned: Decimal = Decimal(0)) -> Result:
    pos = s.position()
    r.violations += t.violations
    r.realized_pnl_usd = s.bot.portfolio.total_realized_usd
    r.planned_loss_usd = planned
    r.residual_qty = pos.qty if pos else Decimal(0)
    r.unprotected = bool(pos and pos.exit_failed)
    r.halts = sorted(s.bot.halts)
    r.latency = {k: list(v) for k, v in s.bot.latency.samples.items()}
    r.latency_budgets = dict(s.bot.latency.budgets_ms)
    approved = {p["decision_id"] for *_, p in s.journal.events("DECISION") if p.get("approved")}
    for f in s.journal.load_fills().values():
        o = s.bot.engine.orders.get(f.client_order_id)
        if o:
            r.markouts += grade_fill(s.cfg, f, o, s.recorder, approved)
    return r


# ---------------------------------------------------------------- scenarios
def baseline(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("baseline", "entry then +25% move -> take profit")
    t.step(2); d = t.propose(); t.step(2)
    s.venue.shock(TOKEN, Decimal("0.25")); t.step(15)
    if s.position().qty != 0:
        r.violations.append("TP not executed")
    return _finish(s, t, r, d.planned_loss_usd)


def gap_through_stop(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("gap_through_stop", "-35% gap through an 8% stop")
    t.step(2); d = t.propose(); t.step(2)
    s.venue.shock(TOKEN, Decimal("-0.35")); t.step(12)
    loss = -s.bot.portfolio.total_realized_usd
    r.notes.append(f"planned loss {d.planned_loss_usd:.2f}, stressed {d.stressed_loss_usd:.2f}, realized {loss:.2f}")
    if s.position().qty != 0:
        r.violations.append("stop not executed after gap")
    if loss > d.stressed_loss_usd:
        r.findings.append(f"realized gap loss {loss:.2f} exceeded stressed budget {d.stressed_loss_usd:.2f} "
                          f"(stress_gap_multiplier={s.cfg.stress_gap_multiplier} does not cover a 35% gap)")
    return _finish(s, t, r, d.planned_loss_usd)


def submit_timeout_lands(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("submit_timeout_lands", "entry submit times out but tx lands")
    t.step(2); s.venue.faults.submit_timeout_prob = 1.0
    t.propose(); t.propose(); s.venue.faults.submit_timeout_prob = 0.0
    t.step(5)
    if len(s.venue.submit_calls) != 1:
        r.violations.append(f"expected 1 submission, saw {len(s.venue.submit_calls)}")
    return _finish(s, t, r)


def crash_mid_submission(seed: int) -> Result:
    r = Result("crash_mid_submission", "process dies after write-ahead, before send; restart")
    with tempfile.TemporaryDirectory() as d:
        s = Sim(seed=seed, journal_path=str(Path(d) / "j.db")); t = Tracker(s)
        t.step(2)
        orig = s.venue.submit_swap
        s.venue.submit_swap = lambda o: (_ for _ in ()).throw(SystemExit("crash"))
        try:
            s.propose()
        except SystemExit:
            pass
        s.venue.submit_swap = orig
        s.restart(); t.step(70)
        states = {o.state for o in s.bot.engine.orders.values()}
        if states != {OrderState.EXPIRED}:
            r.violations.append(f"in-flight order not resolved to EXPIRED: {states}")
        return _finish(s, t, r)


def rug_pull(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("rug_pull", "90% liquidity removed while holding")
    t.step(2); d = t.propose(); t.step(2)
    s.venue.remove_liquidity(TOKEN, Decimal("0.9")); t.step(10)
    r.notes.append("residual risk: a rug inside one block cannot be prevented, only exited after")
    return _finish(s, t, r, d.planned_loss_usd)


def honeypot(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("honeypot", "sells blocked after entry")
    t.step(2); d = t.propose(); t.step(2)
    s.venue.pools[TOKEN].sell_blocked = True
    s.venue.shock(TOKEN, Decimal("-0.1")); t.step(40)
    if not s.position().exit_failed:
        r.violations.append("exit failure not flagged")
    if not s.bot.halted:
        r.violations.append("not halted after exit failure")
    attempts = sum(1 for o in s.bot.engine.orders.values() if o.side is Side.SELL and o.attempt < s.cfg.max_exit_attempts)
    probes = len(list(s.journal.events("EXIT_PROBE")))
    interval = s.cfg.exit_failed_probe_interval_s
    if probes > ((40 // interval + 1) if interval else 0):
        r.violations.append(f"probe rate exceeded: {probes}")
    if attempts > s.cfg.max_exit_attempts:
        r.violations.append(f"unbounded retries: {attempts}")
    r.notes.append("position remains UNPROTECTED and unsellable: full notional at risk")
    return _finish(s, t, r, d.planned_loss_usd)


def stale_feed(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("stale_feed", "data feed stops while holding")
    t.step(2); t.propose(); t.step(2)
    s.feed_up = False; t.step(6)
    if not s.bot.halted:
        r.violations.append("no halt on stale feed")
    return _finish(s, t, r)


def rate_limit_exhaustion(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("rate_limit_exhaustion", "venue/RPC budget exhausted while holding")
    t.step(2); t.propose(); t.step(2)
    s.venue.faults.rate_limit_budget = 0
    s.venue.shock(TOKEN, Decimal("-0.12")); t.step(5)
    pos = s.position()
    if pos.qty > 0 and not pos.exit_failed and not s.bot.halted:
        r.violations.append("position below stop, exits rate-limited, yet neither exited nor halted")
    r.notes.append(f"quote errors: {len(list(s.journal.events('QUOTE_ERROR')))}, "
                   f"rate-limited submits: {len(list(s.journal.events('SUBMIT_RATE_LIMITED')))}")
    s.venue.faults.rate_limit_budget = None; t.step(5)
    return _finish(s, t, r)


def fee_spike(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("fee_spike", "priority fee spike at entry time")
    t.step(1); s.venue.faults.priority_fee_lamports = 10_000_000; t.step(1)
    d = t.propose()
    if d.approved:
        r.violations.append("entry approved during fee spike")
    return _finish(s, t, r)


def failed_exit_txs(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("failed_exit_txs", "every exit tx fails (congestion)")
    t.step(2); t.propose(); t.step(2)
    s.venue.faults.tx_fail_prob = 1.0
    s.venue.shock(TOKEN, Decimal("-0.1")); t.step(30)
    n = sum(1 for o in s.bot.engine.orders.values() if o.side is Side.SELL and o.attempt < s.cfg.max_exit_attempts)
    if n > s.cfg.max_exit_attempts:
        r.violations.append(f"retries exceeded bound: {n}")
    return _finish(s, t, r)


def late_fill_after_vanish(seed: int) -> Result:
    s = Sim(seed=seed); t = Tracker(s); r = Result("late_fill_after_vanish", "entry lands after market invalidation")
    t.step(2); s.venue.faults.confirm_delay_ms = 3500; t.propose()
    s.venue.remove_liquidity(TOKEN, Decimal("0.5"))
    s.venue.faults.confirm_delay_ms = 400; t.step(10)
    if s.position() and s.position().qty > 0:
        r.violations.append("late fill left open after vanish")
    return _finish(s, t, r)


def daily_loss_restart(seed: int) -> Result:
    r = Result("daily_loss_restart", "breach daily loss, restart, try to trade")
    with tempfile.TemporaryDirectory() as d:
        s = Sim(cfg=paper_config(daily_loss_limit_usd="25"), seed=seed, journal_path=str(Path(d) / "j.db"))
        t = Tracker(s)
        t.step(2); t.propose(usd="150", edge="900"); t.step(2)
        s.venue.shock(TOKEN, Decimal("-0.3")); t.step(4)
        s.restart(); t.step(1)
        if t.propose(usd="20").approved:
            r.violations.append("restart bypassed daily loss limit")
        return _finish(s, t, r)


def random_chaos(seed: int) -> Result:  # randomized: run with several seeds
    rnd = random.Random(seed)
    s = Sim(seed=seed); t = Tracker(s); r = Result("random_chaos", f"randomized fault sequence (seed {seed})")
    f = s.venue.faults
    t.step(2)
    for _ in range(80):
        a = rnd.choice(["step", "step", "step", "propose", "down", "gap", "rug", "to", "fail", "feed", "ok"])
        if a == "propose": t.propose()
        elif a == "down": s.venue.shock(TOKEN, Decimal("-0.06"))
        elif a == "gap": s.venue.shock(TOKEN, Decimal("-0.3"))
        elif a == "rug": s.venue.remove_liquidity(TOKEN, Decimal("0.5"))
        elif a == "to": f.submit_timeout_prob, f.status_timeout_prob = 0.6, 0.3
        elif a == "fail": f.tx_fail_prob = 0.7
        elif a == "feed": s.feed_up = not s.feed_up
        elif a == "ok": f.submit_timeout_prob = f.status_timeout_prob = f.tx_fail_prob = 0.0
        t.step()
    return _finish(s, t, r)


random_chaos.randomized = True  # type: ignore[attr-defined]

SCENARIOS: list[Callable[[int], Result]] = [
    baseline, gap_through_stop, submit_timeout_lands, crash_mid_submission, rug_pull, honeypot,
    stale_feed, rate_limit_exhaustion, fee_spike, failed_exit_txs, late_fill_after_vanish,
    daily_loss_restart, random_chaos,
]
