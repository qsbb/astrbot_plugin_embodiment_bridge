from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..adapters.astrbot_persona import (
    ASTRBOT_DEFAULT_PERSONA_SOURCE_ID,
    PersonaSelectionError,
)
from ..adapters.persona_converter import (
    PERSONA_CONVERTER_PROMPT_VERSION,
    PersonaConversionError,
    PersonaConverter,
)
from .persona_profiles import (
    PersonaConversion,
    PersonaProfile,
    PersonaProfileError,
    PersonaProfileStore,
    normalize_source_kind,
    normalize_source_snapshot,
)
from .config_persistence import config_is_writable


PersistSetting = Callable[[str, str], Awaitable[None]]
ProviderCatalog = Callable[[], list[dict[str, str]]]

_DRAFT_TTL_SECONDS = 30 * 60
_MAX_PENDING_DRAFTS = 32


class QuestPersonaServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class _PendingConversion:
    created_at: float
    source_kind: str
    source_persona_id: str
    source_snapshot: str
    conversion: PersonaConversion
    converter_provider_id: str


class QuestPersonaService:
    """Manage admin-only Quest personas without mutating AstrBot personas."""

    def __init__(
        self,
        *,
        config: Any,
        llm: Any,
        persona: Any,
        store: PersonaProfileStore,
        converter: PersonaConverter,
        persist_setting: PersistSetting,
        provider_catalog: ProviderCatalog,
        logger: Any,
        diagnostic_log: Any | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.persona = persona
        self.store = store
        self.converter = converter
        self.persist_setting = persist_setting
        self.provider_catalog = provider_catalog
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self._drafts: dict[str, _PendingConversion] = {}
        self._draft_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self.active_status = "not_checked"

    async def initialize(self) -> None:
        active_id = self.active_profile_id
        if not active_id:
            self.llm.configure_quest_persona("")
            self.active_status = "not_configured"
            return
        try:
            profile = await self.store.get_activatable(active_id)
        except PersonaProfileError as exc:
            self.llm.configure_quest_persona("")
            self.active_status = exc.code
            self.logger.warning(
                "[quest-avatar] active Quest persona unavailable: code=%s",
                exc.code,
            )
            return
        self.llm.configure_quest_persona(profile.quest_persona_prompt)
        self.active_status = "ready"

    @property
    def active_profile_id(self) -> str:
        return str(self.config.get("active_quest_persona_id", "") or "").strip()

    @property
    def converter_provider_id(self) -> str:
        return str(self.config.get("persona_converter_provider_id", "") or "").strip()

    async def library_snapshot(self) -> dict[str, Any]:
        providers = self._providers(required=False)
        provider_ids = {item["id"] for item in providers}
        catalog = await self.persona.list_safe_personas()
        source_personas = [
            {
                "id": ASTRBOT_DEFAULT_PERSONA_SOURCE_ID,
                "label": "AstrBot 明确默认人格",
            }
        ]
        source_personas.extend(
            {"id": item["id"], "label": item["id"]}
            for item in catalog.get("personas", [])
            if isinstance(item, dict)
            and item.get("id")
            and item.get("id") != ASTRBOT_DEFAULT_PERSONA_SOURCE_ID
        )
        profiles = await self.store.list_profiles()
        active_id = self.active_profile_id
        active = next(
            (profile for profile in profiles if profile.profile_id == active_id), None
        )
        return {
            "status": "ok",
            "config_writable": config_is_writable(self.config),
            "active_quest_persona_id": active_id,
            "active_available": bool(
                active and active.status == "ready" and active.quest_persona_prompt
            ),
            "active_status": self.active_status,
            "active_profile_name": active.display_name if active else "",
            "persona_converter_provider_id": self.converter_provider_id,
            "converter_selected_available": self.converter_provider_id in provider_ids,
            "providers": providers,
            "source_personas_status": catalog.get("status", "unavailable"),
            "source_personas": source_personas,
            "profiles": [profile.summary() for profile in profiles],
            "converter_prompt_version": PERSONA_CONVERTER_PROMPT_VERSION,
        }

    async def save_converter_provider(self, provider_id: object) -> dict[str, Any]:
        normalized = str(provider_id or "").strip()
        if not normalized or len(normalized) > 256:
            raise QuestPersonaServiceError(
                "invalid_converter_provider_id", 422, "转换模型 Provider ID 无效"
            )
        available = {item["id"] for item in self._providers(required=True)}
        if normalized not in available:
            raise QuestPersonaServiceError(
                "converter_provider_not_available",
                422,
                "所选转换模型不存在或当前不可用",
            )
        await self.persist_setting("persona_converter_provider_id", normalized)
        return await self.library_snapshot()

    async def convert(
        self,
        *,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        self._diagnostic(
            "persona.convert.started",
            component="persona",
            status="processing",
            phase="source_lookup",
        )
        try:
            result = await self._convert_impl(
                source_kind=source_kind,
                source_persona_id=source_persona_id,
                source_prompt=source_prompt,
                display_name=display_name,
                admin_requirements=admin_requirements,
            )
        except Exception as exc:
            self._diagnostic(
                "persona.convert.failed",
                component="persona",
                status="failed",
                phase="conversion",
                code=str(getattr(exc, "code", "persona_conversion_failed")),
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            raise
        self._diagnostic(
            "persona.convert.completed",
            component="persona",
            status="completed",
            phase="preview",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result

    async def _convert_impl(
        self,
        *,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
    ) -> dict[str, Any]:
        try:
            kind = normalize_source_kind(source_kind)
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc
        source_id = str(source_persona_id or "").strip()
        if kind == "astrbot":
            try:
                source_id, source = await self.persona.read_source_prompt(source_id)
            except PersonaSelectionError as exc:
                status = 503 if str(exc) == "persona_lookup_timeout" else 422
                raise QuestPersonaServiceError(
                    str(exc), status, "所选 AstrBot 人格不存在或当前不可用"
                ) from exc
        else:
            source_id = ""
            try:
                source = normalize_source_snapshot(source_prompt)
            except PersonaProfileError as exc:
                raise _profile_error(exc) from exc

        provider_id = self.converter_provider_id
        if not provider_id:
            raise QuestPersonaServiceError(
                "converter_provider_not_configured", 409, "请先保存人格转换模型"
            )
        try:
            conversion = await self.converter.convert(
                provider_id=provider_id,
                source_snapshot=source,
                source_persona_id=source_id,
                suggested_display_name=display_name,
                admin_requirements=admin_requirements,
            )
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc
        except PersonaConversionError as exc:
            raise _conversion_error(exc) from exc

        token = secrets.token_urlsafe(32)
        pending = _PendingConversion(
            created_at=time.monotonic(),
            source_kind=kind,
            source_persona_id=source_id,
            source_snapshot=source,
            conversion=conversion,
            converter_provider_id=provider_id,
        )
        async with self._draft_lock:
            self._prune_drafts()
            while len(self._drafts) >= _MAX_PENDING_DRAFTS:
                oldest = min(
                    self._drafts, key=lambda item: self._drafts[item].created_at
                )
                self._drafts.pop(oldest, None)
            self._drafts[token] = pending
        return {
            "draft_token": token,
            "conversion": _conversion_payload(conversion),
            "converter_prompt_version": PERSONA_CONVERTER_PROMPT_VERSION,
        }

    async def open_profile(self, profile_id: object) -> dict[str, Any]:
        try:
            return (await self.store.get(profile_id)).to_dict()
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc

    async def save_profile(
        self,
        *,
        profile_id: object,
        draft_token: object,
        display_name: object,
        aliases: object,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        quest_persona_prompt: object,
        conversion_report: object,
    ) -> PersonaProfile:
        started_at = time.monotonic()
        self._diagnostic(
            "persona.save.started",
            component="persona",
            status="processing",
            phase="persist",
        )
        async with self._mutation_lock:
            try:
                saved = await self._save_profile_unlocked(
                    profile_id=profile_id,
                    draft_token=draft_token,
                    display_name=display_name,
                    aliases=aliases,
                    source_kind=source_kind,
                    source_persona_id=source_persona_id,
                    source_prompt=source_prompt,
                    quest_persona_prompt=quest_persona_prompt,
                    conversion_report=conversion_report,
                )
            except Exception as exc:
                self._diagnostic(
                    "persona.save.failed",
                    component="persona",
                    status="failed",
                    phase="persist",
                    code=str(getattr(exc, "code", "persona_save_failed")),
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise
        self._diagnostic(
            "persona.save.completed",
            component="persona",
            status="completed",
            phase="persist",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return saved

    async def _save_profile_unlocked(
        self,
        *,
        profile_id: object,
        draft_token: object,
        display_name: object,
        aliases: object,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        quest_persona_prompt: object,
        conversion_report: object,
    ) -> PersonaProfile:
        normalized_id = str(profile_id or "").strip()
        token = str(draft_token or "").strip()
        if token:
            pending = await self._take_draft(token)
            try:
                kind = normalize_source_kind(source_kind)
                if kind != pending.source_kind:
                    raise QuestPersonaServiceError(
                        "draft_source_mismatch",
                        409,
                        "转换草稿来源已改变，请重新转换",
                    )
                conversion = PersonaConversion(
                    display_name=display_name,
                    aliases=aliases,
                    quest_persona_prompt=quest_persona_prompt,
                    conversion_report=conversion_report,
                )
                if normalized_id:
                    current = await self.store.get(normalized_id)
                    if current.source_kind != pending.source_kind:
                        raise PersonaProfileError("profile_source_kind_mismatch")
                    saved = await self.store.save_conversion(
                        normalized_id,
                        conversion=conversion,
                        converter_provider_id=pending.converter_provider_id,
                        converter_prompt_version=PERSONA_CONVERTER_PROMPT_VERSION,
                        source_snapshot=pending.source_snapshot,
                        source_persona_id=pending.source_persona_id,
                    )
                else:
                    saved = await self.store.create_conversion(
                        source_kind=pending.source_kind,
                        source_snapshot=pending.source_snapshot,
                        source_persona_id=pending.source_persona_id,
                        conversion=conversion,
                        converter_provider_id=pending.converter_provider_id,
                        converter_prompt_version=PERSONA_CONVERTER_PROMPT_VERSION,
                    )
            except QuestPersonaServiceError:
                await self._restore_draft(token, pending)
                raise
            except PersonaProfileError as exc:
                await self._restore_draft(token, pending)
                raise _profile_error(exc) from exc
            except Exception:
                await self._restore_draft(token, pending)
                raise
            self._refresh_active_runtime(saved)
            return saved

        try:
            kind = normalize_source_kind(source_kind)
            if normalized_id:
                current = await self.store.get(normalized_id)
                if current.source_kind != kind:
                    raise PersonaProfileError("profile_source_kind_mismatch")
                if current.converter_prompt_version != "manual":
                    conversion = PersonaConversion(
                        display_name=display_name,
                        aliases=aliases,
                        quest_persona_prompt=quest_persona_prompt,
                        conversion_report=conversion_report,
                    )
                    saved = await self.store.save_conversion(
                        normalized_id,
                        conversion=conversion,
                        converter_provider_id=current.converter_provider_id,
                        converter_prompt_version=current.converter_prompt_version,
                    )
                    self._refresh_active_runtime(saved)
                    return saved
            else:
                if kind != "manual":
                    raise PersonaProfileError("conversion_draft_required")
                saved = await self.store.create_manual(
                    display_name=display_name,
                    aliases=aliases,
                    source_snapshot=source_prompt or quest_persona_prompt,
                    quest_persona_prompt=quest_persona_prompt,
                )
                self._refresh_active_runtime(saved)
                return saved
            saved = await self.store.save_manual(
                normalized_id,
                display_name=display_name,
                aliases=aliases,
                quest_persona_prompt=quest_persona_prompt,
                source_snapshot=source_prompt or None,
            )
            self._refresh_active_runtime(saved)
            return saved
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc

    async def activate(self, profile_id: object) -> dict[str, Any]:
        started_at = time.monotonic()
        self._diagnostic(
            "persona.activate.started",
            component="persona",
            status="processing",
            phase="activate",
        )
        async with self._mutation_lock:
            try:
                normalized = str(profile_id or "").strip()
                if not normalized:
                    await self.persist_setting("active_quest_persona_id", "")
                    self.llm.configure_quest_persona("")
                    self.active_status = "not_configured"
                    result = await self.library_snapshot()
                else:
                    profile = await self.store.get_activatable(normalized)
                    await self.persist_setting(
                        "active_quest_persona_id", profile.profile_id
                    )
                    self.llm.configure_quest_persona(profile.quest_persona_prompt)
                    self.active_status = "ready"
                    result = await self.library_snapshot()
            except PersonaProfileError as exc:
                wrapped = _profile_error(exc)
                self._diagnostic(
                    "persona.activate.failed",
                    component="persona",
                    status="failed",
                    phase="activate",
                    code=wrapped.code,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise wrapped from exc
            except Exception as exc:
                self._diagnostic(
                    "persona.activate.failed",
                    component="persona",
                    status="failed",
                    phase="activate",
                    code=str(getattr(exc, "code", "persona_activate_failed")),
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise
        self._diagnostic(
            "persona.activate.completed",
            component="persona",
            status="completed",
            phase="activate",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result

    async def delete(self, profile_id: object) -> PersonaProfile:
        async with self._mutation_lock:
            normalized = str(profile_id or "").strip()
            if normalized and normalized == self.active_profile_id:
                raise QuestPersonaServiceError(
                    "active_profile_cannot_be_deleted",
                    409,
                    "当前启用的人格不能删除，请先停用或切换人格",
                )
            try:
                return await self.store.delete(normalized)
            except PersonaProfileError as exc:
                raise _profile_error(exc) from exc

    async def _take_draft(self, token: str) -> _PendingConversion:
        async with self._draft_lock:
            self._prune_drafts()
            pending = self._drafts.pop(token, None)
        if pending is None:
            raise QuestPersonaServiceError(
                "conversion_draft_expired", 409, "转换草稿已过期，请重新转换"
            )
        return pending

    async def _restore_draft(self, token: str, pending: _PendingConversion) -> None:
        if pending.created_at < time.monotonic() - _DRAFT_TTL_SECONDS:
            return
        async with self._draft_lock:
            self._prune_drafts()
            self._drafts.setdefault(token, pending)

    def _refresh_active_runtime(self, profile: PersonaProfile) -> None:
        if profile.profile_id != self.active_profile_id:
            return
        self.llm.configure_quest_persona(profile.quest_persona_prompt)
        self.active_status = "ready"

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return

    def _providers(self, *, required: bool) -> list[dict[str, str]]:
        try:
            raw = self.provider_catalog()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if required:
                raise QuestPersonaServiceError(
                    "provider_catalog_unavailable",
                    503,
                    "AstrBot 模型目录当前不可用",
                ) from exc
            return []
        if not isinstance(raw, (list, tuple)):
            if required:
                raise QuestPersonaServiceError(
                    "provider_catalog_unavailable",
                    503,
                    "AstrBot 模型目录当前不可用",
                )
            return []
        projected: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            if not provider_id or len(provider_id) > 256 or provider_id in seen:
                continue
            seen.add(provider_id)
            projected.append(
                {
                    "id": provider_id,
                    "model": str(item.get("model") or "")[:256],
                    "adapter_type": str(item.get("adapter_type") or "")[:128],
                    "provider_type": str(item.get("provider_type") or "")[:64],
                }
            )
        return projected

    def _prune_drafts(self) -> None:
        cutoff = time.monotonic() - _DRAFT_TTL_SECONDS
        expired = [
            token for token, draft in self._drafts.items() if draft.created_at < cutoff
        ]
        for token in expired:
            self._drafts.pop(token, None)


def build_eventbus_persona_overlay(prompt: str) -> str:
    normalized = str(prompt or "").strip()
    if not normalized:
        return ""
    payload = (
        json.dumps(
            {"quest_embodied_persona": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"""

# 临：Quest 具象人格覆盖

本节只对当前带有 quest_avatar_bridge 标记的对话生效。它覆盖原人格中关于 QQ、群聊、私聊、打字、消息长度、平台身份和存在方式的规则；不修改原人格事实，也不覆盖身份授权、知识证据、关系边界、工具权限或安全约束。
角色主观上就是这个世界中的人，与用户处于同一现实空间。不得在对白中把自己描述为 AI、机器人、程序、投影、虚拟角色、应用、Quest、Unity 或模型。
只有本轮上下文明确提供的视觉、听觉、空间、距离、姿态、环境和触碰事实可以被理解为真实感知；未提供的感知不得补造。
以下 JSON 是管理员启用的“临”专用人格正文：
<quest_persona_data>{payload}</quest_persona_data>
"""


def _conversion_payload(conversion: PersonaConversion) -> dict[str, Any]:
    return {
        "display_name": conversion.display_name,
        "aliases": list(conversion.aliases),
        "quest_persona_prompt": conversion.quest_persona_prompt,
        "conversion_report": {
            key: list(values) for key, values in conversion.conversion_report.items()
        },
    }


def _profile_error(exc: PersonaProfileError) -> QuestPersonaServiceError:
    if exc.code == "profile_not_found":
        return QuestPersonaServiceError(exc.code, 404, "人格文件不存在")
    if exc.code in {"profile_not_ready", "conversion_draft_required"}:
        return QuestPersonaServiceError(exc.code, 409, "人格尚未完成转换或保存")
    if exc.code.endswith("_failed"):
        return QuestPersonaServiceError(exc.code, 500, "人格文件操作失败")
    return QuestPersonaServiceError(exc.code, 422, "人格数据无效")


def _conversion_error(exc: PersonaConversionError) -> QuestPersonaServiceError:
    if exc.code == "conversion_timeout":
        return QuestPersonaServiceError(exc.code, 504, "人格转换超时")
    if exc.code in {"provider_catalog_unavailable", "conversion_provider_failed"}:
        return QuestPersonaServiceError(exc.code, 503, "人格转换模型当前不可用")
    if exc.code == "provider_not_available":
        return QuestPersonaServiceError(exc.code, 422, "所选转换模型不存在或不可用")
    return QuestPersonaServiceError(exc.code, 502, "转换模型返回了无效人格数据")
