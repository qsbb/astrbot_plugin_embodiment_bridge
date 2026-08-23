from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from .avatar_skills import AvatarSkillRegistry
from .models import ModelDecision, safe_neutral_decision


class IntentParser:
    """Parse reply JSON while keeping autonomous actions out of the main path."""

    def __init__(self, *, allow_model_actions: bool = False) -> None:
        self.allow_model_actions = bool(allow_model_actions)

    def parse(self, raw: Any) -> ModelDecision:
        if not isinstance(raw, str) or not raw.strip():
            return safe_neutral_decision()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("model output is not an object")
            action = payload.get("action")
            if self.allow_model_actions and action is not None:
                if not isinstance(action, dict):
                    raise ValueError("action is not an object")
                action_intent = AvatarSkillRegistry.invoke(
                    action.get("name"), action.get("arguments")
                )
                if action_intent is None:
                    raise ValueError("unknown or invalid avatar skill")
                payload["intent"] = action_intent.model_dump(mode="json")
            elif not self.allow_model_actions:
                # Legacy action/intent fields are ignored rather than allowed
                # to steer the avatar from a normal dialogue response.
                should_reply = payload.get("should_reply") is True
                reply_text = payload.get("reply_text", "")
                if not isinstance(reply_text, str):
                    raise ValueError("reply_text is not a string")
                payload.pop("action", None)
                payload.pop("intent", None)
                payload["should_reply"] = should_reply
                payload["reply_text"] = reply_text if should_reply else ""
                payload["intent"] = {
                    "emotion": "neutral",
                    "gesture": "talk" if should_reply else "idle",
                    "look_at": "user" if should_reply else "none",
                    "intensity": 0.0,
                    "duration_ms": 0,
                    "reason_code": "dialogue_only",
                }
            return ModelDecision.model_validate(payload)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            return safe_neutral_decision()
