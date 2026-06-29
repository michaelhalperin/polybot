#!/usr/bin/env python3
"""Polybot command-line entry point.

Usage:
  python run.py web             # launch the browser dashboard (recommended)
  python run.py run             # start the trading loop in the terminal
  python run.py scan            # one-shot: list opportunities, place NO trades
  python run.py status          # print current portfolio summary
  python run.py report          # performance + calibration on resolved trades
  python run.py pull            # download prod DB snapshot (needs POLYBOT_PROD_SSH)
  python run.py reset           # wipe the paper-trading database (asks first)

Profiles (local vs prod dashboard view):
  python run.py web --profile prod     # open dashboard viewing prod (live or snapshot)
  Set POLYBOT_PROD_URL in .env for live prod view; POLYBOT_PASSWORD must match prod.

All behaviour is controlled by config.yaml.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from polybot.api import clob, gamma
from polybot.bot import Bot
from polybot.config import load_config
from polybot.env import load_dotenv
from polybot.log import setup_logging
from polybot.profiles import db_path_for_profile, resolve_profile
from polybot.store import Store
from polybot.strategy import Strategy, rank_opportunities


def cmd_run(cfg):
    Bot(cfg).run()


def cmd_scan(cfg):
    """Dry run: show what the bot WOULD trade, without opening positions."""
    log = setup_logging(cfg.log_level)
    strat = Strategy(cfg)
    markets = gamma.fetch_active_markets(
        limit=int(cfg.universe.get("max_markets_scan", 250)),
        min_liquidity=float(cfg.universe.get("min_liquidity", 0)),
    )
    min_vol = float(cfg.universe.get("min_volume_24hr", 0))
    markets = [m for m in markets if m.volume_24hr >= min_vol]
    tokens = [t for m in markets for t in m.token_ids]
    books = clob.fetch_books(tokens)

    opps = []
    for m in markets:
        opps.extend(strat.evaluate(m, books))
    opps = rank_opportunities(opps)

    print(f"\n{'='*80}\nTOP OPPORTUNITIES ({len(opps)} found)\n{'='*80}")
    if not opps:
        print("None right now. Markets may be efficiently priced — try lowering")
        print("strategy.min_edge in config.yaml, or scan again later.")
        return
    for i, o in enumerate(opps[:25], 1):
        print(f"{i:2d}. [{o.kind:9s}] edge {o.edge:+.3f}  conf {o.confidence:.2f}  "
              f"score {o.score:.3f}")
        print(f"     {o.market.question[:74]}")
        print(f"     {o.notes}")
    print(f"{'='*80}\n(scan is read-only — no trades placed)\n")


def cmd_status(cfg):
    log = setup_logging(cfg.log_level)
    if not os.path.exists(cfg.db_path):
        print("No database yet. Run `python run.py run` first.")
        return
    store = Store(cfg.db_path)
    s = store.stats()
    cash = float(store.get_meta("cash") or cfg.bankroll)
    start = float(store.get_meta("starting_bankroll") or cfg.bankroll)
    open_pos = store.open_positions()
    open_val = sum(p["cost_usd"] for p in open_pos)  # cost basis (no live book here)
    print(f"\n{'='*64}\nPOLYBOT STATUS\n{'='*64}")
    print(f"  cash:           ${cash:.2f}")
    print(f"  open positions: {len(open_pos)} (cost basis ${open_val:.2f})")
    print(f"  realized P&L:   ${s['realized_pnl']:+.2f}")
    print(f"  closed trades:  {s['closed']} (win rate {s['win_rate']*100:.0f}%)")
    print(f"  est. equity:    ${cash + open_val:.2f}  (start ${start:.2f})")
    if open_pos:
        print(f"\n  OPEN POSITIONS:")
        for p in open_pos:
            print(f"   - {p['side_name']:5s} x{p['shares']:.1f} @ {p['avg_cost']:.3f}"
                  f"  [{p['kind']}]  {p['market'][:46]}")
    print(f"{'='*64}\n")
    store.close()


def cmd_backtest(cfg, max_scan, horizon_hours):
    """Offline edge test: does the crypto model beat the market on resolved markets?"""
    from polybot import backtest
    setup_logging(cfg.log_level)
    vol_scale = float(cfg.strategy.get("crypto_vol_scale", 1.0))
    r = backtest.run_backtest(max_scan=max_scan, horizon_hours=horizon_hours,
                              vol_scale=vol_scale)
    print(backtest.format_result(r))


def cmd_shadow(cfg, profile="local"):
    """Show the observe-only crypto model's scorecard vs the market.

    With --profile prod, read the live scoreboard from the remote dashboard
    (POLYBOT_PROD_URL) instead of the local SQLite DB, since the bot itself
    runs on the server, not locally.
    """
    if profile == "prod":
        from polybot.profiles import prod_remote_url
        from polybot.web.remote import remote_get
        base = prod_remote_url()
        if not base:
            print("Set POLYBOT_PROD_URL in .env to read the prod scoreboard.")
            return
        s = remote_get(base, "/api/shadow")
        if s is None:
            print(f"Could not reach prod dashboard at {base}.")
            print("Check POLYBOT_PROD_URL and POLYBOT_PASSWORD in .env.")
            return
    else:
        if not os.path.exists(cfg.db_path):
            print("No database yet. Run `python run.py web` and let it run first.")
            return
        store = Store(cfg.db_path)
        s = store.shadow_stats()
        store.close()
    print("=" * 60)
    print("SHADOW MODE — crypto model vs market (observe-only, no trades)")
    print("=" * 60)
    print(f"  recorded predictions: {s['recorded']}")
    print(f"  resolved / settled:   {s['settled']}")
    print(f"  pending resolution:   {s['pending']}")
    print(f"  scored (have price):  {s['scored']}")
    if not s.get("scored"):
        print("\n  Not enough resolved data yet — let it run for a couple weeks,")
        print("  then re-check. The bot must be RUNNING to collect samples.")
        print("=" * 60)
        return
    print(f"\n  base rate (YES won):  {s['base_rate']:.1%}")
    print(f"  {'forecaster':<10}{'Brier':>9}{'log loss':>11}{'avg P(yes)':>13}")
    print("  " + "-" * 42)
    print(f"  {'model':<10}{s['model_brier']:>9.4f}{s['model_logloss']:>11.4f}"
          f"{s['model_avg_p']:>13.3f}")
    print(f"  {'market':<10}{s['market_brier']:>9.4f}{s['market_logloss']:>11.4f}"
          f"{s['market_avg_p']:>13.3f}")
    better = "MARKET" if s["market_brier"] < s["model_brier"] else "MODEL"
    print(f"\n  >> Better forecaster (lower Brier): {better}")
    print("\n  Betting the model's disagreement with the market:")
    print(f"  {'min_edge':>9}{'bets':>6}{'win%':>7}{'avg P&L/$':>12}{'total P&L':>11}")
    print("  " + "-" * 45)
    for b in s["betting"]:
        print(f"  {b['min_edge']:>9.2f}{b['bets']:>6}{b['win_rate']*100:>6.0f}%"
              f"{b['avg_pnl']:>12.4f}{b['total_pnl']:>11.2f}")
    print("\n  Positive avg P&L/$ = the model adds value; negative = -EV.")
    print("=" * 60)


def cmd_report(cfg):
    from polybot.report import build_report, format_text
    if not os.path.exists(cfg.db_path):
        print("No database yet. Run `python run.py web` and let it trade first.")
        return
    store = Store(cfg.db_path)
    print(format_text(build_report(store)))
    store.close()


def cmd_web(cfg, host, port, autostart=False, profile="local"):
    from polybot.web.server import BotRunner, ViewState, create_app
    log = setup_logging(cfg.log_level)
    runner = BotRunner(cfg)
    view_state = ViewState(profile)
    app = create_app(cfg, runner, view_state)
    url = f"http://{host if host != '0.0.0.0' else 'localhost'}:{port}"
    log.info("Polybot dashboard running at %s  (Ctrl+C to quit)", url)
    if profile == "prod":
        log.info("Viewing PROD data (read-only). Local bot uses %s.", cfg.db_path)
    if autostart:
        runner.start()
        log.info("Autostart enabled — bot loop is RUNNING.")
    else:
        log.info("The bot is loaded but PAUSED — click ▶ Start in the browser.")
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def cmd_reset(cfg):
    if os.path.exists(cfg.db_path):
        ans = input(f"Delete {cfg.db_path} and all paper history? [y/N] ")
        if ans.strip().lower() == "y":
            os.remove(cfg.db_path)
            print("Database deleted.")
        else:
            print("Cancelled.")
    else:
        print("No database to delete.")


def cmd_pull(cfg):
    """Download the prod SQLite DB from Render into data/polybot-prod.db."""
    ssh = (os.environ.get("POLYBOT_PROD_SSH") or "").strip()
    if not ssh:
        print("Set POLYBOT_PROD_SSH in .env (Render → Connect → SSH).")
        print("Example: POLYBOT_PROD_SSH=srv-abc123@ssh.render.com")
        sys.exit(1)
    remote = os.environ.get("POLYBOT_PROD_DB_REMOTE", "/var/data/polybot.db")
    dest = db_path_for_profile("prod", cfg.db_path)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = f"{ssh}:{remote}{suffix}"
        dst = f"{dest}{suffix}"
        print(f"scp {src} -> {dst}")
        subprocess.run(["scp", src, dst], check=True)
    print(f"\nProd snapshot saved to {dest}")
    print("View it with: python run.py web --profile prod")


def main():
    load_dotenv()  # pick up ANTHROPIC_API_KEY etc. from a local .env (if present)
    parser = argparse.ArgumentParser(description="Polybot — Polymarket paper-trading bot")
    parser.add_argument("command", nargs="?", default="web",
                        choices=["web", "run", "scan", "status", "report", "backtest",
                                 "shadow", "pull", "reset"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--profile", choices=["local", "prod"], default=None,
                        help="dashboard data source: local SQLite or prod (read-only)")
    parser.add_argument("--autostart", action="store_true",
                        help="start the trading loop immediately (for servers)")
    parser.add_argument("--max-scan", type=int, default=1500,
                        help="backtest: how many closed markets to scan")
    parser.add_argument("--horizon-hours", type=float, default=24.0,
                        help="backtest: how long before resolution to score the forecast")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    profile = resolve_profile(args.profile)

    if args.command == "web":
        # Hosts like Render provide $PORT and expect binding on 0.0.0.0.
        port = int(os.environ.get("PORT", args.port))
        host = "0.0.0.0" if (os.environ.get("PORT") and args.host == "127.0.0.1") else args.host
        autostart = args.autostart or os.environ.get("POLYBOT_AUTOSTART") == "1"
        cmd_web(cfg, host, port, autostart, profile=profile)
    elif args.command == "backtest":
        cmd_backtest(cfg, args.max_scan, args.horizon_hours)
    else:
        {
            "run": cmd_run,
            "scan": cmd_scan,
            "status": cmd_status,
            "report": cmd_report,
            "shadow": lambda cfg: cmd_shadow(cfg, profile=profile),
            "pull": cmd_pull,
            "reset": cmd_reset,
        }[args.command](cfg)


if __name__ == "__main__":
    main()
