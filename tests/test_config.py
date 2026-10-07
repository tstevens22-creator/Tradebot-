import pytest

from tradebot.config import ConfigError, assert_live_allowed, from_dict, load
from tradebot.sim import PAPER_CONFIG, paper_config


def test_valid_paper_config_loads():
    cfg = paper_config()
    assert cfg.mode == "paper" and len(cfg.config_hash) == 16


def test_missing_settings_reported_in_one_batch():
    raw = dict(PAPER_CONFIG)
    for k in ("stop_loss_pct", "daily_loss_limit_usd", "max_data_age_ms"):
        raw.pop(k)
    with pytest.raises(ConfigError) as e:
        from_dict(raw)
    msg = str(e.value)
    assert "stop_loss_pct" in msg and "daily_loss_limit_usd" in msg and "max_data_age_ms" in msg


def test_nullable_features_must_still_be_explicit():
    raw = dict(PAPER_CONFIG)
    raw.pop("trailing_stop_pct")
    with pytest.raises(ConfigError):
        from_dict(raw)
    raw["trailing_stop_pct"] = None
    assert from_dict(raw).trailing_stop_pct is None


def test_non_nullable_null_rejected():
    with pytest.raises(ConfigError):
        paper_config(stop_loss_pct=None)


def test_secrets_not_in_config():
    raw = dict(PAPER_CONFIG, api_key="abc")
    with pytest.raises(ConfigError, match="secrets"):
        from_dict(raw)


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        from_dict(dict(PAPER_CONFIG, stop_los_pct="0.1"))


@pytest.mark.parametrize("override", [
    {"max_position_usd": "1000", "max_total_exposure_usd": "500"},
    {"per_trade_risk_usd": "100", "daily_loss_limit_usd": "50", "max_stressed_loss_usd": "200"},
    {"exit_slippage_ladder_bps": [300, 100]},
    {"exit_slippage_ladder_bps": [100, 2000]},  # ladder may not exceed emergency cap
    {"scale_outs": [["0.5", "0.5"]]},  # scale-out above TP
    {"scale_outs": [["0.05", "0.7"], ["0.1", "0.7"]]},  # >100%
    {"stop_loss_pct": "1.5"},
    {"mode": "live"},  # live without authorization
    {"mode": "live", "live_trading_authorized": True, "trigger_price_source": "indicative"},
    {"max_open_positions": True},  # bool is not an int
    {"allow_average_down": "yes"},
])
def test_conflicting_or_invalid_rules_rejected(override):
    with pytest.raises(ConfigError):
        paper_config(**override)


def test_config_is_immutable():
    cfg = paper_config()
    with pytest.raises(Exception):
        cfg.stop_loss_pct = 0.5  # type: ignore[misc]


def test_live_always_refused_without_adapter(monkeypatch):
    cfg = paper_config(mode="live", live_trading_authorized=True)
    with pytest.raises(ConfigError, match="TRADEBOT_LIVE_ACK"):
        assert_live_allowed(cfg)
    monkeypatch.setenv("TRADEBOT_LIVE_ACK", cfg.config_hash)
    with pytest.raises(ConfigError, match="no live venue"):
        assert_live_allowed(cfg)


def test_example_config_files():
    root = __import__("pathlib").Path(__file__).resolve().parents[1] / "config"
    assert load(str(root / "paper.example.toml")).mode == "paper"
    with pytest.raises(ConfigError) as e:
        load(str(root / "live.TEMPLATE.toml"))
    assert "missing" in str(e.value) or "null" in str(e.value)
