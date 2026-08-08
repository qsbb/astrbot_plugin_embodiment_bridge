from __future__ import annotations

import asyncio
import re
from typing import Any

from ..adapters.astrbot_persona import (
    PersonaSelectionError,
    normalize_persona_id,
    normalize_source_mode,
)


_PERSON_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PERSONA_KEYS = (
    "persona_source_mode",
    "astrbot_persona_id",
    "character_name",
    "character_self_reference",
    "character_self_description",
    "character_user_relationship",
)


class OperatorSettingsError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


class OperatorSettings:
    """Persist administrator-only Bridge choices without exposing provider secrets."""

    def __init__(
        self,
        *,
        context: Any,
        config: Any,
        llm: Any,
        relationship: Any,
        persona: Any,
        logger: Any,
        diagnostic_log: Any | None = None,
        identity: Any | None = None,
        message_pipeline: Any | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self.llm = llm
        self.relationship = relationship
        self.persona = persona
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self.identity = identity
        self.message_pipeline = message_pipeline
        self._save_lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        providers = self._list_chat_providers()
        selected_id = str(self.llm.chat_provider_id or "").strip()
        provider_ids = {item["id"] for item in providers}
        if not providers:
            status = "empty"
        elif selected_id and selected_id not in provider_ids:
            status = "selected_missing"
        else:
            status = "ok"
        return {
            "status": status,
            "selected_id": selected_id,
            "selected_available": bool(selected_id and selected_id in provider_ids),
            "providers": providers,
            "relationship_person_id": str(
                getattr(self.relationship, "person_id", "") or ""
            ).strip(),
            "persona": self.persona_snapshot(),
            "config_writable": callable(
                getattr(self.config, "save_config_async", None)
            ),
        }

    def platform_snapshot(self) -> dict[str, Any]:
        platform_id = str(
            getattr(self.message_pipeline, "platform_id", "")
            or getattr(self.identity, "trusted_platform_id", "")
            or self.config.get("trusted_platform_id", "")
            or ""
        ).strip()
        reason = (
            str(getattr(self.message_pipeline, "availability_reason", "") or "")
            if self.message_pipeline is not None
            else "astrbot_event_api_unavailable"
        )
        return {
            "trusted_platform_id": platform_id,
            "configured": bool(platform_id),
            "available": reason == "ready",
            "availability_reason": reason or "astrbot_event_api_unavailable",
            "config_writable": callable(
                getattr(self.config, "save_config_async", None)
            ),
        }

    def persona_snapshot(self) -> dict[str, Any]:
        inherited = self.persona.status_snapshot()
        manual_mode = inherited["source_mode"] == "manual_override"
        return {
            **inherited,
            "astrbot_persona_id": self.persona.persona_id,
            "personas_status": "not_loaded",
            "personas": [],
            "character_name": str(getattr(self.llm, "character_name", "") or ""),
            "character_self_reference": str(
                getattr(self.llm, "character_self_reference", "") or ""
            ),
            "character_self_description": str(
                getattr(self.llm, "character_self_description", "") or ""
            ),
            "character_user_relationship": str(
                getattr(self.llm, "character_user_relationship", "") or ""
            ),
            "persona_configured": (
                bool(getattr(self.llm, "persona_configured", False))
                if manual_mode
                else inherited["status"] == "ready"
            ),
            "character_name_configured": bool(
                getattr(self.llm, "character_name_configured", False)
                if manual_mode
                else False
            ),
            "name_configured": bool(
                getattr(self.llm, "character_name_configured", False)
                if manual_mode
                else inherited["name_configured"]
            ),
            "config_writable": callable(
                getattr(self.config, "save_config_async", None)
            ),
        }

    async def persona_overview(self) -> dict[str, Any]:
        resolved, catalog = await asyncio.gather(
            self.persona.resolve(),
            self.persona.list_safe_personas(),
        )
        snapshot = self.persona_snapshot()
        snapshot.update(
            source=resolved.source,
            status=resolved.status,
            name_configured=(
                self.llm.character_name_configured
                if resolved.source == "manual_override"
                else resolved.name_configured
            ),
            persona_configured=(
                self.llm.persona_configured
                if resolved.source == "manual_override"
                else resolved.status == "ready"
            ),
            personas_status=catalog["status"],
            personas=catalog["personas"],
        )
        return snapshot

    async def save_chat_provider_id(self, value: str) -> dict[str, Any]:
        provider_id = str(value or "").strip()
        if not provider_id or len(provider_id) > 256:
            raise OperatorSettingsError(
                "invalid_chat_provider_id",
                422,
                "聊天模型 Provider ID 无效",
            )
        available = {item["id"] for item in self._list_chat_providers()}
        if provider_id not in available:
            raise OperatorSettingsError(
                "chat_provider_not_available",
                422,
                "所选聊天模型不存在或当前不可用",
            )
        await self._persist("chat_provider_id", provider_id)
        self.llm.configure_provider(provider_id)
        return self.snapshot()

    async def save_relationship_person_id(self, value: str) -> dict[str, Any]:
        person_id = str(value or "").strip()
        if person_id and not _PERSON_ID_RE.fullmatch(person_id):
            raise OperatorSettingsError(
                "invalid_relationship_person_id",
                422,
                "自然人 ID 无效",
            )
        await self._persist("relationship_person_id", person_id)
        self.relationship.configure_person_id(person_id)
        return self.snapshot()

    async def save_trusted_platform_id(self, value: str) -> dict[str, Any]:
        platform_id = str(value or "").strip()
        if (
            len(platform_id) > 128
            or "|" in platform_id
            or any(char.isspace() or ord(char) < 33 for char in platform_id)
        ):
            raise OperatorSettingsError(
                "invalid_trusted_platform_id",
                422,
                "AstrBot platform ID is invalid",
            )
        if platform_id:
            getter = getattr(self.context, "get_platform_inst", None)
            if not callable(getter):
                raise OperatorSettingsError(
                    "astrbot_platform_api_unavailable",
                    503,
                    "AstrBot platform lookup API is unavailable",
                )
            try:
                platform = getter(platform_id)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                raise OperatorSettingsError(
                    "astrbot_platform_lookup_failed",
                    503,
                    "AstrBot platform lookup failed",
                ) from exc
            if platform is None:
                raise OperatorSettingsError(
                    "trusted_platform_not_available",
                    422,
                    "The selected AstrBot platform is not available",
                )

        await self._persist("trusted_platform_id", platform_id)
        if self.identity is not None:
            self.identity.configure_trusted_platform(platform_id)
        if self.message_pipeline is not None:
            self.message_pipeline.configure_platform(platform_id)
        snapshot = self.platform_snapshot()
        self._diagnostic(
            "platform.updated",
            component="message_pipeline",
            status=snapshot["availability_reason"],
            configured=snapshot["configured"],
            available=snapshot["available"],
        )
        return snapshot

    async def save_character_persona(
        self,
        *,
        persona_source_mode: str,
        astrbot_persona_id: str,
        character_name: str,
        character_self_reference: str,
        character_self_description: str,
        character_user_relationship: str,
    ) -> dict[str, Any]:
        source_mode = normalize_source_mode(persona_source_mode)
        if str(persona_source_mode or "").strip().lower() not in {
            "astrbot",
            "manual_override",
        }:
            raise OperatorSettingsError(
                "invalid_persona_source_mode",
                422,
                "人格来源模式无效",
            )
        try:
            persona_id = normalize_persona_id(astrbot_persona_id)
        except ValueError as exc:
            raise OperatorSettingsError(
                "invalid_astrbot_persona_id",
                422,
                "AstrBot 人格 ID 无效",
            ) from exc
        if source_mode == "astrbot" and persona_id:
            try:
                persona_id = await self.persona.validate_selection(persona_id)
            except PersonaSelectionError as exc:
                status = 503 if str(exc) == "persona_lookup_timeout" else 422
                raise OperatorSettingsError(
                    str(exc),
                    status,
                    "所选 AstrBot 人格不存在或当前不可用",
                ) from exc

        values = {
            "persona_source_mode": source_mode,
            "astrbot_persona_id": persona_id,
            "character_name": _single_line(character_name, 64),
            "character_self_reference": _single_line(character_self_reference, 64),
            "character_self_description": _multi_line(
                character_self_description, 2_000
            ),
            "character_user_relationship": _single_line(
                character_user_relationship, 256
            ),
        }
        await self._persist_many(values)
        self.persona.configure(
            source_mode=source_mode,
            persona_id=persona_id,
        )
        self.llm.configure_persona(
            character_name=values["character_name"],
            character_self_reference=values["character_self_reference"],
            character_self_description=values["character_self_description"],
            character_user_relationship=values["character_user_relationship"],
        )
        resolved = await self.persona.resolve()
        self._diagnostic(
            "persona.updated",
            component="persona",
            status=resolved.status,
            persona_source=resolved.source,
            persona_status=resolved.status,
            persona_configured=(
                bool(any(values[key] for key in _PERSONA_KEYS[2:]))
                if source_mode == "manual_override"
                else resolved.status == "ready"
            ),
            character_name_configured=(
                bool(values["character_name"])
                if source_mode == "manual_override"
                else False
            ),
            name_configured=(
                bool(values["character_name"])
                if source_mode == "manual_override"
                else resolved.name_configured
            ),
        )
        return await self.persona_overview()

    def _list_chat_providers(self) -> list[dict[str, str]]:
        try:
            providers = self.context.get_all_providers()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            self.logger.warning(
                "[quest-avatar] failed to list chat providers: error_type=%s",
                type(exc).__name__,
            )
            return []
        if not isinstance(providers, (list, tuple)):
            return []

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in providers:
            try:
                metadata = provider.meta()
                provider_id = str(metadata.id or "").strip()
                model = str(metadata.model or "").strip()
                adapter_type = str(metadata.type or "").strip()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if not provider_id or len(provider_id) > 256 or provider_id in seen:
                continue
            seen.add(provider_id)
            items.append(
                {
                    "id": provider_id,
                    "model": model[:256],
                    "adapter_type": adapter_type[:128],
                    "provider_type": "chat_completion",
                }
            )
        items.sort(key=lambda item: (item["id"].casefold(), item["model"].casefold()))
        return items

    async def _persist(self, key: str, value: str) -> None:
        await self._persist_many({key: value})

    async def _persist_many(self, changes: dict[str, str]) -> None:
        if not changes or any(
            key
            not in {
                *_PERSONA_KEYS,
                "chat_provider_id",
                "relationship_person_id",
                "trusted_platform_id",
            }
            for key in changes
        ):
            raise OperatorSettingsError(
                "invalid_config_key",
                422,
                "配置字段不在允许范围内",
            )
        save = getattr(self.config, "save_config_async", None)
        if not callable(save):
            raise OperatorSettingsError(
                "native_config_unavailable",
                503,
                "当前 AstrBot 配置对象不支持安全异步保存",
            )

        async with self._save_lock:
            old_values: dict[str, tuple[Any, bool]] = {}
            for key in changes:
                try:
                    old_exists = key in self.config
                except TypeError:
                    old_exists = False
                try:
                    old_value = self.config.get(key)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    old_value = None
                old_values[key] = (old_value, old_exists)
            try:
                committed = await save(dict(changes))
            except Exception as exc:
                self._restore_values(old_values)
                self.logger.warning(
                    "[quest-avatar] operator setting save failed: key=%s error_type=%s",
                    key,
                    type(exc).__name__,
                )
                raise OperatorSettingsError(
                    "config_save_failed",
                    500,
                    "配置保存失败，运行时设置未改变",
                ) from exc

            if committed is not True:
                self._restore_values(old_values)
                raise OperatorSettingsError(
                    "config_save_superseded",
                    409,
                    "配置已被更新，请刷新页面后重试",
                )

    def _restore_values(self, values: dict[str, tuple[Any, bool]]) -> None:
        for key, (old_value, old_exists) in values.items():
            try:
                if old_exists:
                    self.config[key] = old_value
                else:
                    self.config.pop(key, None)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return


def _single_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _multi_line(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(char for char in text if char == "\n" or ord(char) >= 32)
    return cleaned[:limit].strip()
