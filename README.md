# Tradebot: safety-first meme-coin trading core (paper/shadow only)

A hardened execution and risk core for Solana DEX meme-coin trading, with
read-only Birdeye market data. **There is no live trading.** No live venue
adapter exists, and live mode is hard-blocked.

> No promise of profitability, perfect fills or instant exits. On an AMM,
> stops are synthetic: they only work while this process runs with fresh data.

## Read in this order
1. [`docs/01_AUDIT.md`](docs/01_AUDIT.md): what existed (nothing) and the assumed execution model
2. [`docs/02_LOSS_REGISTER.md`](docs/02_LOSS_REGISTER.md): ways to lose money, each with a mitigation and a test
3. [`docs/03_RISK_SPEC.md`](docs/03_RISK_SPEC.md): settings, conflict resolution, invariants
4. [`docs/04_INPUT_CHECKLIST.md`](docs/04_INPUT_CHECKLIST.md): **decisions needed from you**
5. [`docs/05_READINESS.md`](docs/05_READINESS.md): results, findings, verdict
6. [`reports/`](reports/): daily adversarial report (markouts, latency, failures)

## Layout
```
tradebot/
  config.py           explicit, frozen, hashed risk config (no defaults)
  signals.py          strict AI proposal schema (proposals only, no exits, no limits)
  risk.py             deterministic, fail-closed pre-trade gate
  state_machine.py    order states incl. UNKNOWN / CANCEL_PENDING
  execution.py        write-ahead, idempotent submit, reconciliation
  journal.py          persistent SQLite event log (+ separate markout table)
  portfolio.py        net P&L from actual fills
  protection.py       SL / TP / trailing / max-hold / scale-out
  circuit_breaker.py  "vanish" triggers and gated recovery
  bot.py              orchestrator: protection first, entries last; restart recovery
  markout.py          +1/+5/+10 s fill grading
  latency.py          p50/p95/p99 + budget misses
  data/birdeye.py     read-only Birdeye adapter (indicative data)
  venues/paper_dex.py simulated AMM with fault injection
  venues/jupiter.py   Jupiter Swap V2 quotes + paper venue on live prices
  data/solana_rpc.py  read-only on-chain token safety and tx status
  live_paper.py       paper-trade a real token on live data
  adversarial/        daily scenario runner + report
```

## Run
```bash
pip install -r requirements-dev.txt
python -m pytest -q
python -m tradebot.adversarial.daily --out reports
# paper trading on LIVE Jupiter prices (nothing is signed or sent):
python -m tradebot.live_paper --mint <SOLANA_MINT> --usd 20 --seconds 90
```
Optional env: `JUPITER_API_KEY` (free; keyless is 0.5 req/s), `SOLANA_RPC_URL` (paid RPC recommended).

Secrets (e.g. `BIRDEYE_API_KEY`) come from the environment only. The config loader rejects key-like fields.
