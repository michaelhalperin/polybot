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

# How long past a market's end date to wait before settling a closed-but-not-
# cleanly-resolved market as a void/refund (resolution can lag by a day+).
VOID_GRACE_SECONDS = 48 * 3600


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
        # Set by the web runner so a long cycle (e.g. many LLM calls) can be
        # interrupted promptly when the user clicks Stop.
        self.should_stop = lambda: False

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
    def find_opportunities(self, record_shadow: bool = False):
        """Read-only*: fetch markets + books and return (ranked opps, books).

        Shared by the trading loop and the UI's 'scan' button. *When
        `record_shadow` is set (the trading loop), it also logs observe-only
        crypto-model predictions to the store — the UI scan leaves it off.
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
            if self.should_stop():   # bail out fast on Stop (between LLM calls)
                break
            opps.extend(self.strategy.evaluate(m, books))
        if record_shadow and self.strategy.enable_shadow:
            self._record_shadow(markets, books)
        return rank_opportunities(opps), books

    # ------------------------------------------------------------------
    def _record_shadow(self, markets: List[Market], books: Dict[str, OrderBook]):
        """Log first-sighting crypto-model predictions vs the live market."""
        recs = []
        for m in markets:
            rec = self.strategy.shadow_predict(m, books)
            if rec is not None:
                recs.append(rec)
        if recs:
            self.store.record_shadow_batch(recs)
            self.log.info("Shadow: recorded %d crypto prediction(s).", len(recs))

    def settle_shadow(self):
        """Fill in true outcomes for shadow predictions whose markets resolved."""
        if not self.strategy.enable_shadow:
            return
        rows = self.store.unsettled_shadow()
        now = time.time()
        # Only bother checking markets whose expected resolution has passed.
        due = [r for r in rows
               if r["resolution_ts"] is None or r["resolution_ts"] <= now]
        if not due:
            return
        markets = gamma.fetch_markets([r["condition_id"] for r in due])
        settled = 0
        for r in due:
            m = markets.get(r["condition_id"])
            if m is None or not m.closed or not _is_resolved(m):
                continue   # not cleanly resolved yet — retry a later cycle
            self.store.settle_shadow(
                r["condition_id"], 1 if _token_won(m, r["token_id"]) else 0)
            settled += 1
        if settled:
            self.log.info("Shadow: settled %d prediction(s).", settled)

    def run_cycle(self):
        self.cycle_count += 1
        self.manage_positions()
        self.settle_shadow()
        opps, books = self.find_opportunities(record_shadow=True)
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

        # One batched lookup of every market we hold (incl. closed ones),
        # instead of one Gamma call per position.
        markets = gamma.fetch_markets([p["condition_id"] for p in positions])
        # Books for live MTM of value positions.
        tokens = [p["token_id"] for p in positions]
        books = clob.fetch_books(tokens)

        for pos in positions:
            if self.should_stop():
                break
            market = markets.get(pos["condition_id"])

            # 1) Settle closed markets.
            if market is not None and market.closed:
                if _is_resolved(market):           # clean $1/$0 resolution
                    won = _token_won(market, pos["token_id"])
                    self.broker.resolve(pos, won)
                else:
                    # Closed but not cleanly resolved: void / 50-50 / refund.
                    # Wait out a grace period past the end date first, in case
                    # resolution is just lagging; then settle so capital isn't
                    # locked forever.
                    end_ts = _parse_iso(market.end_date)
                    if end_ts is None or (time.time() - end_ts) > VOID_GRACE_SECONDS:
                        self.broker.settle_void(pos, _token_price(market, pos["token_id"]))
                    # else: within grace -> leave open, retry next cycle
                continue

            # 2) Crypto-model and arbitrage bets are PROBABILITY bets — held to
            #    resolution (paid out $1 or $0). Applying day-trade take-profit/
            #    stop-loss to them just churns the bid/ask spread to death, so
            #    TP/SL is restricted to the (off-by-default) "value" strategy.
            if pos["kind"] != "value":
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


def _token_price(market: Market, token_id: str):
    """Resolved outcome price for our token (e.g. 0.5 on a 50/50), or None."""
    try:
        idx = market.token_ids.index(token_id)
        return float(market.outcome_prices[idx])
    except (ValueError, IndexError, TypeError):
        return None
