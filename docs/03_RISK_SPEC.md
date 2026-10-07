# 3. Risk Specification and Invariants

The AI **proposes**. Deterministic code (`tradebot/risk.py`) **authorizes**.
The config is a frozen dataclass whose hash is stamped on every journal event.
No runtime path, the AI included, can mutate limits, widen stops, or suppress
protective exits.

## Owner-mandated hard limits (code constants, not config)

Defined in `tradebot/config.py`. No config file or AI proposal can loosen them. Changing them requires a code change.

| Constant | Value | Effect |
|---|---|---|
| `HARD_MAX_STOP_LOSS_PCT` | **0.10** | `stop_loss_pct` may be tighter than 10%, never wider. The stop fires when the **worse** of price return and net P&L (after estimated exit costs) reaches −10%. |
| `HARD_MIN_PROFIT_EXIT_PCT` | **0.30** | `take_profit_pct` and every scale-out trigger must be ≥ 30%. At runtime, take-profit, scale-out and trailing-stop sales are blocked until the **worse** of price return and net P&L is ≥ +30%. |

The 30% floor applies only to **profit-taking** exits. It does **not** block protective exits: stop-loss, emergency/vanish exits, max-hold, and exit-failure probes.

**What −10% does and does not guarantee:** the bot *starts* exiting at −10%. A price gap, a rug, or a failed or slow exit can realize a larger loss. The daily report shows a −35% gap realizing about −35%. No code can prevent that on an AMM.

## Settings

All settings live in `tradebot/config.py`. Every one is required, and a missing
key fails loading with a single error that lists all missing keys.
`trailing_stop_pct`, `max_holding_seconds` and `exit_failed_probe_interval_s`
may be `null` (disabled), but they must still be written out.

| Group | Settings | Semantics |
|---|---|---|
| Stops/targets | `stop_loss_pct`, `take_profit_pct`, `trigger_price_source`, `target_basis`, `trailing_stop_pct`, `max_holding_seconds`, `scale_outs` | Trigger source `executable_bid` = router/pool quote for the **full position size**. `indicative` is forbidden in live. TP uses `target_basis` (`price_return` or `net_pnl`). The **stop always uses the worse of the two.** |
| Sizing | `per_trade_risk_usd`, `max_position_usd`, `max_total_exposure_usd`, `max_concentration_pct`, `max_open_positions`, `stress_gap_multiplier`, `max_stressed_loss_usd`, `allow_average_down` | Planned loss = size × (stop + round-trip cost). Stressed loss = size × min(1, stop × multiplier + cost). Both are checked. In-flight entries count toward exposure. |
| Portfolio | `daily_loss_limit_usd`, `max_drawdown_pct`, `starting_equity_usd` | Daily = realized net P&L for the UTC day, rebuilt from the journal on restart. Each new trade's planned loss must fit the **remaining** daily budget. Drawdown is measured from peak equity, with open positions marked at executable liquidation value. |
| Market quality | `min_liquidity_usd`, `max_spread_bps`, `max_entry_slippage_bps`, `max_entry_price_impact_bps`, `max_data_age_ms`, `min_edge_buffer_bps` | Pool "spread" = round-trip cost of the reference size from executable quotes (no invented midpoint). Edge must exceed round-trip + 2×tax + network fees + buffer. |
| Token safety | `reject_mint_authority`, `reject_freeze_authority`, `max_transfer_tax_bps`, `max_top10_holder_pct`, `require_sell_simulation` | **Unknown = reject.** |
| Chain fees | `max_priority_fee_lamports`, `emergency_max_priority_fee_lamports` | Entries are blocked above the normal cap. |
| Exits | `max_exit_attempts`, `exit_retry_backoff_ms`, `exit_failed_probe_interval_s`, `exit_slippage_ladder_bps`, `emergency_max_slippage_bps`, `unknown_resolution_timeout_s`, `cooldown_after_stop_s` | Ladder is non-decreasing and capped by the emergency max (validated). Backoff doubles after each failed attempt. After exhaustion the position is flagged UNPROTECTED and trading halts. Then either one capped probe per interval runs, or (`null`) recovery is operator-only. |
| Vanish | `vanish_adverse_move_pct`, `vanish_window_s`, `vanish_spread_bps`, `vanish_liquidity_drop_pct`, `vanish_volatility_bps`, `vanish_emergency_policy`, `recovery_healthy_observations`, `recovery_requires_manual_ack` | See `circuit_breaker.py`. |
| Latency | `budget_detect_to_decision_ms`, `budget_decision_to_submit_ms`, `budget_exit_confirm_ms` | p50/p95/p99 and misses are reported. |
| Markouts | `markout_horizons_s`, `markout_benchmark_tolerance_ms`, `markout_adverse_threshold_bps` | Frozen in the config hash **before** evaluation. |

