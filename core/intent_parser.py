from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .models import ModelDecision, safe_neutral_decision


class IntentParser:
    """Strictly parse one JSON object and fail closed on any model drift."""

    def parse(self, raw: Any) -> ModelDecision:
        if not isinstance(raw, str) or not raw.strip():
            return safe_neutral_decision()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("model output is not an object")
            return ModelDecision.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            return safe_neutral_decision()
