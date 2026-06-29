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
from ..profiles import (
    db_path_for_profile,
    prod_available,
    prod_remote_url,
    prod_view_mode,
)
from ..store import Store
from .remote import remote_get
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


class ViewState:
    """Which dataset the dashboard reads (local SQLite vs prod remote/snapshot)."""

    def __init__(self, profile: str = "local"):
        self._profile = profile
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            return self._profile

    def set(self, profile: str):
        with self._lock:
            self._profile = profile

    def read_only(self) -> bool:
        return self.get() == "prod"

    def source(self) -> str:
        return prod_view_mode(self.get())


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
        # Let the bot abort a long in-progress cycle when Stop is pressed.
        self.bot.should_stop = self._stop.is_set

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
        # Clear the active flag right away so the dashboard flips to "stopped"
        # immediately, instead of waiting for the loop's cycle to finish.
        set_bot_active(self.bot.store, False)
        self.log.info(">>> stop requested; finishing current cycle...")
        return True

    def run_once(self):
        """Run exactly one cycle now (used when the loop is stopped)."""
        if self.running:
            return False
        self._stop.clear()   # a prior Stop must not cancel this one-off cycle
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
def create_app(cfg: Config, runner: BotRunner,
               view_state: ViewState | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    view = view_state or ViewState("local")

    def local_store() -> Store:
        return Store(cfg.db_path)

    def viewed_store() -> Store:
        profile = view.get()
        path = db_path_for_profile(profile, cfg.db_path)
        return Store(path)

    def _profile_payload() -> dict:
        mode = view.source()
        return {
            "profile": view.get(),
            "profile_source": mode,
            "read_only": view.read_only(),
            "prod_available": prod_available(),
            "prod_url": prod_remote_url() if prod_remote_url() else None,
        }

    def _remote_or_local(path: str, params: dict | None = None):
        if view.source() == "remote":
            return remote_get(prod_remote_url(), path, params=params)
        return None

    def _remote_history(path: str, params: dict | None = None, fallback: str | None = None):
        """Remote history route, with optional legacy endpoint fallback."""
        remote = _remote_or_local(path, params=params)
        if remote is not None:
            return remote
        if view.source() == "remote" and fallback:
            return _remote_or_local(fallback, params=params)
        return None

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

    # Optional password protection on public hosts (e.g. Render). POLYBOT_PASSWORD
    # in a local .env is also used to authenticate to the remote prod API — that
    # must NOT lock down localhost unless POLYBOT_REQUIRE_AUTH=1.
    password = os.environ.get("POLYBOT_PASSWORD")
    require_auth = bool(password) and (
        os.environ.get("POLYBOT_REQUIRE_AUTH") == "1"
        or os.environ.get("PORT")  # Render / cloud bind
    )

    @app.before_request
    def _require_auth():
        if not require_auth or request.path == "/healthz":
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

    @app.route("/api/profile", methods=["GET", "POST"])
    def api_profile():
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            profile = (body.get("profile") or "").strip().lower()
            if profile not in ("local", "prod"):
                return jsonify({"ok": False, "error": "profile must be local or prod"}), 400
            if profile == "prod" and not prod_available():
                return jsonify({
                    "ok": False,
                    "error": (
                        "Prod not configured. Set POLYBOT_PROD_URL in .env for live view, "
                        "or run `python run.py pull` to download a snapshot."
                    ),
                    **_profile_payload(),
                }), 400
            view.set(profile)
            return jsonify({"ok": True, **_profile_payload()})
        return jsonify(_profile_payload())

    @app.route("/api/status")
    def api_status():
        remote = _remote_or_local("/api/status")
        if remote is not None:
            remote.update(_profile_payload())
            return jsonify(remote)
        st = viewed_store()
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
        payload = {
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
        }
        payload.update(_profile_payload())
        return jsonify(payload)

    @app.route("/api/ai")
    def api_ai():
        """What the AI understanding layer has been doing."""
        remote = _remote_or_local("/api/ai")
        if remote is not None:
            return jsonify(remote)
        u = getattr(runner.bot.strategy, "understanding", None)
        st = viewed_store()
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
        remote = _remote_or_local("/api/positions")
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
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
        remote = _remote_or_local("/api/trades", params={"limit": limit})
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        rows = [dict(t) for t in st.recent_trades(limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/equity")
    def api_equity():
        limit = int(request.args.get("limit", 5000))
        remote = _remote_or_local("/api/equity", params={"limit": limit})
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        rows = [dict(e) for e in st.equity_curve(limit=limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/opportunities")
    def api_opportunities():
        remote = _remote_or_local("/api/opportunities")
        if remote is not None:
            return jsonify(remote)
        if view.get() == "local" and runner.running and runner.bot.last_opportunities:
            rows = [_opp_dict(o) for o in runner.bot.last_opportunities[:30]]
        else:
            st = viewed_store()
            rows = load_opportunities(st)
            st.close()
        return jsonify(rows)

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        if view.read_only():
            return jsonify({"ok": False, "error": "read-only prod view"}), 403
        """Fresh read-only scan (does not trade)."""
        opps, _ = runner.bot.find_opportunities()
        runner.bot.last_opportunities = opps
        runner.bot.last_cycle_ts = time.time()
        rows = [_opp_dict(o) for o in opps[:30]]
        st = local_store()
        st.log_opportunities(rows, ts=runner.bot.last_cycle_ts)
        st.close()
        return jsonify(rows)

    @app.route("/api/history/trades")
    def api_history_trades():
        limit = int(request.args.get("limit", 500))
        remote = _remote_history(
            "/api/history/trades", params={"limit": limit}, fallback="/api/trades")
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        rows = [dict(t) for t in st.recent_trades(limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/history/positions")
    def api_history_positions():
        limit = int(request.args.get("limit", 200))
        remote = _remote_history("/api/history/positions", params={"limit": limit})
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        rows = [dict(p) for p in st.position_history(limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/history/opportunities")
    def api_history_opportunities():
        limit = int(request.args.get("limit", 300))
        remote = _remote_history("/api/history/opportunities", params={"limit": limit})
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        rows = [dict(o) for o in st.opportunity_history(limit)]
        st.close()
        return jsonify(rows)

    @app.route("/api/history/ai")
    def api_history_ai():
        limit = int(request.args.get("limit", 200))
        remote = _remote_history("/api/history/ai", params={"limit": limit}, fallback="/api/ai")
        if remote is not None:
            if isinstance(remote, dict) and "recent" in remote:
                remote = remote["recent"][:limit]
            return jsonify(remote)
        st = viewed_store()
        recent = []
        for r in st.recent_analyses(limit):
            try:
                d = json.loads(r["json"])
            except (ValueError, TypeError):
                continue
            recent.append({
                "question": r["question"], "ts": r["ts"],
                "tradeable": d.get("tradeable"),
                "resolution_risk": d.get("resolution_risk"),
                "summary": d.get("summary"),
            })
        st.close()
        return jsonify(recent)

    @app.route("/api/report")
    def api_report():
        remote = _remote_or_local("/api/report")
        if remote is not None:
            return jsonify(remote)
        from ..report import build_report
        st = viewed_store()
        rep = build_report(st)
        st.close()
        return jsonify(rep)

    @app.route("/api/shadow")
    def api_shadow():
        remote = _remote_or_local("/api/shadow")
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        stats = st.shadow_stats()
        st.close()
        return jsonify(stats)

    @app.route("/api/logs")
    def api_logs():
        remote = _remote_or_local("/api/logs")
        if remote is not None:
            return jsonify(remote)
        st = viewed_store()
        lines = load_logs(st)
        st.close()
        if view.get() == "local":
            lines = lines or LOG_BUFFER.lines()
        return jsonify(lines)

    @app.route("/api/control", methods=["POST"])
    def api_control():
        if view.read_only():
            return jsonify({"ok": False, "running": False, "error": "read-only prod view"})
        action = (request.get_json(silent=True) or {}).get("action")
        ok = False
        st = local_store()
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
        st2 = local_store()
        external = is_bot_alive(st2, cfg.poll_interval) and not runner.running
        st2.close()
        return jsonify({"ok": ok, "running": runner.running or external})

    @app.route("/api/config", methods=["GET", "POST"])
    def api_config():
        if view.read_only() and request.method == "POST":
            return jsonify({"ok": False, "error": "read-only prod view"}), 403
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
