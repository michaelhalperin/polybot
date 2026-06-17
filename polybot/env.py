"""Minimal .env loader (no extra dependency).

Loads KEY=VALUE lines from a `.env` file into os.environ so secrets like
ANTHROPIC_API_KEY don't have to be exported by hand for local development.

Important for deployment (e.g. Render): **real environment variables always
win.** We only set a key if it isn't already in the environment — so a value
configured in Render's dashboard takes precedence over any stray local `.env`,
and you can deploy without shipping the file at all.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: Optional[str] = None) -> Optional[Path]:
    """Load the first `.env` found into os.environ. Returns the path used, if any.

    Search order: an explicit `path`, then `./.env`, then the project root.
    Existing environment variables are never overwritten.
    """
    if path:
        candidates = [Path(path)]
    else:
        root = Path(__file__).resolve().parent.parent  # project root
        candidates = [Path.cwd() / ".env", root / ".env"]

    seen = set()
    for p in candidates:
        p = p.resolve()
        if p in seen or not p.is_file():
            seen.add(p)
            continue
        seen.add(p)
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:   # real env vars win
                os.environ[key] = val
        return p
    return None
