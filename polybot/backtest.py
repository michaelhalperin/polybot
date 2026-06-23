"""Offline measurement harness: does a strategy actually beat the market?

The live bot's only feedback loop is paper trades resolving slowly (a handful
per week). That is far too slow to tell whether a strategy has a real edge, and
far too slow to improve one. This module closes that gap.

Idea: Polymarket has *thousands* of already-resolved crypto price-target markets
("Will Bitcoin be above $X on <date>?"). For each one we know the true outcome.
Using public historical data we can reconstruct, at a chosen time *before*
resolution, three numbers:

    model_p   — what OUR model would have predicted (live spot + realized vol
                at that past moment, run through pricing.py)
    market_p  — what the market was charging at that moment (CLOB price history)
    outcome   — what actually happened (Gamma resolution)

With those three across hundreds of markets we can answer the only question that
matters: is `model_p` a *better* forecast of `outcome` than `market_p`? If the
market scores better (lower Brier / log loss), the model has no edge and betting
it is -EV. A betting simulation then turns that into a concrete P&L.

Data sources (all public, no auth):
  * Gamma  /markets?closed=true          -> resolved markets + outcomes
  * CLOB   /prices-history               -> the market's own price over time
  * Binance/Coinbase klines              -> historical spot + realized vol

Everything is offline and repeatable: no positions, no live trading.
"""
from __future__ import annotations

import datetime
import math
import re
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import pricing
from .api.http import get_json
from .feeds import SYMBOLS
from .log import get_logger

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com/api/v3/klines"
HOUR = 3600
VOL_LOOKBACK_HOURS = 24 * 14   # match the live feed's ~14-day realized-vol window

# Reuse the same parsing vocabulary the live strategy uses, so the backtest
# measures the *real* model, not a reimplementation of it.
_NUM = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(k|thousand|m|mn|million|b|bn|billion)?", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
         "b": 1e9, "bn": 1e9, "billion": 1e9}
_ABOVE = re.compile(r"\b(above|over|exceed|exceeds|greater than|more than|at least|>=?|≥)\b", re.I)
_BELOW = re.compile(r"\b(below|under|less than|lower than|<=?|≤)\b", re.I)
_TOUCH = re.compile(r"\b(hit|hits|reach|reaches|touch|touches|dip to|dips to)\b", re.I)
_BETWEEN = re.compile(r"\bbetween\b", re.I)


def _symbol_for(text: str) -> Optional[str]:
    t = text.lower()
    # Whole-word match: a bare substring check mistakes "Airbnb" for "bnb".
    for key, sym in SYMBOLS.items():
        if re.search(r"\b" + re.escape(key) + r"\b", t):
            return sym
    return None


def _parse_threshold(text: str) -> Optional[float]:
    m = _NUM.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if m.group(2):
        val *= _MULT.get(m.group(2).lower(), 1.0)
    return val if val > 0 else None


def _parse_kind(q: str) -> Optional[str]:
    if _BETWEEN.search(q):
        return None
    if _ABOVE.search(q):
        return "above"
    if _BELOW.search(q):
        return "below"
    if _TOUCH.search(q):
        return "touch"
    return None


def _end_ts(end_date: str) -> Optional[float]:
    try:
        return datetime.datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


# ----------------------------------------------------------------------
@dataclass
class ResolvedMarket:
    question: str
    symbol: str
    strike: float
    kind: str            # above | below | touch
    end_ts: float
    yes_token: str
    yes_won: bool


def fetch_resolved_crypto_markets(max_scan: int = 1500) -> List[ResolvedMarket]:
    """Page Gamma's closed markets and keep the crypto price-target Yes/No ones."""
    log = get_logger()
    out: List[ResolvedMarket] = []
    seen = 0
    for offset in range(0, max_scan, 100):
        data = get_json(f"{GAMMA}/markets", params={
            "limit": 100, "offset": offset, "closed": "true",
            "order": "volume", "ascending": "false",
        })
        if not data:
            break
        seen += len(data)
        for d in data:
            rm = _to_resolved(d)
            if rm is not None:
                out.append(rm)
        if len(data) < 100:
            break
    log.info("Backtest: scanned %d closed markets, %d usable crypto targets", seen, len(out))
    return out


