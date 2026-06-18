"""Shared UI state in SQLite so the CLI bot and web dashboard stay in sync."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .models import Opportunity
    from .store import Store


def opp_to_dict(o: "Opportunity") -> dict:
    leg = o.legs[0]
    return {
        "kind": o.kind,
        "edge": o.edge,
        "confidence": o.confidence,
        "score": o.score,
        "question": o.market.question,
        "side": leg.side_name,
        "price": leg.entry_price,
        "fair_value": o.fair_value,
        "liquidity": o.market.liquidity,
        "notes": o.notes,
        "analysis": o.analysis,   # AI verdict (summary/risk), or None if regex-only
    }


def set_bot_active(store: "Store", active: bool):
    store.set_meta("bot_active", "1" if active else "0")
    if not active:
        store.set_meta("bot_source", "")


def touch_cycle(store: "Store", cycle_count: int, opps: List["Opportunity"]):
    ts = time.time()
    store.set_meta("last_cycle_ts", str(ts))
    store.set_meta("cycle_count", str(cycle_count))
    opp_dicts = [opp_to_dict(o) for o in opps[:30]]
    store.set_meta("last_opportunities", json.dumps(opp_dicts))
    store.log_opportunities(opp_dicts, ts=ts)


def is_bot_alive(store: "Store", poll_interval: float) -> bool:
    """True only when a bot loop is *currently* running (CLI or web).

    Relies on the explicit `bot_active` flag plus a fresh heartbeat
    (`last_cycle_ts`). It deliberately does NOT fall back to "a recent equity
    snapshot exists" — that snapshot lingers for minutes after the bot stops,
    which made a stopped bot still look alive (the Stop button appeared to do
    nothing). If a process dies without clearing the flag, the heartbeat goes
    stale and this returns False on its own.
    """
    if store.get_meta("bot_active") != "1":
        return False
    ts = float(store.get_meta("last_cycle_ts") or 0)
    return bool(ts and (time.time() - ts) < poll_interval * 2.5)


def load_opportunities(store: "Store") -> list:
    raw = store.get_meta("last_opportunities")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def load_logs(store: "Store") -> list:
    raw = store.get_meta("log_lines")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []
