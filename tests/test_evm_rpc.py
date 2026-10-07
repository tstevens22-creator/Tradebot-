"""Base token safety from REAL captured Base RPC responses (2026-10-07)."""
import json
from pathlib import Path

from tradebot.data.evm_rpc import EvmRpc, interpret
from tradebot.ratelimit import TokenBucket
from tradebot.sim import Sim

FIX = Path(__file__).parent / "fixtures" / "base"
DEGEN = "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ZERO_WORD = "0x" + "0" * 64


def res(name):
    return json.loads((FIX / f"rpc_{name}.json").read_text())["result"]


def test_degen_owned_not_proxy():
    i = interpret(DEGEN, res("degen_decimals"), res("degen_owner"), res("degen_impl_slot"), ZERO_WORD)
    assert i.decimals == 18 and i.owner == "0x704ec5c12ca20a293c2c0b72b22619a4231f3c0d"
    assert i.owner_renounced is False and not i.is_proxy and i.hazards == ()


def test_usdc_legacy_proxy_detected():
    i = interpret(USDC, res("usdc_decimals"), res("usdc_owner"), res("usdc_impl_slot"), res("usdc_zos_impl_slot"))
    assert i.decimals == 6 and i.is_proxy and i.hazards == ("UPGRADEABLE_PROXY",)


def test_no_owner_function_is_unknown():
    i = interpret("0x4200000000000000000000000000000000000006", "0x" + "0" * 62 + "12", res("weth_owner"),
                  ZERO_WORD, ZERO_WORD)
    assert res("weth_owner") == "0x" and i.owner is None and i.owner_renounced is None


def test_renounced_owner():
    i = interpret(DEGEN, res("degen_decimals"), ZERO_WORD, ZERO_WORD, ZERO_WORD)
    assert i.owner_renounced is True


def test_token_safety_mapping_and_gate():
    files = {"0x313ce567": "degen_decimals", "0x8da5cb5b": "degen_owner"}

    def opener(req, t):
        body = json.loads(req.data)
        if body["method"] == "eth_call":
            return (FIX / f"rpc_{files[body['params'][0]['data']]}.json").read_bytes()
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": ZERO_WORD}).encode()

    rpc = EvmRpc(url="https://x", bucket=TokenBucket(1000, 1000), opener=opener)
    s = rpc.token_safety(DEGEN.upper().replace("0X", "0x"), 0)
    assert s.token == DEGEN and s.chain == "base" and s.mint_authority is True and s.freeze_authority is True
    assert s.top10_holder_pct is None and s.sell_simulation_ok is None  # from Codex / live step
    assert "https://x" not in repr(rpc)
    # owned token -> gate rejects (owner may mint/blacklist) unless the owner policy changes
    sim = Sim()
    sim.safety_overrides = {"mint_authority": True, "freeze_authority": True}
    sim.run(2)
    reasons = " ".join(sim.propose().reasons)
    assert "MINT_AUTHORITY" in reasons and "FREEZE_AUTHORITY" in reasons
