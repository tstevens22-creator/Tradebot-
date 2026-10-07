"""Paper trading on LIVE data: real Jupiter quotes and real on-chain token
safety, simulated fills. Nothing is ever signed or sent.

Usage:
  python -m tradebot.live_paper --mint <SOLANA_MINT> --usd 20 --seconds 90 \
      [--operator-asserts-graduated] [--operator-asserts-sellable]

Inputs this build can't fetch yet, and must be ASSERTED by the operator to
get past the fail-closed gate (journaled and printed in the report):
  * lifecycle stage (graduated/AMM): needs the Bitquery/Codex feed (step 3)
  * sellability: needs a signed-tx sell simulation (live step)
Without those flags the gate rejects the token, which is correct behavior.

Keyless Jupiter allows only 0.5 req/s, so ticks are slow and the data-age
limit must be relaxed for this demo (reported). Set JUPITER_API_KEY for
real-time freshness.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .bot import Bot
from .chains import STAGE_AMM, normalize_address, SOLANA
from .clock import Clock
from .config import load
from .journal import Journal
from .latency import percentile
from .markout import Benchmark, grade_fill, row_to_dict, summarize
from .models import BPS, MarketSnapshot, Side
from .venues.base import VenueError
from .venues.jupiter import JupiterClient, JupiterPaperVenue

ROOT = Path(__file__).resolve().parents[1]


def impact_implied_liquidity(ref_usd: Decimal, impact_bps: Decimal) -> Decimal:
    """Rough pool-depth estimate from an executable quote's price impact
    (constant-product: impact ~ size / reserve). A 1 bp floor keeps it finite.
    Labelled as an ESTIMATE. Real liquidity comes from the data feed (step 3)."""
    return 2 * ref_usd / (max(impact_bps, Decimal(1)) / BPS)


def build_snapshot(client: JupiterClient, mint: str, decimals: int, ref_usd: Decimal,
                   priority_fee_lamports: int, now_ms: int, quote_rtts: list[int]) -> MarketSnapshot:
    ask, oq_a = client.quote_buy(mint, ref_usd, decimals)
    bid, oq_b = client.quote_sell(mint, ask.qty, decimals)
    quote_rtts += [oq_a.latency_ms, oq_b.latency_ms]
    return MarketSnapshot(token=mint, ts_source_ms=now_ms, ts_received_ms=now_ms,
                          liquidity_usd=impact_implied_liquidity(ref_usd, ask.price_impact_bps),
                          indicative_price=None, bid=bid, ask=ask, priority_fee_lamports=priority_fee_lamports,
                          liquidity_is_estimate=True)


class SnapshotBenchmark:
    """Markout benchmark from the recorded reference-size bid/ask quotes.
    Reference size == trade size in this runner, so no size mismatch."""

    def __init__(self) -> None:
        self.series: list[MarketSnapshot] = []

    def __call__(self, token: str, side: Side, qty: Decimal, target_ms: int, tol_ms: int) -> Optional[Benchmark]:
        for s in self.series:
            if s.token == token and target_ms <= s.ts_source_ms <= target_ms + tol_ms and s.bid and s.ask:
                price = s.bid.price if side is Side.BUY else s.ask.price
                return Benchmark(price, s.ts_source_ms, s.liquidity_usd, "jupiter_quote_ref_size")
        return None


def run(mint: str, usd: Decimal, seconds: int, tick_s: float, max_data_age_ms: int,
        assert_graduated: bool, assert_sellable: bool, out_dir: Path,
        client: Optional[JupiterClient] = None, rpc=None, clock: Optional[Clock] = None,
        sleep=time.sleep) -> dict:
    from .data.solana_rpc import SolanaRpc

    mint = normalize_address(SOLANA, mint)
    clock = clock or Clock()
    client = client or JupiterClient(now_ms=clock.now_ms)
    rpc = rpc or SolanaRpc()
    base = load(str(ROOT / "config" / "paper.example.toml"))
    cfg = dataclasses.replace(base, enabled_chains=(SOLANA,), max_data_age_ms=max_data_age_ms,
                              unknown_resolution_timeout_s=max(base.unknown_resolution_timeout_s, 180))
    stamp = dt.datetime.fromtimestamp(clock.now_ms() / 1000, tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(str(out_dir / f"live-paper-{stamp}.db"), cfg.config_hash)

    warnings: list[str] = []
    if not client.api_key:
        warnings.append(f"Keyless Jupiter (0.5 req/s): ticks every {tick_s}s, data-age limit relaxed to "
                        f"{max_data_age_ms} ms (production: 3000 ms with an API key).")
    warnings.append("Liquidity is an ESTIMATE implied from quote price impact (real feed comes in step 3).")

    safety = rpc.token_safety(mint, clock.now_ms(), venue_stage=STAGE_AMM if assert_graduated else None)
    assertions = []
    if assert_graduated:
        assertions.append("stage=amm (graduated) asserted by operator; launchpad feed not built yet")
    if assert_sellable:
        safety = dataclasses.replace(safety, sell_simulation_ok=True)
        assertions.append("sellable asserted by operator; on-chain sell simulation not built yet")
    for a in assertions:
        journal.append(clock.now_ms(), "OPERATOR_ASSERTION", {"mint": mint, "assertion": a})

    venue = JupiterPaperVenue(clock, client, lambda _t: safety.decimals)
    bot = Bot(cfg, venue, journal, clock)
    bench = SnapshotBenchmark()
    quote_rtts: list[int] = []
    errors: list[str] = []
    decision = None
    end = clock.now_ms() + seconds * 1000
    ticks = 0
    while clock.now_ms() < end:
        t0 = clock.now_ms()
        venue.process()
        try:
            snap = build_snapshot(client, mint, safety.decimals, usd, venue.priority_fee_lamports,
                                  clock.now_ms(), quote_rtts)
            bench.series.append(snap)
            bot.ingest(snap, dataclasses.replace(safety, ts_ms=clock.now_ms()))
        except VenueError as e:
            errors.append(f"snapshot: {e}")  # no ingest -> the data ages -> the breaker reacts
        bot.tick()
        ticks += 1
        if decision is None and mint in bot.snapshots:
            decision = bot.propose({"proposal_id": f"live-paper-{stamp}", "chain": SOLANA, "token": mint,
                                    "usd_size": str(usd), "expected_edge_bps": "500"})
        sleep(max(0.0, tick_s - (clock.now_ms() - t0) / 1000))

    venue.process()
    bot.tick()
    fills = journal.load_fills()
    approved = {p["decision_id"] for *_, p in journal.events("DECISION") if p.get("approved")}
    rows = []
    for f in fills.values():
        o = bot.engine.orders.get(f.client_order_id)
        if o:
            rows += grade_fill(cfg, f, o, bench, approved)
    pos = bot.portfolio.positions.get(mint)
    last = bench.series[-1] if bench.series else None
    report = {
        "mint": mint, "usd": str(usd), "seconds": seconds, "ticks": ticks, "config_hash": cfg.config_hash,
        "mode": "PAPER on live Jupiter quotes; nothing signed or sent",
        "warnings": warnings, "operator_assertions": assertions,
        "onchain_safety": {"decimals": safety.decimals, "mint_authority": safety.mint_authority,
                           "freeze_authority": safety.freeze_authority,
                           "transfer_tax_bps": str(safety.transfer_tax_bps),
                           "top10_holder_pct": str(safety.top10_holder_pct), "hazards": list(safety.hazards)},
        "decision": None if decision is None else {"approved": decision.approved, "reasons": list(decision.reasons),
                                                   "planned_loss_usd": str(round(decision.planned_loss_usd, 4))},
        "fills": [{"side": f.side.name, "qty": str(f.qty), "price": str(f.price), "fee_usd": str(f.fee_usd)}
                  for f in fills.values()],
        "position_qty": str(pos.qty) if pos else "0",
        "realized_pnl_usd": str(round(bot.portfolio.total_realized_usd, 6)),
        "unrealized_at_last_bid_usd": (str(round(pos.qty * last.bid.price - pos.cost_usd, 6))
                                       if pos and pos.is_open and last and last.bid else None),
        "last_round_trip_bps": str(round(last.round_trip_bps, 2)) if last and last.round_trip_bps is not None else None,
        "quote_rtt_ms": {"n": len(quote_rtts), "p50": percentile(quote_rtts, 50),
                         "p95": percentile(quote_rtts, 95), "p99": percentile(quote_rtts, 99)},
        "bot_latency": bot.latency.summary(),
        "halts": sorted(bot.halts), "errors": errors[:20],
        "markouts": summarize(rows), "markout_rows": [row_to_dict(r) for r in rows],
    }
    (out_dir / f"live-paper-{stamp}.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mint", required=True)
    ap.add_argument("--usd", default="20")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--tick-s", type=float, default=None)
    ap.add_argument("--max-data-age-ms", type=int, default=None)
    ap.add_argument("--operator-asserts-graduated", action="store_true")
    ap.add_argument("--operator-asserts-sellable", action="store_true")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args(argv)
    keyed = bool(JupiterClient().api_key)
    tick = a.tick_s or (1.0 if keyed else 8.0)
    age = a.max_data_age_ms or (3000 if keyed else int(tick * 1000 * 2.5))
    rep = run(a.mint, Decimal(a.usd), a.seconds, tick, age, a.operator_asserts_graduated,
              a.operator_asserts_sellable, Path(a.out))
    print(json.dumps({k: v for k, v in rep.items() if k != "markout_rows"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