def _to_resolved(d: dict) -> Optional[ResolvedMarket]:
    import json
    q = d.get("question", "")
    if not q:
        return None
    symbol = _symbol_for(q)
    if symbol is None:
        return None
    kind = _parse_kind(q)
    strike = _parse_threshold(q)
    if kind is None or strike is None:
        return None
    outcomes = d.get("outcomes")
    prices = d.get("outcomePrices")
    tokens = d.get("clobTokenIds")
    try:
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
    except (json.JSONDecodeError, TypeError):
        return None
    if not (outcomes and prices and tokens) or len(outcomes) != 2:
        return None
    # Only clean binary Yes/No markets with a decisive 1/0 resolution.
    if {str(o).lower() for o in outcomes} != {"yes", "no"}:
        return None
    try:
        p_yes = float(prices[0]) if str(outcomes[0]).lower() == "yes" else float(prices[1])
    except (ValueError, TypeError):
        return None
    if p_yes not in (0.0, 1.0):
        return None  # not cleanly resolved (void/refund) — skip
    yes_idx = 0 if str(outcomes[0]).lower() == "yes" else 1
    end_ts = _end_ts(d.get("endDate"))
    if end_ts is None:
        return None
    return ResolvedMarket(
        question=q, symbol=symbol, strike=strike, kind=kind, end_ts=end_ts,
        yes_token=str(tokens[yes_idx]), yes_won=(p_yes == 1.0),
    )


# ----------------------------------------------------------------------
class HistoricalFeed:
    """Historical hourly closes per symbol, fetched once and sliced locally.

    Mirrors the live feed's volatility methodology (annualized realized vol from
    hourly log-returns) so the backtest scores the *same* model the bot runs.
    """

    def __init__(self):
        self.log = get_logger()
        self._closes: Dict[str, List[tuple]] = {}   # symbol -> [(ts, close), ...] ascending

    def ensure(self, symbol: str, start_ts: float, end_ts: float):
        if symbol in self._closes:
            return
        rows: List[tuple] = []
        cur = int(start_ts * 1000)
        end_ms = int(end_ts * 1000)
        while cur < end_ms:
            data = get_json(BINANCE, params={
                "symbol": symbol, "interval": "1h",
                "startTime": cur, "endTime": end_ms, "limit": 1000,
            })
            if not isinstance(data, list) or not data:
                break
            for row in data:
                rows.append((row[0] / 1000.0, float(row[4])))
            nxt = data[-1][0] + HOUR * 1000
            if nxt <= cur:
                break
            cur = nxt
            if len(data) < 1000:
                break
        rows.sort()
        self._closes[symbol] = rows
        self.log.info("Backtest: loaded %d hourly closes for %s", len(rows), symbol)

    def spot(self, symbol: str, ts: float) -> Optional[float]:
        rows = self._closes.get(symbol)
        if not rows:
            return None
        # last close at or before ts
        prev = None
        for t, c in rows:
            if t > ts:
                break
            prev = c
        return prev

    def volatility(self, symbol: str, ts: float) -> Optional[float]:
        rows = self._closes.get(symbol)
        if not rows:
            return None
        lo = ts - VOL_LOOKBACK_HOURS * HOUR
        window = [c for t, c in rows if lo <= t <= ts]
        if len(window) < 24:
            return None
        rets = [math.log(window[i] / window[i - 1])
                for i in range(1, len(window)) if window[i - 1] > 0]
        if len(rets) < 10:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        annual = math.sqrt(var) * math.sqrt(24 * 365)
        return max(0.05, min(3.0, annual))


def fetch_price_history(token: str, end_ts: float, days_back: float = 45.0) -> List[dict]:
    """Full YES price history for a market over the `days_back` before resolution."""
    data = get_json(f"{CLOB}/prices-history", params={
        "market": token, "startTs": int(end_ts - days_back * 86400),
        "endTs": int(end_ts), "fidelity": 60,
    })
    hist = (data or {}).get("history") if isinstance(data, dict) else None
    return hist or []


def price_at(hist: List[dict], ts: float, tol_hours: float = 3.0) -> Optional[float]:
    """The market price at (close to) `ts` from a fetched history. Same-instant,
    tight tolerance — no reaching across hours to a different price regime."""
    if not hist:
        return None
    best = min(hist, key=lambda p: abs(p["t"] - ts))
    if abs(best["t"] - ts) > tol_hours * HOUR:
        return None
    p = float(best["p"])
    return p if 0.0 < p < 1.0 else None


