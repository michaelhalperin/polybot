"""AI market-understanding layer.

For each market, asks a Claude model to read the QUESTION *and the resolution
rules* and return structured facts:
  * is it a crypto price-target market, and if so its asset/threshold/direction
    (this catches phrasings the regex parser misses);
  * how risky the resolution rules are to misread (the "gotcha" detector);
  * whether the bot should trade it at all.

Results are cached in SQLite per market (keyed by a hash of the text), so the
model is called at most once per market unless its wording changes — keeping
cost tiny. A cheap keyword pre-screen avoids spending tokens on markets that
clearly aren't price targets.

If the LLM is unavailable (no API key / SDK), `.active` is False and callers
fall back to the regex parser.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .llm import DEFAULT_MODEL, LLMClient
from .log import get_logger
from .models import Market
from .store import Store

# Recall-oriented pre-screen: only spend tokens on markets that *might* be a
# crypto price target. Cheap and wide on purpose; the model does the precise work.
_HINT = re.compile(
    r"(bitcoin|btc|ethereum|\beth\b|ether|solana|\bsol\b|dogecoin|doge|"
    r"xrp|ripple|cardano|\bada\b|litecoin|\bltc\b|\bbnb\b|crypto|\$\s?\d)", re.I)

SYSTEM = """You are a careful analyst for a Polymarket prediction-market trading bot. \
You are given a market's QUESTION and its RESOLUTION RULES. Extract structured facts \
so the bot can decide whether and how to price it.

Definitions:
- is_crypto_price_target: true ONLY if the market resolves purely on the US-dollar \
price of a single crypto asset (Bitcoin, Ethereum, Solana, etc.) versus a numeric \
threshold. Otherwise false.
- asset: the crypto ticker (BTC, ETH, SOL, DOGE, XRP, ADA, LTC, BNB) if a crypto \
price target; otherwise an empty string.
- threshold: the US-dollar number; 0 if not applicable.
- direction: "above" = resolves YES if the price is at/above the threshold at the end \
date; "below" = at/below at the end date; "touch" = reaches the threshold at any time \
before the end date; "none" if not a crypto price target.
- resolution_risk: how likely a naive automated bot is to MISREAD how this resolves. \
Use "high" if there are special conditions, ambiguous/odd data sources, partial or \
early resolution, subjective judgement, multiple linked conditions, or wording like \
"lowest"/"highest"/"between"/"average". Use "low" only if it is simple and unambiguous.
- tradeable: true only if the market is clean and unambiguous enough for an automated \
bot to price safely. If in doubt, set false.
- summary: one short plain sentence describing exactly what makes it resolve YES.

Be conservative: when unsure, set tradeable=false and resolution_risk="high". \
Fill every field."""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_crypto_price_target": {"type": "boolean"},
        "asset": {"type": "string"},
        "threshold": {"type": "number"},
        "direction": {"type": "string", "enum": ["above", "below", "touch", "none"]},
        "resolution_risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "tradeable": {"type": "boolean"},
        "summary": {"type": "string"},
    },
    "required": ["is_crypto_price_target", "asset", "threshold", "direction",
                 "resolution_risk", "tradeable", "summary"],
    "additionalProperties": False,
}


@dataclass
class Analysis:
    is_crypto_price_target: bool
    asset: str
    threshold: float
    direction: str
    resolution_risk: str
    tradeable: bool
    summary: str

    @classmethod
    def from_json(cls, d: dict) -> "Analysis":
        return cls(
            is_crypto_price_target=bool(d["is_crypto_price_target"]),
            asset=str(d.get("asset", "")),
            threshold=float(d.get("threshold", 0) or 0),
            direction=str(d.get("direction", "none")),
            resolution_risk=str(d.get("resolution_risk", "high")),
            tradeable=bool(d.get("tradeable", False)),
            summary=str(d.get("summary", "")),
        )


class MarketUnderstanding:
    def __init__(self, cfg: Config, llm: Optional[LLMClient] = None,
                 store: Optional[Store] = None):
        self.log = get_logger()
        self.enabled = bool(cfg.strategy.get("enable_llm_understanding", False))
        model = cfg.strategy.get("llm_model", DEFAULT_MODEL)
        self.llm = llm if llm is not None else (LLMClient(model) if self.enabled else None)
        self.store = store if store is not None else Store(cfg.db_path)

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.llm and self.llm.available)

    def _prescreen(self, market: Market) -> bool:
        return bool(_HINT.search(market.question or ""))

    def analyze(self, market: Market) -> Optional[Analysis]:
        """Return cached/fresh analysis, or None to fall back to the regex path."""
        if not self.active or not self._prescreen(market):
            return None

        text = f"{market.question}\x00{market.description}"
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        row = self.store.get_analysis(market.condition_id)
        if row and row["text_hash"] == text_hash:
            try:
                return Analysis.from_json(json.loads(row["json"]))
            except (ValueError, KeyError):
                pass  # corrupt cache -> re-analyze

        raw = self.llm.analyze(market.question, market.description,
                               market.end_date or "", SCHEMA, SYSTEM)
        if raw is None:
            return None
        try:
            analysis = Analysis.from_json(raw)
        except (ValueError, KeyError) as e:
            self.log.warning("LLM returned unparseable analysis: %s", e)
            return None
        self.store.set_analysis(market.condition_id, market.question,
                                text_hash, json.dumps(raw))
        return analysis
