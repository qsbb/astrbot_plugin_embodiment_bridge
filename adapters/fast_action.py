from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..core.avatar_skills import AvatarSkillRegistry
from ..core.models import InteractionEvent, ProposedIntent


class FastActionUnavailable(RuntimeError):
    """Raised when the optional fast action provider cannot be used."""


class FastActionDecisionAdapter:
    """Small, action-only LLM adapter kept outside the normal reply pipeline.

    The adapter deliberately receives only the current utterance and a short
    bounded history. It never generates reply text and never exposes provider
    configuration. The resulting action is still validated by the shared
    ``AvatarSkillRegistry`` before it reaches the client.
    """

    def __init__(
        self,
        context: Any,
        *,
        provider_id: str = "",
        enabled: bool = True,
        timeout_seconds: float = 4.0,
    ) -> None:
        self.context = context
        self.provider_id = str(provider_id or "").strip()
        self.enabled = bool(enabled)
        self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
        self.last_status = "disabled" if not self.enabled else "not_configured"
        self.last_error = ""
        self.last_duration_ms = 0
        self.last_action = ""

    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.provider_id:
            return "provider_not_configured"
        try:
            generator = self.context.llm_generate
        except AttributeError:
            return "llm_api_unavailable"
        if not callable(generator):
            return "llm_api_unavailable"
        catalog = getattr(self.context, "get_all_providers", None)
        if callable(catalog):
            try:
                providers = catalog()
            except Exception:
                return "provider_catalog_unavailable"
            matched = False
            if isinstance(providers, (list, tuple)):
                for provider in providers:
                    try:
                        if str(provider.meta().id or "").strip() == self.provider_id:
                            matched = True
                            break
                    except Exception:
                        continue
            if not matched:
                return "selected_missing"
        return "ready"

    def configure(
        self,
        *,
        enabled: bool | None = None,
        provider_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if provider_id is not None:
            self.provider_id = str(provider_id or "").strip()
        if timeout_seconds is not None:
            self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
        self.last_status = "disabled" if not self.enabled else (
            "ready" if self.provider_id else "not_configured"
        )
        self.last_error = ""
        self.last_action = ""
        self.last_duration_ms = 0

    async def decide(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        interaction: InteractionEvent | None = None,
    ) -> ProposedIntent | None:
        if not self.enabled:
            self.last_status = "disabled"
            raise FastActionUnavailable("fast_action_disabled")
        if not self.provider_id:
            self.last_status = "not_configured"
            raise FastActionUnavailable("fast_action_provider_not_configured")
        availability = self.availability_reason
        if availability != "ready":
            self.last_status = "unavailable"
            self.last_error = availability
            raise FastActionUnavailable(f"fast_action_{availability}")
        try:
            generator = self.context.llm_generate
        except AttributeError as exc:
            self.last_status = "unavailable"
            self.last_error = "llm_api_unavailable"
            raise FastActionUnavailable("fast_action_llm_api_unavailable") from exc

        payload = {
            "current_user_text": str(user_text or "")[:2_000],
            "recent_conversation": self._bounded_history(history),
            "interaction": (
                interaction.model_dump(mode="json")
                if interaction is not None
                else None
            ),
        }
        started = asyncio.get_running_loop().time()
        self.last_error = ""
        self.last_action = ""
        self.last_status = "processing"
        try:
            response = await asyncio.wait_for(
                generator(
                    chat_provider_id=self.provider_id,
                    prompt=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    system_prompt=self._system_prompt(),
                ),
                timeout=self.timeout_seconds,
            )
            raw = getattr(response, "completion_text", response)
            intent = self._parse(raw)
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            self.last_status = "selected" if intent is not None else "no_action"
            if intent is not None:
                self.last_action = intent.gesture.value
            return intent
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            self.last_status = "timeout"
            self.last_error = "fast_action_timeout"
            raise FastActionUnavailable("fast_action_timeout") from exc
        except FastActionUnavailable:
            raise
        except Exception as exc:
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            self.last_status = "error"
            self.last_error = type(exc).__name__
            raise FastActionUnavailable("fast_action_failed") from exc

    @staticmethod
    def _bounded_history(
        history: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        bounded: list[dict[str, str]] = []
        for item in (history or [])[-4:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            text = str(item.get("text") or "").strip()
            if role not in {"user", "assistant"} or not text:
                continue
            bounded.append({"role": role, "text": text[:800]})
        return bounded

    @staticmethod
    def _system_prompt() -> str:
        names = ",".join(AvatarSkillRegistry.names())
        return (
            "You are the fast action selector for an embodied avatar. "
            "Decide only whether the avatar should perform one allowlisted "
            "action in response to the user's current utterance. Prefer an "
            "explicitly requested action; a subtle natural gesture is allowed "
            "only when it is clearly appropriate. Do not answer the user. "
            "Return exactly one compact JSON object and no markdown: "
            '{"action":null} or '
            '{"action":{"name":"<name>","arguments":{"emotion":"neutral",'
            '"intensity":0.45,"duration_ms":2000,"look_at":"user"}}}. '
            "Use null for ordinary conversation, descriptions, quoted requests, "
            "or ambiguous/unsafe wording. Never invent names, "
            "files, bones, paths, or arguments. Allowlisted names: "
            + names
        )

    @staticmethod
    def _parse(raw: Any) -> ProposedIntent | None:
        if not isinstance(raw, str) or not raw.strip():
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if set(payload) != {"action"}:
            return None
        action = payload.get("action")
        if action is None:
            return None
        if isinstance(action, str):
            name = action
            arguments = {}
        elif isinstance(action, dict):
            if "name" not in action or set(action) - {"name", "arguments"}:
                return None
            name = action.get("name")
            arguments = action.get("arguments", {})
        else:
            return None
        if not isinstance(arguments, dict):
            return None
        return AvatarSkillRegistry.invoke(name, arguments)

    def snapshot(self) -> dict[str, Any]:
        availability = self.availability_reason
        status = self.last_status
        if availability != "ready" and status != "processing":
            status = availability
        return {
            "enabled": self.enabled,
            "available": self.available,
            "availability_reason": availability,
            "selected": bool(self.provider_id),
            "selected_id": self.provider_id,
            "status": status,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
            "last_action": self.last_action,
            "timeout_seconds": self.timeout_seconds,
        }

    async def close(self) -> None:
        return None
