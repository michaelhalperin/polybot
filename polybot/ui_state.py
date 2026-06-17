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
    store.set_meta("last_cycle_ts", str(time.time()))
    store.set_meta("cycle_count", str(cycle_count))
    store.set_meta(
        "last_opportunities",
        json.dumps([opp_to_dict(o) for o in opps[:30]]),
    )


def is_bot_alive(store: "Store", poll_interval: float) -> bool:
    """True when a bot loop completed a cycle recently (CLI or web)."""
    threshold = poll_interval * 2.5
    now = time.time()

    if store.get_meta("bot_active") == "1":
        ts = float(store.get_meta("last_cycle_ts") or 0)
        if ts and (now - ts) < threshold:
            return True

    # Fallback: equity snapshots are written every trading cycle.
    eq = store.latest_equity()
    if eq and (now - float(eq["ts"])) < threshold:
        return True

    return False


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
