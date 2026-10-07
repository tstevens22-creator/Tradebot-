# 5. Implementation Plan, Results and Readiness Verdict

## Prioritized plan (status)

| P | Item | Status |
|---|---|---|
| P0 | Explicit, immutable, fail-closed risk config; live hard-blocked | ✅ `config.py` |
| P0 | Order state machine (UNKNOWN, CANCEL_PENDING, swap non-cancellability) | ✅ `state_machine.py` |
| P0 | Write-ahead journal, idempotent submit, reconcile-before-resubmit | ✅ `journal.py`, `execution.py` |
| P0 | Deterministic risk gate (exposure, daily loss, drawdown, token safety, costs vs edge, planned vs stressed loss) | ✅ `risk.py` |
| P0 | Synthetic SL/TP/trail/max-hold on executable quotes; no oversell | ✅ `protection.py`, `bot.py` |
| P0 | Startup reconciliation; persisted halts and limits | ✅ `Bot.recover` |
| P1 | Vanish circuit breaker + emergency policy + gated recovery | ✅ `circuit_breaker.py` |
| P1 | Bounded exit escalation with backoff, explicit post-exhaustion policy | ✅ |
| P1 | Latency p50/p95/p99 with budgets | ✅ `latency.py` |
| P1 | Fill grading at +1/+5/+10 s, UNKNOWN on missing data, compliance kept separate | ✅ `markout.py` |
| P1 | Daily adversarial run (simulation) + CI schedule | ✅ `adversarial/`, `.github/workflows/` |
| P2 | Birdeye read-only adapter with rate budget reserving quota for exits | ✅ (field mapping **unverified** against the live API) |
| P2 | Executable quote source (router) adapter | ❌ not started: needs your venue choice |
| P2 | On-chain sell simulation, mint-authority read, multi-RPC quorum | ❌ not started |
| P3 | Live venue adapter | ❌ **intentionally absent** |
| P3 | Strategy / signal improvements | ❌ out of scope until correctness is proven on real data |

## Test and replay results (2026-10-07, this container)

- `pytest`: **114 passed**. Covers config, risk gate, execution, protection, circuit breaker, reconciliation/restart, markouts, Birdeye parsing, proposal schema, architecture boundaries, and property-based fault fuzzing. Fuzzing was additionally run under 28 extra Hypothesis seeds, all passing.
- Fuzz coverage check (300 random sequences): reached filled entries, stop and emergency exits, failed exits, UNKNOWN orders, and exhausted-exit positions.
- Daily adversarial run: `reports/adversarial-2026-10-07.md`, **0 invariant violations**, 1 financial-impact finding.

### Defects found by the adversarial run and fixed in this change

1. **Rate-limited exits left a below-stop position unexited and un-halted.** Fixes: a 429 on submit is now a definite REJECTED (not UNKNOWN); protection falls back to the fresh snapshot bid; repeated quote failures halt. Regression tests: `test_rate_limited_exits_stay_protected_or_halt`, `test_rate_limited_submit_is_rejected_not_unknown`.
2. **`exit_retry_backoff_ms` was configured but unused**, so retries burned out within seconds. Fixed with exponential backoff (`test_exit_backoff_spreads_attempts`).
3. **An exit-failed position was never retried**, even after conditions cleared. Now governed by the explicit `exit_failed_probe_interval_s` (null = operator only). Tests: `test_probe_recovers_after_transient_failure`, `test_operator_only_policy_never_probes`.

4. **(Found in self-review.) An in-flight scale-out was forgotten after a crash and could repeat**, selling more than intended. It could never oversell. The pending scale-out is now rebuilt from the journal (`test_inflight_scale_out_not_repeated_after_restart`).

### Open finding (needs your decision, not code)

- A −35% gap realized a **$35.34** loss on a $100 position, against a **$24.72** stressed budget (`stress_gap_multiplier = 3`, stop 8%). Meme coins gap this much routinely. Either raise the multiplier or cut position size.

### Latency (simulated, NOT production)

`detect_to_decision` p50 5 ms / p99 ≈ 5 s. The tail comes from decisions made on stale data during feed-outage scenarios, and it is reported as budget misses. `exit_decision_to_confirm` p50 450 ms. All delays are injected by the simulator, so they say nothing about real-world latency.

## Readiness verdict

**NOT READY FOR REAL MONEY.** Paper / shadow mode only.

Blockers before any live consideration:
1. Answer every item in `04_INPUT_CHECKLIST.md`.
2. Build and test an executable-quote adapter and a live venue adapter, with on-chain reconciliation (signature status + balance) and multi-RPC agreement.
3. Add sell simulation and a mint/freeze-authority read from the chain itself, not only from Birdeye.
4. Verify Birdeye field mappings against the live API.
5. Run shadow mode on live data for long enough to measure real latency, markouts and data gaps.
6. Owner sets `live_trading_authorized = true` and `TRADEBOT_LIVE_ACK=<config hash>`.

Residual risks no code removes: synthetic stops don't work while the bot is down; rugs inside a single block; gap losses beyond any stop; MEV within the slippage tolerance; wrong data from upstream sources.
