# Daily adversarial report: 2026-10-07

**Mode:** SIMULATION ONLY  
**Scenarios run:** 18  
**Invariant violations:** 0  
**Harness crashes:** 0

## Findings: financial impact beyond budget

- **gap_through_stop**: realized gap loss 35.34 exceeded stressed budget 30.72 (stress_gap_multiplier=3 does not cover a 35% gap)

## Scenario results

| Scenario | Pass | Realized P&L | Planned loss | Residual qty | Unprotected | Halts |
|---|---|---|---|---|---|---|
| baseline | ✅ | 30.5749 | 10.7172 | 0.0000 | False | - |
| gap_through_stop | ✅ | -35.3444 | 10.7172 | 0.0000 | False | VANISH |
| submit_timeout_lands | ✅ | 0.0000 | 0.0000 | 996.5060 | False | - |
| crash_mid_submission | ✅ | 0.0000 | 0.0000 | 0.0000 | False | - |
| rug_pull | ✅ | -1.4014 | 10.7172 | 0.0000 | False | VANISH |
| honeypot | ✅ | 0.0000 | 10.7172 | 996.5060 | True | EXIT_FAILED |
| stale_feed | ✅ | -0.5191 | 0.0000 | 0.0000 | False | VANISH |
| rate_limit_exhaustion | ✅ | -12.4592 | 0.0000 | 0.0000 | False | EXIT_QUOTES_UNAVAILABLE |
| fee_spike | ✅ | 0.0000 | 0.0000 | 0.0000 | False | - |
| failed_exit_txs | ✅ | 0.0000 | 0.0000 | 996.5060 | True | EXIT_FAILED |
| late_fill_after_vanish | ✅ | -0.5189 | 0.0000 | 0.0000 | False | VANISH |
| daily_loss_restart | ✅ | -45.5440 | 0.0000 | 0.0000 | False | DAILY_LOSS_LIMIT, VANISH |
| pumpfun_lifecycle | ✅ | -0.5191 | 0.0000 | 0.0000 | False | VANISH |
| random_chaos | ✅ | -6.7673 | 0.0000 | 0.0000 | False | VANISH |
| random_chaos | ✅ | -0.5191 | 0.0000 | 0.0000 | False | VANISH |
| random_chaos | ✅ | 0.0000 | 0.0000 | 0.0000 | False | VANISH |
| random_chaos | ✅ | -0.5191 | 0.0000 | 0.0000 | False | VANISH |
| random_chaos | ✅ | 0.0000 | 0.0000 | 0.0000 | False | VANISH |

## Notes and financial impact

- **gap_through_stop**: planned loss 10.72, stressed 30.72, realized 35.34
- **honeypot**: position remains UNPROTECTED and unsellable: full notional at risk
- **rate_limit_exhaustion**: quote errors: 8, rate-limited submits: 3
- **rug_pull**: residual risk: a rug inside one block cannot be prevented, only exited after

## Latency (simulated, ms)

| Metric | n | p50 | p95 | p99 | Budget | Misses |
|---|---|---|---|---|---|---|
| decision_to_submit | 22 | 0 | 0 | 0 | 250 | 0 |
| detect_to_decision | 89 | 5 | 3020 | 6135 | 250 | 5 |
| exit_decision_to_confirm | 10 | 450 | 450 | 450 | 5000 | 0 |
| submit_ack | 30 | 50 | 50 | 50 | - | 0 |
| submit_to_fill | 23 | 450 | 450 | 3550 | - | 0 |

## Fill markouts (executable-quote benchmark)

| Horizon | Fills | Unknown | Mean bps | Median bps | Worst bps | Adverse | Violations |
|---|---|---|---|---|---|---|---|
| +1s | 25 | 0 | 68.97 | -49.91 | -89.33 | 11 | 0 |
| +5s | 25 | 6 | -36.84 | -69.67 | -3545.29 | 16 | 0 |
| +10s | 25 | 11 | -114.41 | -205.04 | -5134.14 | 10 | 0 |

### By purpose (entry vs exit quality)

| Purpose | Horizon | Fills | Unknown | Mean bps | Adverse |
|---|---|---|---|---|---|
| EMERGENCY | +1s | 7 | 0 | 380.47 | 6 |
| EMERGENCY | +5s | 7 | 2 | 1654.98 | 3 |
| EMERGENCY | +10s | 7 | 4 | 4146.03 | 0 |
| ENTRY | +1s | 14 | 0 | -52.73 | 1 |
| ENTRY | +5s | 14 | 2 | -739.53 | 11 |
| ENTRY | +10s | 14 | 5 | -1548.83 | 8 |
| STOP_LOSS | +1s | 3 | 0 | -50.22 | 3 |
| STOP_LOSS | +5s | 3 | 2 | -50.21 | 1 |
| STOP_LOSS | +10s | 3 | 2 | -50.21 | 1 |
| TAKE_PROFIT | +1s | 1 | 0 | -50.21 | 1 |
| TAKE_PROFIT | +5s | 1 | 0 | -50.21 | 1 |
| TAKE_PROFIT | +10s | 1 | 0 | -50.21 | 1 |

Markouts are retrospective diagnostics. They are not realized P&L and never feed trading decisions. Scenarios inject shocks after fills, so later horizons mostly measure subsequent market movement, not execution quality.

## Proposed patches

None generated automatically. Each violation above needs a human-reviewed patch plus a regression test.

## Remaining uncertainty

- Paper DEX is a constant-product model; real routing, MEV and bonding curves differ.
- Latency figures are simulated delays, not production measurements.
- Birdeye field mapping is unverified against the live API.
- No live venue adapter exists; on-chain reconciliation (multi-RPC quorum) is untested.