## How conflicting rules are resolved

1. **Exit precedence:** STOP_LOSS > EMERGENCY > TRAILING_STOP > MAX_HOLD > TAKE_PROFIT > SCALE_OUT.
2. **The 30% profit floor never blocks a protective exit.** The stop is evaluated on min(price return, net return). The floor gates only TP, scale-out and trailing sales.
3. **Halts never block exits.** A halt blocks new exposure only.
4. **Fee caps never block exits.** Exits are bounded by the slippage ladder and the emergency cap instead.
5. Config validation rejects: position cap > total cap; per-trade risk > daily limit; stressed budget < per-trade risk; ladder above the emergency cap or decreasing; scale-outs ≥ TP or summing above 100%; live mode with indicative triggers.
6. Averaging down is rejected unless `allow_average_down = true`. Stops are never widened: no code path for that exists.

## Invariants (enforced in code, checked by tests and the daily run)

| ID | Invariant | Enforced in | Checked by |
|---|---|---|---|
| I1 | Position qty ≥ 0. A sell can never exceed confirmed qty. | `Portfolio.apply_fill`, `Bot._exit` | property tests, scenarios |
| I2 | A client order executes at most once | deterministic signatures, `DuplicateOrder` | fuzz, scenarios |
| I3 | A client_order_id is never resubmitted. UNKNOWN orders block new orders on the token. | `ExecutionEngine`, `RiskContext.unresolved_orders_on_token` | `test_no_resubmit_while_unknown` |
| I4 | Total exposure (incl. in-flight) ≤ cap | `risk.authorize` | fuzz |
| I5 | Journal position == venue balance when nothing is in flight. A mismatch halts. | `Bot.reconcile_balances` | fuzz, `test_balance_mismatch_halts` |
| I6 | A held position on stale data ⇒ halted (vanish STALE_DATA) | `CircuitBreaker` | fuzz |
| I7 | No entry is submitted while halted | `risk.authorize` | fuzz, scenarios |
| I8 | A timeout ⇒ UNKNOWN. UNKNOWN resolves only via venue status or proven expiry. | `ExecutionEngine` | `test_timeout_is_unknown_not_failed`, `test_crash_between_write_ahead_and_send` |
| I9 | Write-ahead: SUBMITTING is journaled before send | `ExecutionEngine.submit` | `test_write_ahead_journal_before_send` |
| I10 | Exit retries are bounded, backed off and capped in slippage. Exhaustion ⇒ UNPROTECTED flag + halt. | `Bot._exit` | `test_exit_escalation_is_bounded`, honeypot scenario |
| I11 | Halts, daily P&L, stop cooldowns, scale-outs and exit-failure flags survive restart | `Bot.recover` | `test_reconcile.py` |
| I12 | Markouts never feed decisions. No LLM/network imports in the trading path. | module boundaries | `test_architecture.py` |

## Execution model specifics (DEX)

- **Cancellation:** a broadcast swap transaction **cannot be cancelled**.
  `Order.request_cancel()` raises `NotCancellable` for swaps. During a vanish,
  pending entries are journaled `ENTRY_NOT_CANCELLABLE`. If they land, the
  late fill is exited under the emergency policy (`test_late_fill_after_vanish_is_exited`).
  Only order-book orders (`is_swap=False`) go to `CANCEL_PENDING`.
- **Strict slippage vs urgent liquidation:** entries use `max_entry_slippage_bps`.
  Exits climb `exit_slippage_ladder_bps` one failed attempt at a time and never
  exceed `emergency_max_slippage_bps`. If the market cannot fill inside that cap,
  the position is flagged UNPROTECTED and trading halts. **The limit is never
  relaxed silently.** Raising it is an explicit config change.
- **Synthetic stops only:** AMMs have no native stops. While the bot is down,
  nothing protects open positions.

## Unresolved settings

See `04_INPUT_CHECKLIST.md`. `config/live.TEMPLATE.toml` will not load until those are decided.
