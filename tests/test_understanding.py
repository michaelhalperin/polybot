"""Tests for the AI market-understanding layer (no API key / network needed).

A fake LLM client returns canned JSON so we can test parsing, caching, the
resolution-safety gate, and the spec->price path deterministically.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polybot.config import Config
from polybot.crypto import build_view
from polybot.models import Level, Market, OrderBook
from polybot.store import Store
from polybot.understanding import Analysis, MarketUnderstanding


class FakeLLM:
    """Stand-in for LLMClient: returns a fixed payload and counts calls."""
    available = True

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def analyze(self, *args, **kwargs):
        self.calls += 1
        return self.payload


class FakeFeed:
    """Stand-in for CryptoFeed with no network."""
    def symbol_for(self, text):
        t = (text or "").lower()
        return "BTCUSDT" if "btc" in t or "bitcoin" in t else None

    def spot(self, symbol):
        return 65000.0

    def volatility(self, symbol):
        return 0.55


def make_market(**kw):
    base = dict(
        condition_id="c1", question="Will Bitcoin be above $70,000 on June 30?",
        slug="q", outcomes=["Yes", "No"], token_ids=["YES", "NO"],
        outcome_prices=[0.3, 0.7], volume=10000, volume_24hr=5000, liquidity=40000,
        tick_size=0.01, min_order_size=5, neg_risk=False, one_day_price_change=0.0,
        end_date="2026-06-30T12:00:00Z", taker_fee_rate=0.0, enable_order_book=True,
        accepting_orders=True, closed=False, description="Resolves based on Binance BTC price.")
    base.update(kw)
    return Market(**base)


def cfg_enabled():
    return Config(raw={"strategy": {"enable_llm_understanding": True},
                       "storage": {"db_path": tempfile.mktemp(suffix=".db")}})


def crypto_payload(**over):
    p = dict(is_crypto_price_target=True, asset="BTC", threshold=70000.0,
             direction="above", resolution_risk="low", tradeable=True,
             summary="YES if BTC >= $70k on June 30.")
    p.update(over)
    return p


# ---------------------------------------------------------------- parsing
def test_analysis_from_json_maps_fields():
    a = Analysis.from_json(crypto_payload())
    assert a.is_crypto_price_target and a.asset == "BTC"
    assert a.threshold == 70000.0 and a.direction == "above"
    assert a.tradeable and a.resolution_risk == "low"


# ---------------------------------------------------------------- caching
def test_caches_after_first_call():
    llm = FakeLLM(crypto_payload())
    mu = MarketUnderstanding(cfg_enabled(), llm=llm, store=Store(tempfile.mktemp(suffix=".db")))
    m = make_market()
    a1 = mu.analyze(m)
    a2 = mu.analyze(m)            # same text -> served from cache
    assert a1 is not None and a2 is not None
    assert llm.calls == 1         # LLM called only once


def test_reanalyzes_when_text_changes():
    llm = FakeLLM(crypto_payload())
    mu = MarketUnderstanding(cfg_enabled(), llm=llm, store=Store(tempfile.mktemp(suffix=".db")))
    mu.analyze(make_market())
    mu.analyze(make_market(description="DIFFERENT rules now."))
    assert llm.calls == 2


# ---------------------------------------------------------------- pre-screen
def test_prescreen_skips_non_crypto_without_calling_llm():
    llm = FakeLLM(crypto_payload())
    mu = MarketUnderstanding(cfg_enabled(), llm=llm, store=Store(tempfile.mktemp(suffix=".db")))
    a = mu.analyze(make_market(question="Will Trump win the election?", description="politics"))
    assert a is None and llm.calls == 0


# ---------------------------------------------------------------- inactive
def test_inactive_when_llm_unavailable():
    class Off(FakeLLM):
        available = False
    mu = MarketUnderstanding(cfg_enabled(), llm=Off({}), store=Store(tempfile.mktemp(suffix=".db")))
    assert mu.active is False
    assert mu.analyze(make_market()) is None


# ------------------------------------------------------ spec -> price path
def test_build_view_prices_from_spec():
    m = make_market()
    view = build_view(m, "BTCUSDT", 70000.0, "above", FakeFeed())
    assert view is not None
    # spot 65k, strike 70k, above -> should be clearly under 50%
    assert 0.0 < view.fair_yes < 0.5


def test_build_view_rejects_bad_spec():
    m = make_market()
    assert build_view(m, None, 70000.0, "above", FakeFeed()) is None
    assert build_view(m, "BTCUSDT", 0, "above", FakeFeed()) is None
    assert build_view(m, "BTCUSDT", 70000.0, "bogus", FakeFeed()) is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
