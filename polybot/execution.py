"""Paper-trading broker: simulates real fills against the live order book.

No real money or wallet is ever touched. Buys walk the ask side and sells
walk the bid side, so reported prices include realistic slippage. Taker fees
(per-market when present) are applied to every fill.
"""
from __future__ import annotations

import time
import uuid

from .config import Config
from .log import get_logger
from .models import Opportunity
from .risk import SizedTrade
from .store import Store

CASH_KEY = "cash"


class PaperBroker:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.log = get_logger()
        existing = store.get_meta(CASH_KEY)
        if existing is None:
            store.set_meta(CASH_KEY, cfg.bankroll)
            store.set_meta("starting_bankroll", cfg.bankroll)
            self.cash = cfg.bankroll
        else:
            self.cash = float(existing)

    def _save_cash(self):
        self.store.set_meta(CASH_KEY, self.cash)

    def _fee_rate(self, opp: Opportunity) -> float:
        m = opp.market
        if m.taker_fee_rate > 0:
            return m.taker_fee_rate
        return float(self.cfg.fees.get("default_taker_rate", 0.0))

    # ------------------------------------------------------------------
    def open(self, sized: SizedTrade) -> bool:
        """Execute the buy leg(s) of an opportunity as paper fills."""
        opp = sized.opportunity
        fee_rate = self._fee_rate(opp)
        group_id = uuid.uuid4().hex if opp.kind == "arbitrage" else None

        if opp.kind == "arbitrage":
            combined_price = sum(l.entry_price for l in opp.legs)
            if combined_price <= 0:
                return False
            target_shares = sized.target_usd / combined_price
            # Fill each leg, then balance to the smallest fill.
            fills = []
            for leg in opp.legs:
                shares, cost, avg = leg.book.fill_buy(target_shares)
                fills.append((leg, shares, avg))
            min_shares = min(f[1] for f in fills)
            if min_shares <= 0:
                return False
            legs_to_book = [(leg, min_shares, avg) for (leg, _, avg) in fills]
        else:
            leg = opp.legs[0]
            target_shares = sized.target_usd / leg.entry_price
            shares, cost, avg = leg.book.fill_buy(target_shares)
            if shares <= 0:
                return False
            legs_to_book = [(leg, shares, avg)]

        total_cost = 0.0
        for leg, shares, avg in legs_to_book:
            cost = shares * avg
            fee = cost * fee_rate
            total_cost += cost + fee

        if total_cost > self.cash:
            self.log.debug("Skip %s: cost %.2f > cash %.2f",
                           opp.market.slug, total_cost, self.cash)
            return False

        for leg, shares, avg in legs_to_book:
            cost = shares * avg
            fee = cost * fee_rate
            avg_with_fee = (cost + fee) / shares if shares else avg
            self.store.add_position(
                condition_id=opp.market.condition_id,
                token_id=leg.token_id,
                market=opp.market.question[:200],
                side_name=leg.side_name,
                kind=opp.kind,
                shares=shares,
                avg_cost=avg_with_fee,
                cost_usd=cost + fee,
                fair_at_entry=opp.fair_value,
                group_id=group_id,
                theme=opp.market.theme,
                ai_verified=1 if opp.analysis else 0,
                status="open",
                opened_at=time.time(),
            )
            self.store.add_trade(
                ts=time.time(), condition_id=opp.market.condition_id,
                token_id=leg.token_id, side="buy", shares=shares, price=avg,
                fee=fee, usd=cost + fee, note=opp.notes,
            )
            self.cash -= (cost + fee)

        self._save_cash()
        self.log.info(
            "OPEN  %-9s %-6s x%.1f @%.3f  $%.2f  | %s",
            opp.kind, legs_to_book[0][0].side_name,
            legs_to_book[0][1], legs_to_book[0][2], total_cost,
            opp.market.question[:60],
        )
        return True

    # ------------------------------------------------------------------
    def close_at_price(self, pos, price: float, reason: str, fee_rate=None):
        """Close a position by selling all shares at `price` (paper).

        `fee_rate=None` uses the configured taker fee (a normal sell). Pass 0.0
        for settlement/void — at resolution there is no trade and no taker fee.
        """
        if fee_rate is None:
            fee_rate = float(self.cfg.fees.get("default_taker_rate", 0.0))
        shares = pos["shares"]
        gross = shares * price
        fee = gross * fee_rate
        proceeds = gross - fee
        realized = proceeds - pos["cost_usd"]
        self.cash += proceeds
        self._save_cash()
        self.store.close_position(pos["id"], price, proceeds, realized, reason)
        self.store.add_trade(
            ts=time.time(), condition_id=pos["condition_id"],
            token_id=pos["token_id"], side="sell", shares=shares, price=price,
            fee=fee, usd=proceeds, note=reason,
        )
        self.log.info(
            "CLOSE %-6s x%.1f @%.3f  pnl $%+.2f (%s) | %s",
            pos["side_name"], shares, price, realized, reason, pos["market"][:50],
        )
        return realized

    def resolve(self, pos, won: bool):
        """Settle a position at market resolution: $1/share if it won else $0.

        No taker fee — settlement is a payout, not a trade.
        """
        price = 1.0 if won else 0.0
        return self.close_at_price(pos, price, "resolved", fee_rate=0.0)

    def settle_void(self, pos, token_price):
        """Settle a closed market that did NOT resolve cleanly to $1/$0.

        Covers 50/50 resolutions and voids/refunds. If the token carries a sane
        resolved price (e.g. 0.5), pay that; otherwise refund the cost basis so
        capital isn't locked forever (realized P&L ~0). Marked separately so it
        does not pollute the calibration table.
        """
        if token_price is not None and 0.01 < token_price < 0.99:
            return self.close_at_price(pos, token_price, "voided", fee_rate=0.0)
        # No usable resolution price -> refund what was paid (avg_cost incl. fee).
        return self.close_at_price(pos, pos["avg_cost"], "refunded", fee_rate=0.0)
