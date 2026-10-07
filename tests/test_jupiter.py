"""Jupiter Swap V2 client + paper venue, against REAL captured /order responses
(tests/fixtures/jup_order_*.json, captured 2026-10-07, $100 USDC <-> BONK)."""
import json
import urllib.error
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.clock import SimClock
from tradebot.models import Purpose, Side
from tradebot.ratelimit import TokenBucket
from tradebot.state_machine import Order
from tradebot.venues.base import RateLimited, VenueTimeout
from tradebot.venues.jupiter import (USDC_MINT, JupiterClient, JupiterError, JupiterPaperVenue, parse_order)

FIX = Path(__file__).parent / "fixtures"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
BUY = json.loads((FIX / "jup_order_buy.json").read_text())
SELL = json.loads((FIX / "jup_order_sell.json").read_text())


def free_bucket():
    return TokenBucket(rate_per_s=1000, capacity=1000, reserve=0)


class FakeJup:
    """Serves /order responses scaled from the real fixtures; price multiplier adjustable."""

    def __init__(self):
        self.mult = Decimal(1)
        self.fail = None
        self.calls = []

    def __call__(self, req, timeout):
        from urllib.parse import parse_qs, urlparse
        q = {k: v[0] for k, v in parse_qs(urlparse(req.full_url).query).items()}
        self.calls.append((q, dict(req.header_items())))
        if self.fail == "timeout":
            raise TimeoutError()
        if self.fail == "429":
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)
        amt = int(q["amount"])
        if q["inputMint"] == USDC_MINT:  # buy: tokens per USDC from fixture, inverse price mult
            rate = Decimal(BUY["outAmount"]) / Decimal(BUY["inAmount"])
            out = int(Decimal(amt) * rate / self.mult)
            base = BUY
        else:  # sell: USDC per token from fixture
            rate = Decimal(SELL["outAmount"]) / Decimal(SELL["inAmount"])
            out = int(Decimal(amt) * rate * self.mult)
            base = SELL
        impact = base["priceImpact"] * amt / int(base["inAmount"])  # impact scales with size, as in a real pool
        body = dict(base, inputMint=q["inputMint"], outputMint=q["outputMint"], inAmount=str(amt), outAmount=str(out),
                    priceImpact=impact)
        return json.dumps(body).encode()


def client(fake=None):
    return JupiterClient(api_key="k", bucket=free_bucket(), opener=fake or FakeJup(), now_ms=lambda: 0)


# ---- parsing real responses ------------------------------------------
def test_parse_real_buy_quote():
    oq = parse_order(BUY, USDC_MINT, BONK, 100_000_000)
    assert oq.in_amount == 100_000_000 and oq.out_amount == int(BUY["outAmount"])  # fee in input mint: not re-deducted
    assert oq.price_impact_bps == abs(Decimal(str(BUY["priceImpact"]))) * 100
    assert oq.fee_bps == 10 and oq.router == "metis"


def test_parse_real_sell_quote_deducts_output_fee_conservatively():
    oq = parse_order(SELL, BONK, USDC_MINT, int(SELL["inAmount"]))
    assert oq.raw_out_amount == int(SELL["outAmount"])
    assert oq.out_amount == int(Decimal(SELL["outAmount"]) * Decimal("0.999"))


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(inputMint=BONK),  # wrong mint
    lambda b: b.update(inAmount="1"),  # wrong amount
    lambda b: b.update(outAmount="0"),  # no route
    lambda b: b.pop("outAmount"),
    lambda b: b.update(priceImpact=None),
    lambda b: b.update(errorCode=1, error="Insufficient funds"),
])
def test_inconsistent_quote_rejected(mutate):
    b = dict(BUY)
    mutate(b)
    with pytest.raises(JupiterError):
        parse_order(b, USDC_MINT, BONK, 100_000_000)


