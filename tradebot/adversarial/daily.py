"""Daily adversarial review (simulation only).

Usage:  python -m tradebot.adversarial.daily [--date YYYY-MM-DD] [--out reports/]
Exit code 1 if any invariant is violated, so CI/cron surfaces it.

This proposes nothing automatically, deploys nothing and changes no risk
settings. Humans review the report.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import traceback
from decimal import Decimal
from pathlib import Path

from ..latency import percentile
from ..markout import row_to_dict, summarize, summarize_by_purpose
from .scenarios import SCENARIOS, Result


def _seed(date: str, name: str, i: int) -> int:
    return int(hashlib.sha256(f"{date}|{name}|{i}".encode()).hexdigest()[:8], 16)


def run(date: str, repeats: int = 3) -> dict:
    results: list[Result] = []
    crashes = []
    for fn in SCENARIOS:
        for i in range(repeats if getattr(fn, "randomized", False) else 1):
            seed = _seed(date, fn.__name__, i)
            try:
                results.append(fn(seed))
            except Exception:
                crashes.append({"scenario": fn.__name__, "seed": seed, "trace": traceback.format_exc()})
    lat: dict[str, list[float]] = {}
    budgets: dict[str, int] = {}
    for r in results:
        budgets.update(r.latency_budgets)
        for k, v in r.latency.items():
            lat.setdefault(k, []).extend(v)
    latency = {k: {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "p99": percentile(v, 99),
                   "budget_ms": budgets.get(k),
                   "budget_misses": sum(1 for x in v if budgets.get(k) is not None and x > budgets[k])}
               for k, v in sorted(lat.items())}
    all_rows = [m for r in results for m in r.markouts]
    report = {
        "date": date,
        "mode": "SIMULATION ONLY",
        "scenarios_run": len(results),
        "harness_crashes": crashes,
        "findings": [{"scenario": r.name, "finding": f} for r in results for f in r.findings],
        "invariant_violations": [
            {"scenario": r.name, "violations": r.violations} for r in results if r.violations],
        "results": [{
            "scenario": r.name, "description": r.description, "passed": r.passed,
            "realized_pnl_usd": str(round(r.realized_pnl_usd, 4)),
            "planned_loss_usd": str(round(r.planned_loss_usd, 4)),
            "residual_qty": str(r.residual_qty), "unprotected": r.unprotected,
            "halts": r.halts, "notes": r.notes,
        } for r in results],
        "latency_ms": latency,
        "markout_summary": summarize(all_rows),
        "markout_by_purpose": summarize_by_purpose(all_rows),
        "markout_rows_sample": [row_to_dict(x) for x in all_rows[:12]],
        "remaining_uncertainty": [
            "Paper DEX is a constant-product model; real routing, MEV and bonding curves differ.",
            "Latency figures are simulated delays, not production measurements.",
            "Birdeye field mapping is unverified against the live API.",
            "No live venue adapter exists; on-chain reconciliation (multi-RPC quorum) is untested.",
        ],
    }
    return report


def to_markdown(rep: dict) -> str:
    L = [f"# Daily adversarial report: {rep['date']}", "", f"**Mode:** {rep['mode']}  ",
         f"**Scenarios run:** {rep['scenarios_run']}  ",
         f"**Invariant violations:** {len(rep['invariant_violations'])}  ",
         f"**Harness crashes:** {len(rep['harness_crashes'])}", ""]
    if rep["findings"]:
        L += ["## Findings: financial impact beyond budget", ""]
        L += [f"- **{f['scenario']}**: {f['finding']}" for f in rep["findings"]] + [""]
    if rep["invariant_violations"]:
        L += ["## New failures (reproducible: scenario + seed in JSON)", ""]
        for v in rep["invariant_violations"]:
            L.append(f"- **{v['scenario']}**: {'; '.join(v['violations'])}")
        L.append("")
    L += ["## Scenario results", "", "| Scenario | Pass | Realized P&L | Planned loss | Residual qty | Unprotected | Halts |",
          "|---|---|---|---|---|---|---|"]
    for r in rep["results"]:
        L.append(f"| {r['scenario']} | {'✅' if r['passed'] else '❌'} | {r['realized_pnl_usd']} | "
                 f"{r['planned_loss_usd']} | {Decimal(r['residual_qty']):.4f} | {r['unprotected']} | "
                 f"{', '.join(h.split(':')[0] for h in r['halts']) or '-'} |")
    notes = [(r["scenario"], n) for r in rep["results"] for n in r["notes"]]
    if notes:
        L += ["", "## Notes and financial impact", ""] + sorted({f"- **{s}**: {n}" for s, n in notes})
    L += ["", "## Latency (simulated, ms)", "", "| Metric | n | p50 | p95 | p99 | Budget | Misses |", "|---|---|---|---|---|---|---|"]
    for k, m in rep["latency_ms"].items():
        L.append(f"| {k} | {m['n']} | {m['p50']} | {m['p95']} | {m['p99']} | {m['budget_ms'] or '-'} | {m['budget_misses']} |")
    L += ["", "## Fill markouts (executable-quote benchmark)", "", "| Horizon | Fills | Unknown | Mean bps | Median bps | Worst bps | Adverse | Violations |",
          "|---|---|---|---|---|---|---|---|"]
    for h, m in rep["markout_summary"].items():
        L.append(f"| {h} | {m['fills']} | {m['unknown']} | {m['mean_bps']} | {m['median_bps']} | "
                 f"{m['worst_bps']} | {m['adverse']} | {m['violations']} |")
    L += ["", "### By purpose (entry vs exit quality)", "", "| Purpose | Horizon | Fills | Unknown | Mean bps | Adverse |", "|---|---|---|---|---|---|"]
    for purpose, hs in rep["markout_by_purpose"].items():
        for h, m in hs.items():
            L.append(f"| {purpose} | {h} | {m['fills']} | {m['unknown']} | {m['mean_bps']} | {m['adverse']} |")
    L += ["", "Markouts are retrospective diagnostics. They are not realized P&L and never feed trading decisions. "
          "Scenarios inject shocks after fills, so later horizons mostly measure subsequent market movement, not execution quality."]
    L += ["", "## Proposed patches", "",
          "None generated automatically. Each violation above needs a human-reviewed patch plus a regression test.",
          "", "## Remaining uncertainty", ""] + [f"- {u}" for u in rep["remaining_uncertainty"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--out", default="reports")
    ap.add_argument("--repeats", type=int, default=5, help="seeds per randomized scenario")
    a = ap.parse_args(argv)
    rep = run(a.date, a.repeats)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"adversarial-{a.date}.json").write_text(json.dumps(rep, indent=2, default=str))
    (out / f"adversarial-{a.date}.md").write_text(to_markdown(rep))
    print(to_markdown(rep))
    return 1 if rep["invariant_violations"] or rep["harness_crashes"] else 0


if __name__ == "__main__":
    sys.exit(main())
