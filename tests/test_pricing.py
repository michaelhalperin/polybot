"""Tests for the crypto fair-value model (pure math + parsing, no network)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from polybot import pricing
from polybot.crypto import _parse_threshold
from polybot.feeds import CryptoFeed


# ----------------------------------------------------------- pricing math
def test_prob_above_is_half_at_the_money_short_horizon():
    # spot == strike, tiny time -> ~50/50
    p = pricing.prob_above_at_expiry(100.0, 100.0, 0.5, 1e-4)
    assert 0.45 < p < 0.55


def test_prob_above_high_when_spot_far_above_strike():
    p = pricing.prob_above_at_expiry(100.0, 60.0, 0.5, 0.01)
    assert p > 0.99


def test_prob_above_low_when_spot_far_below_strike():
    p = pricing.prob_above_at_expiry(60.0, 100.0, 0.5, 0.01)
    assert p < 0.01


def test_above_and_below_sum_to_one():
    a = pricing.prob_above_at_expiry(100.0, 105.0, 0.6, 0.05)
    b = pricing.prob_below_at_expiry(100.0, 105.0, 0.6, 0.05)
    assert abs((a + b) - 1.0) < 1e-9


def test_touch_at_least_terminal_for_upper_barrier():
    S, K, sig, tau = 100.0, 110.0, 0.6, 0.1
    touch = pricing.prob_touch(S, K, sig, tau)
    term = pricing.prob_above_at_expiry(S, K, sig, tau)
    assert touch >= term - 1e-9
    assert 0.0 <= touch <= 1.0


def test_touch_bounds_for_lower_barrier():
    p = pricing.prob_touch(100.0, 90.0, 0.6, 0.1)
    assert 0.0 <= p <= 1.0
    # closer barrier -> higher touch probability
    near = pricing.prob_touch(100.0, 98.0, 0.6, 0.1)
    assert near >= p


def test_higher_vol_raises_otm_probability():
    low = pricing.prob_above_at_expiry(100.0, 130.0, 0.3, 0.1)
    high = pricing.prob_above_at_expiry(100.0, 130.0, 0.9, 0.1)
    assert high > low


# ----------------------------------------------------------- threshold parsing
def test_parse_plain_dollar_amount():
    assert _parse_threshold("be above $70,000 on June 17") == 70000.0


def test_parse_k_suffix():
    assert _parse_threshold("Bitcoin above $150k by 2026") == 150000.0


def test_parse_million_suffix():
    assert _parse_threshold("reach $1.2 million") == 1200000.0


def test_parse_none_when_no_number():
    assert _parse_threshold("Will Bitcoin go up?") is None


# ----------------------------------------------------------- symbol mapping
def test_symbol_mapping():
    f = CryptoFeed()
    assert f.symbol_for("Will the price of Bitcoin be above $70,000?") == "BTCUSDT"
    assert f.symbol_for("Will Ethereum be below $3000?") == "ETHUSDT"
    assert f.symbol_for("Will Trump withdraw troops?") is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
