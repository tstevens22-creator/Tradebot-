"""End-to-end live-paper runner with the network faked (real fixture shapes)."""
import json
from decimal import Decimal
from pathlib import Path

from tradebot.clock import SimClock
from tradebot.data.bitquery import BitqueryClient
from tradebot.data.codex import CodexClient
from tradebot.data.solana_rpc import SolanaRpc
from tradebot.live_paper import impact_implied_liquidity, run
from tradebot.chains import STAGE_AMM, STAGE_BONDING_CURVE
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
                assert_graduated=True, assert_sellable=True, out_dir=tmp_path, skip_creator_check=True,
                client=client, rpc=fake_rpc(), clock=clock, sleep=lambda s: clock.advance(int(s * 1000)),
                bitquery=BitqueryClient(token=""), codex=CodexClient(api_key=""))
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
              assert_sellable=True, out_dir=tmp_path, client=client, rpc=fake_rpc(), clock=clock, sleep=sleep,
              skip_creator_check=True, bitquery=BitqueryClient(token=""), codex=CodexClient(api_key=""))
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
              assert_sellable=True, out_dir=tmp_path, client=client, rpc=fake_rpc(), clock=clock, sleep=sleep,
              skip_creator_check=True, bitquery=BitqueryClient(token=""), codex=CodexClient(api_key=""))
    assert any(h.startswith("VANISH") for h in rep["halts"])
    assert rep["errors"]


class FakeFeeds:
    def __init__(self, bq_stage=STAGE_AMM, codex_stage=STAGE_AMM, liq="900000", created=3, migrated=2):
        from tradebot.data.feeds import FeedReading
        self.FR = FeedReading
        self.bq_stage, self.codex_stage, self.liq, self.created, self.migrated = bq_stage, codex_stage, liq, created, migrated

        class G:
            auth_header = "x"
        self.gql = G()

    def reading(self, token, now):
        return self.FR("bitquery", token, "solana", now, stage=self.bq_stage, is_pumpfun=True)

    def readings(self, chain, tokens, now):
        return {t: self.FR("codex", t, chain, now, stage=self.codex_stage, liquidity_usd=Decimal(self.liq),
                           top10_holder_pct=Decimal("0.3"), creator_tokens_created=self.created,
                           creator_tokens_migrated=self.migrated) for t in tokens}


def run_with_feeds(tmp_path, feeds):
    clock = SimClock()
    client = JupiterClient(api_key="k", bucket=TokenBucket(1000, 1000), opener=FakeJup(), now_ms=clock.now_ms)
    return run(mint=BONK, usd=Decimal(20), seconds=15, tick_s=1.0, max_data_age_ms=3000, assert_graduated=False,
               assert_sellable=True, out_dir=tmp_path, client=client, rpc=fake_rpc(), clock=clock,
               sleep=lambda s: clock.advance(int(s * 1000)), bitquery=feeds, codex=feeds)


def test_live_paper_with_feeds_uses_real_stage_liquidity_and_creator(tmp_path):
    rep = run_with_feeds(tmp_path, FakeFeeds())
    assert rep["feeds_on"] and rep["decision"]["approved"], rep["decision"]
    assert not any("ESTIMATE" in w for w in rep["warnings"])
    assert rep["operator_assertions"] == ["sellable asserted by operator; on-chain sell simulation not built yet"]


def test_live_paper_feed_disagreement_rejects(tmp_path):
    rep = run_with_feeds(tmp_path, FakeFeeds(codex_stage=STAGE_BONDING_CURVE))
    assert not rep["decision"]["approved"]
    assert any("STAGE_UNKNOWN" in r for r in rep["decision"]["reasons"])
    assert "disagreement" in rep["feed_views"][0]["notes"][0]


def test_live_paper_serial_launcher_rejected(tmp_path):
    rep = run_with_feeds(tmp_path, FakeFeeds(created=80, migrated=1))
    assert any("CREATOR_SERIAL_LAUNCHER" in r for r in rep["decision"]["reasons"])


# ---- Base (0x) ----------------------------------------------------------------
def base_rpc(owner_word="0x" + "0" * 64):
    from tradebot.data.evm_rpc import EvmRpc

    def opener(req, t):
        b = json.loads(req.data)
        if b["method"] == "eth_call" and b["params"][0]["data"] == "0x313ce567":
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 62 + "12"}).encode()
        if b["method"] == "eth_call":
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": owner_word}).encode()
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 64}).encode()
    return EvmRpc(url="https://x", bucket=TokenBucket(1000, 1000), opener=opener)


def run_base(tmp_path, fake=None, rpc=None, feeds=None, seconds=15, sleep_hook=None):
    from tradebot.venues.zeroex import ZeroExClient
    from test_zeroex import DEGEN, FakeZx
    clock = SimClock()
    fake = fake or FakeZx()
    client = ZeroExClient(api_key="k", bucket=TokenBucket(1000, 1000), opener=fake, now_ms=clock.now_ms)
    feeds = feeds or FakeFeeds()

    def sleep(s):
        if sleep_hook:
            sleep_hook()
        clock.advance(int(s * 1000))

    return run(mint=DEGEN, usd=Decimal(20), seconds=seconds, tick_s=1.0, max_data_age_ms=3000,
               assert_graduated=False, assert_sellable=True, out_dir=tmp_path, client=client,
               rpc=rpc or base_rpc(), clock=clock, sleep=sleep, bitquery=BitqueryClient(token=""),
               codex=feeds, chain="base")


def test_base_paper_trade_with_codex_feed(tmp_path):
    rep = run_base(tmp_path)
    assert rep["feeds_on"] and rep["decision"]["approved"], rep["decision"]
    assert rep["fills"] and rep["fills"][0]["side"] == "BUY"
    assert rep["onchain_safety"]["mint_authority"] is False  # renounced owner


def test_base_owned_token_rejected(tmp_path):
    rep = run_base(tmp_path, rpc=base_rpc("0x" + "0" * 24 + "704ec5c12ca20a293c2c0b72b22619a4231f3c0d"))
    reasons = " ".join(rep["decision"]["reasons"])
    assert "MINT_AUTHORITY" in reasons and "FREEZE_AUTHORITY" in reasons and not rep["fills"]


def test_base_sell_tax_from_0x_rejected(tmp_path):
    from test_zeroex import FakeZx
    fake = FakeZx()
    fake.sell_tax = "500"  # 5% sell tax measured by 0x
    rep = run_base(tmp_path, fake=fake)
    assert any("TRANSFER_TAX" in r for r in rep["decision"]["reasons"])


def test_base_requires_0x_key(tmp_path):
    import pytest
    from tradebot.venues.zeroex import ZeroExClient
    with pytest.raises(SystemExit):
        run(mint="0x4ed4e862860bed51a9570b96d89af5e1b0efefed", usd=Decimal(20), seconds=1, tick_s=1.0,
            max_data_age_ms=3000, assert_graduated=False, assert_sellable=False, out_dir=tmp_path,
            client=ZeroExClient(api_key=""), chain="base")


def test_base_gas_cap_blocks_entries():
    from tradebot.chains import BASE
    from tradebot.sim import BASE_TOKEN, Sim
    s = Sim()
    s.add_token(BASE_TOKEN, BASE)
    s.base_gas_wei = 2_000_000_000  # 2 gwei spike > 0.5 cap
    s.run(2)
    assert any("GAS_PRICE" in r for r in s.propose(token=BASE_TOKEN).reasons)
    s.base_gas_wei = 5_000_000
    s.run(1)
    assert s.propose(token=BASE_TOKEN).approved
