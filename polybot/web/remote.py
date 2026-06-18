"""Proxy read-only API calls to a remote Polybot dashboard (e.g. Render)."""
from __future__ import annotations

import os

import requests

from ..log import get_logger


def remote_get(base_url: str, path: str, params: dict | None = None, timeout: int = 30):
    """GET a JSON API route from a remote Polybot instance.

    Returns parsed JSON on success, or None if the remote is unreachable or
    responds with an error (callers fall back to local snapshot SQLite).
    """
    log = get_logger()
    url = f"{base_url.rstrip('/')}{path}"
    password = os.environ.get("POLYBOT_PASSWORD") or ""
    user = os.environ.get("POLYBOT_REMOTE_USER", "polybot")
    auth = (user, password) if password else None
    try:
        resp = requests.get(url, params=params, auth=auth, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        log.warning("Remote GET %s failed: %s", path, e)
        return None
