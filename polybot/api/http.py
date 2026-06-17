"""Small HTTP helper with retries and a shared session."""
from __future__ import annotations

import time

import requests

from ..log import get_logger

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "polybot/0.1 (paper-trading research)"})


def get_json(url: str, params: dict = None, retries: int = 3, timeout: int = 15):
    """GET a URL and return parsed JSON, retrying on transient errors."""
    log = get_logger()
    last_err = None
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            wait = 1.5 * (attempt + 1)
            log.debug("GET %s failed (%s); retry in %.1fs", url, e, wait)
            time.sleep(wait)
    log.warning("GET %s failed after %d attempts: %s", url, retries, last_err)
    return None
