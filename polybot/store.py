"""SQLite persistence: positions, trades (fills), and the equity curve.

Everything the bot does is recorded so you can audit performance, resume
after a restart, and analyze results later.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id  TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    market        TEXT NOT NULL,
    side_name     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    shares        REAL NOT NULL,
    avg_cost      REAL NOT NULL,        -- per-share cost incl. fees
    cost_usd      REAL NOT NULL,        -- total deployed
    fair_at_entry REAL NOT NULL,
    group_id      TEXT,                 -- links arb legs together
    theme         TEXT,                 -- event slug; groups correlated markets
    ai_verified   INTEGER DEFAULT 0,    -- 1 if the AI layer vetted this trade
    status        TEXT NOT NULL,        -- open | closed
    opened_at     REAL NOT NULL,
    closed_at     REAL,
    exit_price    REAL,
    proceeds_usd  REAL,
    realized_pnl  REAL,
    exit_reason   TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    condition_id TEXT NOT NULL,
    token_id    TEXT NOT NULL,
    side        TEXT NOT NULL,          -- buy | sell
    shares      REAL NOT NULL,
    price       REAL NOT NULL,
    fee         REAL NOT NULL,
    usd         REAL NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts            REAL PRIMARY KEY,
    cash          REAL NOT NULL,
    positions_val REAL NOT NULL,
    total         REAL NOT NULL,
    realized_pnl  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_analysis (
    condition_id TEXT PRIMARY KEY,
    question     TEXT,                 -- the market question (for the UI feed)
    text_hash    TEXT NOT NULL,        -- hash of question+rules; re-analyze if it changes
    json         TEXT NOT NULL,        -- the LLM's structured analysis
    ts           REAL NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # check_same_thread=False + WAL lets the web UI read while the bot
        # writes from its own thread. Writes are still serialized by SQLite.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Add columns introduced after the first release (safe to re-run)."""
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(positions)").fetchall()}
        if "theme" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN theme TEXT")
        if "ai_verified" not in cols:
            self.conn.execute("ALTER TABLE positions ADD COLUMN ai_verified INTEGER DEFAULT 0")
        acols = {r["name"] for r in
                 self.conn.execute("PRAGMA table_info(market_analysis)").fetchall()}
        if "question" not in acols:
            self.conn.execute("ALTER TABLE market_analysis ADD COLUMN question TEXT")

    # ---- meta (e.g. starting bankroll / cash) ----
    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---- market analysis cache (LLM understanding) ----
    def get_analysis(self, condition_id: str):
        return self.conn.execute(
            "SELECT * FROM market_analysis WHERE condition_id=?",
            (condition_id,)).fetchone()

    def set_analysis(self, condition_id: str, question: str, text_hash: str,
                     payload: str):
        self.conn.execute(
            "INSERT INTO market_analysis(condition_id,question,text_hash,json,ts) "
            "VALUES(?,?,?,?,?) ON CONFLICT(condition_id) DO UPDATE SET "
            "question=excluded.question, text_hash=excluded.text_hash, "
            "json=excluded.json, ts=excluded.ts",
            (condition_id, question, text_hash, payload, time.time()))
        self.conn.commit()

    def recent_analyses(self, limit: int = 40) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT condition_id, question, json, ts FROM market_analysis "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    def analysis_stats(self) -> dict:
        """Aggregate what the AI has concluded across all analyzed markets."""
        rows = self.conn.execute("SELECT json FROM market_analysis").fetchall()
        stats = {"analyzed": len(rows), "tradeable": 0, "gated": 0, "crypto": 0}
        for r in rows:
            try:
                d = json.loads(r["json"])
            except (ValueError, TypeError):
                continue
            if (not d.get("tradeable")) or d.get("resolution_risk") == "high":
                stats["gated"] += 1
            else:
                stats["tradeable"] += 1
            if d.get("is_crypto_price_target"):
                stats["crypto"] += 1
        return stats

    # ---- positions ----
    def add_position(self, **kw) -> int:
        cols = ", ".join(kw.keys())
        ph = ", ".join("?" for _ in kw)
        cur = self.conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({ph})", tuple(kw.values()))
        self.conn.commit()
        return cur.lastrowid

    def open_positions(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE status='open'").fetchall()

    def closed_positions(self) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY closed_at").fetchall()

    def has_open_position(self, condition_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM positions WHERE condition_id=? AND status='open' LIMIT 1",
            (condition_id,)).fetchone()
        return row is not None

    def close_position(self, pos_id: int, exit_price: float, proceeds: float,
                       realized_pnl: float, reason: str):
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_price=?, "
            "proceeds_usd=?, realized_pnl=?, exit_reason=? WHERE id=?",
            (time.time(), exit_price, proceeds, realized_pnl, reason, pos_id),
        )
        self.conn.commit()

    # ---- trades ----
    def add_trade(self, **kw):
        cols = ", ".join(kw.keys())
        ph = ", ".join("?" for _ in kw)
        self.conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(kw.values()))
        self.conn.commit()

    # ---- equity ----
    def record_equity(self, cash: float, positions_val: float,
                      realized_pnl: float):
        total = cash + positions_val
        self.conn.execute(
            "INSERT OR REPLACE INTO equity(ts,cash,positions_val,total,realized_pnl) "
            "VALUES (?,?,?,?,?)",
            (time.time(), cash, positions_val, total, realized_pnl),
        )
        self.conn.commit()

    def total_realized_pnl(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS p FROM positions "
            "WHERE status='closed'").fetchone()
        return float(row["p"])

    def latest_equity(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM equity ORDER BY ts DESC LIMIT 1").fetchone()

    def equity_curve(self, limit: int = 1000) -> List[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT ts,total,cash,positions_val,realized_pnl FROM equity "
            "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return list(reversed(rows))

    def recent_trades(self, limit: int = 50) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()

    def stats(self) -> dict:
        closed = self.conn.execute(
            "SELECT realized_pnl FROM positions WHERE status='closed'").fetchall()
        wins = sum(1 for r in closed if r["realized_pnl"] > 0)
        return {
            "closed": len(closed),
            "wins": wins,
            "win_rate": (wins / len(closed)) if closed else 0.0,
            "realized_pnl": sum(r["realized_pnl"] for r in closed),
        }

    def close(self):
        self.conn.close()
