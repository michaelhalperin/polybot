"""Centralized logging setup."""
from __future__ import annotations

import collections
import json
import logging
import sys
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .store import Store

_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
_DATEFMT = "%H:%M:%S"


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted log lines in memory for the web UI."""

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.buffer = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass

    def lines(self):
        return list(self.buffer)


# Shared instance the web server reads from.
LOG_BUFFER = RingBufferHandler()


class MetaLogHandler(logging.Handler):
    """Mirrors log lines into SQLite so the web UI can read CLI bot output."""

    def __init__(self, store: "Store", capacity: int = 500):
        super().__init__()
        self.store = store
        self.capacity = capacity

    def emit(self, record):
        try:
            line = self.format(record)
            raw = self.store.get_meta("log_lines")
            lines = json.loads(raw) if raw else []
            lines.append(line)
            if len(lines) > self.capacity:
                lines = lines[-self.capacity:]
            self.store.set_meta("log_lines", json.dumps(lines))
        except Exception:
            pass


_STORE_HANDLER: Optional[MetaLogHandler] = None


def attach_store_logger(store: "Store"):
    """Persist log lines to the DB (safe to call more than once)."""
    global _STORE_HANDLER
    if _STORE_HANDLER is not None:
        return
    logger = get_logger()
    _STORE_HANDLER = MetaLogHandler(store)
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    _STORE_HANDLER.setFormatter(fmt)
    logger.addHandler(_STORE_HANDLER)


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("polybot")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    LOG_BUFFER.setFormatter(fmt)
    logger.addHandler(LOG_BUFFER)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("polybot")
