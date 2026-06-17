"""Probability math for price-target markets.

We model an asset price as a driftless geometric Brownian motion (GBM) — i.e.
we assume the current price is a fair ("martingale") estimate of the future
price, and only diffusion/volatility moves it from here. This is the standard,
neutral assumption for short horizons and avoids us pretending to predict
direction. The edge does NOT come from predicting drift; it comes from
recomputing the probability off the *live* spot price + volatility faster than
a stale order book updates.

Under driftless GBM with annualized volatility `sigma` over `tau` years:
    ln(S_T / S) ~ Normal(-0.5*sigma^2*tau, sigma^2*tau)

All functions return a probability in [0, 1].
"""
from __future__ import annotations

import math

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above_at_expiry(S: float, K: float, sigma: float, tau: float) -> float:
    """P(price at expiry >= K)."""
    if tau <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if S >= K else 0.0
    d = (math.log(S / K) - 0.5 * sigma * sigma * tau) / (sigma * math.sqrt(tau))
    return norm_cdf(d)


def prob_below_at_expiry(S: float, K: float, sigma: float, tau: float) -> float:
    """P(price at expiry <= K)."""
    return 1.0 - prob_above_at_expiry(S, K, sigma, tau)


def prob_touch(S: float, K: float, sigma: float, tau: float) -> float:
    """P(price reaches the barrier K at any time before expiry).

    Uses the first-passage-time formula for driftless GBM. Handles an upper
    barrier (K > S) or a lower barrier (K < S). Touch probability is always
    >= the terminal probability of finishing past the same level.
    """
    if tau <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 1.0 if (S >= K and K > 0) else 0.0
    s = sigma * math.sqrt(tau)
    nu = -0.5 * sigma * sigma            # drift of log-price
    if K > S:                            # upper barrier
        b = math.log(K / S)              # > 0
        p = (norm_cdf((-b + nu * tau) / s)
             + math.exp(2 * nu * b / (sigma * sigma)) * norm_cdf((-b - nu * tau) / s))
    elif K < S:                          # lower barrier
        b = math.log(K / S)              # < 0
        p = (norm_cdf((b - nu * tau) / s)
             + math.exp(2 * nu * b / (sigma * sigma)) * norm_cdf((b + nu * tau) / s))
    else:
        return 1.0
    return max(0.0, min(1.0, p))


def years_until(end_ts: float, now_ts: float) -> float:
    return max(0.0, (end_ts - now_ts) / SECONDS_PER_YEAR)
