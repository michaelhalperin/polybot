"""Core data types: Market, OrderBook, and trade Opportunity.

These wrap the raw JSON returned by Polymarket's Gamma and CLOB APIs into
typed objects with the helper math the rest of the bot relies on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional


def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value) -> list:
    """Gamma encodes several list fields as JSON *strings*. Decode safely."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


# ----------------------------------------------------------------------
# Markets
# ----------------------------------------------------------------------
@dataclass
class Market:
    condition_id: str
    question: str
    slug: str
    outcomes: List[str]
    token_ids: List[str]
    outcome_prices: List[float]
    volume: float
    volume_24hr: float
    liquidity: float
    tick_size: float
    min_order_size: float
    neg_risk: bool
    one_day_price_change: float
    end_date: Optional[str]
    taker_fee_rate: float
    enable_order_book: bool
    accepting_orders: bool
    closed: bool
    event_slug: Optional[str] = None   # groups correlated markets (same event)
    description: str = ""              # resolution rules / fine print

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2 and len(self.token_ids) == 2

    def other_index(self, index: int) -> int:
        return 1 - index

    @classmethod
    def from_gamma(cls, d: dict) -> "Market":
        fee_rate = 0.0
        sched = d.get("feeSchedule")
        if isinstance(sched, dict) and d.get("feesEnabled"):
            fee_rate = _to_float(sched.get("rate"), 0.0)
        events = d.get("events") or []
        event_slug = None
        if isinstance(events, list) and events and isinstance(events[0], dict):
            event_slug = events[0].get("slug") or events[0].get("ticker")
        return cls(
            condition_id=d.get("conditionId", ""),
            question=d.get("question", ""),
            slug=d.get("slug", ""),
            outcomes=_json_list(d.get("outcomes")),
            token_ids=_json_list(d.get("clobTokenIds")),
            outcome_prices=[_to_float(p) for p in _json_list(d.get("outcomePrices"))],
            volume=_to_float(d.get("volumeNum", d.get("volume"))),
            volume_24hr=_to_float(d.get("volume24hr")),
            liquidity=_to_float(d.get("liquidityNum", d.get("liquidity"))),
            tick_size=_to_float(d.get("orderPriceMinTickSize"), 0.01),
            min_order_size=_to_float(d.get("orderMinSize"), 5.0),
            neg_risk=bool(d.get("negRisk", False)),
            one_day_price_change=_to_float(d.get("oneDayPriceChange")),
            end_date=d.get("endDate"),
            taker_fee_rate=fee_rate,
            enable_order_book=bool(d.get("enableOrderBook", False)),
            accepting_orders=bool(d.get("acceptingOrders", False)),
            closed=bool(d.get("closed", False)),
            event_slug=event_slug,
            description=d.get("description") or "",
        )

    @property
    def theme(self) -> str:
        """Correlation key: markets sharing an event move together."""
        return self.event_slug or self.condition_id


# ----------------------------------------------------------------------
# Order books
# ----------------------------------------------------------------------
@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[Level]  # highest price first (best bid at index 0)
    asks: List[Level]  # lowest price first (best ask at index 0)

    @classmethod
    def from_clob(cls, token_id: str, d: dict) -> "OrderBook":
        bids = [Level(_to_float(x.get("price")), _to_float(x.get("size")))
                for x in d.get("bids", [])]
        asks = [Level(_to_float(x.get("price")), _to_float(x.get("size")))
                for x in d.get("asks", [])]
        bids.sort(key=lambda l: l.price, reverse=True)
        asks.sort(key=lambda l: l.price)
        return cls(token_id=token_id, bids=bids, asks=asks)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid if self.best_bid is not None else self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def has_two_sides(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def depth_imbalance(self, levels: int) -> float:
        """+1 = all buying pressure, -1 = all selling pressure, 0 = balanced."""
        bid_vol = sum(l.size for l in self.bids[:levels])
        ask_vol = sum(l.size for l in self.asks[:levels])
        total = bid_vol + ask_vol
        if total <= 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def fill_buy(self, max_shares: float):
        """Simulate a market BUY walking the ask book.

        Returns (shares_filled, total_cost, avg_price). Models slippage:
        each level is consumed at its own price until `max_shares` is met
        or the book runs out.
        """
        remaining = max_shares
        cost = 0.0
        filled = 0.0
        for lvl in self.asks:
            if remaining <= 0:
                break
            take = min(remaining, lvl.size)
            cost += take * lvl.price
            filled += take
            remaining -= take
        avg = cost / filled if filled > 0 else 0.0
        return filled, cost, avg

    def fill_sell(self, max_shares: float):
        """Simulate a market SELL walking the bid book.

        Returns (shares_sold, total_proceeds, avg_price).
        """
        remaining = max_shares
        proceeds = 0.0
        sold = 0.0
        for lvl in self.bids:
            if remaining <= 0:
                break
            take = min(remaining, lvl.size)
            proceeds += take * lvl.price
            sold += take
            remaining -= take
        avg = proceeds / sold if sold > 0 else 0.0
        return sold, proceeds, avg

    def available_buy_shares(self, levels: int) -> float:
        return sum(l.size for l in self.asks[:levels])


# ----------------------------------------------------------------------
# Opportunities (what the strategy emits)
# ----------------------------------------------------------------------
@dataclass
class Leg:
    """One side to buy as part of an opportunity."""
    token_id: str
    side_name: str        # e.g. "Yes" / "No"
    entry_price: float    # best ask we'd pay
    book: OrderBook


@dataclass
class Opportunity:
    market: Market
    kind: str             # "arbitrage" | "value"
    legs: List[Leg]
    fair_value: float     # estimated win probability of the bought side
    edge: float           # expected profit per $1 deployed
    confidence: float     # 0..1 quality score
    notes: str = ""
    analysis: Optional[dict] = None   # AI verdict, when the LLM layer was used

    @property
    def score(self) -> float:
        """Ranking key: risk-adjusted edge."""
        return self.edge * self.confidence
