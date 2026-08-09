from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal


PersonaSourceMode = Literal["astrbot", "manual_override"]
PersonaSource = Literal[
    "astrbot_selected",
    "astrbot_default",
    "manual_override",
    "generic",
]

_VALID_MODES = frozenset({"astrbot", "manual_override"})
_MAX_PERSONA_ID_CHARS = 255
_MAX_PROMPT_CHARS = 16_000
ASTRBOT_DEFAULT_PERSONA_SOURCE_ID = "@astrbot-default"


@dataclass(frozen=True, slots=True)
class PersonaSnapshot:
    source: PersonaSource
    status: str
    prompt: str = ""
    selected: bool = False
    name_configured: bool = False


class AstrBotPersonaAdapter:
    """Read AstrBot's documented PersonaManager without exposing prompt content."""

    def __init__(
        self,
        context: Any,
        *,
        source_mode: str = "astrbot",
        persona_id: str = "",
        timeout_seconds: float = 1.0,
    ) -> None:
        self.context = context
        self.source_mode = normalize_source_mode(source_mode)
        self._configuration_invalid = False
        try:
            self.persona_id = normalize_persona_id(persona_id)
        except ValueError:
            self.persona_id = ""
            self._configuration_invalid = True
        self.timeout_seconds = max(0.05, min(float(timeout_seconds), 5.0))
        self._last_snapshot = self._initial_snapshot()

    def configure(self, *, source_mode: str, persona_id: str) -> None:
        self.source_mode = normalize_source_mode(source_mode)
        self.persona_id = normalize_persona_id(persona_id)
        self._configuration_invalid = False
        self._last_snapshot = self._initial_snapshot()

    def status_snapshot(self) -> dict[str, Any]:
        snapshot = self._last_snapshot
        return {
            "source_mode": self.source_mode,
            "source": snapshot.source,
            "status": snapshot.status,
            "persona_selected": bool(self.persona_id),
            "name_configured": snapshot.name_configured,
        }

    async def resolve(self) -> PersonaSnapshot:
        if self.source_mode == "manual_override":
            return self._remember(
                PersonaSnapshot(
                    source="manual_override",
                    status="ready",
                    selected=False,
                )
            )
        if self._configuration_invalid:
            return self._remember(self._generic("configuration_invalid"))

        try:
            manager = self.context.persona_manager
            if self.persona_id:
                persona = await asyncio.wait_for(
                    manager.get_persona(self.persona_id),
                    timeout=self.timeout_seconds,
                )
                prompt = _validated_prompt(persona.system_prompt)
                return self._remember(
                    PersonaSnapshot(
                        source="astrbot_selected",
                        status="ready",
                        prompt=prompt,
                        selected=True,
                    )
                )

            personality = await asyncio.wait_for(
                manager.get_default_persona_v3(None),
                timeout=self.timeout_seconds,
            )
            if not isinstance(personality, dict):
                raise TypeError("default persona response is not an object")
            prompt = _validated_prompt(personality["prompt"])
            return self._remember(
                PersonaSnapshot(
                    source="astrbot_default",
                    status="ready",
                    prompt=prompt,
                )
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._remember(self._generic("timeout"))
        except ValueError:
            status = "selected_missing" if self.persona_id else "default_missing"
            return self._remember(self._generic(status))
        except (AttributeError, KeyError, RuntimeError, TypeError):
            return self._remember(self._generic("unavailable"))

    async def list_safe_personas(self) -> dict[str, Any]:
        try:
            manager = self.context.persona_manager
            personas = await asyncio.wait_for(
                manager.get_all_personas(),
                timeout=self.timeout_seconds,
            )
            if not isinstance(personas, (list, tuple)):
                raise TypeError("persona list response is not a sequence")
            ids: list[str] = []
            seen: set[str] = set()
            for persona in personas:
                persona_id = normalize_persona_id(persona.persona_id)
                if not persona_id or persona_id in seen:
                    continue
                seen.add(persona_id)
                ids.append(persona_id)
            ids.sort(key=str.casefold)
            return {
                "status": "ok",
                "personas": [{"id": persona_id} for persona_id in ids],
            }
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return {"status": "timeout", "personas": []}
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return {"status": "unavailable", "personas": []}

    async def validate_selection(self, persona_id: str) -> str:
        normalized = normalize_persona_id(persona_id)
        if not normalized:
            return ""
        try:
            manager = self.context.persona_manager
            persona = await asyncio.wait_for(
                manager.get_persona(normalized),
                timeout=self.timeout_seconds,
            )
            actual_id = normalize_persona_id(persona.persona_id)
            _validated_prompt(persona.system_prompt)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise PersonaSelectionError("persona_lookup_timeout") from exc
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise PersonaSelectionError("persona_not_available") from exc
        if actual_id != normalized:
            raise PersonaSelectionError("persona_not_available")
        return normalized

    async def read_source_prompt(self, persona_id: str) -> tuple[str, str]:
        """Read one administrator-selected source without exposing other personas."""
        normalized = normalize_persona_id(persona_id)
        try:
            manager = self.context.persona_manager
            if normalized == ASTRBOT_DEFAULT_PERSONA_SOURCE_ID:
                personality = await asyncio.wait_for(
                    manager.get_default_persona_v3(None),
                    timeout=self.timeout_seconds,
                )
                if not isinstance(personality, dict):
                    raise TypeError("default persona response is not an object")
                return normalized, _validated_prompt(personality["prompt"])
            if not normalized:
                raise ValueError("persona id is empty")
            persona = await asyncio.wait_for(
                manager.get_persona(normalized),
                timeout=self.timeout_seconds,
            )
            actual_id = normalize_persona_id(persona.persona_id)
            prompt = _validated_prompt(persona.system_prompt)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise PersonaSelectionError("persona_lookup_timeout") from exc
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            raise PersonaSelectionError("persona_not_available") from exc
        if actual_id != normalized:
            raise PersonaSelectionError("persona_not_available")
        return actual_id, prompt

    def _initial_snapshot(self) -> PersonaSnapshot:
        if self.source_mode == "manual_override":
            return PersonaSnapshot(source="manual_override", status="not_checked")
        return self._generic("not_checked")

    @staticmethod
    def _generic(status: str) -> PersonaSnapshot:
        return PersonaSnapshot(source="generic", status=status)

    def _remember(self, snapshot: PersonaSnapshot) -> PersonaSnapshot:
        self._last_snapshot = snapshot
        return snapshot


class PersonaSelectionError(RuntimeError):
    pass


def normalize_source_mode(value: object) -> PersonaSourceMode:
    normalized = str(value or "astrbot").strip().lower()
    return normalized if normalized in _VALID_MODES else "astrbot"


def normalize_persona_id(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > _MAX_PERSONA_ID_CHARS:
        raise ValueError("persona id is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("persona id contains control characters")
    return normalized


def _validated_prompt(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("persona prompt is not text")
    prompt = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not prompt:
        raise ValueError("persona prompt is empty")
    return prompt[:_MAX_PROMPT_CHARS]
