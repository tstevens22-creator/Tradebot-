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
     - (1d) **FOMO as an execution venue: rejected.** It has no official trading API. App automation would be fragile, likely against its terms, would give no executable quotes and no reconciliation, and would expose credentials. The same tokens are traded directly via Jupiter or 0x.
     - (1e) **PumpSniper (iOS app): owner's research tool ONLY**, for following every new and graduated token as fast as possible. It is not a bot input and has no API; the bot never depends on it. The bot replicates its data from on-chain feeds: new mints, **graduation events** (the entry trigger for post-graduation trading) and **creator history** (Bitquery, Solana Tracker, Codex; to be chosen).
     - (1f) **Data feeds. Decided (2026-10-07):** **Bitquery = primary** (pump.fun launches, curve, graduations, migrations, PumpSwap trades, FOMO trades; claimed <300 ms gRPC/Kafka). **Codex = secondary** (Solana + Base, 80+ chains) for Base coverage and cross-checking. If the two feeds disagree on a token, or the primary goes silent, the data is treated as untrustworthy and the bot blocks entries on that token. Solana Tracker is not used (Solana-only; its 0.5% swap fee is too costly). To verify: Bitquery's Base coverage, and real latency and missed-token rates in shadow mode.
     - (1c) **Signals fully automated.** FOMO signal candidates: Bitquery FOMO API (indexes FOMO trades on-chain; preferred) and unofficial feeds (fomoapi.io, getfomoapi.fun), treated as untrusted. Beware copy-bait wallets (loss register F3). No public trending API was found for FOMO or Coinbase, so an automated stand-in feed is needed (e.g. Birdeye trending/new-listing data on Solana and Base; endpoint to be verified). Automated proposals still pass the deterministic risk gate.
2. **Executable-quote source. Proposed (2026-10-07); owner suggested Coinbase Advanced:**
   - **Solana:** Jupiter quote + execution (Coinbase's app also routes Solana DEX trades via Jupiter).
   - **Base:** Coinbase CDP Swap API (0x-powered) or 0x directly. Must verify that it uses **exact-amount** approvals (loss register F5).
   - **Coinbase Advanced Trade API:** a centralized order book for Coinbase-*listed* assets (~300). No evidence was found that it can quote or trade the in-app DEX tokens, so it does **not** cover fresh pump.fun/Base meme coins. Optional third venue for listed coins. Advantage: **exchange-held stop-limit orders** that protect positions while the bot is offline. Caveat: a stop-limit may not fill on a gap. It is an order-book model (real partial fills, real cancels), so it needs its own adapter.
   - **Rule:** the quote must come from the same path that executes. A quote from one router is not executable on another.
   - **Open (2a):** include Coinbase Advanced as a third venue for listed coins?
3. Transaction submission path: public RPC, a private/MEV-protected relay, or a bundle service? How many RPCs for reconciliation quorum?
   **Build-step-2 proposal:** Solana via Jupiter `/order` + `/execute` (Jupiter lands and retries). The bot journals the signature before `/execute` and reconciles via its own RPC (`getSignatureStatuses`). **Needed from owner:** a free `JUPITER_API_KEY` (keyless is 0.5 req/s, too slow to protect positions) and a paid `SOLANA_RPC_URL` (the public RPC rate-limits holder lookups).
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

15b. **Creator-reputation filter (proposed):** reject tokens whose creator wallet has a graduated-to-dead ratio below a threshold, or more than N launches in a window. Unknown history = reject. Thresholds needed.

## C. Operations
16. Hot-wallet funding cap (a separate wallet holding only the risk budget is strongly recommended).
17. Alerting channel for HALT / EXIT_FAILED / RECONCILE_MISMATCH events.
18. Who may run `resume()` and acknowledge breaker recovery.
19. Birdeye plan rate limits, to size the token bucket and the exit reserve.
