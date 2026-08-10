from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

from ..adapters.astrbot_persona import (
    PersonaSelectionError,
    normalize_persona_id,
    normalize_source_mode,
)
from ..adapters.identity_control_plane import (
    IdentityControlPlaneAdapter,
    IdentityControlPlaneError,
    validate_principal_digest,
)
from .config_persistence import config_is_writable, save_config_changes


_PERSON_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
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
        stt: Any | None = None,
        diagnostic_log: Any | None = None,
        identity: Any | None = None,
        message_pipeline: Any | None = None,
        identity_control_plane: IdentityControlPlaneAdapter | None = None,
        pairing_manager: Any | None = None,
        transport: Any | None = None,
        identity_store: Any | None = None,
        config_save_lock: asyncio.Lock | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self.llm = llm
        self.stt = stt
        self.relationship = relationship
        self.persona = persona
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self.identity = identity
        self.message_pipeline = message_pipeline
        self.identity_control_plane = identity_control_plane
        self.pairing_manager = pairing_manager
        self.transport = transport
        self.identity_store = identity_store
        self._save_lock = config_save_lock or asyncio.Lock()
        self._identity_sync_lock = asyncio.Lock()

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
            "config_writable": config_is_writable(self.config),
        }

    def platform_snapshot(self) -> dict[str, Any]:
        platforms = self._list_platforms()
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
            "platforms_status": "ok" if platforms else "empty",
            "platforms": platforms,
            "config_writable": config_is_writable(self.config),
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
            "config_writable": config_is_writable(self.config),
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

    async def quest_identity_overview(self) -> dict[str, Any]:
        try:
            control_plane = (
                await self.identity_control_plane.snapshot()
                if self.identity_control_plane is not None
                else {
                    "source": "bridge_local",
                    "authoritative": False,
                    "status": "ready",
                    "reason": "identity_guardian_not_installed",
                    "config_writable": True,
                    "owner_count": 0,
                    "quest_binding_count": 0,
                }
            )
        except IdentityControlPlaneError as exc:
            control_plane = {
                "source": "identity_guardian",
                "authoritative": True,
                "status": "unavailable",
                "reason": exc.code,
                "config_writable": False,
                "owner_count": 0,
                "quest_binding_count": 0,
            }
        api_key = str(self.config.get("pairing_astrbot_api_key", "") or "")
        bridge_key = str(self.config.get("bridge_api_key", "") or "")
        client_id = str(self.config.get("trusted_client_id", "") or "").strip()
        platform = self.platform_snapshot()
        server_identity = (
            getattr(self.identity_store, "identity", None)
            if self.identity_store is not None
            else None
        )
        bot_id = str(getattr(server_identity, "bot_id", "") or "").strip()
        user_id = str(getattr(server_identity, "user_id", "") or "").strip()
        ready = bool(
            len(api_key) >= 16
            and len(bridge_key) >= 32
            and client_id
            and platform["trusted_platform_id"]
            and bot_id
            and user_id
            and control_plane.get("status") == "ready"
            and str(self.config.get("pairing_identity_sync_state", "ready")) == "ready"
        )
        return {
            "status": "ready" if ready else "incomplete",
            "client_id": client_id,
            "platform_id": platform["trusted_platform_id"],
            "bot_id": "",
            "user_id": "",
            "bot_id_configured": bool(bot_id),
            "user_id_configured": bool(user_id),
            "identity_source": str(
                self.config.get("pairing_identity_source", "manual") or "manual"
            )[:32],
            "identity_sync_state": str(
                self.config.get("pairing_identity_sync_state", "ready") or "ready"
            )[:32],
            "astrbot_auth_configured": len(api_key) >= 16,
            "bridge_auth_configured": len(bridge_key) >= 32,
            "control_plane": control_plane,
            "local_fallback_configured": bool(
                getattr(self.identity, "local_binding_configured", False)
            ),
            "config_writable": config_is_writable(self.config),
        }

    async def save_quest_identity(
        self,
        *,
        client_id: str,
        platform_id: str,
        bot_id: str,
        user_id: str,
        api_principal_digest: str,
        astrbot_api_key: str,
    ) -> dict[str, Any]:
        async with self._identity_sync_lock:
            return await self._save_quest_identity_unlocked(
                client_id=client_id,
                platform_id=platform_id,
                bot_id=bot_id,
                user_id=user_id,
                api_principal_digest=api_principal_digest,
                astrbot_api_key=astrbot_api_key,
            )

    async def _save_quest_identity_unlocked(
        self,
        *,
        client_id: str,
        platform_id: str,
        bot_id: str,
        user_id: str,
        api_principal_digest: str,
        astrbot_api_key: str,
    ) -> dict[str, Any]:
        client = str(client_id or "").strip()
        platform = str(platform_id or "").strip()
        bot = _identity_value(bot_id, "bot_id")
        user = _identity_value(user_id, "user_id")
        if not _CLIENT_ID_RE.fullmatch(client):
            raise OperatorSettingsError(
                "invalid_trusted_client_id",
                422,
                "Quest 客户端 ID 无效",
            )
        self._validate_platform_id(platform)
        api_key = str(astrbot_api_key or "").strip() or str(
            self.config.get("pairing_astrbot_api_key", "") or ""
        )
        if len(api_key) < 16 or len(api_key) > 4096:
            raise OperatorSettingsError(
                "pairing_astrbot_api_key_missing",
                422,
                "请填写可访问“临”的 AstrBot API Key",
            )
        try:
            principal_digest = validate_principal_digest(api_principal_digest)
        except ValueError as exc:
            raise OperatorSettingsError(
                "invalid_authenticated_api_principal",
                401,
                "AstrBot 未提供有效的 API Key 身份凭据",
            ) from exc
        bridge_key = str(self.config.get("bridge_api_key", "") or "")
        if len(bridge_key) < 32:
            bridge_key = secrets.token_urlsafe(32)

        changes = {
            "trusted_client_id": client,
            "trusted_platform_id": platform,
            "pairing_bot_id": "",
            "pairing_user_id": "",
            "pairing_group_id": "",
            "pairing_astrbot_api_key": api_key,
            "pairing_api_principal_digest": principal_digest,
            "bridge_api_key": bridge_key,
            "relationship_person_id": "",
            "pairing_identity_source": "manual",
            "pairing_identity_sync_state": "pending",
        }
        await self._persist_many(changes)
        if self.identity is not None:
            self.identity.configure_sync_ready(False)

        control_result: dict[str, Any] = {
            "source": "bridge_local",
            "authorized": True,
            "reason": "saved_to_bridge_local_fallback",
        }
        if self.identity_control_plane is not None:
            try:
                control_result = await (
                    self.identity_control_plane.upsert_quest_owner_binding(
                        api_principal_digest=principal_digest,
                        client_id=client,
                        platform_id=platform,
                        bot_id=bot,
                        user_id=user,
                    )
                )
            except IdentityControlPlaneError as exc:
                raise OperatorSettingsError(exc.code, 503, str(exc)) from exc

        if self.identity_store is None:
            raise OperatorSettingsError(
                "server_identity_store_unavailable",
                503,
                "服务端身份存储不可用",
            )
        await self.identity_store.save(bot_id=bot, user_id=user)
        await self._persist("pairing_identity_sync_state", "ready")
        if self.identity is not None:
            self.identity.configure_local_binding(
                api_principal_digest=principal_digest,
                client_id=client,
                platform_id=platform,
                bot_id=bot,
                user_id=user,
                group_id="",
            )
            self.identity.configure_relationship_person_id("")
            self.identity.configure_sync_ready(True)
        self.relationship.configure_person_id("")
        if self.message_pipeline is not None:
            self.message_pipeline.configure_platform(platform)
        if self.pairing_manager is not None:
            self.pairing_manager.bridge_api_key = bridge_key
        if self.transport is not None:
            self.transport.configure_bridge_api_key(bridge_key)
        self._diagnostic(
            "identity.updated",
            component="identity",
            status="ready",
            configured=True,
            available=True,
        )
        snapshot = await self.quest_identity_overview()
        snapshot["binding_validation"] = {
            "authorized": control_result.get("authorized") is True,
            "source": str(control_result.get("source") or "bridge_local")[:32],
            "reason": str(control_result.get("reason") or "")[:64],
        }
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

    def list_chat_providers(self) -> list[dict[str, str]]:
        return self._list_chat_providers()

    def stt_snapshot(self) -> dict[str, Any]:
        if self.stt is None:
            return {
                "source": "astrbot_stt_provider",
                "available": False,
                "status": "adapter_unavailable",
                "selected": False,
                "selected_id": "",
                "legacy_default": False,
                "external_contract_status": "no_standard_contract",
                "providers": [],
                "config_writable": config_is_writable(self.config),
            }
        snapshot = dict(self.stt.status_snapshot())
        snapshot["config_writable"] = config_is_writable(self.config)
        return snapshot

    async def save_stt_provider_id(self, value: str) -> dict[str, Any]:
        provider_id = str(value or "").strip()
        if len(provider_id) > 256 or any(ord(char) < 33 for char in provider_id):
            raise OperatorSettingsError(
                "invalid_stt_provider_id",
                422,
                "语音识别 Provider ID 无效",
            )
        if self.stt is None:
            raise OperatorSettingsError(
                "stt_adapter_unavailable",
                503,
                "语音识别适配器当前不可用",
            )
        available = {item["id"] for item in self.stt.provider_catalog()}
        if provider_id and provider_id not in available:
            raise OperatorSettingsError(
                "stt_provider_not_available",
                422,
                "所选语音识别 Provider 不存在或当前不可用",
            )
        await self._persist_many(
            {
                "astrbot_stt_provider_id": provider_id,
                "enable_astrbot_stt": False,
                "enable_plugin_mimo_stt": False,
                "plugin_mimo_stt_api_base": "",
                "plugin_mimo_stt_api_key": "",
                "plugin_mimo_stt_model": "",
            }
        )
        self.stt.configure_provider(provider_id)
        return self.stt_snapshot()

    async def save_quest_persona_setting(self, key: str, value: str) -> None:
        if key not in {
            "persona_converter_provider_id",
            "active_quest_persona_id",
        }:
            raise OperatorSettingsError(
                "invalid_persona_config_key",
                422,
                "人格配置字段无效",
            )
        await self._persist(key, str(value or "").strip())

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
        if self.identity is not None:
            self.identity.configure_relationship_person_id(person_id)
        return self.snapshot()

    async def clear_resolved_relationship_identity(self) -> dict[str, Any]:
        source = str(
            self.config.get("pairing_identity_source", "manual") or "manual"
        ).strip()
        if source != "relationship":
            return await self.save_relationship_person_id("")
        client = str(self.config.get("trusted_client_id", "") or "").strip()
        try:
            principal_digest = validate_principal_digest(
                self.config.get("pairing_api_principal_digest", "")
            )
        except ValueError as exc:
            raise OperatorSettingsError(
                "pairing_api_principal_digest_missing",
                422,
                "无法验证需要撤销的 Quest 身份摘要",
            ) from exc

        async with self._identity_sync_lock:
            await self._persist("pairing_identity_sync_state", "pending")
            if self.identity is not None:
                self.identity.configure_sync_ready(False)
            if self.identity_control_plane is not None:
                try:
                    await self.identity_control_plane.revoke_quest_read_only_binding(
                        api_principal_digest=principal_digest,
                        client_id=client,
                    )
                except IdentityControlPlaneError as exc:
                    raise OperatorSettingsError(exc.code, 503, str(exc)) from exc
            await self._persist_many(
                {
                    "relationship_person_id": "",
                    "pairing_bot_id": "",
                    "pairing_user_id": "",
                    "pairing_group_id": "",
                    "pairing_identity_source": "none",
                    "pairing_identity_sync_state": "ready",
                }
            )
            if self.identity_store is not None:
                await self.identity_store.clear()
            self.relationship.configure_person_id("")
            if self.identity is not None:
                self.identity.clear_local_binding()
        return self.snapshot()

    async def mark_relationship_identity_sync_pending(self) -> None:
        if str(self.config.get("relationship_person_id", "") or "").strip() == "":
            return
        if str(self.config.get("pairing_identity_sync_state", "") or "") != "pending":
            await self._persist("pairing_identity_sync_state", "pending")
        if self.identity is not None:
            self.identity.configure_sync_ready(False)

    def active_platform_ids(self) -> tuple[str, ...]:
        return tuple(item["id"] for item in self._list_platforms())

    def quest_identity_platform_candidates(self) -> tuple[str, ...]:
        active = self.active_platform_ids()
        preferred = str(
            getattr(self.message_pipeline, "platform_id", "")
            or getattr(self.identity, "trusted_platform_id", "")
            or self.config.get("trusted_platform_id", "")
            or ""
        ).strip()
        if preferred in active:
            return (preferred,)
        return active

    def server_identity_values(self) -> tuple[str, str]:
        identity = (
            getattr(self.identity_store, "identity", None)
            if self.identity_store is not None
            else None
        )
        return (
            str(getattr(identity, "bot_id", "") or ""),
            str(getattr(identity, "user_id", "") or ""),
        )

    async def save_resolved_relationship_identity(
        self,
        *,
        person_id: str,
        platform_id: str,
        bot_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        person = str(person_id or "").strip()
        if not _PERSON_ID_RE.fullmatch(person):
            raise OperatorSettingsError(
                "invalid_relationship_person_id",
                422,
                "自然人 ID 无效",
            )
        client = str(self.config.get("trusted_client_id", "") or "").strip()
        if not _CLIENT_ID_RE.fullmatch(client):
            raise OperatorSettingsError(
                "trusted_client_id_missing",
                422,
                "请先配置 Quest 客户端 ID",
            )
        platform = str(platform_id or "").strip()
        self._validate_platform_id(platform)
        bot = _identity_value(bot_id, "bot_id")
        user = _identity_value(user_id, "user_id")
        try:
            principal_digest = validate_principal_digest(
                self.config.get("pairing_api_principal_digest", "")
            )
        except ValueError as exc:
            raise OperatorSettingsError(
                "pairing_api_principal_digest_missing",
                422,
                "请先在 Quest 身份设置中验证 AstrBot API Key",
            ) from exc

        async with self._identity_sync_lock:
            await self._persist_many(
                {
                    "relationship_person_id": person,
                    "trusted_platform_id": platform,
                    "pairing_bot_id": "",
                    "pairing_user_id": "",
                    "pairing_group_id": "",
                    "pairing_identity_source": "relationship",
                    "pairing_identity_sync_state": "pending",
                }
            )
            if self.identity is not None:
                self.identity.configure_sync_ready(False)
            control_result: dict[str, Any] = {
                "source": "bridge_local",
                "authorized": True,
                "reason": "saved_to_bridge_local_fallback",
            }
            if self.identity_control_plane is not None:
                try:
                    control_result = await (
                        self.identity_control_plane.upsert_quest_read_only_binding(
                            api_principal_digest=principal_digest,
                            client_id=client,
                            platform_id=platform,
                            bot_id=bot,
                            user_id=user,
                        )
                    )
                except IdentityControlPlaneError as exc:
                    self._diagnostic(
                        "identity.relationship_sync_pending",
                        component="identity",
                        status="pending",
                        code=exc.code,
                        configured=True,
                        available=False,
                    )
                    raise OperatorSettingsError(exc.code, 503, str(exc)) from exc
            if self.identity_store is None:
                raise OperatorSettingsError(
                    "server_identity_store_unavailable",
                    503,
                    "服务端身份存储不可用",
                )
            await self.identity_store.save(bot_id=bot, user_id=user)
            await self._persist("pairing_identity_sync_state", "ready")
        self.relationship.configure_person_id(person)
        if self.identity is not None:
            self.identity.configure_relationship_person_id(person)
            self.identity.configure_local_binding(
                api_principal_digest=principal_digest,
                client_id=client,
                platform_id=platform,
                bot_id=bot,
                user_id=user,
                group_id="",
            )
            self.identity.configure_sync_ready(True)
        if self.message_pipeline is not None:
            self.message_pipeline.configure_platform(platform)
        self._diagnostic(
            "identity.relationship_resolved",
            component="identity",
            status="ready",
            configured=True,
            available=control_result.get("authorized") is True,
        )
        return self.snapshot()

    async def save_trusted_platform_id(self, value: str) -> dict[str, Any]:
        platform_id = str(value or "").strip()
        self._validate_platform_id(platform_id, allow_empty=True)

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

    def _validate_platform_id(
        self, platform_id: str, *, allow_empty: bool = False
    ) -> None:
        if not platform_id and allow_empty:
            return
        if (
            not platform_id
            or len(platform_id) > 128
            or "|" in platform_id
            or any(char.isspace() or ord(char) < 33 for char in platform_id)
        ):
            raise OperatorSettingsError(
                "invalid_trusted_platform_id",
                422,
                "AstrBot 平台实例 ID 无效",
            )
        getter = getattr(self.context, "get_platform_inst", None)
        if not callable(getter):
            raise OperatorSettingsError(
                "astrbot_platform_api_unavailable",
                503,
                "AstrBot 平台查询接口不可用",
            )
        try:
            platform = getter(platform_id)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise OperatorSettingsError(
                "astrbot_platform_lookup_failed",
                503,
                "AstrBot 平台查询失败",
            ) from exc
        if platform is None:
            raise OperatorSettingsError(
                "trusted_platform_not_available",
                422,
                "所选 AstrBot 平台实例当前不可用",
            )

    async def save_character_persona(
        self,
        *,
        persona_source_mode: str,
        astrbot_persona_id: str,
        character_name: str,
        character_self_reference: str,
        character_self_description: str,
        character_user_relationship: str,
        deactivate_quest_persona: bool = False,
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
        if deactivate_quest_persona:
            values["active_quest_persona_id"] = ""
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
        if deactivate_quest_persona:
            self.llm.configure_quest_persona("")
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

    def _list_platforms(self) -> list[dict[str, str]]:
        try:
            manager = self.context.platform_manager
            get_insts = manager.get_insts
            platforms = get_insts()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return []
        if not isinstance(platforms, (list, tuple)):
            return []

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for platform in platforms:
            try:
                metadata = platform.meta()
                platform_id = str(metadata.id or "").strip()
                adapter_type = str(metadata.name or "").strip()
                display_name = str(
                    metadata.adapter_display_name or adapter_type
                ).strip()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if (
                not platform_id
                or len(platform_id) > 128
                or platform_id in seen
                or "|" in platform_id
                or any(char.isspace() or ord(char) < 33 for char in platform_id)
            ):
                continue
            seen.add(platform_id)
            items.append(
                {
                    "id": platform_id,
                    "adapter_type": adapter_type[:128],
                    "display_name": display_name[:128],
                }
            )
        items.sort(
            key=lambda item: (
                item["display_name"].casefold(),
                item["adapter_type"].casefold(),
                item["id"].casefold(),
            )
        )
        return items

    async def _persist(self, key: str, value: str) -> None:
        await self._persist_many({key: value})

    async def _persist_many(self, changes: dict[str, Any]) -> None:
        if not changes or any(
            key
            not in {
                *_PERSONA_KEYS,
                "chat_provider_id",
                "astrbot_stt_provider_id",
                "enable_astrbot_stt",
                "enable_plugin_mimo_stt",
                "plugin_mimo_stt_api_base",
                "plugin_mimo_stt_api_key",
                "plugin_mimo_stt_model",
                "persona_converter_provider_id",
                "active_quest_persona_id",
                "relationship_person_id",
                "trusted_platform_id",
                "trusted_client_id",
                "pairing_bot_id",
                "pairing_user_id",
                "pairing_group_id",
                "pairing_astrbot_api_key",
                "pairing_api_principal_digest",
                "pairing_identity_source",
                "pairing_identity_sync_state",
                "bridge_api_key",
            }
            for key in changes
        ):
            raise OperatorSettingsError(
                "invalid_config_key",
                422,
                "配置字段不在允许范围内",
            )
        if not config_is_writable(self.config):
            raise OperatorSettingsError(
                "native_config_unavailable",
                503,
                "当前 AstrBot 配置对象不支持安全保存",
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
                committed = await save_config_changes(self.config, changes)
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


def _identity_value(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or "|" in normalized
        or any(char.isspace() or ord(char) < 33 for char in normalized)
    ):
        raise OperatorSettingsError(
            f"invalid_{field}",
            422,
            f"Quest {field} 无效",
        )
    return normalized