def _history_is_tradeable(hist: List[dict], min_days: float, min_points: int) -> bool:
    """Reject markets with too little real price discovery. A history pinned at a
    single value (e.g. the 0.50 default) or spanning only a few hours is not a
    real market to forecast against."""
    if len(hist) < min_points:
        return False
    ts = [p["t"] for p in hist]
    if (max(ts) - min(ts)) < min_days * 86400:
        return False
    ps = [float(p["p"]) for p in hist]
    if statistics.pstdev(ps) < 0.01:   # essentially flat -> no price discovery
        return False
    return True


# ----------------------------------------------------------------------
def _model_p_yes(rm: ResolvedMarket, feed: HistoricalFeed, at_ts: float,
                 vol_scale: float = 1.0) -> Optional[float]:
    spot = feed.spot(rm.symbol, at_ts)
    sigma = feed.volatility(rm.symbol, at_ts)
    if spot is None or sigma is None:
        return None
    sigma *= vol_scale
    tau = pricing.years_until(rm.end_ts, at_ts)
    if tau <= 0:
        return None
    if rm.kind == "above":
        return pricing.prob_above_at_expiry(spot, rm.strike, sigma, tau)
    if rm.kind == "below":
        return pricing.prob_below_at_expiry(spot, rm.strike, sigma, tau)
    return pricing.prob_touch(spot, rm.strike, sigma, tau)


@dataclass
class Sample:
    rm: ResolvedMarket
    model_p: float    # model P(yes)
    market_p: float   # market P(yes)
    outcome: int      # 1 if yes won else 0


def build_samples(markets: List[ResolvedMarket], horizon_hours: float = 48.0,
                  min_uncertainty: float = 0.10, min_history_days: float = 2.0,
                  min_points: int = 12, vol_scale: float = 1.0) -> List[Sample]:
    """Reconstruct (model_p, market_p, outcome) at `horizon_hours` before
    resolution — hardened against the biases the first run exposed:

      * model spot/vol and market price are read at the SAME instant;
      * only markets with real price discovery are kept (min span / points /
        non-flat), excluding stale default-priced markets;
      * only moments of genuine uncertainty are scored (market price in
        [min_uncertainty, 1-min_uncertainty]), excluding the near-settled zone
        where the outcome is already obvious from spot.
    """
    log = get_logger()
    feed = HistoricalFeed()
    by_symbol: Dict[str, List[ResolvedMarket]] = {}
    for rm in markets:
        by_symbol.setdefault(rm.symbol, []).append(rm)
    for symbol, rms in by_symbol.items():
        ends = [r.end_ts for r in rms]
        start = min(ends) - (VOL_LOOKBACK_HOURS + horizon_hours + 48) * HOUR
        feed.ensure(symbol, start, max(ends) + HOUR)

    samples: List[Sample] = []
    drops = {"no_history": 0, "thin": 0, "no_price_at_t": 0, "near_settled": 0, "no_model": 0}
    for rm in markets:
        hist = fetch_price_history(rm.yes_token, rm.end_ts)
        if not hist:
            drops["no_history"] += 1
            continue
        if not _history_is_tradeable(hist, min_history_days, min_points):
            drops["thin"] += 1
            continue
        at = rm.end_ts - horizon_hours * HOUR
        market_p = price_at(hist, at)
        if market_p is None:
            drops["no_price_at_t"] += 1
            continue
        if not (min_uncertainty <= market_p <= 1 - min_uncertainty):
            drops["near_settled"] += 1
            continue
        model_p = _model_p_yes(rm, feed, at, vol_scale)
        if model_p is None:
            drops["no_model"] += 1
            continue
        samples.append(Sample(rm=rm, model_p=_clip01(model_p),
                              market_p=market_p, outcome=1 if rm.yes_won else 0))
    log.info("Backtest: %d scored samples of %d markets (dropped: %s)",
             len(samples), len(markets), drops)
    return samples


def _clip01(p: float) -> float:
    return max(1e-6, min(1 - 1e-6, p))


