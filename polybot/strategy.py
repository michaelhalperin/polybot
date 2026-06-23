"""The decision engine: turns markets + order books into ranked Opportunities.

Edges, in priority order:

1. ARBITRAGE (riskless): In a binary market a "Yes" share and a "No" share
   together always pay out exactly $1 at resolution. If you can BUY both for
   less than $1 (asks sum < $1, after fees), the profit is locked in no
   matter the outcome. Rare, but essentially free money.

2. CRYPTO MODEL (real, independent edge): For crypto price-target markets
   ("Will BTC be above $X on <date>?") we compute a genuine probability from
   the LIVE exchange spot price + realized volatility (see crypto.py /
   feeds.py / pricing.py). Because this estimate does NOT come from
   Polymarket's order book, it can legitimately disagree with a stale market
   price. We bet only when the disagreement clears spread + fees + a margin.

3. BOOK VALUE (off by default): the original order-book imbalance + momentum
   signal. It has no proven predictive edge (it just reshuffles the price it's
   betting against) and tends to bleed the bid/ask spread, so it is disabled
   unless you explicitly turn `enable_book_value` on in config.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .config import Config
from .crypto import build_view, fair_view
from .feeds import CryptoFeed
from .log import get_logger
from .models import Leg, Market, OrderBook, Opportunity


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class Strategy:
    def __init__(self, cfg: Config):
        s = cfg.strategy
        self.arb_min_profit = float(s.get("arb_min_profit", 0.01))
        self.min_edge = float(s.get("min_edge", 0.04))
        self.imbalance_coeff = float(s.get("imbalance_coeff", 0.03))
        self.momentum_coeff = float(s.get("momentum_coeff", 0.30))
        self.depth_levels = int(s.get("depth_levels", 8))
        self.min_confidence = float(s.get("min_confidence", 0.25))
        self.default_fee = float(cfg.fees.get("default_taker_rate", 0.0))
        # New, edge-bearing settings
        self.enable_book_value = bool(s.get("enable_book_value", False))
        self.enable_crypto_model = bool(s.get("enable_crypto_model", True))
        self.crypto_min_edge = float(s.get("crypto_min_edge", 0.05))
        self.crypto_max_spread = float(s.get("crypto_max_spread", 0.08))
        # Relative spread guard: skip markets whose bid/ask gap is a large
        # fraction of the price. An absolute guard misses cheap markets where a
        # 1.6c spread on a 5c price is ~30% — exactly the illiquid longshots
        # that produced terrible fills.
        self.crypto_max_spread_pct = float(s.get("crypto_max_spread_pct", 0.20))
        # Don't trade at floor/ceiling prices. There, the market is ~certain and
        # the model's probability is least reliable — the source of absurd bets
        # like "8000 shares of NO at $0.001". Only trade priced 3c..97c.
        self.crypto_min_price = float(s.get("crypto_min_price", 0.03))
        self.crypto_max_price = float(s.get("crypto_max_price", 0.97))
        # Calibration knob: scales realized vol before pricing. 1.0 = unchanged.
        self.crypto_vol_scale = float(s.get("crypto_vol_scale", 1.0))
        self.feed = CryptoFeed() if self.enable_crypto_model else None
        # Optional AI market-understanding layer (off unless enabled + API key).
        self.understanding = None
        if s.get("enable_llm_understanding", False):
            from .understanding import MarketUnderstanding
            self.understanding = MarketUnderstanding(cfg)

    def _fee_rate(self, market: Market) -> float:
        return market.taker_fee_rate if market.taker_fee_rate > 0 else self.default_fee

    # ------------------------------------------------------------------
    def evaluate(self, market: Market,
                 books: Dict[str, OrderBook]) -> List[Opportunity]:
        """Return all opportunities found in one market (may be empty)."""
        if not market.is_binary:
            return []
        yes_book = books.get(market.token_ids[0])
        no_book = books.get(market.token_ids[1])
        if yes_book is None or no_book is None:
            return []

        # 1) Riskless arbitrage always wins if present.
        arb = self._arbitrage(market, yes_book, no_book)
        if arb:
            return [arb]

        # 2) Crypto model — a real, independent estimate.
        if self.enable_crypto_model:
            crypto = self._crypto(market, yes_book, no_book)
            if crypto:
                return crypto

        # 3) Order-book value signal (only if explicitly enabled).
        if self.enable_book_value:
            return self._value(market, yes_book, no_book)
        return []

    # ------------------------------------------------------------------
    def _crypto_view(self, market: Market):
        """Get a priceable crypto view + the AI analysis (if any).

        Returns (view, analysis). The LLM both (a) parses markets the regex
        can't and (b) gates out markets whose resolution rules are
        risky/ambiguous. Falls back to the regex parser when the LLM is off.
        """
        if self.understanding is not None:
            analysis = self.understanding.analyze(market)
            if analysis is not None:
                # Resolution-safety gate: skip anything tricky or non-crypto.
                if not analysis.tradeable or analysis.resolution_risk == "high":
                    return None, analysis
                if not analysis.is_crypto_price_target:
                    return None, analysis
                symbol = self.feed.symbol_for(analysis.asset) if analysis.asset else None
                view = build_view(market, symbol, analysis.threshold,
                                  analysis.direction, self.feed, self.crypto_vol_scale)
                return view, analysis
        return fair_view(market, self.feed, self.crypto_vol_scale), None

    def _crypto(self, market: Market, yes_book: OrderBook,
                no_book: OrderBook) -> List[Opportunity]:
        view, analysis = self._crypto_view(market)
        if view is None:
            return []
        adict = None
        if analysis is not None:
            adict = {"summary": analysis.summary,
                     "resolution_risk": analysis.resolution_risk,
                     "tradeable": analysis.tradeable, "verified": True}
        fee = self._fee_rate(market)
        fair_yes = _clip(view.fair_yes, 0.0, 1.0)
        candidates = [
            (yes_book, market.outcomes[0], fair_yes, market.token_ids[0]),
            (no_book, market.outcomes[1], 1.0 - fair_yes, market.token_ids[1]),
        ]
        opps: List[Opportunity] = []
        for book, name, prob, token in candidates:
            ask = book.best_ask
            if ask is None or ask <= 0 or ask >= 1:
                continue
            if ask < self.crypto_min_price or ask > self.crypto_max_price:
                continue  # extreme price -> model unreliable, skip
            spread = book.spread
            mid = book.mid
            if spread is not None and (
                    spread > self.crypto_max_spread
                    or (mid and spread / mid > self.crypto_max_spread_pct)):
                continue  # too wide / illiquid — bad fills, skip
            edge = (prob - ask) - fee * ask
            if edge < self.crypto_min_edge:
                continue
            # Confidence: edge size, decisiveness (far from 0.5), liquidity.
            conf = (0.5 * _clip(edge / (self.crypto_min_edge * 2), 0, 1)
                    + 0.3 * _clip(abs(prob - 0.5) * 2, 0, 1)
                    + 0.2 * _clip(market.liquidity / 50000.0, 0, 1))
            if conf < self.min_confidence:
                continue
            opps.append(Opportunity(
                market=market, kind="crypto",
                legs=[Leg(token, name, ask, book)],
                fair_value=prob, edge=edge, confidence=conf,
                notes=(f"crypto[{view.kind}]: {view.symbol} spot "
                       f"{view.spot:.0f} vs {view.strike:.0f}, vol {view.sigma:.0%}, "
                       f"model P(yes)={fair_yes:.3f}; buy {name} @ {ask:.3f}"),
                analysis=adict,
            ))
        return opps

    # ------------------------------------------------------------------
    def _arbitrage(self, market: Market, yes_book: OrderBook,
                   no_book: OrderBook) -> Optional[Opportunity]:
        ask_yes, ask_no = yes_book.best_ask, no_book.best_ask
        if ask_yes is None or ask_no is None:
            return None
        cost = ask_yes + ask_no
        fee = self._fee_rate(market)
        # Buying both legs: pay `cost`, plus taker fee on each leg's notional.
        profit = 1.0 - cost - fee * cost
        if profit < self.arb_min_profit:
            return None
        edge_per_dollar = profit / cost if cost > 0 else 0.0
        return Opportunity(
            market=market,
            kind="arbitrage",
            legs=[
                Leg(market.token_ids[0], market.outcomes[0], ask_yes, yes_book),
                Leg(market.token_ids[1], market.outcomes[1], ask_no, no_book),
            ],
            fair_value=1.0,
            edge=edge_per_dollar,
            confidence=0.99,
            notes=f"arb: yes {ask_yes:.3f} + no {ask_no:.3f} = {cost:.3f}",
        )

    # ------------------------------------------------------------------
    def _value(self, market: Market, yes_book: OrderBook,
               no_book: OrderBook) -> List[Opportunity]:
        if not yes_book.has_two_sides():
            return []
        mid = yes_book.mid
        if mid is None:
            return []

        # Fair value for the YES outcome, in [0,1].
        imbalance = yes_book.depth_imbalance(self.depth_levels)
        momentum = market.one_day_price_change  # already in price units
        fair_yes = _clip(
            mid
            + self.imbalance_coeff * imbalance
            + self.momentum_coeff * momentum,
            0.01, 0.99,
        )

        fee = self._fee_rate(market)
        candidates = [
            # (book to buy from, outcome name, prob the side wins, token)
            (yes_book, market.outcomes[0], fair_yes, market.token_ids[0]),
            (no_book, market.outcomes[1], 1.0 - fair_yes, market.token_ids[1]),
        ]

        opps: List[Opportunity] = []
        for book, name, prob, token in candidates:
            ask = book.best_ask
            if ask is None or ask <= 0 or ask >= 1:
                continue
            # Expected profit per share at resolution, net of taker fee.
            edge = (prob - ask) - fee * ask
            if edge < self.min_edge:
                continue
            confidence = self._confidence(edge, book, market)
            if confidence < self.min_confidence:
                continue
            opps.append(Opportunity(
                market=market,
                kind="value",
                legs=[Leg(token, name, ask, book)],
                fair_value=prob,
                edge=edge,
                confidence=confidence,
                notes=(f"value: buy {name} @ {ask:.3f}, fair {prob:.3f}, "
                       f"imb {imbalance:+.2f}, mom {momentum:+.3f}"),
            ))
        return opps

    # ------------------------------------------------------------------
    def _confidence(self, edge: float, book: OrderBook, market: Market) -> float:
        """Heuristic 0..1 quality score combining edge, spread and liquidity."""
        # Bigger edge -> more confident (saturating).
        edge_score = _clip(edge / (self.min_edge * 3), 0.0, 1.0)
        # Tighter spread -> the price signal is more trustworthy.
        spread = book.spread or 0.5
        spread_score = _clip(1.0 - spread / 0.10, 0.0, 1.0)
        # More liquidity -> estimate is more reliable.
        liq_score = _clip(market.liquidity / 50000.0, 0.0, 1.0)
        return 0.5 * edge_score + 0.25 * spread_score + 0.25 * liq_score


_KIND_RANK = {"arbitrage": 2, "crypto": 1, "value": 0}


def rank_opportunities(opps: List[Opportunity]) -> List[Opportunity]:
    """Best first. Riskless arbitrage first, then crypto-model, then value."""
    return sorted(
        opps,
        key=lambda o: (_KIND_RANK.get(o.kind, 0), o.score),
        reverse=True,
    )
