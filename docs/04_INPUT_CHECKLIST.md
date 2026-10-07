# 4. Missing Inputs (one batch)

Live execution stays blocked until **every** item below is answered by the
account owner. The values in `config/paper.example.toml` are simulator
placeholders, **not recommendations**.

## A. Venue and execution
1. Which venue(s)? A Solana DEX via an aggregator, specific AMMs, pump.fun-style bonding curves, a CEX, or a mix?
2. Which executable-quote source? Birdeye price is indicative only.
3. Transaction submission path: public RPC, a private/MEV-protected relay, or a bundle service? How many RPCs for reconciliation quorum?
4. Is there an existing strategy/signal codebase to integrate? Please attach it. Nothing has been audited yet because the repo was empty.

## B. Risk limits (all required)
5. ~~Stop-loss %, take-profit %~~ **Decided:** stop 10% (hard max), no profit-taking below +30% (hard floor). Still open: should emergency/vanish and max-hold exits be allowed to sell a position that is up less than 30%? They currently are, because they are protective.
6. Trailing stop % (or none), scale-out ladder (or none), max holding time (or none).
7. Per-trade risk $, max position $, max total exposure $, concentration %, max open positions.
8. Stress gap multiplier and max stressed loss $. Note: the daily report shows a 35% gap exceeding a 3× multiplier on the 10% stop.
9. Daily loss limit $ and max drawdown %. Should the daily halt auto-reset at UTC midnight? It is currently manual-resume.
10. Liquidity floor, max round-trip spread, entry slippage, max price impact, max data age.
11. Token-safety thresholds: accept mint/freeze authority ever? Max transfer tax? Max top-10 holder share? Note: confirm whether Birdeye's `top10HolderPercent` is a fraction or a percent.
12. Priority-fee caps (normal and emergency).
13. Exit ladder, emergency slippage cap, max attempts, backoff, and post-exhaustion probe interval (or operator-only).
14. Vanish thresholds, emergency policy (`exit_all` / `exit_if_loss` / `hold_protected`), and recovery requirements.
15. Latency budgets, and markout adverse threshold (frozen before evaluation).

## C. Operations
16. Hot-wallet funding cap (a separate wallet holding only the risk budget is strongly recommended).
17. Alerting channel for HALT / EXIT_FAILED / RECONCILE_MISMATCH events.
18. Who may run `resume()` and acknowledge breaker recovery.
19. Birdeye plan rate limits, to size the token bucket and the exit reserve.