def test_real_round_trip_cost_is_measured():
    c = client()
    ask, _ = c.quote_buy(BONK, Decimal(100), 5)
    bid, _ = c.quote_sell(BONK, ask.qty, 5)
    rt_bps = (ask.price - bid.price) / ask.price * 10_000
    assert 20 < rt_bps < 40  # ~24 bps in the captured market, plus the conservative fee deduction


# ---- client behaviour ------------------------------------------------
def test_api_key_header_and_redaction():
    fake = FakeJup()
    c = JupiterClient(api_key="SECRET", bucket=free_bucket(), opener=fake)
    c.quote_buy(BONK, Decimal(10), 5)
    assert fake.calls[0][1].get("X-api-key") == "SECRET"
    assert "SECRET" not in repr(c)
    assert fake.calls[0][0]["amount"] == "10000000"  # 10 USDC in atomic units


def test_errors_map_to_venue_errors():
    fake = FakeJup()
    fake.fail = "timeout"
    with pytest.raises(VenueTimeout):
        client(fake).quote_buy(BONK, Decimal(10), 5)
    fake.fail = "429"
    with pytest.raises(RateLimited):
        client(fake).quote_buy(BONK, Decimal(10), 5)


def test_keyless_budget_reserves_exit_quotes():
    c = JupiterClient(api_key="", opener=FakeJup())
    c.bucket = TokenBucket(rate_per_s=0.0, capacity=3, reserve=1)
    c.quote_buy(BONK, Decimal(1), 5)
    c.quote_buy(BONK, Decimal(1), 5)
    with pytest.raises(RateLimited):
        c.quote_buy(BONK, Decimal(1), 5)  # analytics/entries cannot use the reserve
    c.quote_sell(BONK, Decimal(1000), 5, priority=True)  # exits can


# ---- paper venue on (fake-)live prices --------------------------------
def venue(fake):
    clock = SimClock()
    v = JupiterPaperVenue(clock, client(fake), lambda t: 5, confirm_delay_ms=1500, sol_usd=lambda: Decimal(150))
    return clock, v


def buy_order(usd="100", min_out="0"):
    return Order("c1", BONK, Side.BUY, Purpose.ENTRY, Decimal(usd), Decimal(min_out), Decimal(100))


def test_paper_fill_uses_requote_at_landing():
    fake = FakeJup()
    clock, v = venue(fake)
    v.submit_swap(buy_order())
    v.process()
    assert v.get_tx_status("c1").status == "PENDING"
    clock.advance(1500)
    v.process()
    st = v.get_tx_status("c1")
    assert st.status == "CONFIRMED" and st.fill.qty > 0 and v.balance(BONK) == st.fill.qty
    assert v.network_fee_usd() == Decimal(55_000) / Decimal(1_000_000_000) * 150


def test_paper_fill_fails_when_price_moves_past_min_out():
    fake = FakeJup()
    clock, v = venue(fake)
    expected = client(fake).quote_buy(BONK, Decimal(100), 5)[0].qty
    v.submit_swap(buy_order(min_out=str(expected * Decimal("0.99"))))
    fake.mult = Decimal("1.05")  # price up 5% before landing
    clock.advance(1500)
    v.process()
    assert v.get_tx_status("c1").status == "FAILED"


def test_paper_tx_expires_if_landing_quote_unavailable():
    fake = FakeJup()
    clock, v = venue(fake)
    v.submit_swap(buy_order())
    fake.fail = "timeout"
    for _ in range(100):
        clock.advance(1000)
        v.process()
    assert v.get_tx_status("c1").status == "EXPIRED"
    assert v.balance(BONK) == 0


def test_paper_submit_is_idempotent():
    fake = FakeJup()
    clock, v = venue(fake)
    v.submit_swap(buy_order())
    v.submit_swap(buy_order())
    clock.advance(1500)
    v.process()
    first = v.balance(BONK)
    clock.advance(1500)
    v.process()
    assert v.balance(BONK) == first
