"""CLOB API client — fetches live order books.

The CLOB (Central Limit Order Book) API serves real-time bids/asks.
Reading books is public; only placing orders needs credentials (not used
here, since we paper-trade).
Docs: https://docs.polymarket.com (CLOB API)
"""
from __future__ import annotations

from typing import Dict, List

import requests

from ..log import get_logger
from ..models import OrderBook
from .http import _SESSION, get_json

BASE = "https://clob.polymarket.com"


def fetch_book(token_id: str) -> OrderBook:
    data = get_json(f"{BASE}/book", params={"token_id": token_id})
    if data is None:
        return None
    return OrderBook.from_clob(token_id, data)


def fetch_books(token_ids: List[str]) -> Dict[str, OrderBook]:
    """Fetch many books at once via the batch POST endpoint.

    Falls back to per-token GETs if the batch call is unavailable.
    Returns a dict keyed by token_id.
    """
    log = get_logger()
    result: Dict[str, OrderBook] = {}
    if not token_ids:
        return result

    # Batch in chunks to keep request bodies reasonable.
    chunk = 100
    for i in range(0, len(token_ids), chunk):
        batch = token_ids[i:i + chunk]
        payload = [{"token_id": t} for t in batch]
        ok = False
        try:
            resp = _SESSION.post(f"{BASE}/books", json=payload, timeout=20)
            if resp.status_code == 200:
                for b in resp.json():
                    tid = b.get("asset_id") or b.get("token_id")
                    if tid:
                        result[str(tid)] = OrderBook.from_clob(str(tid), b)
                ok = True
        except (requests.RequestException, ValueError) as e:
            log.debug("Batch /books failed (%s); falling back to singles", e)

        if not ok:
            for t in batch:
                book = fetch_book(t)
                if book is not None:
                    result[t] = book

    log.info("CLOB: fetched %d order books", len(result))
    return result
