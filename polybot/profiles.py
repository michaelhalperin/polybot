"""Local vs prod profile resolution for the dashboard."""
from __future__ import annotations

import os
from pathlib import Path

LOCAL_DB = "data/polybot.db"
PROD_SNAPSHOT_DB = "data/polybot-prod.db"


def resolve_profile(name: str | None = None) -> str:
    profile = (name or os.environ.get("POLYBOT_PROFILE") or "local").strip().lower()
    if profile not in ("local", "prod"):
        raise SystemExit(f"Unknown profile {profile!r} — use 'local' or 'prod'.")
    return profile


def db_path_for_profile(profile: str, config_default: str = LOCAL_DB) -> str:
    if profile == "local":
        return os.environ.get("POLYBOT_DB_PATH") or config_default
    return os.environ.get("POLYBOT_PROD_DB_PATH") or PROD_SNAPSHOT_DB


def prod_remote_url() -> str | None:
    url = (os.environ.get("POLYBOT_PROD_URL") or "").strip().rstrip("/")
    return url or None


def prod_view_mode(profile: str) -> str:
    """How prod data is served: 'remote', 'snapshot', or 'missing'."""
    if profile != "prod":
        return "local"
    return prod_source_mode()


def prod_source_mode() -> str:
    """'remote', 'snapshot', or 'missing' — whether prod view can work at all."""
    if prod_remote_url():
        return "remote"
    if Path(db_path_for_profile("prod")).exists():
        return "snapshot"
    return "missing"


def prod_available() -> bool:
    return prod_source_mode() != "missing"
