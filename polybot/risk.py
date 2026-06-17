"""Position sizing and hard risk limits.

Sizing uses *fractional Kelly*. Full Kelly for a binary bet bought at price
`c` with win probability `p` is f* = (p - c) / (1 - c) of bankroll. Full Kelly
is famously too aggressive, so we scale it by `kelly_fraction` and cap it by
`max_fraction_per_trade`. Arbitrage is riskless, so it just deploys up to the
per-trade cap (limited only by available book depth).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .models import Opportunity


@dataclass
class SizedTrade:
    opportunity: Opportunity
    target_usd: float       # dollars we intend to deploy
    reason: str


class RiskManager:
    def __init__(self, cfg: Config):
        r = cfg.risk
        self.max_fraction_per_trade = float(r.get("max_fraction_per_trade", 0.05))
        self.kelly_fraction = float(r.get("kelly_fraction", 0.25))
        self.max_total_exposure = float(r.get("max_total_exposure", 0.80))
        self.max_open_positions = int(r.get("max_open_positions", 15))
        self.max_book_take_fraction = float(r.get("max_book_take_fraction", 0.10))
        self.min_trade_usd = float(r.get("min_trade_usd", 1.0))
        # Correlation control: cap exposure to any one event/theme.
        self.max_fraction_per_theme = float(r.get("max_fraction_per_theme", 0.15))
        self.max_positions_per_theme = int(r.get("max_positions_per_theme", 3))
        self.depth_levels = int(cfg.strategy.get("depth_levels", 8))

    def size(self, opp: Opportunity, bankroll: float, deployed_usd: float,
             open_positions: int, theme_deployed_usd: float = 0.0,
             theme_positions: int = 0) -> Optional[SizedTrade]:
        """Decide how many dollars to put on an opportunity, or None to skip."""
        if open_positions >= self.max_open_positions:
            return None
        # Correlation guard: don't pile into one event (the "12 Iran bets" trap).
        if theme_positions >= self.max_positions_per_theme:
            return None

        exposure_budget = self.max_total_exposure * bankroll - deployed_usd
        if exposure_budget <= self.min_trade_usd:
            return None
        theme_budget = self.max_fraction_per_theme * bankroll - theme_deployed_usd
        if theme_budget <= self.min_trade_usd:
            return None

        per_trade_cap = self.max_fraction_per_trade * bankroll

        if opp.kind == "arbitrage":
            target = per_trade_cap
            reason = "arb: deploy up to per-trade cap (riskless)"
        else:
            leg = opp.legs[0]
            price = leg.entry_price
            p = opp.fair_value
            if price >= 1.0:
                return None
            kelly = (p - price) / (1.0 - price)
            kelly = max(0.0, kelly)
            frac = min(self.kelly_fraction * kelly, self.max_fraction_per_trade)
            target = frac * bankroll
            reason = f"kelly {kelly:.3f} -> frac {frac:.4f}"

        # Respect overall exposure budget, per-trade cap, and per-theme cap.
        target = min(target, per_trade_cap, exposure_budget, theme_budget)

        # Respect book depth: don't try to take more than a fraction of what's
        # available across the legs (this is our slippage guard).
        depth_cap = self._depth_cap_usd(opp)
        target = min(target, depth_cap)

        if target < self.min_trade_usd:
            return None
        return SizedTrade(opportunity=opp, target_usd=target, reason=reason)

    def _depth_cap_usd(self, opp: Opportunity) -> float:
        """Max USD we allow given available depth on the thinnest leg."""
        caps = []
        for leg in opp.legs:
            shares = leg.book.available_buy_shares(self.depth_levels)
            allowed_shares = shares * self.max_book_take_fraction
            caps.append(allowed_shares * leg.entry_price)
        # For multi-leg (arb) the binding constraint is the smallest leg.
        return min(caps) if caps else 0.0
