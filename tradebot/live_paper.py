"""Paper trading on LIVE data: real Jupiter quotes and real on-chain token
safety, simulated fills. Nothing is ever signed or sent.

Usage:
  python -m tradebot.live_paper --mint <SOLANA_MINT> --usd 20 --seconds 90 \
      [--operator-asserts-graduated] [--operator-asserts-sellable]

Feeds (step 3): with BITQUERY_API_KEY and CODEX_API_KEY set, the lifecycle
stage, real liquidity, holder concentration and creator history come from
the Bitquery/Codex consensus. Without them, the operator must ASSERT the stage
(--operator-asserts-graduated) and may skip the creator check
(--skip-creator-check). Both are journaled and printed in the report.
Sellability always needs --operator-asserts-sellable until signed-tx sell
simulation exists (live step). Without these flags the gate rejects the
token, which is correct behavior.

Jupiter plan limits: keyless 0.5 rps, Free key 1 rps, Developer 10 rps
(set JUPITER_RPS to your plan). Each tick needs ~3 quotes (bid, ask, and the
protection quote while holding), so ticks are spaced to fit the plan. If that
spacing exceeds the 3 s freshness limit, the limit is relaxed for this run
and reported.
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
from .chains import BASE, SOLANA, STAGE_AMM, normalize_address
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
                   priority_fee_lamports: int, now_ms: int, quote_rtts: list[int]):
    """-> (snapshot, raw ask quote). The raw quote carries chain-specific extras (0x taxes, gas)."""
    ask, oq_a = client.quote_buy(mint, ref_usd, decimals)
    bid, oq_b = client.quote_sell(mint, ask.qty, decimals)
    quote_rtts += [oq_a.latency_ms, oq_b.latency_ms]
    gas = getattr(oq_a, "gas_price_wei", None)  # Base (0x); None on Solana
    return MarketSnapshot(token=mint, ts_source_ms=now_ms, ts_received_ms=now_ms,
                          liquidity_usd=impact_implied_liquidity(ref_usd, ask.price_impact_bps),
                          indicative_price=None, bid=bid, ask=ask,
                          priority_fee_lamports=None if gas is not None else priority_fee_lamports,
                          liquidity_is_estimate=True, gas_price_wei=gas), oq_a


def token_tax_bps(raw) -> Optional[Decimal]:
    """Base: 0x-measured buy/sell tax of the token (worse of the two). None = unknown -> reject."""
    b, s = getattr(raw, "buy_tax_bps", None), getattr(raw, "sell_tax_bps", None)
    return None if b is None or s is None else max(b, s)


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
        sleep=time.sleep, skip_creator_check: bool = False, bitquery=None, codex=None,
        feed_every_s: float = 30.0, chain: str = SOLANA) -> dict:
    from .data.evm_rpc import EvmRpc
    from .data.solana_rpc import SolanaRpc
    from .venues.zeroex import ZeroExClient, ZeroExPaperVenue

    mint = normalize_address(chain, mint)
    clock = clock or Clock()
    if chain == BASE:
        client = client or ZeroExClient(now_ms=clock.now_ms)
        if not client.api_key:
            raise SystemExit("Base needs ZEROX_API_KEY (0x requires a key; get one at dashboard.0x.org)")
        rpc = rpc or EvmRpc()
    else:
        client = client or JupiterClient(now_ms=clock.now_ms)
        rpc = rpc or SolanaRpc()
    base = load(str(ROOT / "config" / "paper.example.toml"))
    cfg = dataclasses.replace(base, enabled_chains=(chain,), max_data_age_ms=max_data_age_ms,
                              unknown_resolution_timeout_s=max(base.unknown_resolution_timeout_s, 180),
                              require_creator_history=base.require_creator_history and not skip_creator_check)
    stamp = dt.datetime.fromtimestamp(clock.now_ms() / 1000, tz=dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    journal = Journal(str(out_dir / f"live-paper-{stamp}.db"), cfg.config_hash)

    warnings: list[str] = []
    if max_data_age_ms > 3000:
        warnings.append(f"Quote API plan allows {client.rps} req/s: ticks every {tick_s:.1f}s, data-age limit "
                        f"relaxed to {max_data_age_ms} ms (production target 3000 ms needs ~10 req/s).")
    from .data.bitquery import BitqueryClient
    from .data.codex import CodexClient
    from .data.feeds import apply_view, consensus
    bitquery = bitquery or BitqueryClient()
    codex = codex or CodexClient()
    # Solana needs both feeds (Bitquery primary); Base uses Codex only (Bitquery Base coverage unverified).
    feeds_on = bool(codex.gql.auth_header and (chain == BASE or bitquery.gql.auth_header))
    if not feeds_on:
        need = "CODEX_API_KEY" if chain == BASE else "BITQUERY_API_KEY/CODEX_API_KEY"
        warnings.append(f"No {need}: stage must be operator-asserted and liquidity is an "
                        "ESTIMATE implied from quote price impact.")
    if skip_creator_check:
        warnings.append("Creator-reputation check SKIPPED by operator (--skip-creator-check).")

    safety = rpc.token_safety(mint, clock.now_ms(),
                              venue_stage=STAGE_AMM if (assert_graduated and not feeds_on) else None)
    assertions = []
    if assert_graduated and not feeds_on:
        assertions.append("stage=amm (graduated) asserted by operator; no feed keys configured")
    elif assert_graduated:
        warnings.append("--operator-asserts-graduated ignored: feeds are configured and decide the stage.")
    if assert_sellable:
        safety = dataclasses.replace(safety, sell_simulation_ok=True)
        assertions.append("sellable asserted by operator; on-chain sell simulation not built yet")
    if skip_creator_check:
        assertions.append("creator-reputation check skipped by operator")
    for a in assertions:
        journal.append(clock.now_ms(), "OPERATOR_ASSERTION", {"mint": mint, "assertion": a})
    onchain_safety = safety
    view = None
    last_feed_ms = -10**15
    feed_log: list[dict] = []

    venue = (ZeroExPaperVenue(clock, client, lambda _t: safety.decimals) if chain == BASE
             else JupiterPaperVenue(clock, client, lambda _t: safety.decimals))
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
        if feeds_on and clock.now_ms() - last_feed_ms >= feed_every_s * 1000:
            last_feed_ms = clock.now_ms()
            p = s = None
            if chain == SOLANA:
                try:
                    p = bitquery.reading(mint, clock.now_ms())
                except VenueError as e:
                    errors.append(f"bitquery: {e}")
            try:
                s = codex.readings(chain, [mint], clock.now_ms()).get(mint)
            except VenueError as e:
                errors.append(f"codex: {e}")
            view = consensus(mint, chain, clock.now_ms(), cfg.feed_max_age_ms, p, s)
            safety = apply_view(onchain_safety if not assert_sellable else
                                dataclasses.replace(onchain_safety, sell_simulation_ok=True), view)
            feed_log.append({"ts": clock.now_ms(), "stage": view.stage, "liquidity_usd": str(view.liquidity_usd),
                             "creator": [view.creator_tokens_created, view.creator_tokens_migrated],
                             "notes": list(view.notes)})
            journal.append(clock.now_ms(), "FEED_VIEW", feed_log[-1])
        elif feeds_on and view is not None and clock.now_ms() - view.ts_ms > cfg.feed_max_age_ms:
            safety = dataclasses.replace(safety, venue_stage=None)  # feed went stale -> unknown
        try:
            snap, ask_raw = build_snapshot(client, mint, safety.decimals, usd,
                                           getattr(venue, "priority_fee_lamports", 0), clock.now_ms(), quote_rtts)
            if chain == BASE:
                safety = dataclasses.replace(safety, transfer_tax_bps=token_tax_bps(ask_raw))
            if feeds_on and view is not None:
                snap = dataclasses.replace(snap, liquidity_usd=view.liquidity_usd, liquidity_is_estimate=False)
            bench.series.append(snap)
            bot.ingest(snap, dataclasses.replace(safety, ts_ms=clock.now_ms()))
        except VenueError as e:
            errors.append(f"snapshot: {e}")  # no ingest -> the data ages -> the breaker reacts
        bot.tick()
        ticks += 1
        if decision is None and mint in bot.snapshots:
            sleep(1.0 / (client.rps * 0.9))  # let the rate budget refill so the exact-size entry quote fits
            decision = bot.propose({"proposal_id": f"live-paper-{stamp}", "chain": chain, "token": mint,
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
        "halts": sorted(bot.halts), "errors": errors[:20], "jupiter_429s": client.rate_limited_count, "feeds_on": feeds_on, "feed_views": feed_log[-5:],
        "markouts": summarize(rows), "markout_rows": [row_to_dict(r) for r in rows],
    }
    (out_dir / f"live-paper-{stamp}.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mint", required=True, help="Solana mint or Base 0x token address")
    ap.add_argument("--chain", choices=[SOLANA, BASE], default=SOLANA)
    ap.add_argument("--usd", default="20")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--tick-s", type=float, default=None)
    ap.add_argument("--max-data-age-ms", type=int, default=None)
    ap.add_argument("--operator-asserts-graduated", action="store_true")
    ap.add_argument("--operator-asserts-sellable", action="store_true")
    ap.add_argument("--skip-creator-check", action="store_true")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args(argv)
    from .venues.zeroex import ZeroExClient
    rps = (ZeroExClient() if a.chain == BASE else JupiterClient()).rps
    quotes_per_tick = 3
    tick = a.tick_s or max(1.0, quotes_per_tick / (rps * 0.9))
    age = a.max_data_age_ms or max(3000, int(tick * 1000 * 2.5))
    rep = run(a.mint, Decimal(a.usd), a.seconds, tick, age, a.operator_asserts_graduated,
              a.operator_asserts_sellable, Path(a.out), skip_creator_check=a.skip_creator_check, chain=a.chain)
    print(json.dumps({k: v for k, v in rep.items() if k != "markout_rows"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
