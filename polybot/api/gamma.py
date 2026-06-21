"""Gamma API client — fetches the catalogue of markets.

Gamma is Polymarket's public, read-only metadata API. No API key required.
Docs: https://docs.polymarket.com (Gamma Markets API)
"""
from __future__ import annotations

from typing import List

from ..log import get_logger
from ..models import Market
from .http import get_json

BASE = "https://gamma-api.polymarket.com"


def fetch_active_markets(limit: int = 250, min_liquidity: float = 0.0) -> List[Market]:
    """Return active, open, order-book-enabled markets sorted by volume.

    Paginates through Gamma until `limit` qualifying markets are collected.
    """
    log = get_logger()
    out: List[Market] = []
    offset = 0
    page = 100
    while len(out) < limit:
        data = get_json(f"{BASE}/markets", params={
            "limit": page,
            "offset": offset,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        })
        if not data:
            break
        for d in data:
            m = Market.from_gamma(d)
            if not m.enable_order_book or not m.accepting_orders:
                continue
            if not m.is_binary:
                continue
            if m.liquidity < min_liquidity:
                continue
            out.append(m)
            if len(out) >= limit:
                break
        if len(data) < page:
            break  # no more pages
        offset += page
    log.info("Gamma: fetched %d qualifying markets", len(out))
    return out


def fetch_market(condition_id: str) -> Market:
    """Fetch a single market by its condition id (used to check resolution).

    IMPORTANT: Gamma's /markets endpoint hides *closed* markets by default, so a
    plain condition_ids lookup returns nothing once a market resolves — which is
    exactly when we need it (to settle a position). We therefore try the normal
    lookup first (still-open markets) and then explicitly with closed=true
    (resolved markets), so settlement actually fires.
    """
    for params in ({"condition_ids": condition_id},
                   {"condition_ids": condition_id, "closed": "true"}):
        data = get_json(f"{BASE}/markets", params=params)
        if data:
            return Market.from_gamma(data[0])
    return None


def fetch_markets(condition_ids) -> dict:
    """Batch-fetch many markets by condition id, INCLUDING closed ones.

    Returns {condition_id: Market}. Used for position settlement so we make a
    couple of calls per cycle instead of one per open position (avoids Gamma
    rate-limiting on a long run). Two queries per chunk — the default endpoint
    hides closed markets, so we also query closed=true and merge.
    """
    result: dict = {}
    ids = list(dict.fromkeys(condition_ids))   # dedup, keep order
    chunk = 50
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        for params in ({"condition_ids": batch, "limit": len(batch)},
                       {"condition_ids": batch, "closed": "true", "limit": len(batch)}):
            data = get_json(f"{BASE}/markets", params=params)
            for d in data or []:
                m = Market.from_gamma(d)
                if m.condition_id:
                    result[m.condition_id] = m
    return result
