"""Trading orchestrator (paper / shadow only).

Tick order (protection first, entries last):
  1. Reconcile in-flight orders (fills -> portfolio).
  2. Escalate stuck UNKNOWN orders -> halt.
  3. Circuit breaker per token -> vanish procedure.
  4. Protective exits on executable liquidation quotes.
  5. Portfolio limits (daily loss, drawdown) -> halt new exposure.
Entries come only via ``propose()``, which is gated by ``risk.authorize``.
No LLM call happens anywhere in this module.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Optional

from .chains import STAGE_MIGRATING
from .circuit_breaker import CircuitBreaker
from .clock import Clock
from .config import ConfigError, RiskConfig, assert_live_allowed
from .execution import ExecutionEngine
from .journal import Journal
from .latency import LatencyMetrics
from .models import BPS, EXIT_PRECEDENCE, Fill, MarketSnapshot, Purpose, Side, TokenSafety
from .portfolio import Portfolio
from .protection import ExitIntent, evaluate_exits
from .risk import Decision, RiskContext, authorize
from .signals import ProposalError, parse_proposal
from .state_machine import Order, OrderState
from .venues.base import SwapVenue, VenueError

MANUAL = "manual"  # needs operator resume()
AUTO = "auto"  # clears when the breaker recovers


class Bot:
    def __init__(self, cfg: RiskConfig, venue: SwapVenue, journal: Journal, clock: Clock,
                 portfolio: Optional[Portfolio] = None):
        if cfg.mode == "live":
            assert_live_allowed(cfg)  # always raises today: no live adapter
        if cfg.mode == "paper" and not getattr(venue, "is_simulated", False):
            raise ConfigError("paper mode requires a simulated venue")
        if journal.config_hash != cfg.config_hash:
            raise ConfigError("journal config hash does not match config")
        self.cfg, self.venue, self.journal, self.clock = cfg, venue, journal, clock
        self.portfolio = portfolio or Portfolio(starting_equity_usd=cfg.starting_equity_usd)
        self.latency = LatencyMetrics(budgets_ms={
            "detect_to_decision": cfg.budget_detect_to_decision_ms,
            "decision_to_submit": cfg.budget_decision_to_submit_ms,
            "exit_decision_to_confirm": cfg.budget_exit_confirm_ms,
        })
        self.engine = ExecutionEngine(venue, journal, clock, self.portfolio, self.latency, on_fill=self._on_fill)
        self.breaker = CircuitBreaker(cfg)
        self.snapshots: dict[str, MarketSnapshot] = {}
        self.safety: dict[str, TokenSafety] = {}
        self.halts: dict[str, str] = {}
        self.exit_attempts: dict[str, int] = {}
        self.counted_exit_failures: set[str] = set()
        self.last_stop_ms: dict[str, int] = {}
        self.seen_proposals: set[str] = set()
        self.exit_decision_ms: dict[str, int] = {}
        self.pending_scale: dict[str, int] = {}
        self.quote_failures: dict[str, int] = {}
        self.next_exit_allowed_ms: dict[str, int] = {}
        self.last_probe_ms: dict[str, int] = {}
        self.journal.append(clock.now_ms(), "CONFIG_LOADED", {"mode": cfg.mode, "hash": cfg.config_hash})

    # ================================================================ state
    @property
    def halted(self) -> bool:
        return bool(self.halts)

    def halt(self, reason: str, kind: str = MANUAL) -> None:
        if reason not in self.halts:
            self.halts[reason] = kind
            self.journal.append(self.clock.now_ms(), "HALT", {"reason": reason, "kind": kind})

    def resume(self, operator: str) -> list[str]:
        """Operator action. Refuses while hazards remain. Returns blockers."""
        blockers = [f"exit_failed:{p.token}" for p in self.portfolio.open_positions() if p.exit_failed]
        blockers += [f"unknown:{o.client_order_id}" for o in self.engine.orders.values()
                     if o.state is OrderState.UNKNOWN]
        blockers += [f"breaker:{t}" for t, s in self.breaker.tokens.items() if s.tripped]
        if blockers:
            return blockers
        self.journal.append(self.clock.now_ms(), "RESUME", {"operator": operator, "cleared": sorted(self.halts)})
        self.halts.clear()
        return []

    # ================================================================ data
    def ingest(self, snap: MarketSnapshot, safety: Optional[TokenSafety] = None) -> None:
        self.snapshots[snap.token] = snap
        if safety is not None:
            self.safety[snap.token] = safety

    # ================================================================ entries
    def propose(self, raw: Any) -> Decision:
        now = self.clock.now_ms()
        try:
            p = parse_proposal(raw)
        except ProposalError as e:
            self.journal.append(now, "PROPOSAL_REJECTED", {"error": str(e)})
            return Decision(False, (f"MALFORMED: {e}",), "", self.cfg.config_hash)
        if p.proposal_id in self.seen_proposals:
            return Decision(False, ("DUPLICATE_PROPOSAL",), "", self.cfg.config_hash)
        self.seen_proposals.add(p.proposal_id)
        self.journal.append(now, "PROPOSAL", {"id": p.proposal_id, "token": p.token, "chain": p.chain,
                                              "usd": p.usd_size, "edge_bps": p.expected_edge_bps})
        try:
            q = self.venue.quote_buy(p.token, p.usd_size)
        except (VenueError, KeyError) as e:
            self.journal.append(now, "QUOTE_ERROR", {"token": p.token, "error": repr(e)})
            q = None
        ctx = RiskContext(
            now_ms=self.clock.now_ms(), snapshot=self.snapshots.get(p.token), safety=self.safety.get(p.token),
            entry_quote=q, portfolio=self.portfolio, halted=self.halted,
            token_paused=self.breaker.is_tripped(p.token), inflight_entry_usd=self.engine.inflight_entry_usd(),
            unresolved_orders_on_token=self.engine.unresolved_count(p.token),
            last_stop_ms=self.last_stop_ms.get(p.token),
            est_network_fee_usd=2 * self.venue.network_fee_usd(),
            venue_supports_chain=p.chain in getattr(self.venue, "chains", ()),
            liquidation_values=self._liquidation_values(),
        )
        d = authorize(self.cfg, p, ctx)
        self.journal.append(self.clock.now_ms(), "DECISION", {
            "proposal": p.proposal_id, "approved": d.approved, "reasons": list(d.reasons),
            "decision_id": d.decision_id, "planned_loss": d.planned_loss_usd, "stressed_loss": d.stressed_loss_usd,
        })
        if not d.approved:
            return d
        order = Order(client_order_id=f"e-{uuid.uuid4().hex[:16]}", token=p.token, side=Side.BUY,
                      purpose=Purpose.ENTRY, qty=d.usd_size, min_out=d.min_tokens_out,
                      slippage_bps=self.cfg.max_entry_slippage_bps, decision_id=d.decision_id)
        if self.cfg.mode == "shadow":
            self.journal.append(self.clock.now_ms(), "SHADOW_ORDER", {"cid": order.client_order_id})
            return d
        self.engine.submit(order)
        return d

    # ================================================================ main loop
    def tick(self) -> None:
        now = self.clock.now_ms()
        self.engine.poll()
        self._count_exit_failures()

        for o in self.engine.stuck_unknown(self.cfg.unknown_resolution_timeout_s):
            self.halt(f"UNKNOWN_UNRESOLVED:{o.client_order_id}")

        held = {p.token for p in self.portfolio.open_positions()}
        for token in sorted(held | set(self.snapshots)):
            stage = self.safety[token].venue_stage if token in self.safety else None
            extra = ("MIGRATION",) if stage == STAGE_MIGRATING else ()
            new = self.breaker.observe(self.snapshots.get(token), token, now, extra)
            if new:
                self._vanish(token, new)
            elif not self.breaker.is_tripped(token) and self.halts.get(f"VANISH:{token}") == AUTO:
                del self.halts[f"VANISH:{token}"]
                self.journal.append(now, "AUTO_RECOVERED", {"token": token})

        for pos in self.portfolio.open_positions():
            self._protect(pos.token)

        liq = self._liquidation_values()
        eq = self.portfolio.equity(liq)
        self.portfolio.update_peak(eq)
        if self.portfolio.realized_today(now) <= -self.cfg.daily_loss_limit_usd:
            self.halt("DAILY_LOSS_LIMIT")
        if self.portfolio.drawdown_pct(eq) >= self.cfg.max_drawdown_pct:
            self.halt("MAX_DRAWDOWN")

    # ================================================================ exits
    def _liq_quote(self, token: str, qty: Decimal):
        try:
            return self.venue.quote_sell(token, qty)
        except (VenueError, KeyError) as e:
            self.journal.append(self.clock.now_ms(), "QUOTE_ERROR", {"token": token, "error": repr(e)})
            return None

    def _liquidation_values(self) -> dict[str, Decimal]:
        out = {}
        for p in self.portfolio.open_positions():
            snap = self.snapshots.get(p.token)
            if snap and snap.bid and snap.bid.qty > 0:
                # Conservative estimate from the latest snapshot: reference-size bid price
                # (an over-estimate when the position is larger than the reference size;
                # see loss register B3). The full-size quote is used in _protect.
                out[p.token] = p.qty * snap.bid.price - self.venue.network_fee_usd()
        return out

    def _protect(self, token: str) -> None:
        pos = self.portfolio.positions[token]
        now = self.clock.now_ms()
        snap = self.snapshots.get(token)
        fresh = snap is not None and abs(snap.age_ms(now)) <= self.cfg.max_data_age_ms
        trigger: Optional[Decimal] = None
        net_liq: Optional[Decimal] = None
        if fresh:
            if self.cfg.trigger_price_source == "executable_bid":
                q = self._liq_quote(token, pos.qty)
                if q is not None:
                    trigger = q.price
                    net_liq = q.usd - self.venue.network_fee_usd()
                    self.quote_failures[token] = 0
                elif snap.bid is not None:
                    # Full-size quote unavailable (e.g. rate limit). Fall back to the fresh
                    # reference-size executable bid so protection stays active. It is optimistic
                    # for large positions, so the escalation ladder absorbs the difference.
                    trigger = snap.bid.price
                    self.quote_failures[token] = self.quote_failures.get(token, 0) + 1
                    if self.quote_failures[token] >= self.cfg.max_exit_attempts:
                        self.halt(f"EXIT_QUOTES_UNAVAILABLE:{token}")
            else:
                trigger = snap.indicative_price
        intents = evaluate_exits(self.cfg, pos, now, trigger, net_liq, self.engine.inflight_exit_qty(token))

        if self.breaker.is_tripped(token):
            policy = self.cfg.vanish_emergency_policy
            losing = net_liq is None or net_liq < pos.cost_usd
            if policy == "exit_all" or (policy == "exit_if_loss" and losing):
                avail = pos.qty - self.engine.inflight_exit_qty(token)
                if avail > 0:
                    intents.append(ExitIntent(token, Purpose.EMERGENCY, avail, "vanish policy", trigger))
                    intents.sort(key=lambda e: EXIT_PRECEDENCE[e.purpose])
        if intents:
            if snap is not None:
                self.latency.record("detect_to_decision", now - snap.ts_received_ms)
            self._exit(intents[0])

    def _exit(self, it: ExitIntent) -> None:
        token = it.token
        pos = self.portfolio.positions[token]
        now = self.clock.now_ms()
        if any(o.side is Side.SELL for o in self.engine.open_orders(token)):
            return  # one active exit per position; cannot cancel a pending swap
        qty = min(it.qty, pos.qty - self.engine.inflight_exit_qty(token))
        if qty <= 0:
            return
        if now < self.next_exit_allowed_ms.get(token, 0):
            return  # bounded backoff between failed attempts
        attempts = self.exit_attempts.get(token, 0)
        if attempts >= self.cfg.max_exit_attempts:
            if not pos.exit_failed:
                pos.exit_failed = True
                self.journal.append(now, "EXIT_FAILED", {"token": token, "qty": pos.qty, "attempts": attempts,
                                                         "purpose": it.purpose.name})
                self.halt(f"EXIT_FAILED:{token}")
                self.last_probe_ms[token] = now
                return
            interval = self.cfg.exit_failed_probe_interval_s
            if interval is None or now - self.last_probe_ms.get(token, 0) < interval * 1000:
                return  # operator-only recovery, or waiting for the next probe slot
            self.last_probe_ms[token] = now
            self.journal.append(now, "EXIT_PROBE", {"token": token})
        ladder = self.cfg.exit_slippage_ladder_bps
        slip = min(ladder[min(attempts, len(ladder) - 1)], self.cfg.emergency_max_slippage_bps)
        decision_ms = self.clock.now_ms()
        q = self._liq_quote(token, qty)
        if q is not None:
            expected_usd = q.usd
        else:
            snap = self.snapshots.get(token)
            if snap is None or snap.bid is None or abs(snap.age_ms(decision_ms)) > self.cfg.max_data_age_ms:
                self.journal.append(decision_ms, "EXIT_BLOCKED_NO_QUOTE", {"token": token, "purpose": it.purpose.name})
                return
            expected_usd = qty * snap.bid.price  # fresh snapshot fallback; never invents a price
        min_out = expected_usd * (1 - slip / BPS)
        o = Order(client_order_id=f"x-{uuid.uuid4().hex[:16]}", token=token, side=Side.SELL, purpose=it.purpose,
                  qty=qty, min_out=min_out, slippage_bps=slip, attempt=attempts)
        self.journal.append(decision_ms, "EXIT_DECISION", {
            "cid": o.client_order_id, "purpose": it.purpose.name, "reason": it.reason, "qty": qty,
            "trigger_price": it.trigger_price, "slippage_bps": slip, "scale_index": it.scale_index})
        if self.cfg.mode == "shadow":
            return
        self.exit_decision_ms[o.client_order_id] = decision_ms
        self.engine.submit(o)
        self.latency.record("decision_to_submit", (o.submitted_ms or decision_ms) - decision_ms)
        if it.scale_index is not None:
            self.pending_scale[o.client_order_id] = it.scale_index

    def _count_exit_failures(self) -> None:
        for o in self.engine.orders.values():
            if (o.side is Side.SELL and o.state in (OrderState.FAILED, OrderState.EXPIRED, OrderState.REJECTED)
                    and o.client_order_id not in self.counted_exit_failures):
                self.counted_exit_failures.add(o.client_order_id)
                self.exit_attempts[o.token] = self.exit_attempts.get(o.token, 0) + 1
                n = min(self.exit_attempts[o.token], self.cfg.max_exit_attempts)
                self.next_exit_allowed_ms[o.token] = (
                    self.clock.now_ms() + self.cfg.exit_retry_backoff_ms * 2 ** (n - 1))
                self.journal.append(self.clock.now_ms(), "EXIT_ATTEMPT_FAILED",
                                    {"cid": o.client_order_id, "token": o.token,
                                     "attempt": self.exit_attempts[o.token]})

    def _on_fill(self, o: Order, f: Fill) -> None:
        pos = self.portfolio.positions[o.token]
        if o.side is Side.SELL:
            dm = self.exit_decision_ms.pop(o.client_order_id, None)
            if dm is not None:
                self.latency.record("exit_decision_to_confirm", f.ts_exec_ms - dm)
            self.exit_attempts[o.token] = 0
            self.next_exit_allowed_ms.pop(o.token, None)
            if pos.exit_failed:
                pos.exit_failed = False
                self.journal.append(self.clock.now_ms(), "EXIT_RECOVERED", {"token": o.token, "remaining": pos.qty})
            if o.purpose in (Purpose.STOP_LOSS, Purpose.TRAILING_STOP, Purpose.EMERGENCY):
                self.last_stop_ms[o.token] = f.ts_exec_ms
                self.journal.append(self.clock.now_ms(), "STOPPED_OUT", {"token": o.token, "ts": f.ts_exec_ms})
            idx = self.pending_scale.pop(o.client_order_id, None)
            if idx is not None:
                pos.scale_outs_done.add(idx)
                self.journal.append(self.clock.now_ms(), "SCALE_OUT_DONE", {"token": o.token, "index": idx})

    # ================================================================ vanish
    def _vanish(self, token: str, triggers: list[str]) -> None:
        now = self.clock.now_ms()
        snap = self.snapshots.get(token)
        self.journal.append(now, "VANISH", {"token": token, "triggers": triggers,
                                            "detected_ms": snap.ts_received_ms if snap else None})
        # 1. Block new exposure everywhere immediately.
        self.halt(f"VANISH:{token}", AUTO)
        # 2. Resting entries: swaps cannot be cancelled. Record it; late fills get the vanish policy.
        for o in self.engine.open_orders(token):
            if o.side is Side.BUY:
                if o.is_swap:
                    self.journal.append(now, "ENTRY_NOT_CANCELLABLE", {"cid": o.client_order_id,
                                        "note": "pending swap tx; will be reconciled and exited per policy"})
                else:
                    o.request_cancel()
                    self.engine._save(o)
        # 3./4. Protective exits stay in place; emergency exit is handled by _protect on this tick.

    # ================================================================ restart
    @classmethod
    def recover(cls, cfg: RiskConfig, venue: SwapVenue, journal: Journal, clock: Clock) -> "Bot":
        fills = journal.load_fills()
        pf = Portfolio.from_fills(cfg.starting_equity_usd, fills.values())
        orders = journal.load_orders()
        halts: dict[str, str] = {}
        exit_failed: set[str] = set()
        last_stop: dict[str, int] = {}
        seen: set[str] = set()
        scale_done: dict[str, set[int]] = {}
        scale_pending: dict[str, int] = {}
        for _, _, t, _, p in journal.events():
            if t == "HALT":
                halts[p["reason"]] = p["kind"]
            elif t == "RESUME":
                halts.clear()
                exit_failed.clear()
            elif t == "EXIT_FAILED":
                exit_failed.add(p["token"])
            elif t == "EXIT_RECOVERED":
                exit_failed.discard(p["token"])
            elif t == "STOPPED_OUT":
                last_stop[p["token"]] = p["ts"]
            elif t == "PROPOSAL":
                seen.add(p["id"])
            elif t == "SCALE_OUT_DONE":
                scale_done.setdefault(p["token"], set()).add(p["index"])
            elif t == "EXIT_DECISION" and p.get("scale_index") is not None:
                scale_pending[p["cid"]] = p["scale_index"]
        bot = cls(cfg, venue, journal, clock, portfolio=pf)
        bot.halts = {k: (MANUAL if v == AUTO else v) for k, v in halts.items()}  # restart => manual review
        bot.last_stop_ms = last_stop
        bot.seen_proposals = seen
        for t, idx in scale_done.items():
            if t in pf.positions:
                pf.positions[t].scale_outs_done = idx
        for t in exit_failed:
            if t in pf.positions and pf.positions[t].is_open:
                pf.positions[t].exit_failed = True
        bot.pending_scale = {cid: i for cid, i in scale_pending.items()
                             if cid in orders and not orders[cid].is_terminal}
        # Any non-terminal order is ambiguous after a crash.
        for o in orders.values():
            bot.engine.orders[o.client_order_id] = o
            pf.applied_fill_ids.update(o.fills)  # fills already applied via from_fills
            if not o.is_terminal and o.state is not OrderState.UNKNOWN:
                if o.state is OrderState.NEW:
                    o.transition(OrderState.SUBMITTING)
                o.transition(OrderState.UNKNOWN)
                o.unknown_since_ms = clock.now_ms()
                bot.engine._save(o)
        bot.engine.poll()
        bot._count_exit_failures()
        bot.journal.append(clock.now_ms(), "RECOVERED", {"orders": len(orders), "fills": len(fills)})
        bot.reconcile_balances()
        return bot

    def reconcile_balances(self) -> list[str]:
        mismatches = []
        tokens = {p.token for p in self.portfolio.positions.values()}
        for t in sorted(tokens):
            if self.engine.unresolved_count(t):
                continue  # cannot compare while txs are in flight
            try:
                bal = self.venue.balance(t)
            except VenueError as e:
                mismatches.append(f"{t}:balance_unavailable:{e!r}")
                continue
            if bal != self.portfolio.positions[t].qty:
                mismatches.append(f"{t}:journal={self.portfolio.positions[t].qty}:venue={bal}")
        for m in mismatches:
            self.halt(f"RECONCILE_MISMATCH:{m}")
        self.journal.append(self.clock.now_ms(), "RECONCILE", {"mismatches": mismatches})
        return mismatches
