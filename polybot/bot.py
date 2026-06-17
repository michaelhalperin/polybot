"""The main loop: scan markets -> find edges -> size -> paper-trade -> manage.

One "cycle" does, in order:
  1. Manage existing open positions (resolve settled markets, take profit /
     stop loss on value positions, mark everything to market).
  2. Scan the market universe and fetch live order books.
  3. Evaluate every market for arbitrage / value opportunities, rank them.
  4. Size the best ones within risk limits and open paper positions.
  5. Record an equity snapshot and print a short dashboard.
"""
from __future__ import annotations

import time
from typing import Dict, List

from .api import clob, gamma
from .config import Config
from .execution import PaperBroker
from .log import attach_store_logger, get_logger
from .ui_state import set_bot_active, touch_cycle
from .models import Market, OrderBook
from .risk import RiskManager
from .store import Store
from .strategy import Strategy, rank_opportunities


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()
        self.store = Store(cfg.db_path)
        self.broker = PaperBroker(cfg, self.store)
        self.strategy = Strategy(cfg)
        self.risk = RiskManager(cfg)
        attach_store_logger(self.store)
        # Cached for the web UI: most recent scan results + bookkeeping.
        self.last_opportunities = []
        self.last_cycle_ts = 0.0
        self.cycle_count = 0

    # ------------------------------------------------------------------
    def run(self):
        set_bot_active(self.store, True)
        self.log.info("=" * 64)
        self.log.info("Polybot starting | mode=%s | bankroll=$%.2f | cash=$%.2f",
                      self.cfg.mode, self.cfg.bankroll, self.broker.cash)
        self.log.info("=" * 64)
        cycle = 0
        try:
            while True:
                cycle += 1
                self.log.info("--- cycle %d ---", cycle)
                try:
                    self.run_cycle()
                except Exception as e:  # never let one bad cycle kill the bot
                    self.log.exception("cycle error: %s", e)
                if self.cfg.max_cycles and cycle >= self.cfg.max_cycles:
                    self.log.info("Reached max_cycles=%d; stopping.", self.cfg.max_cycles)
                    break
                time.sleep(self.cfg.poll_interval)
        except KeyboardInterrupt:
            self.log.info("Interrupted by user; shutting down.")
        finally:
            set_bot_active(self.store, False)
            self.print_dashboard()
            self.store.close()

    # ------------------------------------------------------------------
    def find_opportunities(self):
        """Read-only: fetch markets + books and return (ranked opps, books).

        Shared by the trading loop and the UI's 'scan' button.
        """
        markets = gamma.fetch_active_markets(
            limit=int(self.cfg.universe.get("max_markets_scan", 250)),
            min_liquidity=float(self.cfg.universe.get("min_liquidity", 0)),
        )
        markets = self._filter_universe(markets)
        if not markets:
            return [], {}
        token_ids: List[str] = []
        for m in markets:
            token_ids.extend(m.token_ids)
        books = clob.fetch_books(token_ids)
        opps = []
        for m in markets:
            opps.extend(self.strategy.evaluate(m, books))
        return rank_opportunities(opps), books

    def run_cycle(self):
        self.cycle_count += 1
        self.manage_positions()
        opps, books = self.find_opportunities()
        self.last_opportunities = opps
        self.last_cycle_ts = time.time()
        touch_cycle(self.store, self.cycle_count, opps)
        if not opps and not books:
            self.log.info("No markets passed universe filters this cycle.")
            return
        self.log.info("Found %d opportunities.", len(opps))
        self._open_trades(opps)
        self.snapshot_equity(books)

    # ------------------------------------------------------------------
    def _filter_universe(self, markets: List[Market]) -> List[Market]:
        min_vol = float(self.cfg.universe.get("min_volume_24hr", 0))
        excl_hours = float(self.cfg.universe.get("exclude_closing_within_hours", 0))
        now = time.time()
        out = []
        for m in markets:
            if m.volume_24hr < min_vol:
                continue
            if excl_hours and m.end_date:
                end = _parse_iso(m.end_date)
                if end and (end - now) < excl_hours * 3600:
                    continue
            out.append(m)
        return out

    # ------------------------------------------------------------------
    def _open_trades(self, opps):
        open_positions = self.store.open_positions()
        deployed = sum(p["cost_usd"] for p in open_positions)
        n_open = len(open_positions)
        bankroll = self.broker.cash + self._positions_value({})

        # Tally current exposure per correlated theme (event), to cap pile-ups.
        theme_usd: Dict[str, float] = {}
        theme_n: Dict[str, int] = {}
        for p in open_positions:
            t = p["theme"] if ("theme" in p.keys() and p["theme"]) else p["condition_id"]
            theme_usd[t] = theme_usd.get(t, 0.0) + p["cost_usd"]
            theme_n[t] = theme_n.get(t, 0) + 1

        for opp in opps:
            # one position per market keeps things diversified & simple
            if self.store.has_open_position(opp.market.condition_id):
                continue
            theme = opp.market.theme
            sized = self.risk.size(
                opp, bankroll, deployed, n_open,
                theme_deployed_usd=theme_usd.get(theme, 0.0),
                theme_positions=theme_n.get(theme, 0),
            )
            if sized is None:
                continue
            if self.broker.open(sized):
                deployed += sized.target_usd
                n_open += 1
                theme_usd[theme] = theme_usd.get(theme, 0.0) + sized.target_usd
                theme_n[theme] = theme_n.get(theme, 0) + 1

    # ------------------------------------------------------------------
    def manage_positions(self):
        positions = self.store.open_positions()
        if not positions:
            return
        r = self.cfg.risk
        take_profit = float(r.get("take_profit", 0.40))
        stop_loss = float(r.get("stop_loss", 0.30))

        # Cache market resolution lookups per condition_id this cycle.
        resolved_cache: Dict[str, Market] = {}
        # Books for live MTM of value positions.
        tokens = [p["token_id"] for p in positions]
        books = clob.fetch_books(tokens)

        for pos in positions:
            cid = pos["condition_id"]
            if cid not in resolved_cache:
                resolved_cache[cid] = gamma.fetch_market(cid)
            market = resolved_cache[cid]

            # 1) Settle if the market has resolved.
            if market is not None and market.closed and _is_resolved(market):
                won = _token_won(market, pos["token_id"])
                self.broker.resolve(pos, won)
                continue

            # 2) Arbitrage legs are held to resolution; nothing else to do.
            if pos["kind"] == "arbitrage":
                continue

            # 3) Take-profit / stop-loss for value positions, marked at bid.
            book = books.get(pos["token_id"])
            bid = book.best_bid if book else None
            if bid is None:
                continue
            cur_value = pos["shares"] * bid
            ret = (cur_value - pos["cost_usd"]) / pos["cost_usd"] if pos["cost_usd"] else 0.0
            if ret >= take_profit:
                self.broker.close_at_price(pos, bid, f"take_profit {ret:+.0%}")
            elif ret <= -stop_loss:
                self.broker.close_at_price(pos, bid, f"stop_loss {ret:+.0%}")

    # ------------------------------------------------------------------
    def _positions_value(self, books: Dict[str, OrderBook]) -> float:
        """Liquidation value of open positions (at best bid; cost if unknown)."""
        total = 0.0
        for p in self.store.open_positions():
            book = books.get(p["token_id"])
            if book and book.best_bid is not None:
                total += p["shares"] * book.best_bid
            else:
                total += p["cost_usd"]  # fall back to cost basis
        return total

    def snapshot_equity(self, books: Dict[str, OrderBook]):
        pos_val = self._positions_value(books)
        realized = self.store.total_realized_pnl()
        self.store.record_equity(self.broker.cash, pos_val, realized)

    # ------------------------------------------------------------------
    def print_dashboard(self):
        s = self.store.stats()
        pos_val = self._positions_value({})
        total = self.broker.cash + pos_val
        start = float(self.store.get_meta("starting_bankroll") or self.cfg.bankroll)
        self.log.info("=" * 64)
        self.log.info("PORTFOLIO SUMMARY")
        self.log.info("  cash:            $%.2f", self.broker.cash)
        self.log.info("  open positions:  $%.2f", pos_val)
        self.log.info("  total equity:    $%.2f  (start $%.2f, %+.2f%%)",
                      total, start, (total / start - 1) * 100 if start else 0)
        self.log.info("  realized P&L:    $%+.2f", s["realized_pnl"])
        self.log.info("  closed trades:   %d  (win rate %.0f%%)",
                      s["closed"], s["win_rate"] * 100)
        self.log.info("=" * 64)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _parse_iso(s: str):
    import datetime
    try:
        s = s.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def _is_resolved(market: Market) -> bool:
    """A binary market is resolved once its outcome prices snap to ~0 and ~1."""
    if not market.outcome_prices:
        return False
    return max(market.outcome_prices) >= 0.99 and min(market.outcome_prices) <= 0.01


def _token_won(market: Market, token_id: str) -> bool:
    try:
        idx = market.token_ids.index(token_id)
    except ValueError:
        return False
    return market.outcome_prices[idx] >= 0.99
