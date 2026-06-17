"""Thin wrapper over the Anthropic API for structured market analysis.

Degrades gracefully on purpose: if the `anthropic` SDK isn't installed or
`ANTHROPIC_API_KEY` isn't set, `.available` is False and the rest of the bot
falls back to its non-AI behavior. No key, no problem — nothing breaks.

Uses Claude Haiku 4.5 by default (cheap + fast — this runs across many
markets). You can point `llm_model` in config.yaml at a stronger model.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .log import get_logger

DEFAULT_MODEL = "claude-haiku-4-5"


class LLMClient:
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.log = get_logger()
        self._client = None
        self.available = False
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.log.info("LLM understanding off: ANTHROPIC_API_KEY not set.")
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic()
            self.available = True
            self.log.info("LLM understanding on (model=%s).", self.model)
        except Exception as e:  # SDK missing / import error
            self.log.warning("anthropic SDK unavailable (%s); LLM understanding off.", e)

    def analyze(self, question: str, description: str, end_date: str,
                schema: dict, system: str) -> Optional[dict]:
        """Send one market to the model; return parsed JSON or None on failure."""
        if not self.available:
            return None
        user = (f"QUESTION: {question}\n\n"
                f"RESOLUTION RULES:\n{description or '(none provided)'}\n\n"
                f"ENDS: {end_date or '(unknown)'}")
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=500,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), None)
            return json.loads(text) if text else None
        except Exception as e:
            # Any failure (network, rate limit, schema, old SDK) -> fall back.
            self.log.warning("LLM analyze failed (%s); using fallback.", e)
            return None
