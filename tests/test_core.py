"""Unit tests for the math the bot's money depends on.

Run with:  python -m pytest -q   (or)   python tests/test_core.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polybot.config import Config
from polybot.models import Level, Market, OrderBook
from polybot.risk import RiskManager
from polybot.strategy import Strategy


def make_book(token, bids, asks):
    ob = OrderBook(token_id=token, bids=[], asks=[])
    ob.bids = [Level(p, s) for p, s in bids]
    ob.asks = [Level(p, s) for p, s in asks]
    ob.bids.sort(key=lambda l: l.price, reverse=True)
    ob.asks.sort(key=lambda l: l.price)
    return ob


def make_market(**kw):
    base = dict(
        condition_id="c1", question="Q?", slug="q", outcomes=["Yes", "No"],
        token_ids=["YES", "NO"], outcome_prices=[0.5, 0.5], volume=10000,
        volume_24hr=5000, liquidity=40000, tick_size=0.01, min_order_size=5,
        neg_risk=False, one_day_price_change=0.0, end_date=None,
        taker_fee_rate=0.0, enable_order_book=True, accepting_orders=True,
        closed=False,
    )
    base.update(kw)
    return Market(**base)


def default_cfg(**strat):
    # Tests exercise arbitrage + the book-value signal, so enable book value
    # and disable the (network-dependent) crypto model.
    s = dict(arb_min_profit=0.01, min_edge=0.04, imbalance_coeff=0.03,
             momentum_coeff=0.30, depth_levels=8, min_confidence=0.0,
             enable_book_value=True, enable_crypto_model=False)
    s.update(strat)
    return Config(raw={"strategy": s, "fees": {"default_taker_rate": 0.0},
                       "risk": {}})


# ---------------------------------------------------------------- fills
def test_fill_buy_walks_levels_with_slippage():
    ob = make_book("t", bids=[], asks=[(0.50, 10), (0.52, 10), (0.55, 100)])
    shares, cost, avg = ob.fill_buy(15)
    assert shares == 15
    # 10 @ .50 + 5 @ .52 = 5.0 + 2.6 = 7.6
    assert abs(cost - 7.6) < 1e-9
    assert abs(avg - 7.6 / 15) < 1e-9


def test_fill_buy_partial_when_book_thin():
    ob = make_book("t", bids=[], asks=[(0.50, 5)])
    shares, cost, avg = ob.fill_buy(100)
    assert shares == 5
    assert abs(cost - 2.5) < 1e-9


def test_imbalance_sign():
    ob = make_book("t", bids=[(0.49, 100)], asks=[(0.51, 10)])
    assert ob.depth_imbalance(8) > 0  # more bid depth -> upward pressure


# ----------------------------------------------------------- arbitrage
def test_arbitrage_detected_when_asks_sum_below_one():
    strat = Strategy(default_cfg())
    m = make_market()
    yes = make_book("YES", bids=[(0.44, 50)], asks=[(0.45, 50)])
    no = make_book("NO", bids=[(0.49, 50)], asks=[(0.50, 50)])  # .45+.50=.95
    opps = strat.evaluate(m, {"YES": yes, "NO": no})
    assert len(opps) == 1
    assert opps[0].kind == "arbitrage"
    assert opps[0].edge > 0


def test_no_arbitrage_when_asks_sum_at_one():
    strat = Strategy(default_cfg())
    m = make_market()
    yes = make_book("YES", bids=[(0.49, 50)], asks=[(0.50, 50)])
    no = make_book("NO", bids=[(0.49, 50)], asks=[(0.50, 50)])  # sums to 1.00
    opps = strat.evaluate(m, {"YES": yes, "NO": no})
    assert all(o.kind != "arbitrage" for o in opps)


# ---------------------------------------------------------------- value
def test_value_signal_from_strong_imbalance_and_momentum():
    # Big upward pressure + positive momentum should make YES look underpriced.
    strat = Strategy(default_cfg(imbalance_coeff=0.10, momentum_coeff=1.0))
    m = make_market(one_day_price_change=0.05)
    yes = make_book("YES", bids=[(0.49, 1000)], asks=[(0.50, 1000)])
    no = make_book("NO", bids=[(0.50, 5)], asks=[(0.51, 5)])
    opps = strat.evaluate(m, {"YES": yes, "NO": no})
    assert any(o.kind == "value" for o in opps)


# ----------------------------------------------------------------- risk
def test_kelly_sizing_capped_by_max_fraction():
    cfg = Config(raw={"strategy": {"depth_levels": 8}, "risk": {
        "max_fraction_per_trade": 0.05, "kelly_fraction": 0.25,
        "max_total_exposure": 0.8, "max_open_positions": 15,
        "max_book_take_fraction": 1.0, "min_trade_usd": 1.0}})
    rm = RiskManager(cfg)
    strat = Strategy(default_cfg())
    m = make_market()
    yes = make_book("YES", bids=[(0.39, 1000)], asks=[(0.40, 1000)])
    no = make_book("NO", bids=[(0.59, 1000)], asks=[(0.60, 1000)])
    # fabricate a value opp with big edge
    from polybot.models import Leg, Opportunity
    opp = Opportunity(market=m, kind="value",
                      legs=[Leg("YES", "Yes", 0.40, yes)],
                      fair_value=0.70, edge=0.30, confidence=0.9)
    sized = rm.size(opp, bankroll=200, deployed_usd=0, open_positions=0)
    assert sized is not None
    # 5% of 200 = 10 max
    assert sized.target_usd <= 10.0 + 1e-9


def test_risk_blocks_when_max_positions_reached():
    cfg = Config(raw={"strategy": {}, "risk": {"max_open_positions": 3}})
    rm = RiskManager(cfg)
    m = make_market()
    from polybot.models import Leg, Opportunity
    opp = Opportunity(market=m, kind="value",
                      legs=[Leg("YES", "Yes", 0.40, make_book("YES", [], [(0.4, 10)]))],
                      fair_value=0.7, edge=0.3, confidence=0.9)
    assert rm.size(opp, 200, 0, open_positions=3) is None


def _run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ok  {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
