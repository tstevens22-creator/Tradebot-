from decimal import Decimal

from tradebot.models import Position, Purpose, Side
from tradebot.protection import evaluate_exits
from tradebot.sim import TOKEN, Sim, paper_config


def pos(qty="100", cost="10", opened=0):
    p = Position(TOKEN, qty=Decimal(qty), cost_usd=Decimal(cost), opened_ms=opened,
                 initial_qty=Decimal(qty), high_water_price=Decimal("0.1"))
    return p


def test_stop_beats_take_profit_and_profit_floor():
    # Price return says +25% but net liquidation says -20% (e.g. huge exit costs):
    # the stop must fire, and it must outrank TP.
    cfg = paper_config(target_basis="price_return")
    out = evaluate_exits(cfg, pos(), 1000, Decimal("0.125"), Decimal("8"), Decimal(0))
    assert out[0].purpose is Purpose.STOP_LOSS


def test_stop_uses_worse_of_price_and_net():
    cfg = paper_config()
    out = evaluate_exits(cfg, pos(), 1000, Decimal("0.0999"), Decimal("9.1"), Decimal(0))
    assert out and out[0].purpose is Purpose.STOP_LOSS


def test_exit_qty_never_exceeds_available():
    cfg = paper_config()
    out = evaluate_exits(cfg, pos(), 1000, Decimal("0.05"), Decimal("5"), Decimal("60"))
    assert all(i.qty <= Decimal(40) for i in out)
    assert evaluate_exits(cfg, pos(), 1000, Decimal("0.05"), Decimal("5"), Decimal("100")) == []


def test_max_hold_fires_without_price_data():
    cfg = paper_config(max_holding_seconds=60)
    out = evaluate_exits(cfg, pos(opened=0), 61_000, None, None, Decimal(0))
    assert out[0].purpose is Purpose.MAX_HOLD


def test_trailing_stop():
    cfg = paper_config()
    p = pos()
    evaluate_exits(cfg, p, 1, Decimal("0.115"), Decimal("11.4"), Decimal(0))  # new high, +14%
    out = evaluate_exits(cfg, p, 2, Decimal("0.102"), Decimal("10.15"), Decimal(0))
    assert out[0].purpose is Purpose.TRAILING_STOP


def test_scale_out_partial_qty():
    cfg = paper_config()
    out = evaluate_exits(cfg, pos(), 1, Decimal("0.112"), Decimal("11.2"), Decimal(0))
    so = [i for i in out if i.purpose is Purpose.SCALE_OUT]
    assert so and so[0].qty == Decimal(50)


def test_stop_loss_executes_in_sim():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(2)
    s.venue.shock(TOKEN, Decimal("-0.10"))
    s.run(3)
    p = s.position()
    assert p.qty == 0 and p.realized_pnl_usd < 0
    assert s.bot.last_stop_ms.get(TOKEN)


def test_gap_loss_exceeds_planned_stop_and_is_reported():
    s = Sim()
    s.run(2)
    d = s.propose()
    s.run(2)
    s.venue.shock(TOKEN, Decimal("-0.35"))  # gap straight through the 8% stop
    s.run(3)
    loss = -s.position().realized_pnl_usd
    assert loss > d.planned_loss_usd  # gap: realized loss beyond planned risk...
    assert loss <= d.stressed_loss_usd * Decimal("1.6")  # ...measured against the stressed budget


def test_partial_fill_is_protected():
    """Two entries filled at different times: protection always covers the
    full confirmed qty (no unprotected slice)."""
    s = Sim(cfg=paper_config(max_concentration_pct="1"))
    s.run(2)
    s.propose(usd="80")
    s.run(2)
    s.venue.shock(TOKEN, Decimal("0.02"))
    s.run(1)
    s.propose(usd="80")  # adding while in profit is allowed
    s.run(2)
    s.venue.shock(TOKEN, Decimal("-0.15"))
    s.run(3)
    assert s.position().qty == 0
    sells = [o for o in s.bot.engine.orders.values() if o.side is Side.SELL and o.fills]
    assert sum(o.filled_qty for o in sells) == sum(
        f.qty for o in s.bot.engine.orders.values() if o.side is Side.BUY for f in o.fills.values())