# ----------------------------------------------------------------------
def _brier(ps, ys) -> float:
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def _logloss(ps, ys) -> float:
    return -sum(y * math.log(_clip01(p)) + (1 - y) * math.log(1 - _clip01(p))
                for p, y in zip(ps, ys)) / len(ps)


def simulate_betting(samples: List[Sample], min_edge: float, fee: float = 0.0) -> dict:
    """Trade the model's disagreement with the market; settle at the true outcome.

    For each market: if model says YES is underpriced by > min_edge, buy YES at
    the market price; if NO is underpriced, buy NO. P&L per $1 staked = payoff
    (1 if that side won else 0) minus entry price, minus fee. This is the bot's
    actual logic, measured against ground truth.
    """
    pnl = 0.0
    n = wins = 0
    for s in samples:
        edge_yes = s.model_p - s.market_p
        if edge_yes > min_edge:
            entry, won = s.market_p, s.outcome == 1
        elif -edge_yes > min_edge:
            entry, won = 1 - s.market_p, s.outcome == 0
        else:
            continue
        n += 1
        wins += 1 if won else 0
        pnl += (1.0 - entry if won else -entry) - fee * entry
    return {"min_edge": min_edge, "bets": n, "wins": wins,
            "win_rate": wins / n if n else 0.0,
            "total_pnl": pnl, "avg_pnl": pnl / n if n else 0.0,
            "roi": pnl / n if n else 0.0}


def run_backtest(max_scan: int = 1500, horizon_hours: float = 48.0,
                 vol_scale: float = 1.0) -> dict:
    log = get_logger()
    markets = fetch_resolved_crypto_markets(max_scan)
    samples = build_samples(markets, horizon_hours, vol_scale=vol_scale)
    if not samples:
        log.warning("Backtest: no scored samples — try a larger --max-scan.")
        return {"samples": 0}

    ys = [s.outcome for s in samples]
    model_ps = [s.model_p for s in samples]
    market_ps = [s.market_p for s in samples]
    base_rate = sum(ys) / len(ys)

    result = {
        "samples": len(samples),
        "horizon_hours": horizon_hours,
        "base_rate_yes": base_rate,
        "model": {"brier": _brier(model_ps, ys), "logloss": _logloss(model_ps, ys),
                  "avg_p": statistics.mean(model_ps)},
        "market": {"brier": _brier(market_ps, ys), "logloss": _logloss(market_ps, ys),
                   "avg_p": statistics.mean(market_ps)},
        "betting": [simulate_betting(samples, e) for e in (0.03, 0.05, 0.10, 0.15, 0.20)],
    }
    return result


def format_result(r: dict) -> str:
    if not r.get("samples"):
        return "No samples — increase --max-scan or widen --horizon-hours."
    L = []
    L.append("=" * 70)
    L.append(f"BACKTEST — {r['samples']} resolved crypto markets, "
             f"scored {r['horizon_hours']:.0f}h before resolution")
    L.append("=" * 70)
    L.append(f"  base rate (YES won): {r['base_rate_yes']:.1%}")
    L.append("")
    L.append(f"  {'forecaster':<10}{'Brier':>10}{'log loss':>12}{'avg P(yes)':>13}")
    L.append("  " + "-" * 43)
    for who in ("model", "market"):
        d = r[who]
        L.append(f"  {who:<10}{d['brier']:>10.4f}{d['logloss']:>12.4f}{d['avg_p']:>13.3f}")
    better = "market" if r["market"]["brier"] < r["model"]["brier"] else "model"
    L.append("")
    L.append(f"  >> Better forecaster (lower Brier): {better.upper()}")
    L.append("")
    L.append("  Betting the model's disagreement with the market:")
    L.append(f"  {'min_edge':>9}{'bets':>7}{'win%':>8}{'avg P&L/$':>12}{'total P&L':>12}")
    L.append("  " + "-" * 48)
    for b in r["betting"]:
        L.append(f"  {b['min_edge']:>9.2f}{b['bets']:>7}{b['win_rate']*100:>7.0f}%"
                 f"{b['avg_pnl']:>12.4f}{b['total_pnl']:>12.2f}")
    L.append("=" * 70)
    L.append("  Positive avg P&L/$ at a given edge = the model adds value there.")
    L.append("  Negative = following the model is worse than leaving money in cash.")
    L.append("=" * 70)
    return "\n".join(L)
