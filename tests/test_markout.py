from decimal import Decimal

from tradebot.markout import Benchmark, grade_fill, summarize
from tradebot.models import Fill, Purpose, Side
from tradebot.sim import TOKEN, Sim, paper_config
from tradebot.state_machine import Order


def _order(purpose=Purpose.ENTRY, side=Side.BUY, decision="d1", min_out=Decimal(0)):
    o = Order("c1", TOKEN, side, purpose, Decimal(10), min_out, Decimal(100), decision_id=decision)
    o.submitted_ms = 900
    return o


def _fill(side=Side.BUY, price="1.00", ts=1000):
    return Fill("f1", "c1", TOKEN, side, Decimal(10), Decimal(price), Decimal("0.01"), Decimal(0), ts, 400, "paper-dex")


def bench_const(price, offset_ms=0):
    def fn(token, side, qty, target, tol):
        return Benchmark(Decimal(price), target + offset_ms, Decimal(1e6), "pool_reserves_exact")
    return fn


def test_signed_markout_formula_buy_and_sell():
    cfg = paper_config()
    rows = grade_fill(cfg, _fill(Side.BUY, "1.00"), _order(), bench_const("1.01"), {"d1"})
    assert [r.horizon_s for r in rows] == [1, 5, 10]
    assert rows[0].markout_bps == Decimal(100)
    rows = grade_fill(cfg, _fill(Side.SELL, "1.00"), _order(Purpose.TAKE_PROFIT, Side.SELL), bench_const("1.01"), set())
    assert rows[0].markout_bps == Decimal(-100) and rows[0].execution_grade == "ADVERSE"


def test_missing_benchmark_is_unknown_not_interpolated():
    cfg = paper_config()
    rows = grade_fill(cfg, _fill(), _order(), lambda *a: None, {"d1"})
    assert all(r.data_status == "UNKNOWN" and r.markout_bps is None and r.execution_grade == "UNKNOWN" for r in rows)


def test_out_of_window_benchmark_rejected():
    cfg = paper_config()
    rows = grade_fill(cfg, _fill(), _order(), bench_const("2", offset_ms=-1), {"d1"})  # before t+h
    assert all(r.data_status == "UNKNOWN" for r in rows)


def test_lucky_profitable_fill_still_flagged_as_violation():
    cfg = paper_config()
    rows = grade_fill(cfg, _fill(), _order(decision=None), bench_const("1.5"), set())
    assert rows[0].markout_bps > 0
    assert rows[0].rule_compliance == "VIOLATION"
    assert "ENTRY_WITHOUT_APPROVED_DECISION" in rows[0].violations


def test_end_to_end_markouts_from_sim_with_outage():
    s = Sim()
    s.run(2)
    s.propose()
    s.run(15)
    orders = s.bot.engine.orders
    approved = {p["decision_id"] for _, _, _, _, p in s.journal.events("DECISION") if p["approved"]}
    f = next(iter(s.journal.load_fills().values()))
    s.recorder.drop(TOKEN, f.ts_exec_ms + 4000, f.ts_exec_ms + 7000)  # outage covering +5s
    rows = grade_fill(s.cfg, f, orders[f.client_order_id], s.recorder, approved)
    by_h = {r.horizon_s: r for r in rows}
    assert by_h[1].data_status == "OK" and by_h[1].rule_compliance == "COMPLIANT"
    assert by_h[1].markout_bps < 0  # liquidation benchmark includes the round-trip cost
    assert by_h[5].data_status == "UNKNOWN"
    assert by_h[10].data_status == "OK"
    summ = summarize(rows)
    assert summ["+5s"]["unknown"] == 1


def test_markouts_stored_separately():
    s = Sim()
    s.journal.record_markout("f1", 1, {"x": 1})
    tables = {r[0] for r in s.journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "markouts" in tables
    assert not list(s.journal.events("MARKOUT"))
