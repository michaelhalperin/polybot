"""Load and validate configuration from config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass

import yaml


@dataclass
class Config:
    raw: dict

    def __getitem__(self, key):
        return self.raw[key]

    def get(self, key, default=None):
        return self.raw.get(key, default)

    # convenience accessors used across the codebase
    @property
    def mode(self) -> str:
        return self.raw.get("mode", "paper")

    @property
    def bankroll(self) -> float:
        return float(self.raw.get("bankroll", 100.0))

    @property
    def poll_interval(self) -> int:
        return int(self.raw.get("poll_interval_seconds", 60))

    @property
    def max_cycles(self) -> int:
        return int(self.raw.get("max_cycles", 0))

    @property
    def universe(self) -> dict:
        return self.raw.get("universe", {})

    @property
    def strategy(self) -> dict:
        return self.raw.get("strategy", {})

    @property
    def risk(self) -> dict:
        return self.raw.get("risk", {})

    @property
    def fees(self) -> dict:
        return self.raw.get("fees", {})

    @property
    def db_path(self) -> str:
        # POLYBOT_DB_PATH lets a host (e.g. Render) point the DB at a
        # persistent disk without editing config.yaml.
        return (os.environ.get("POLYBOT_DB_PATH")
                or self.raw.get("storage", {}).get("db_path", "data/polybot.db"))

    @property
    def log_level(self) -> str:
        return self.raw.get("logging", {}).get("level", "INFO")


def load_config(path: str = "config.yaml") -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    cfg = Config(raw=data)
    if cfg.mode == "live":
        raise SystemExit(
            "mode: live is not implemented. This bot only paper-trades. "
            "Set mode: paper in config.yaml."
        )
    return cfg
