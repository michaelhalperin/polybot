"""Flask web server: control the bot and watch it work from a browser.

A single BotRunner owns one Bot instance and runs its cycle loop in a
background thread. The Flask routes only read state (from SQLite + cached
objects) or send control commands, so the UI never blocks the bot.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time

from flask import Flask, Response, jsonify, request

from ..api import clob
from ..bot import Bot
from ..config import Config, load_config
from ..log import LOG_BUFFER, get_logger
from ..store import Store
from ..ui_state import (
    is_bot_alive,
    load_logs,
    load_opportunities,
    opp_to_dict,
    set_bot_active,
    touch_cycle,
)

BOOK_CACHE_TTL = 8  # seconds; avoid refetching books on every UI poll

HERE = os.path.dirname(os.path.abspath(__file__))


class BotRunner:
    """Owns the bot and runs its loop in a controllable background thread."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bot = Bot(cfg)
        self.log = get_logger()
        self._thread = None
        self._stop = threading.Event()
        self._cycle_lock = threading.Lock()  # only one cycle at a time
        self.running = False

    def start(self):
        if self.running:
            return False
        self._stop.clear()
        self.running = True
        set_bot_active(self.bot.store, True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.log.info(">>> bot loop STARTED (interval %ds)", self.cfg.poll_interval)
        return True

    def stop(self):
        if not self.running:
            return False
        self._stop.set()
        self.log.info(">>> stop requested; finishing current cycle...")
        return True

    def run_once(self):
        """Run exactly one cycle now (used when the loop is stopped)."""
        if self.running:
            return False
        threading.Thread(target=self._one_cycle, daemon=True).start()
        return True

    def _one_cycle(self):
        with self._cycle_lock:
            try:
                self.bot.run_cycle()
            except Exception as e:
                self.log.exception("cycle error: %s", e)

    def _loop(self):
        try:
            while not self._stop.is_set():
                with self._cycle_lock:
                    try:
                        self.bot.run_cycle()
                    except Exception as e:
                        self.log.exception("cycle error: %s", e)
                # interruptible sleep so Stop responds quickly
                self._stop.wait(self.cfg.poll_interval)
        finally:
            self.running = False
            set_bot_active(self.bot.store, False)
            self.log.info(">>> bot loop STOPPED")


# ----------------------------------------------------------------------
def create_app(cfg: Config, runner: BotRunner) -> Flask:
    app = Flask(__name__, static_folder=None)

    def store() -> Store:
        # short-lived read connection per request (WAL allows concurrent reads)
        return Store(cfg.db_path)

    # Cache of current best bids (liquidation prices) keyed by token id, so the
    # UI can poll frequently without refetching order books every time.
    bid_cache: dict = {}

    def live_bids(tokens):
        now = time.time()
        stale = [t for t in tokens
                 if now - bid_cache.get(t, (0, None))[0] > BOOK_CACHE_TTL]
        if stale:
            books = clob.fetch_books(stale)
            for t in stale:
                b = books.get(t)
                bid_cache[t] = (now, b.best_bid if b else None)
        return {t: bid_cache.get(t, (0, None))[1] for t in tokens}

    # Optional password protection. Set POLYBOT_PASSWORD on any public host
    # (e.g. Render) so only you can view/control the dashboard. Off locally.
    password = os.environ.get("POLYBOT_PASSWORD")

    @app.before_request
    def _require_auth():
        if not password or request.path == "/healthz":
            return None
        auth = request.authorization
        if not auth or not hmac.compare_digest(auth.password or "", password):
            return Response("Authentication required.", 401,
                            {"WWW-Authenticate": 'Basic realm="polybot"'})
        return None

    @app.route("/healthz")
    def healthz():
        return "ok"

    @app.route("/")
    def index():
        with open(os.path.join(HERE, "index.html")) as f:
            return f.read()

    @app.route("/api/status")
    def api_status():
        st = store()
        s = st.stats()
        eq = st.latest_equity()
        cash = float(st.get_meta("cash") or cfg.bankroll)
        start = float(st.get_meta("starting_bankroll") or cfg.bankroll)
        open_pos = st.open_positions()
        if eq:
            cash = eq["cash"]
            pos_val = eq["positions_val"]
            total = eq["total"]
        else:
            pos_val = sum(p["cost_usd"] for p in open_pos)
            total = cash + pos_val
        external = is_bot_alive(st, cfg.poll_interval) and not runner.running
        running = runner.running or external
        db_cycle = int(st.get_meta("cycle_count") or 0)
        db_cycle_ts = float(st.get_meta("last_cycle_ts") or 0)
        st.close()
        cycle_count = runner.bot.cycle_count if runner.running else db_cycle
        last_cycle_ts = runner.bot.last_cycle_ts if runner.running else db_cycle_ts
        return jsonify({
            "mode": cfg.mode,
            "running": running,
            "external": external,
            "cash": cash,
            "positions_val": pos_val,
            "total": total,
            "start_bankroll": start,
            "pct": (total / start - 1) * 100 if start else 0,
            "realized_pnl": s["realized_pnl"],
            "closed": s["closed"],
            "win_rate": s["win_rate"] * 100,
            "open_count": len(open_pos),
            "poll_interval": cfg.poll_interval,
            "cycle_count": cycle_count,
            "last_cycle_ts": last_cycle_ts,
            "ai_active": _ai_active(runner),
        })

    @app.route("/api/ai")
    def api_ai():
        """What the AI understanding layer has been doing."""
        u = getattr(runner.bot.strategy, "understanding", None)
        st = store()
        stats = st.analysis_stats()
        recent = []
        for r in st.recent_analyses(40):
            try:
                d = json.loads(r["json"])
            except (ValueError, TypeError):
                continue
            recent.append({
                "question": r["question"], "ts": r["ts"],
                "tradeable": d.get("tradeable"),
                "resolution_risk": d.get("resolution_risk"),
                "is_crypto": d.get("is_crypto_price_target"),
                "asset": d.get("asset"), "threshold": d.get("threshold"),
                "direction": d.get("direction"), "summary": d.get("summary"),
            })
        st.close()
        return jsonify({
            "active": _ai_active(runner),
            "enabled": bool(u and u.enabled),
            "model": cfg.strategy.get("llm_model", "claude-haiku-4-5"),
            "stats": stats,
            "recent": recent,
        })

    @app.route("/api/positions")
    def api_positions():
        st = store()
        rows = [dict(p) for p in st.open_positions()]
        st.close()
        bids = live_bids([r["token_id"] for r in rows])
        for r in rows:
            r["age_min"] = (time.time() - r["opened_at"]) / 60.0
            bid = bids.get(r["token_id"])
            if bid is not None:
                r["cur_price"] = bid
                r["cur_value"] = r["shares"] * bid
                r["pnl"] = r["cur_value"] - r["cost_usd"]
                r["pnl_pct"] = (r["pnl"] / r["cost_usd"] * 100) if r["cost_usd"] else 0.0
            else:
                # no live bid available -> mark flat at cost basis
                r["cur_price"] = None
                r["cur_value"] = r["cost_usd"]
                r["pnl"] = 0.0
                r["pnl_pct"] = 0.0
        return jsonify(rows)

    @app.route("/api/trades")
    def api_trades():
        limit = int(request.args.get("limit", 40))
        st = store()
        rows = [dict(t) for t in st.recent_trades(limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/equity")
    def api_equity():
        st = store()
        rows = [dict(e) for e in st.equity_curve()]
        st.close()
        return jsonify(rows)

    @app.route("/api/opportunities")
    def api_opportunities():
        if runner.running and runner.bot.last_opportunities:
            rows = [_opp_dict(o) for o in runner.bot.last_opportunities[:30]]
        else:
            st = store()
            rows = load_opportunities(st)
            st.close()
        return jsonify(rows)

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        """Fresh read-only scan (does not trade)."""
        opps, _ = runner.bot.find_opportunities()
        runner.bot.last_opportunities = opps
        runner.bot.last_cycle_ts = time.time()
        return jsonify([_opp_dict(o) for o in opps[:30]])

    @app.route("/api/report")
    def api_report():
        from ..report import build_report
        st = store()
        rep = build_report(st)
        st.close()
        return jsonify(rep)

    @app.route("/api/logs")
    def api_logs():
        st = store()
        lines = load_logs(st)
        st.close()
        return jsonify(lines or LOG_BUFFER.lines())

    @app.route("/api/control", methods=["POST"])
    def api_control():
        action = (request.get_json(silent=True) or {}).get("action")
        ok = False
        st = store()
        blocked = is_bot_alive(st, cfg.poll_interval) and not runner.running
        st.close()
        if action == "start":
            if blocked:
                ok = False
            else:
                ok = runner.start()
        elif action == "stop":
            ok = runner.stop()
        elif action == "run_once":
            if blocked:
                ok = False
            else:
                ok = runner.run_once()
        st2 = store()
        external = is_bot_alive(st2, cfg.poll_interval) and not runner.running
        st2.close()
        return jsonify({"ok": ok, "running": runner.running or external})

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if request.method == "POST":
            updates = request.get_json(silent=True) or {}
            _apply_config_updates(cfg, runner, updates)
            return jsonify({"ok": True})
        return jsonify({
            "mode": cfg.mode,
            "bankroll": cfg.bankroll,
            "poll_interval_seconds": cfg.poll_interval,
            "strategy": cfg.strategy,
            "risk": cfg.risk,
            "universe": cfg.universe,
        })

    return app


def _ai_active(runner: BotRunner) -> bool:
    u = getattr(runner.bot.strategy, "understanding", None)
    return bool(u and u.active)


def _opp_dict(o) -> dict:
    return opp_to_dict(o)


def _apply_config_updates(cfg: Config, runner: BotRunner, updates: dict):
    """Update tunable settings live and rebuild strategy/risk."""
    import yaml
    from ..risk import RiskManager
    from ..strategy import Strategy

    for section in ("strategy", "risk", "universe"):
        if section in updates and isinstance(updates[section], dict):
            cfg.raw.setdefault(section, {}).update(
                {k: _num(v) for k, v in updates[section].items()})
    if "poll_interval_seconds" in updates:
        cfg.raw["poll_interval_seconds"] = int(updates["poll_interval_seconds"])

    runner.bot.strategy = Strategy(cfg)
    runner.bot.risk = RiskManager(cfg)
    # persist to disk so changes survive a restart
    try:
        with open("config.yaml", "w") as f:
            yaml.safe_dump(cfg.raw, f, sort_keys=False)
    except OSError:
        pass


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and abs(f) >= 1 and "." not in str(v) else f
    except (TypeError, ValueError):
        return v
