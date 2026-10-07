"""Live-trading preflight: the owner's authorization condition, as code.

Owner authorization (2026-10-07): live trading is authorized "after all tests
have passed with flying colors, no flaws or leaks detected". Each part of that
is a deterministic check here. Live mode refuses to start without a PASSING
preflight record for the exact config hash, less than 24 h old.

Usage:  python -m tradebot.preflight --config config/live.toml
Exit 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .chains import BASE, SOLANA

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MAX_RECORD_AGE_S = 24 * 3600
MIN_SOAK_TICKS = 2000  # live-paper ticks on real data before live (owner may raise, never lower silently)
SECRET_ENV = ("JUPITER_API_KEY", "CODEX_API_KEY", "BITQUERY_API_KEY", "ZEROX_API_KEY", "SOLANA_RPC_URL",
              "BASE_RPC_URL", "BIRDEYE_API_KEY")
SECRET_PATTERNS = [re.compile(p) for p in (r"jup_[0-9a-f]{40,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                                            r"(?i)(mnemonic|seed phrase|private[_ ]key)\s*[:=]\s*\S{20,}")]
PUBLIC_RPCS = ("api.mainnet-beta.solana.com", "mainnet.base.org")

# Capabilities that live trading needs and that do not exist yet. Flipped only by
# the code change that implements them, with its own tests.
LIVE_SIGNER_IMPLEMENTED = False
SELL_SIMULATION_IMPLEMENTED = False


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Ctx:
    config_path: Optional[str]
    cfg: object = None
    env: dict = None
    run_tests: bool = True


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / f for f in out.splitlines()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


# ---------------------------------------------------------------- checks
def check_tests(ctx: Ctx) -> Check:
    if not ctx.run_tests:
        return Check("tests_pass", False, "not run")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT,
                       capture_output=True, text=True)
    tail = (r.stdout.strip().splitlines() or ["no output"])[-1]
    return Check("tests_pass", r.returncode == 0, tail)


def check_adversarial(ctx: Ctx) -> Check:
    from .adversarial.daily import run as run_daily
    rep = run_daily(dt.date.today().isoformat(), repeats=5)
    bad = len(rep["invariant_violations"]) + len(rep["harness_crashes"])
    return Check("adversarial_clean", bad == 0,
                 f"{rep['scenarios_run']} scenarios, {len(rep['invariant_violations'])} violations, "
                 f"{len(rep['harness_crashes'])} crashes, {len(rep['findings'])} financial findings")


def check_no_leaks(ctx: Ctx) -> Check:
    """No secret VALUE from the environment, and no key-shaped string, appears in
    any tracked file or in any report/journal."""
    env = ctx.env if ctx.env is not None else os.environ
    values = [env[k] for k in SECRET_ENV if env.get(k) and len(env[k]) >= 12]
    files = _tracked_files() + [p for p in REPORTS.glob("*") if p.is_file()]
    hits = []
    for p in files:
        try:
            data = p.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if any(v in data for v in values) or any(rx.search(data) for rx in SECRET_PATTERNS):
            hits.append(os.path.relpath(p, ROOT))
    return Check("no_secret_leaks", not hits, f"scanned {len(files)} files; leaks in: {hits[:5]}" if hits
                 else f"scanned {len(files)} files, none found")


def check_live_config(ctx: Ctx) -> Check:
    from .config import ConfigError, load
    if not ctx.config_path:
        return Check("live_config", False, "no --config given")
    try:
        cfg = load(ctx.config_path)
    except (ConfigError, OSError) as e:
        return Check("live_config", False, f"does not load: {str(e).splitlines()[0]}")
    ctx.cfg = cfg
    problems = []
    if cfg.mode != "live" or not cfg.live_trading_authorized:
        problems.append("mode must be live with live_trading_authorized=true")
    if cfg.config_hash == load(str(ROOT / "config" / "paper.example.toml")).config_hash:
        problems.append("is the paper placeholder config")
    if not (cfg.require_sell_simulation and cfg.require_creator_history):
        problems.append("sell simulation and creator history must be required in live")
    return Check("live_config", not problems, "; ".join(problems) or f"hash {cfg.config_hash}")


def check_ack(ctx: Ctx) -> Check:
    env = ctx.env if ctx.env is not None else os.environ
    if ctx.cfg is None:
        return Check("operator_ack", False, "needs a valid live config")
    ok = env.get("TRADEBOT_LIVE_ACK") == ctx.cfg.config_hash
    return Check("operator_ack", ok, "TRADEBOT_LIVE_ACK matches" if ok else
                 f"set TRADEBOT_LIVE_ACK={ctx.cfg.config_hash} after reviewing this exact config")


def check_credentials(ctx: Ctx) -> Check:
    env = ctx.env if ctx.env is not None else os.environ
    chains = ctx.cfg.enabled_chains if ctx.cfg is not None else (SOLANA, BASE)
    need = ["CODEX_API_KEY"]
    if SOLANA in chains:
        need += ["JUPITER_API_KEY", "BITQUERY_API_KEY", "SOLANA_RPC_URL"]
    if BASE in chains:
        need += ["ZEROX_API_KEY"]
    missing = [k for k in need if not env.get(k)]
    public = [k for k in ("SOLANA_RPC_URL", "BASE_RPC_URL") if any(h in env.get(k, "") for h in PUBLIC_RPCS)]
    ok = not missing and not public
    return Check("credentials", ok, f"missing: {missing}; public RPC not allowed: {public}" if not ok else "present")


def check_wallet(ctx: Ctx) -> Check:
    env = ctx.env if ctx.env is not None else os.environ
    path = env.get("TRADEBOT_WALLET_KEYFILE")
    if not path:
        return Check("hot_wallet", False, "TRADEBOT_WALLET_KEYFILE not set (dedicated hot wallet, risk budget only)")
    p = Path(path).resolve()
    if ROOT in p.parents:
        return Check("hot_wallet", False, "key file must live OUTSIDE the repository")
    if not p.exists():
        return Check("hot_wallet", False, "key file does not exist")
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        return Check("hot_wallet", False, "key file permissions must be 600")
    return Check("hot_wallet", True, "outside repo, mode 600")


def _recent_live_paper(days: int = 7) -> list[dict]:
    out, cutoff = [], time.time() - days * 86400
    for p in REPORTS.glob("live-paper-*.json"):
        if p.stat().st_mtime >= cutoff:
            try:
                out.append(json.loads(p.read_text()))
            except ValueError:
                pass
    return out


def check_feeds_verified(ctx: Ctx) -> Check:
    reps = _recent_live_paper()
    good = [r for r in reps if r.get("feeds_on") and not any(
        e.startswith(("bitquery", "codex")) for e in r.get("errors", []))]
    return Check("feeds_verified_live", bool(good),
                 f"{len(good)} recent live-paper runs with working Bitquery/Codex feeds" if good
                 else "no live-paper run with keyed feeds yet")


def check_soak(ctx: Ctx) -> Check:
    reps = _recent_live_paper()
    ticks = sum(int(r.get("ticks", 0)) for r in reps)
    exit_failed = sum(1 for r in reps for h in r.get("halts", []) if h.startswith("EXIT_FAILED"))
    ok = ticks >= MIN_SOAK_TICKS and exit_failed == 0
    return Check("paper_soak", ok, f"{ticks}/{MIN_SOAK_TICKS} live-data ticks in 7 days, {exit_failed} exit failures")


def check_capabilities(ctx: Ctx) -> Check:
    missing = [n for n, ok in (("live signer + /execute + on-chain reconciliation", LIVE_SIGNER_IMPLEMENTED),
                               ("sell simulation", SELL_SIMULATION_IMPLEMENTED)) if not ok]
    return Check("live_capabilities", not missing, f"not implemented: {missing}" if missing else "implemented")


CHECKS: list[Callable[[Ctx], Check]] = [
    check_tests, check_adversarial, check_no_leaks, check_live_config, check_ack, check_credentials,
    check_wallet, check_feeds_verified, check_soak, check_capabilities,
]


def run_preflight(config_path: Optional[str], checks=None, env=None, run_tests: bool = True,
                  write: bool = True) -> tuple[bool, list[Check]]:
    ctx = Ctx(config_path=config_path, env=env, run_tests=run_tests)
    results = []
    for fn in checks or CHECKS:
        try:
            results.append(fn(ctx))
        except Exception as e:  # a crashing check is a failing check
            results.append(Check(fn.__name__.replace("check_", ""), False, f"check crashed: {e!r}"))
    passed = all(c.ok for c in results)
    if write:
        REPORTS.mkdir(exist_ok=True)
        rec = {"ts": time.time(), "config_hash": getattr(ctx.cfg, "config_hash", None), "passed": passed,
               "checks": [c.__dict__ for c in results]}
        (REPORTS / "preflight-latest.json").write_text(json.dumps(rec, indent=2))
    return passed, results


def preflight_record_ok(config_hash: str, path: Path = REPORTS / "preflight-latest.json") -> tuple[bool, str]:
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        return False, "no preflight record: run python -m tradebot.preflight --config <live.toml>"
    if rec.get("config_hash") != config_hash:
        return False, "preflight was run for a different config"
    if time.time() - rec.get("ts", 0) > MAX_RECORD_AGE_S:
        return False, "preflight record older than 24 h"
    if not rec.get("passed"):
        failing = [c["name"] for c in rec.get("checks", []) if not c["ok"]]
        return False, f"preflight failing: {failing}"
    return True, "ok"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--skip-tests", action="store_true", help="for a quick look only; a skipped check fails")
    a = ap.parse_args(argv)
    passed, results = run_preflight(a.config, run_tests=not a.skip_tests)
    w = max(len(c.name) for c in results)
    for c in results:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name:<{w}}  {c.detail}")
    print("\nLIVE TRADING:", "ALLOWED" if passed else "BLOCKED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
