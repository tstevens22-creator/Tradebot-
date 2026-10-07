"""The owner's live-authorization condition, enforced as code."""
import json
import os
import time

import pytest

from tradebot import preflight as pf
from tradebot.config import ConfigError, assert_live_allowed
from tradebot.sim import paper_config


def test_live_blocked_without_preflight(monkeypatch, tmp_path):
    cfg = paper_config(mode="live", live_trading_authorized=True)
    monkeypatch.setenv("TRADEBOT_LIVE_ACK", cfg.config_hash)
    monkeypatch.setattr(pf, "REPORTS", tmp_path)
    with pytest.raises(ConfigError, match="preflight"):
        assert_live_allowed(cfg)


def test_preflight_record_rules(tmp_path):
    rec = tmp_path / "p.json"
    assert not pf.preflight_record_ok("h", rec)[0]
    rec.write_text(json.dumps({"ts": time.time(), "config_hash": "other", "passed": True, "checks": []}))
    assert "different config" in pf.preflight_record_ok("h", rec)[1]
    rec.write_text(json.dumps({"ts": time.time() - 90000, "config_hash": "h", "passed": True, "checks": []}))
    assert "older" in pf.preflight_record_ok("h", rec)[1]
    rec.write_text(json.dumps({"ts": time.time(), "config_hash": "h", "passed": False,
                               "checks": [{"name": "paper_soak", "ok": False}]}))
    assert "paper_soak" in pf.preflight_record_ok("h", rec)[1]
    rec.write_text(json.dumps({"ts": time.time(), "config_hash": "h", "passed": True, "checks": []}))
    assert pf.preflight_record_ok("h", rec)[0]


def test_even_a_passing_preflight_cannot_enable_missing_live_adapter(monkeypatch):
    cfg = paper_config(mode="live", live_trading_authorized=True)
    monkeypatch.setenv("TRADEBOT_LIVE_ACK", cfg.config_hash)
    monkeypatch.setattr(pf, "preflight_record_ok", lambda h: (True, "ok"))
    with pytest.raises(ConfigError, match="no live venue adapter"):
        assert_live_allowed(cfg)


def test_leak_scan_finds_env_secret_values(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "REPORTS", tmp_path)
    monkeypatch.setattr(pf, "_tracked_files", lambda: [])
    (tmp_path / "live-paper-x.json").write_text('{"note": "supersecretvalue123456"}')
    c = pf.check_no_leaks(pf.Ctx(None, env={"CODEX_API_KEY": "supersecretvalue123456"}))
    assert not c.ok and "live-paper-x.json" in c.detail
    c = pf.check_no_leaks(pf.Ctx(None, env={"CODEX_API_KEY": "differentvalue98765"}))
    assert c.ok


def test_repo_and_reports_have_no_key_shaped_strings():
    """Runs the real scan over every tracked file and report in this repo."""
    assert pf.check_no_leaks(pf.Ctx(None, env={})).ok


def test_wallet_rules(tmp_path):
    k = tmp_path / "hot.key"
    k.write_text("x")
    os.chmod(k, 0o644)
    assert not pf.check_wallet(pf.Ctx(None, env={"TRADEBOT_WALLET_KEYFILE": str(k)})).ok
    os.chmod(k, 0o600)
    assert pf.check_wallet(pf.Ctx(None, env={"TRADEBOT_WALLET_KEYFILE": str(k)})).ok
    inside = pf.ROOT / "config" / "paper.example.toml"
    assert "OUTSIDE" in pf.check_wallet(pf.Ctx(None, env={"TRADEBOT_WALLET_KEYFILE": str(inside)})).detail


def test_credentials_reject_public_rpc():
    env = {"CODEX_API_KEY": "a", "JUPITER_API_KEY": "b", "BITQUERY_API_KEY": "c", "ZEROX_API_KEY": "d",
           "SOLANA_RPC_URL": "https://api.mainnet-beta.solana.com"}
    c = pf.check_credentials(pf.Ctx(None, env=env))
    assert not c.ok and "SOLANA_RPC_URL" in c.detail


def test_paper_config_is_not_a_live_config():
    c = pf.check_live_config(pf.Ctx(str(pf.ROOT / "config" / "paper.example.toml")))
    assert not c.ok


def test_crashing_check_fails_closed():
    def boom(ctx):
        raise RuntimeError("x")
    passed, res = pf.run_preflight(None, checks=[boom], write=False)
    assert not passed and "crashed" in res[0].detail
