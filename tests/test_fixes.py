"""Tests for the spread-churn fixes (relative spread guard; offline)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polybot.config import Config
from polybot.models import Level, Market, OrderBook
from polybot.strategy import Strategy


class FakeFeed:
    def symbol_for(self, text):
        t = (text or "").lower()
        return "BTCUSDT" if ("btc" in t or "bitcoin" in t) else None

    def spot(self, symbol):
        return 65000.0

    def volatility(self, symbol):
        return 0.55


def book(bid, ask):
    ob = OrderBook("t", [], [])
    ob.bids = [Level(bid, 1000)]
    ob.asks = [Level(ask, 1000)]
    return ob


def make_market(question):
    return Market(
        condition_id="c1", question=question, slug="q", outcomes=["Yes", "No"],
        token_ids=["YES", "NO"], outcome_prices=[0.1, 0.9], volume=10000,
        volume_24hr=5000, liquidity=40000, tick_size=0.01, min_order_size=5,
        neg_risk=False, one_day_price_change=0.0, end_date="2026-06-30T12:00:00Z",
        taker_fee_rate=0.0, enable_order_book=True, accepting_orders=True, closed=False)


def strat():
    cfg = Config(raw={"strategy": {"enable_crypto_model": True,
                                   "enable_llm_understanding": False,
                                   "crypto_min_edge": 0.03, "min_confidence": 0.0,
                                   "crypto_max_spread_pct": 0.20},
                      "fees": {"default_taker_rate": 0.0}, "risk": {}})
    s = Strategy(cfg)
    s.feed = FakeFeed()   # no network
    return s


def test_wide_relative_spread_is_skipped():
    s = strat()
    m = make_market("Will Bitcoin reach $66,000 in June?")  # touch ~0.8 fair
    # YES cheap with a huge *relative* spread (0.044/0.060, ~31% of mid)
    yes = book(0.044, 0.060)
    no = book(0.94, 0.96)   # realistic complementary price (no arb)
    opps = s.evaluate(m, {"YES": yes, "NO": no})
    assert opps == []   # the wide-spread YES is filtered out


def test_tight_spread_still_trades():
    s = strat()
    m = make_market("Will Bitcoin reach $66,000 in June?")  # fair ~0.8 (touch above spot)
    yes = book(0.49, 0.50)   # tight spread, big edge vs ~0.8 fair
    no = book(0.49, 0.50)
    opps = s.evaluate(m, {"YES": yes, "NO": no})
    assert any(o.kind == "crypto" for o in opps)


def test_stop_clears_alive_despite_recent_equity():
    """The Stop bug: a recent equity snapshot must NOT keep the bot 'alive'."""
    import tempfile
    import time as _t
    from polybot.store import Store
    from polybot.ui_state import is_bot_alive, set_bot_active
    s = Store(tempfile.mktemp(suffix=".db"))
    # running: active flag + fresh heartbeat
    set_bot_active(s, True)
    s.set_meta("last_cycle_ts", str(_t.time()))
    s.record_equity(100.0, 0.0, 0.0)
    assert is_bot_alive(s, 60) is True
    # stopped: flag cleared. Equity snapshot is still recent but must not count.
    set_bot_active(s, False)
    assert is_bot_alive(s, 60) is False


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
