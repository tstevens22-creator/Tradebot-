"""Base via 0x Swap API v2 (AllowanceHolder). Response shape follows the 0x docs
example (tests/fixtures/base/zx_price_doc_shape.json); no 0x key exists here,
so no live Base quote has been captured yet."""
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tradebot.clock import SimClock
from tradebot.models import Purpose, Side
from tradebot.ratelimit import TokenBucket
from tradebot.state_machine import Order
from tradebot.venues.base import RateLimited
from tradebot.venues.zeroex import (ALLOWANCE_HOLDER, USDC_BASE, WETH_BASE, ZeroExClient, ZeroExError,
                                    ZeroExPaperVenue, parse_price)

FIX = Path(__file__).parent / "fixtures" / "base"
DOC = json.loads((FIX / "zx_price_doc_shape.json").read_text())
DEGEN = "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"
TOKENS_PER_USDC = Decimal(DOC["buyAmount"]) / Decimal(DOC["sellAmount"])  # atomic per atomic


class FakeZx:
    """0x /price responses in the documented shape; constant-product-like impact."""

    def __init__(self):
        self.mult = Decimal(1)
        self.fail = None
        self.spender = ALLOWANCE_HOLDER
        self.sell_tax = "0"
        self.calls = []

    def __call__(self, req, timeout):
        q = {k: v[0] for k, v in parse_qs(urlparse(req.full_url).query).items()}
        self.calls.append((q, dict(req.header_items())))
        if self.fail:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, int(self.fail), "x", {}, None)
        amt = int(q["sellAmount"])
        sell, buy = q["sellToken"], q["buyToken"]
        usd_atomic = Decimal(amt) if sell == USDC_BASE else Decimal(amt) / TOKENS_PER_USDC
        impact = 1 - usd_atomic / Decimal(10 ** 12)  # size-dependent impact, measured in USDC terms
        if sell == USDC_BASE:  # buy DEGEN
            out = Decimal(amt) * TOKENS_PER_USDC / self.mult * impact
        elif buy == USDC_BASE and sell == WETH_BASE:
            out = Decimal(amt) / Decimal(10 ** 18) * Decimal(2500) * Decimal(10 ** 6)
        else:  # sell DEGEN for USDC
            out = Decimal(amt) / TOKENS_PER_USDC * self.mult * Decimal("0.997") * impact
        body = dict(DOC, sellToken=sell, buyToken=buy, sellAmount=str(amt), buyAmount=str(int(out)),
                    minBuyAmount=str(int(out * Decimal("0.99"))),
                    fees={"zeroExFee": {"amount": str(int(out * Decimal("0.0015"))), "token": buy}},
                    issues={"allowance": {"actual": "0", "spender": self.spender}},
                    tokenMetadata={"buyToken": {"buyTaxBps": "0", "sellTaxBps": self.sell_tax},
                                   "sellToken": {"buyTaxBps": "0", "sellTaxBps": "0"}})
        return json.dumps(body).encode()


def client(fake=None, key="k"):
    return ZeroExClient(api_key=key, bucket=TokenBucket(1000, 1000), opener=fake or FakeZx(), now_ms=lambda: 0)


# ---- parsing (documented shape) ------------------------------------------
def test_parse_doc_shape():
    p = parse_price(DOC, USDC_BASE, DEGEN, 20_000_000)
    fee = int(DOC["fees"]["zeroExFee"]["amount"])
    assert p.raw_buy_amount == int(DOC["buyAmount"]) and p.buy_amount == p.raw_buy_amount - fee  # conservative
    assert p.spender == ALLOWANCE_HOLDER and p.buy_tax_bps == 0 and p.sell_tax_bps == 0
    assert p.total_network_fee_wei == int(DOC["totalNetworkFee"])


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(liquidityAvailable=False),
    lambda b: b.update(buyToken=WETH_BASE),
    lambda b: b.update(sellAmount="1"),
    lambda b: b.update(buyAmount="0"),
    lambda b: b.update(issues={"allowance": {"spender": "0x000000000000000000000000000000000000dead"}}),
])
def test_bad_or_dangerous_quotes_rejected(mutate):
    b = json.loads(json.dumps(DOC))
    mutate(b)
    with pytest.raises(ZeroExError):
        parse_price(b, USDC_BASE, DEGEN, 20_000_000)


