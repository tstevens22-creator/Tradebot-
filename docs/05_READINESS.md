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
| P1 | **Build step 1:** chain awareness (Solana + Base), per-chain address checks, EVM case normalization, post-graduation-only gate, migration → vanish, one exposure cap across chains | ✅ `chains.py`, `risk.py`, `bot.py`, `tests/test_chains.py` |
| P1 | **Build step 2:** Jupiter Swap V2 `/order` client (validated quotes, fee-aware), paper venue on LIVE Jupiter prices (re-quotes at landing, expiry on outage), read-only Solana RPC (mint/freeze authority, Token-2022 hazards, holders, signature status), `live_paper` runner | ✅ `venues/jupiter.py`, `data/solana_rpc.py`, `live_paper.py` |
| P1 | **Build step 3:** Bitquery (primary, pump.fun created/migrated) + Codex (secondary: stage, real liquidity, holders, creator history; Solana + Base) adapters, fail-closed consensus (disagreement / silent primary / stale = unknown), creator-reputation gate, feeds wired into `live_paper` | ✅ `data/bitquery.py`, `data/codex.py`, `data/feeds.py`, `tests/test_feeds.py` |
| P1 | **Build step 4:** Base via 0x Swap API v2 (AllowanceHolder): validated quotes (liquidity, token/amount match, allowlisted spender), conservative fees, measured price impact, 0x-measured buy/sell taxes; paper venue with **exact-amount approvals** to the allowlisted AllowanceHolder only, revoked on failure; read-only Base RPC safety (decimals, owner, EIP-1967 + legacy proxy slots); Base gas-price cap; `live_paper --chain base` (Codex-only feed) | ✅ `venues/zeroex.py`, `data/evm_rpc.py`, `venues/live_quote_paper.py` |
| P2 | Executable quote source (router) adapter | ❌ not started: needs your venue choice |
| P2 | On-chain sell simulation, mint-authority read, multi-RPC quorum | ❌ not started |
| P3 | Live venue adapter | ❌ **intentionally absent** |
| P3 | Strategy / signal improvements | ❌ out of scope until correctness is proven on real data |

## Test and replay results (2026-10-07, this container)

- `pytest`: **223 passed**. Covers config, risk gate, execution, protection, circuit breaker, reconciliation/restart, markouts, Birdeye parsing, proposal schema, architecture boundaries, and property-based fault fuzzing. Fuzzing was additionally run under 28 extra Hypothesis seeds, all passing.
- Fuzz coverage check (300 random sequences): reached filled entries, stop and emergency exits, failed exits, UNKNOWN orders, and exhausted-exit positions.
- Daily adversarial run: `reports/adversarial-2026-10-07.md`, **0 invariant violations**, 1 financial-impact finding.

### Defects found by the adversarial run and fixed in this change

1. **Rate-limited exits left a below-stop position unexited and un-halted.** Fixes: a 429 on submit is now a definite REJECTED (not UNKNOWN); protection falls back to the fresh snapshot bid; repeated quote failures halt. Regression tests: `test_rate_limited_exits_stay_protected_or_halt`, `test_rate_limited_submit_is_rejected_not_unknown`.
2. **`exit_retry_backoff_ms` was configured but unused**, so retries burned out within seconds. Fixed with exponential backoff (`test_exit_backoff_spreads_attempts`).
3. **An exit-failed position was never retried**, even after conditions cleared. Now governed by the explicit `exit_failed_probe_interval_s` (null = operator only). Tests: `test_probe_recovers_after_transient_failure`, `test_operator_only_policy_never_probes`.

4. **(Found in self-review.) An in-flight scale-out was forgotten after a crash and could repeat**, selling more than intended. It could never oversell. The pending scale-out is now rebuilt from the journal (`test_inflight_scale_out_not_repeated_after_restart`).

### Live-paper run on real data (2026-10-07, BONK, $20, keyless Jupiter, public RPC)

