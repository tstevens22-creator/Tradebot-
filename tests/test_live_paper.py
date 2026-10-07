"""End-to-end live-paper runner with the network faked (real fixture shapes)."""
import json
from decimal import Decimal
from pathlib import Path

from tradebot.clock import SimClock
from tradebot.data.solana_rpc import SolanaRpc
from tradebot.live_paper import impact_implied_liquidity, run
from tradebot.ratelimit import TokenBucket
from tradebot.venues.jupiter import JupiterClient

from test_jupiter import BONK, FakeJup

FIX = Path(__file__).parent / "fixtures"


def fake_rpc(top10_ok=True):
    def opener(req, t):
        m = json.loads(req.data)["method"]
        if m == "getAccountInfo":
            return (FIX / "rpc_mint_bonk.json").read_bytes()
        if m == "getTokenLargestAccounts" and top10_ok:
            supply = 8799436892347610091
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": [
                {"address": "a", "amount": str(supply // 50), "decimals": 5}]}}).encode()
        return json.dumps({"jsonrpc": "2.0", "error": {"code": 429, "message": "busy"}, "id": 1}).encode()
    return SolanaRpc(url="https://x", bucket=TokenBucket(1000, 1000), opener=opener)


def go(tmp_path, fake, **kw):
    clock = SimClock()
    client = JupiterClient(api_key="k", bucket=TokenBucket(1000, 1000), opener=fake, now_ms=clock.now_ms)
    args = dict(mint=BONK, usd=Decimal(20), seconds=30, tick_s=1.0, max_data_age_ms=3000,
                assert_graduated=True, assert_sellable=True, out_dir=tmp_path,
                client=client, rpc=fake_rpc(), clock=clock, sleep=lambda s: clock.advance(int(s * 1000)))
    args.update(kw)
    return run(**args)


def test_impact_implied_liquidity():
    assert impact_implied_liquidity(Decimal(100), Decimal(10)) == Decimal(200_000)
    assert impact_implied_liquidity(Decimal(100), Decimal(0)) == Decimal(2_000_000)  # 1 bp floor


def test_live_paper_round_trip_entry_and_report(tmp_path):
    rep = go(tmp_path, FakeJup())
    assert rep["decision"]["approved"], rep["decision"]
    assert rep["fills"] and rep["fills"][0]["side"] == "BUY"
    assert Decimal(rep["position_qty"]) > 0
    assert rep["operator_assertions"] and rep["onchain_safety"]["hazards"] == []
    assert rep["markouts"]["+1s"]["fills"] == 1
    assert list(tmp_path.glob("live-paper-*.json")) and list(tmp_path.glob("live-paper-*.db"))


def test_live_paper_without_assertions_is_rejected(tmp_path):
    rep = go(tmp_path, FakeJup(), assert_graduated=False, assert_sellable=False)
    reasons = " ".join(rep["decision"]["reasons"])
    assert "STAGE_UNKNOWN" in reasons and "SELLABILITY_UNVERIFIED" in reasons
    assert rep["fills"] == []


def test_live_paper_stop_loss_on_live_drop(tmp_path):
    fake = FakeJup()
    clock = SimClock()
    client = JupiterClient(api_key="k", bucket=TokenBucket(1000, 1000), opener=fake, now_ms=clock.now_ms)
    ticks = {"n": 0}

    def sleep(s):
        ticks["n"] += 1
        if ticks["n"] == 8:
            fake.mult = Decimal("0.85")  # live price drops 15%
        clock.advance(int(s * 1000))

    rep = run(mint=BONK, usd=Decimal(20), seconds=25, tick_s=1.0, max_data_age_ms=3000, assert_graduated=True,
              assert_sellable=True, out_dir=tmp_path, client=client, rpc=fake_rpc(), clock=clock, sleep=sleep)
    sides = [f["side"] for f in rep["fills"]]
    assert sides == ["BUY", "SELL"] and Decimal(rep["position_qty"]) == 0
    assert Decimal(rep["realized_pnl_usd"]) < 0


def test_live_paper_feed_outage_halts(tmp_path):
    fake = FakeJup()
    clock = SimClock()
    client = JupiterClient(api_key="k", bucket=TokenBucket(1000, 1000), opener=fake, now_ms=clock.now_ms)
    ticks = {"n": 0}

    def sleep(s):
        ticks["n"] += 1
        if ticks["n"] == 6:
            fake.fail = "timeout"  # Jupiter unreachable
        clock.advance(int(s * 1000))

    rep = run(mint=BONK, usd=Decimal(20), seconds=20, tick_s=1.0, max_data_age_ms=3000, assert_graduated=True,
              assert_sellable=True, out_dir=tmp_path, client=client, rpc=fake_rpc(), clock=clock, sleep=sleep)
    assert any(h.startswith("VANISH") for h in rep["halts"])
    assert rep["errors"]
