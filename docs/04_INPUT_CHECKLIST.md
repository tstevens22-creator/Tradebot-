# 4. Missing Inputs (one batch)

Live execution stays blocked until **every** item below is answered by the
account owner. The values in `config/paper.example.toml` are simulator
placeholders, **not recommendations**.

## A. Venue and execution
1. ~~Which venue(s)?~~ **Answered (2026-10-07):** focus on FOMO, pump.fun, and tokens trending on Coinbase.
   Interpretation (to confirm):
   - **Execution:** Solana on-chain. (a) pump.fun **bonding curve** for pre-graduation tokens, and (b) the **Jupiter** aggregator for graduated tokens (PumpSwap and other AMMs). Coinbase's in-app Solana DEX trading also routes via Jupiter.
   - **Signal sources, not venues:** FOMO (a social/copy-trading app; no public trading API found), Coinbase trending/"Launches" (Solana + Base), Birdeye.
   - **Decided (2026-10-07):**
     - (1a) **Solana and Base.** Base needs a separate EVM execution adapter, and its risks are in loss register section F.
     - (1b) **pump.fun only after graduation.** Bonding-curve tokens are rejected. A migration in progress triggers vanish.
     - (1c) **Signals fully automated.** No public trending API was found for FOMO or Coinbase, so an automated stand-in feed is needed (e.g. Birdeye trending/new-listing data on Solana and Base; endpoint to be verified). Automated proposals still pass the deterministic risk gate.
2. **Executable-quote source. Proposed (2026-10-07); owner suggested Coinbase Advanced:**
   - **Solana:** Jupiter quote + execution (Coinbase's app also routes Solana DEX trades via Jupiter).
   - **Base:** Coinbase CDP Swap API (0x-powered) or 0x directly. Must verify that it uses **exact-amount** approvals (loss register F5).
   - **Coinbase Advanced Trade API:** a centralized order book for Coinbase-*listed* assets (~300). No evidence was found that it can quote or trade the in-app DEX tokens, so it does **not** cover fresh pump.fun/Base meme coins. Optional third venue for listed coins. Advantage: **exchange-held stop-limit orders** that protect positions while the bot is offline. Caveat: a stop-limit may not fill on a gap. It is an order-book model (real partial fills, real cancels), so it needs its own adapter.
   - **Rule:** the quote must come from the same path that executes. A quote from one router is not executable on another.
   - **Open (2a):** include Coinbase Advanced as a third venue for listed coins?
3. Transaction submission path: public RPC, a private/MEV-protected relay, or a bundle service? How many RPCs for reconciliation quorum?
4. Is there an existing strategy/signal codebase to integrate? Please attach it. Nothing has been audited yet because the repo was empty.

## B. Risk limits (all required)
5. ~~Stop-loss %, take-profit %~~ **Decided:** stop 10% (hard max), no profit-taking below +30% (hard floor). **Also decided:** emergency/vanish, stop and max-hold exits MAY sell a position that is up less than 30%. Banking profit before a collapse outranks the profit floor.
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