def test_headers_key_required_and_redacted():
    fake = FakeZx()
    c = client(fake, key="SECRET0X")
    c.quote_buy(DEGEN, Decimal(20), 18)
    q, h = fake.calls[0]
    assert h["0x-api-key"] == "SECRET0X" and h["0x-version"] == "v2" and q["chainId"] == "8453"
    assert "SECRET0X" not in repr(c)
    with pytest.raises(ZeroExError):
        ZeroExClient(api_key="", opener=fake).price(USDC_BASE, DEGEN, 1)
    fake.fail = "429"
    with pytest.raises(RateLimited):
        client(fake).quote_buy(DEGEN, Decimal(20), 18)


def test_price_impact_measured_against_probe():
    q, _ = client().quote_buy(DEGEN, Decimal(100_000), 18)
    small, _ = client().quote_buy(DEGEN, Decimal(1), 18)
    assert q.price_impact_bps > small.price_impact_bps >= 0


# ---- paper venue: approvals (F5) and fills -------------------------------
def venue(fake=None):
    clock = SimClock()
    v = ZeroExPaperVenue(clock, client(fake), lambda t: 18, confirm_delay_ms=2500, eth_usd=lambda: Decimal(2500))
    return clock, v


def order(side=Side.BUY, qty="20", min_out="0", cid="c1"):
    return Order(cid, DEGEN, side, Purpose.ENTRY if side is Side.BUY else Purpose.STOP_LOSS, Decimal(qty),
                 Decimal(min_out), Decimal(100))


def test_exact_approvals():
    clock, v = venue()
    v.submit_swap(order())
    clock.advance(2500)
    v.process()
    fill = v.get_tx_status("c1").fill
    assert fill and v.approvals[0] == {"token": USDC_BASE, "spender": ALLOWANCE_HOLDER, "amount": 20_000_000,
                                       "cid": "c1", "ts": clock.now_ms()}
    assert v.allowances[(USDC_BASE, ALLOWANCE_HOLDER)] == 0  # consumed, nothing left standing
    v.submit_swap(order(Side.SELL, qty=str(fill.qty), cid="c2"))
    clock.advance(2500)
    v.process()
    assert v.get_tx_status("c2").status == "CONFIRMED"
    assert v.approvals[1]["token"] == DEGEN and v.approvals[1]["amount"] == int(fill.qty * 10 ** 18)
    assert all(a["spender"] == ALLOWANCE_HOLDER for a in v.approvals)
    assert fill.fee_usd > 0  # network + approval gas charged


def test_approval_revoked_on_failure_and_retry_reapproves_exactly():
    fake = FakeZx()
    clock, v = venue(fake)
    v.submit_swap(order())
    fake.fail = "500"
    clock.advance(2500)
    v.process()
    assert v.get_tx_status("c1").status == "PENDING" and v.allowances[(USDC_BASE, ALLOWANCE_HOLDER)] == 0
    fake.fail = None
    clock.advance(1000)
    v.process()
    assert v.get_tx_status("c1").status == "CONFIRMED"
    assert [a["amount"] for a in v.approvals] == [20_000_000, 20_000_000]


def test_slippage_failure_revokes_allowance():
    fake = FakeZx()
    clock, v = venue(fake)
    expected = client(fake).quote_buy(DEGEN, Decimal(20), 18)[0].qty
    v.submit_swap(order(min_out=str(expected * Decimal("0.99"))))
    fake.mult = Decimal("1.05")
    clock.advance(2500)
    v.process()
    assert v.get_tx_status("c1").status == "FAILED"
    assert v.allowances[(USDC_BASE, ALLOWANCE_HOLDER)] == 0


def test_unallowlisted_spender_blocks_trading():
    fake = FakeZx()
    fake.spender = "0x000000000000000000000000000000000000dead"
    clock, v = venue(fake)
    with pytest.raises(ZeroExError):
        v.quote_buy(DEGEN, Decimal(20))