- Real quotes: round trip ≈ **35 bps** for $20 (including Jupiter's 10 bps fee each way). Quote round-trip time p50 **~350–380 ms**, p95 ~515–640 ms (measured, not simulated).
- On-chain safety read from the chain: no mint or freeze authority, classic SPL (tax 0), no hazards.
- **The gate correctly refused to trade**, because:
  - the keyless 0.5 req/s budget left no quote for the exact entry size;
  - the public RPC rate-limits `getTokenLargestAccounts`, so holder concentration is unknown.
  Both need owner-supplied credentials: `JUPITER_API_KEY` (free) and `SOLANA_RPC_URL` (paid RPC).
- **Defect found and fixed:** impact-implied liquidity swung ~40% as Jupiter's winning router changed, falsely tripping `LIQUIDITY_COLLAPSE` (it would have forced an emergency exit). Estimated liquidity can no longer trip collapse (`test_estimated_liquidity_swings_do_not_trigger_collapse`). Real liquidity comes from the step-3 feed.
- Stage and sellability had to be operator-asserted. Without the assertions the gate rejects, which is correct and tested.

### Step 4 status: Base built, NOT yet run against 0x

- 0x requires an API key (HTTP 401 without one), and none exists here. The parser follows the 0x v2 docs' response example (`tests/fixtures/base/zx_price_doc_shape.json`, re-addressed and illustrative). Base RPC safety reads are verified against **real** Base responses: DEGEN has a live owner (so it is rejected under the owner rule), Base USDC is detected as an upgradeable proxy via the legacy slot, and WETH has no `owner()`, which is unknown and rejected.
- **Owner-rule consequence:** tokens with a live owner, or no `owner()` function, are rejected on Base until Codex `mintable`/`freezable` data is verified, or the owner sets a different policy. This may block many Base tokens.
- Sellability on Base still needs a real sell simulation (live step). Until then it must be operator-asserted.

### Step 3 status: feeds built, NOT yet run against the real APIs

- No Bitquery or Codex key exists in this environment. Both APIs answered unauthenticated (Codex: HTTP 402 payment required). Queries and field names were taken from each provider's docs (Codex `filterTokens` reference; Bitquery `llms-full.txt`), and the tests use responses built from those documented schemas.
- **To verify on the first keyed call:** that the Bitquery query runs as written, the scale of Codex `top10HoldersPercent` (read conservatively until then), Codex `liquidity` semantics, and Bitquery free-trial coverage of older tokens (real-time only; Codex cross-checks).
- Creator thresholds (`creator_max_tokens_created = 20`, `creator_min_graduation_ratio = 0.2`) are **placeholders** until the owner decides checklist item 15b. Codex creator counts are lifetime counts, so a "launches per day" window is not available yet.

### Owner rules added: 10% stop, 30% profit floor

Enforced as code constants (see `03_RISK_SPEC.md`). Baseline scenario: a +25% move is held, and a gradual climb is sold by take-profit at **+$30.57 on $100**.

### Open findings (need your decision, not code)

- A −35% gap realized a **$35.34** loss on a $100 position, despite the 10% stop. The stressed budget was $30.72 (`stress_gap_multiplier = 3`). Meme coins gap this much routinely. The 10% stop limits planned loss, not gap loss.
- The volatility breaker (3000 bps over 60 s) is direction-agnostic. A fast pump past +30% trips it and triggers an **emergency** exit instead of a take-profit. A pump followed by a dump inside the window could emergency-sell at less than +30%. That is allowed by owner decision: protective exits outrank the profit floor.

### Latency (simulated, NOT production)

`detect_to_decision` p50 5 ms / p99 ≈ 5 s. The tail comes from decisions made on stale data during feed-outage scenarios, and it is reported as budget misses. `exit_decision_to_confirm` p50 450 ms. All delays are injected by the simulator, so they say nothing about real-world latency.

## Owner live authorization (2026-10-07) and the preflight gate

The owner authorized live trading **"after all tests have passed with flying colors, no flaws or leaks detected"**. That condition is now code: `python -m tradebot.preflight --config <live.toml>`. Live mode refuses to start without a passing preflight for the exact config hash from the last 24 h, and even then until the live adapter exists.

Preflight result on 2026-10-07: **BLOCKED.** Passing: tests (233/233), adversarial run (0 violations), leak scan (98 files, no secrets; the Jupiter key's value was checked explicitly). Failing:
1. `live_config`: the live config has not been filled in (checklist items 6–15b).
2. `operator_ack`: `TRADEBOT_LIVE_ACK` must equal the reviewed config hash.
3. `credentials`: `CODEX_API_KEY`, `BITQUERY_API_KEY`, a paid `SOLANA_RPC_URL`, `ZEROX_API_KEY` (public RPCs are refused).
4. `hot_wallet`: dedicated wallet key file outside the repo, mode 600, holding only the risk budget.
5. `feeds_verified_live`: at least one live-paper run with working Bitquery and Codex feeds.
6. `paper_soak`: 2,000 live-data ticks in 7 days with no exit failures (107 so far).
7. `live_capabilities`: live signer + `/execute` + on-chain reconciliation, and sell simulation, are not built.

**Flaw found by this review and fixed:** the daily loss budget ignored risk committed in open positions, so concurrent trades could jointly exceed the daily limit (`test_open_positions_count_against_daily_loss_budget`, which fails on the old code).

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
