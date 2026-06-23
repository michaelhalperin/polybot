"""Parse Polymarket crypto price-target markets and compute a real fair value.

Handles questions like:
  "Will the price of Bitcoin be above $70,000 on June 17?"   -> terminal above
  "Will Ethereum be below $3,000 on June 30?"                -> terminal below
  "Will Bitcoin hit $150,000 in 2026?"                       -> barrier touch

The fair value is computed from the LIVE exchange spot price and realized
volatility (see feeds.py) plus the no-drift GBM model (see pricing.py) — an
estimate that is genuinely independent of Polymarket's order book. The market's
own `end_date` gives the expiry, so we never have to parse dates from text.

If a market can't be parsed *confidently*, we return None and skip it. Being
conservative here is a feature: we only trade what we can actually price.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass
from typing import Optional

from . import pricing
from .feeds import CryptoFeed
from .models import Market

_NUM = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|thousand|m|mn|million|b|bn|billion)?",
                  re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
         "b": 1e9, "bn": 1e9, "billion": 1e9}

_ABOVE = re.compile(r"\b(above|over|exceed|exceeds|greater than|more than|at least|>=?|≥)\b", re.I)
_BELOW = re.compile(r"\b(below|under|less than|lower than|<=?|≤)\b", re.I)
_TOUCH = re.compile(r"\b(hit|hits|reach|reaches|touch|touches|dip to|dips to)\b", re.I)
_BETWEEN = re.compile(r"\bbetween\b", re.I)


@dataclass
class CryptoView:
    symbol: str
    spot: float
    strike: float
    sigma: float
    tau: float
    kind: str        # "above" | "below" | "touch"
    fair_yes: float  # model probability the YES outcome resolves true


def _parse_threshold(text: str) -> Optional[float]:
    m = _NUM.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = m.group(2)
    if mult:
        val *= _MULT.get(mult.lower(), 1.0)
    return val if val > 0 else None


def _end_ts(market: Market) -> Optional[float]:
    if not market.end_date:
        return None
    try:
        return datetime.datetime.fromisoformat(
            market.end_date.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def build_view(market: Market, symbol: str, strike: float, kind: str,
               feed: CryptoFeed, vol_scale: float = 1.0) -> Optional[CryptoView]:
    """Price a crypto market from an explicit (symbol, strike, kind) spec.

    Shared by the regex parser (`fair_view`) and the LLM understanding layer,
    so both compute fair value the same way from live spot + volatility.

    `vol_scale` multiplies the realized volatility before pricing — the single
    knob for any future calibration fix. NOTE: a backtest showed scaling did not
    improve the model (see config.yaml), so this defaults to 1.0 (no change).
    """
    if symbol is None or strike is None or strike <= 0 or kind not in ("above", "below", "touch"):
        return None
    end_ts = _end_ts(market)
    if end_ts is None:
        return None
    tau = pricing.years_until(end_ts, time.time())
    if tau <= 0:
        return None
    spot = feed.spot(symbol)
    if spot is None or spot <= 0:
        return None
    sigma = feed.volatility(symbol) * vol_scale
    if kind == "above":
        fair = pricing.prob_above_at_expiry(spot, strike, sigma, tau)
    elif kind == "below":
        fair = pricing.prob_below_at_expiry(spot, strike, sigma, tau)
    else:
        fair = pricing.prob_touch(spot, strike, sigma, tau)
    return CryptoView(symbol=symbol, spot=spot, strike=strike, sigma=sigma,
                      tau=tau, kind=kind, fair_yes=fair)


def fair_view(market: Market, feed: CryptoFeed, vol_scale: float = 1.0) -> Optional[CryptoView]:
    """Regex parser: detect a crypto price-target market and price it.

    Used when the LLM understanding layer is off or unavailable.
    """
    q = market.question
    symbol = feed.symbol_for(q)
    if symbol is None:
        return None
    if _BETWEEN.search(q):   # range markets: skip (not modeled)
        return None
    strike = _parse_threshold(q)
    if strike is None:
        return None
    if _ABOVE.search(q):
        kind = "above"
    elif _BELOW.search(q):
        kind = "below"
    elif _TOUCH.search(q):
        kind = "touch"
    else:
        return None
    return build_view(market, symbol, strike, kind, feed, vol_scale)
