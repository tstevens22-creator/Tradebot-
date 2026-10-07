"""Read-only Solana RPC parsing against REAL captured responses (2026-10-07)."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.data.solana_rpc import (RpcError, SolanaRpc, map_signature_status, parse_mint,
                                      parse_top10_pct)
from tradebot.ratelimit import TokenBucket
from tradebot.venues.base import RateLimited

FIX = Path(__file__).parent / "fixtures"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
PYUSD = "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"


def result(name):
    return json.loads((FIX / name).read_text())["result"]


def test_classic_spl_mint_bonk():
    mi = parse_mint(BONK, result("rpc_mint_bonk.json"))
    assert mi.decimals == 5 and not mi.mint_authority and not mi.freeze_authority
    assert mi.transfer_tax_bps == 0 and mi.hazards == ()


def test_token2022_hazards_pyusd():
    mi = parse_mint(PYUSD, result("rpc_mint_token2022.json"))
    assert mi.mint_authority and mi.freeze_authority
    assert mi.transfer_tax_bps == 0  # currently 0, but the authority can raise it
    assert {"PERMANENT_DELEGATE", "TRANSFER_FEE_MUTABLE", "TRANSFER_HOOK_MUTABLE"} <= set(mi.hazards)
    assert "TRANSFER_HOOK" not in mi.hazards  # programId is null today


def test_unknown_program_and_missing_account():
    r = result("rpc_mint_bonk.json")
    bad = json.loads(json.dumps(r))
    bad["value"]["owner"] = "Some1111111111111111111111111111111111111111"
    with pytest.raises(RpcError):
        parse_mint(BONK, bad)
    with pytest.raises(RpcError):
        parse_mint(BONK, {"value": None})


def test_top10_pct():
    accounts = {"value": [{"address": f"a{i}", "amount": str(a), "decimals": 5} for i, a in
                          enumerate([50, 10, 10, 5, 5, 5, 5, 2, 2, 2, 1, 1])]}
    assert parse_top10_pct(accounts, 1000) == Decimal("0.096")
    assert parse_top10_pct({"error": "x"}, 1000) is None


def test_signature_status_mapping_real():
    vals = result("rpc_sig_statuses.json")["value"]
    assert [map_signature_status(v) for v in vals] == ["CONFIRMED", "NOT_FOUND"]
    assert map_signature_status({"err": {"InstructionError": [0, "x"]}, "confirmationStatus": "confirmed"}) == "FAILED"
    assert map_signature_status({"err": None, "confirmationStatus": "processed"}) == "PENDING"


def test_rpc_429_and_error_mapping():
    def opener_429(req, t):
        return json.dumps({"jsonrpc": "2.0", "error": {"code": 429, "message": "Too many requests"}, "id": 1}).encode()
    rpc = SolanaRpc(url="https://x", bucket=TokenBucket(1000, 1000), opener=opener_429)
    with pytest.raises(RateLimited):
        rpc.call("getTokenLargestAccounts", [BONK])
    assert "https://x" not in repr(rpc)


def test_token_safety_with_holders_unavailable_is_rejected():
    """Real-world case: public RPC 429s getTokenLargestAccounts -> top10 unknown -> gate rejects."""
    calls = []

    def opener(req, t):
        m = json.loads(req.data)["method"]
        calls.append(m)
        if m == "getAccountInfo":
            return (FIX / "rpc_mint_bonk.json").read_bytes()
        return json.dumps({"jsonrpc": "2.0", "error": {"code": 429, "message": "Too many requests"}, "id": 1}).encode()

    rpc = SolanaRpc(url="https://x", bucket=TokenBucket(1000, 1000), opener=opener)
    s = rpc.token_safety(BONK, 0)
    assert s.decimals == 5 and s.top10_holder_pct is None and s.sell_simulation_ok is None and s.venue_stage is None


def test_gate_rejects_hazardous_token():
    from tradebot.sim import Sim, TOKEN
    s = Sim()
    s.safety_overrides = {"hazards": ("PERMANENT_DELEGATE",)}
    s.run(2)
    d = s.propose()
    assert not d.approved and any("TOKEN_HAZARD" in r for r in d.reasons)
