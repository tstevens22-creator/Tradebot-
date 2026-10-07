# 1. Repository Audit

**Date:** 2026-10-07
**Repository:** `tstevens22-creator/Tradebot-`

## Finding: there was no existing code to audit

At the start of this work the repository had **no commits, no files and no remote
branches** (`git log` → "does not have any commits yet"; `git ls-remote origin` → empty).

So **no audit of existing trading code was performed**, and none is claimed. No
TODOs, swallowed exceptions, race conditions or other defects were "found", because
there was nothing to inspect.

What this repository contains instead is a **new, paper-only reference core**,
written against the hardening specification. Treat it as the target that any real
strategy/execution code must be integrated into, or measured against.

## Venue and execution model

**Owner input (2026-10-07):** focus on FOMO, pump.fun, and tokens trending on Coinbase. FOMO and Coinbase trending are treated as **signal sources**. Execution is on Solana: the pump.fun bonding curve (pre-graduation) and Jupiter (post-graduation; PumpSwap etc.). Remaining open questions are in `04_INPUT_CHECKLIST.md` item 1.

Note: a **bonding curve is not an AMM pool**. It needs its own quote maths and a graduation/migration handler (loss register C6), and pre-graduation tokens carry the highest rug risk.

### Original assumptions

The "Birdeye" data integration implies **Solana spot DEX trading of meme coins**:

| Aspect | Assumption | Consequence |
|---|---|---|
| Venue | AMM pools / bonding curves reached via a swap router | No resting orders and **no venue-native stop-loss**. All stops are **synthetic** (bot-evaluated). |
| Order type | Atomic swap transaction with `min_out` (slippage bound) | A swap fills fully or reverts. "Partial fills" come from splitting the exit into child swaps, not from the venue. |
| Pending state | Signed tx broadcast, not yet confirmed | Cannot be "cancelled". It can only **expire** (blockhash validity) or be raced by a replacement. Its status stays UNKNOWN until reconciled on-chain. |
| Market data | Birdeye REST (`X-API-KEY`, `x-chain`) | Aggregated/indicative price. **Not an executable quote** — not valid on its own as an exit trigger or markout benchmark. |
| Executable quotes | A router quote for the exact size (e.g. an aggregator `quote` call) | **Not yet integrated.** Paper mode uses a simulated constant-product pool. |

If the real venue is a centralized order book (CEX), `venues/` must gain an
order-book adapter with reduce-only stops, and the partial-fill paths become
venue-driven. The state machine already supports multiple fills per order.

## Lifecycle as implemented

```
Birdeye/indicative data ─┐
Executable quote source ─┼─> MarketSnapshot (timestamped, freshness-checked)
                         │
AI / signal ──> TradeProposal (proposal only, schema-validated)
                         │
                RiskGate.authorize()  ← deterministic, config-hash pinned, fail-closed
                         │
           ExecutionEngine.submit()   ← idempotent client_order_id, journaled BEFORE send
                         │
       Venue ack / timeout(UNKNOWN) ──> reconcile() before any resubmit
                         │
          fills (journaled) ──> Position (confirmed qty only)
                         │
       ProtectionMonitor (SL/TP/trail/max-hold on executable bid)  ← no LLM in path
       CircuitBreaker ("vanish": block → cancel → exit → pause)
                         │
           exit swaps (reduce-only by construction: qty ≤ confirmed position)
                         │
       Journal ──> startup reconciliation ──> MarkoutGrader (+1/+5/+10 s)
```

## Structural risks to watch when real code is integrated

These are the places where integrated code most often goes wrong. They are not findings.

1. Any code path that submits to a venue without going through `RiskGate` and `ExecutionEngine`.
2. Treating an RPC timeout as failure and resubmitting. That double-buys.
3. Using Birdeye `value` (indicative) as the stop trigger instead of an executable sell quote.
4. Using `float` for token amounts. Token decimals vary (e.g. 6 vs 9 on Solana).
5. Letting LLM output reach config, limits or the exit path.
