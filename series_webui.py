"""``series.webui@2.0`` 统一管理面适配层（临）。

本模块只编排插件现有的配对、监听器、会话、人格与服务控制对象，不复制
任何状态机。面板数据按白名单投影，绝不返回设备密钥、配对 token、绑定地址、
API Key、底层路径或异常正文。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .core.pairing import PairingCreateRequest, PairingError

CONTRACT_NAME = "series.webui@2.0"
CONTRACT_VERSION = "2.0"
PLUGIN_ID = "astrbot_plugin_embodiment_bridge"
SERIES_ID = "ningxin_suxi"

SERVICE_STATUS_PANEL_ID = "service_status"
OPERATOR_PANEL_ID = "operator"

REFRESH_STATUS_ACTION_ID = "refresh_status"
CREATE_PAIRING_ACTION_ID = "create_pairing_session"
REFRESH_PAIRING_ACTION_ID = "refresh_pairing_session"
REVOKE_PAIRING_ACTION_ID = "revoke_pairing_session"
REVOKE_ALL_PAIRINGS_ACTION_ID = "revoke_all_pairing_sessions"
SET_BRIDGE_ACTION_ID = "set_bridge_service"
DISCONNECT_ALL_ACTION_ID = "disconnect_all_sessions"
SET_FALLBACK_ACTION_ID = "set_dialogue_fallback"
SET_PERSONA_MODE_ACTION_ID = "set_persona_source_mode"
ACTIVATE_PERSONA_ACTION_ID = "activate_persona_profile"

MAX_PROFILE_ID_CHARS = 128
MAX_STATUS_TEXT_CHARS = 80
PAIRING_ID_PATTERN = r"^[a-f0-9]{32}$"

_MANAGED_PAIRING_OWNER = "series.webui"
_ROLE_LEVELS = {"viewer": 0, "admin": 1, "owner": 2}
_ACTION_MIN_ROLES = {
    REFRESH_STATUS_ACTION_ID: "viewer",
    CREATE_PAIRING_ACTION_ID: "admin",
    REFRESH_PAIRING_ACTION_ID: "admin",
    REVOKE_PAIRING_ACTION_ID: "admin",
    REVOKE_ALL_PAIRINGS_ACTION_ID: "admin",
    SET_BRIDGE_ACTION_ID: "admin",
    DISCONNECT_ALL_ACTION_ID: "admin",
    SET_FALLBACK_ACTION_ID: "admin",
    SET_PERSONA_MODE_ACTION_ID: "owner",
    ACTIVATE_PERSONA_ACTION_ID: "owner",
}

_PAIRING_ERROR_LABELS = {
    "bridge_not_configured": "桥接密钥尚未配置",
    "pairing_bootstrap_unavailable": "配对交换入口尚未就绪",
    "pairing_server_configuration_incomplete": "配对服务端配置不完整",
    "quick_pairing_defaults_missing": "快速绑定默认配置不完整",
    "quick_pairing_server_identity_missing": "服务端身份尚未就绪",
    "pairing_transport_mismatch": "配对传输方式不一致",
    "certificate_pin_mismatch": "证书指纹不一致",
    "certificate_pin_authority_mismatch": "证书指纹与配对地址不一致",
    "pairing_not_available": "配对会话不存在或已失效",
    "pairing_not_owned": "配对会话不属于当前管理会话",
}


def _failure(code: str, message: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "error": code}
    if message:
        payload["message"] = message
    return payload


def _success(message: str = "", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": True}
    if message:
        payload["message"] = message
    payload.update(extra)
    return payload


def _safe_text(value: Any, maximum: int = MAX_STATUS_TEXT_CHARS) -> str:
    return str(value or "").strip()[:maximum]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class SeriesWebUIPanels:
    """实现临插件侧的 ``series.webui@2.0`` 管理面契约。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # ---------- 契约 ----------

    def contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": CONTRACT_VERSION,
            "plugin_id": PLUGIN_ID,
            "series_id": SERIES_ID,
            "state_owner": "plugin",
            "standalone": {
                "available": True,
                "entry": "/pages/operator",
                "pages": ["operator"],
            },
            "managed": {
                "supported": True,
                "level": "actions",
                "preferred_surface": "kernel",
            },
            "preferred_surface": "dual",
            "capabilities": [
                "generic_actions",
                "generic_table",
                "idempotency",
                "sse",
            ],
            "module_capabilities": ["control", "diagnostics", "webui"],
            "panels": [
                {
                    "id": SERVICE_STATUS_PANEL_ID,
                    "title": "临服务状态",
                    "description": "只读查看配对监听、Bootstrap 与具身运行状态",
                    "read_only": True,
                    "actions": [],
                },
                {
                    "id": OPERATOR_PANEL_ID,
                    "title": "临日常管理",
                    "description": "配对会话、人格模式、桥接与对话策略的受控日常操作",
                    "actions": self._actions(),
                },
            ],
        }

    def _actions(self) -> list[dict[str, Any]]:
        pairing_id_field = {
            "name": "pairing_id",
            "label": "配对会话 ID",
            "type": "text",
            "required": True,
            "hint": "32 位小写十六进制；不是配对 token 或短码",
        }
        return [
            {
                "id": REFRESH_STATUS_ACTION_ID,
                "label": "刷新运行状态",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "viewer",
                "confirm": "",
                "timeout_seconds": 5,
                "payload_fields": [],
            },
            {
                "id": CREATE_PAIRING_ACTION_ID,
                "label": "创建配对会话",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认创建一个新的一次性配对会话？结果不会返回配对 token 或短码。",
                "timeout_seconds": 15,
                "payload_fields": [],
            },
            {
                "id": REFRESH_PAIRING_ACTION_ID,
                "label": "刷新配对会话",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "",
                "timeout_seconds": 5,
                "payload_fields": [pairing_id_field],
            },
            {
                "id": REVOKE_PAIRING_ACTION_ID,
                "label": "撤销配对会话",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认撤销该配对会话？未交换的凭据会立即失效。",
                "timeout_seconds": 10,
                "payload_fields": [pairing_id_field],
            },
            {
                "id": REVOKE_ALL_PAIRINGS_ACTION_ID,
                "label": "撤销全部待配对会话",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认撤销所有等待中的配对凭据？",
                "timeout_seconds": 10,
                "payload_fields": [],
            },
            {
                "id": SET_BRIDGE_ACTION_ID,
                "label": "启停桥接服务",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认切换桥接服务？关闭会停止监听、撤销待配对凭据并断开现有会话。",
                "timeout_seconds": 20,
                "payload_fields": [
                    {
                        "name": "enabled",
                        "label": "启用桥接服务",
                        "type": "bool",
                        "required": True,
                    }
                ],
            },
            {
                "id": DISCONNECT_ALL_ACTION_ID,
                "label": "断开全部具身会话",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认主动断开全部具身会话？当前音频与未完成回合会被终止。",
                "timeout_seconds": 15,
                "payload_fields": [],
            },
            {
                "id": SET_FALLBACK_ACTION_ID,
                "label": "设置直连回退",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "admin",
                "confirm": "确认修改临专属链路不可用时的直连 Provider 回退策略？",
                "timeout_seconds": 10,
                "payload_fields": [
                    {
                        "name": "enabled",
                        "label": "允许直连回退",
                        "type": "bool",
                        "required": True,
                    }
                ],
            },
            {
                "id": SET_PERSONA_MODE_ACTION_ID,
                "label": "切换人格来源模式",
                "effect": "idempotent",
                "idempotency_required": False,
                "revision_required": False,
                "min_role": "owner",
                "confirm": "确认切换临的人格来源模式？该动作不会改写 AstrBot 全局人格。",
                "timeout_seconds": 15,
                "payload_fields": [
                    {
                        "name": "mode",
                        "label": "人格来源",
                        "type": "select",
                        "required": True,
                        "options": [
                            ["astrbot", "继承 AstrBot 人格"],
                            ["manual_override", "使用临的手工人格"],
                        ],
                    }
                ],
            },
            {
                "id": ACTIVATE_PERSONA_ACTION_ID,
                "label": "启用或停用临专用人格",
                "effect": "non_idempotent",
                "idempotency_required": True,
                "revision_required": False,
                "min_role": "owner",
                "confirm": "确认切换当前临专用人格？留空 profile_id 表示停用。",
                "timeout_seconds": 15,
                "payload_fields": [
                    {
                        "name": "profile_id",
                        "label": "人格档案 ID（留空停用）",
                        "type": "text",
                        "required": False,
                        "max_length": MAX_PROFILE_ID_CHARS,
                        "hint": "从管理面板的人格档案列表复制；不会返回人格正文。",
                    }
                ],
            },
        ]

    # ---------- 只读投影 ----------

    def panel_data(self, panel: str) -> dict[str, Any]:
        if panel == SERVICE_STATUS_PANEL_ID:
            return self._service_status_panel()
        if panel == OPERATOR_PANEL_ID:
            return self._operator_panel()
        return _failure("UNKNOWN_PANEL")

    def _service_status_panel(self) -> dict[str, Any]:
        try:
            service_status = self.plugin._webui_service_status_snapshot()
        except Exception:
            return _failure("SERVICE_STATUS_UNAVAILABLE")
        rows = [
            {"item": "配对监听", "value": service_status["status"]},
            {
                "item": "Bootstrap",
                "value": "就绪" if service_status["bootstrap_ready"] else "未就绪",
            },
            {
                "item": "人格模式",
                "value": service_status["persona_mode"] or "未配置",
            },
        ]
        return {
            "success": True,
            "title": "临服务状态",
            "description": "只读服务状态；日常管理操作已迁移到临日常管理面板",
            "columns": [
                {"key": "item", "label": "项目"},
                {"key": "value", "label": "状态"},
            ],
            "rows": rows,
            "actions": [],
        }

    def _operator_panel(self) -> dict[str, Any]:
        config = getattr(self.plugin, "config", {})
        if not isinstance(config, Mapping):
            config = {}
        listener_status: dict[str, Any] = {}
        listener = getattr(self.plugin, "pairing_listener", None)
        status_reader = getattr(listener, "status_snapshot", None)
        if callable(status_reader):
            try:
                raw = status_reader()
                if isinstance(raw, Mapping):
                    listener_status = dict(raw)
            except Exception:
                listener_status = {}
        service = getattr(self.plugin, "service", None)
        service_enabled = bool(
            getattr(service, "enabled", config.get("bridge_service_enabled", False))
        )
        pairing = getattr(self.plugin, "pairing", None)
        bootstrap_ready = bool(getattr(pairing, "bootstrap_ready", False))
        persona_mode = _safe_text(
            config.get("persona_source_mode")
            or config.get("persona_mode")
            or "astrbot"
        )
        active_profile_id = _safe_text(config.get("active_quest_persona_id"))
        orchestrator = getattr(self.plugin, "orchestrator", None)
        fallback_enabled = bool(
            getattr(orchestrator, "allow_direct_provider_fallback", False)
            or config.get("allow_direct_provider_fallback", False)
        )
        pipeline = getattr(orchestrator, "quest_enriched_pipeline", None)
        bridge_available = bool(getattr(pipeline, "available", False))
        rows = [
            {
                "item": "桥接服务",
                "value": "已启用" if service_enabled else "已关闭",
            },
            {
                "item": "内置监听器",
                "value": (
                    "已就绪"
                    if listener_status.get("ready") is True
                    else "已配置未就绪"
                    if listener_status.get("enabled") is True
                    else "未启用"
                ),
            },
            {
                "item": "配对 Bootstrap",
                "value": "就绪" if bootstrap_ready else "未就绪",
            },
            {
                "item": "临专属对话链",
                "value": "可用" if bridge_available else "不可用",
            },
            {
                "item": "直连 Provider 回退",
                "value": "已开启" if fallback_enabled else "已关闭",
            },
            {
                "item": "EventBus 对话",
                "value": "已退役（固定关闭）",
            },
            {
                "item": "人格来源",
                "value": persona_mode,
            },
            {
                "item": "临专用人格",
                "value": active_profile_id or "未启用",
            },
            {
                "item": "最大具身会话",
                "value": str(config.get("max_sessions", "未配置")),
            },
        ]
        return {
            "success": True,
            "title": "临日常管理",
            "description": "所有动作复用插件现有配对、人格和会话服务；敏感凭据不进入管理面。",
            "columns": [
                {"key": "item", "label": "项目"},
                {"key": "value", "label": "状态"},
            ],
            "rows": rows,
            "actions": self._actions(),
            "footer": (
                "EventBus 主消息链路自 1.3.0 起已移除，因此只读展示为固定关闭；"
                "配对 token、短码、设备密钥、绑定地址与底层路径均不会返回。"
            ),
        }

    # ---------- 动作 ----------

    async def panel_action(
        self,
        panel: str,
        action: str,
        payload: Mapping[str, Any] | None,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        if panel != OPERATOR_PANEL_ID:
            return _failure("UNKNOWN_PANEL", "未知面板")
        if action not in _ACTION_MIN_ROLES:
            return _failure("UNKNOWN_ACTION", "未知动作")
        if self._role_forbidden(action, context):
            return _failure("ROLE_FORBIDDEN", "当前角色无权执行该动作")
        if action == REFRESH_STATUS_ACTION_ID:
            if data:
                return _failure("INVALID_PAYLOAD")
            return await self._refresh_status()
        if action == CREATE_PAIRING_ACTION_ID:
            if data:
                return _failure("INVALID_PAYLOAD")
            return await self._create_pairing()
        if action in {REFRESH_PAIRING_ACTION_ID, REVOKE_PAIRING_ACTION_ID}:
            if set(data) != {"pairing_id"}:
                return _failure("INVALID_PAYLOAD")
            pairing_id = self._pairing_id(data.get("pairing_id"))
            if pairing_id is None:
                return _failure("INVALID_PAIRING_ID", "配对会话 ID 无效")
            if action == REFRESH_PAIRING_ACTION_ID:
                return self._refresh_pairing(pairing_id)
            return self._revoke_pairing(pairing_id)
        if action == REVOKE_ALL_PAIRINGS_ACTION_ID:
            if data:
                return _failure("INVALID_PAYLOAD")
            return self._revoke_all_pairings()
        if action == SET_BRIDGE_ACTION_ID:
            if set(data) != {"enabled"} or not isinstance(data.get("enabled"), bool):
                return _failure("INVALID_PAYLOAD")
            return await self._set_bridge_service(data["enabled"])
        if action == DISCONNECT_ALL_ACTION_ID:
            if data:
                return _failure("INVALID_PAYLOAD")
            return await self._disconnect_all_sessions()
        if action == SET_FALLBACK_ACTION_ID:
            if set(data) != {"enabled"} or not isinstance(data.get("enabled"), bool):
                return _failure("INVALID_PAYLOAD")
            return await self._set_dialogue_fallback(data["enabled"])
        if action == SET_PERSONA_MODE_ACTION_ID:
            if set(data) != {"mode"}:
                return _failure("INVALID_PAYLOAD")
            mode = str(data.get("mode") or "").strip().lower()
            if mode not in {"astrbot", "manual_override"}:
                return _failure("INVALID_PERSONA_MODE", "人格来源模式无效")
            return await self._set_persona_source_mode(mode)
        if action == ACTIVATE_PERSONA_ACTION_ID:
            if set(data) != {"profile_id"}:
                return _failure("INVALID_PAYLOAD")
            profile_id = self._profile_id(data.get("profile_id"))
            if profile_id is None:
                return _failure("INVALID_PROFILE_ID", "人格档案 ID 无效")
            return await self._activate_persona_profile(profile_id)
        return _failure("UNKNOWN_ACTION", "未知动作")

    def _role_forbidden(
        self, action: str, context: Mapping[str, Any] | None
    ) -> bool:
        if not isinstance(context, Mapping):
            return False
        role = str(context.get("role") or "").strip().lower()
        if not role:
            return False
        required = _ACTION_MIN_ROLES.get(action, "admin")
        return _ROLE_LEVELS.get(role, -1) < _ROLE_LEVELS.get(required, 1)

    async def _refresh_status(self) -> dict[str, Any]:
        service = getattr(self.plugin, "service", None)
        status_snapshot = getattr(service, "status_snapshot", None)
        if not callable(status_snapshot):
            return _failure("SERVICE_UNAVAILABLE", "服务控制暂不可用")
        try:
            snapshot = await status_snapshot()
        except Exception:
            return _failure("SERVICE_STATUS_UNAVAILABLE", "服务状态读取失败")
        return _success("状态已刷新", service=self._public_service_snapshot(snapshot))

    def _pairing_parts(self) -> tuple[Any, Any] | None:
        pairing_api = getattr(self.plugin, "pairing_api", None)
        manager = getattr(self.plugin, "pairing", None)
        if manager is None and pairing_api is not None:
            manager = getattr(pairing_api, "manager", None)
        if manager is None:
            return None
        return pairing_api, manager

    async def _create_pairing(self) -> dict[str, Any]:
        parts = self._pairing_parts()
        if parts is None:
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        pairing_api, manager = parts
        complete = getattr(pairing_api, "_complete_create_request", None)
        if not callable(complete):
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        try:
            request = complete(PairingCreateRequest())
            result = manager.create(_MANAGED_PAIRING_OWNER, request)
        except PairingError as exc:
            return self._pairing_failure(exc)
        except Exception:
            return _failure("PAIRING_CREATE_FAILED", "配对会话创建失败")
        return _success(
            "配对会话已创建；凭据未返回到管理面",
            pairing=self._public_pairing_result(result),
        )

    def _refresh_pairing(self, pairing_id: str) -> dict[str, Any]:
        parts = self._pairing_parts()
        if parts is None:
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        _, manager = parts
        status = getattr(manager, "status", None)
        if not callable(status):
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        try:
            snapshot = status(_MANAGED_PAIRING_OWNER, pairing_id)
        except PairingError as exc:
            return self._pairing_failure(exc)
        except Exception:
            return _failure("PAIRING_STATUS_FAILED", "配对会话状态读取失败")
        return _success("配对会话状态已刷新", pairing=self._public_pairing(snapshot))

    def _revoke_pairing(self, pairing_id: str) -> dict[str, Any]:
        parts = self._pairing_parts()
        if parts is None:
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        _, manager = parts
        revoke = getattr(manager, "revoke", None)
        if not callable(revoke):
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        try:
            snapshot = revoke(_MANAGED_PAIRING_OWNER, pairing_id)
        except PairingError as exc:
            return self._pairing_failure(exc)
        except Exception:
            return _failure("PAIRING_REVOKE_FAILED", "配对会话撤销失败")
        return _success("配对会话已撤销", pairing=self._public_pairing(snapshot))

    def _revoke_all_pairings(self) -> dict[str, Any]:
        parts = self._pairing_parts()
        if parts is None:
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        _, manager = parts
        revoke_waiting = getattr(manager, "revoke_waiting", None)
        if not callable(revoke_waiting):
            return _failure("PAIRING_UNAVAILABLE", "配对会话服务不可用")
        try:
            revoked = int(revoke_waiting() or 0)
        except Exception:
            return _failure("PAIRING_REVOKE_FAILED", "配对会话撤销失败")
        return _success("全部待配对凭据已撤销", revoked_count=revoked)

    async def _set_bridge_service(self, enabled: bool) -> dict[str, Any]:
        service = getattr(self.plugin, "service", None)
        setter = getattr(service, "set_enabled", None)
        if not callable(setter):
            return _failure("SERVICE_UNAVAILABLE", "服务控制暂不可用")
        try:
            snapshot = await setter(enabled)
        except Exception:
            return _failure("SERVICE_UPDATE_FAILED", "桥接服务状态更新失败")
        return _success(
            "桥接服务已启用" if enabled else "桥接服务已关闭",
            service=self._public_service_snapshot(snapshot),
        )

    async def _disconnect_all_sessions(self) -> dict[str, Any]:
        sessions = getattr(self.plugin, "sessions", None)
        closer = getattr(sessions, "close_all_sessions", None)
        if not callable(closer):
            return _failure("SESSION_SERVICE_UNAVAILABLE", "会话服务不可用")
        try:
            await closer()
        except Exception:
            return _failure("SESSION_CLOSE_FAILED", "具身会话断开失败")
        return _success("全部具身会话已断开")

    async def _set_dialogue_fallback(self, enabled: bool) -> dict[str, Any]:
        settings = getattr(self.plugin, "operator_settings", None)
        saver = getattr(settings, "save_quest_chain_settings", None)
        if not callable(saver):
            return _failure("SETTINGS_UNAVAILABLE", "对话设置服务不可用")
        try:
            snapshot = await saver(allow_direct_provider_fallback=enabled)
        except Exception:
            return _failure("SETTINGS_UPDATE_FAILED", "对话回退设置保存失败")
        return _success(
            "直连回退已开启" if enabled else "直连回退已关闭",
            dialogue=self._public_dialogue_snapshot(snapshot),
        )

    async def _set_persona_source_mode(self, mode: str) -> dict[str, Any]:
        settings = getattr(self.plugin, "operator_settings", None)
        overview = getattr(settings, "persona_overview", None)
        saver = getattr(settings, "save_character_persona", None)
        if not callable(overview) or not callable(saver):
            return _failure("PERSONA_UNAVAILABLE", "人格服务不可用")
        try:
            snapshot = await overview()
            saved = await saver(
                persona_source_mode=mode,
                astrbot_persona_id=str(snapshot.get("astrbot_persona_id") or ""),
                character_name=str(snapshot.get("character_name") or ""),
                character_self_reference=str(
                    snapshot.get("character_self_reference") or ""
                ),
                character_self_description=str(
                    snapshot.get("character_self_description") or ""
                ),
                character_user_relationship=str(
                    snapshot.get("character_user_relationship") or ""
                ),
            )
        except Exception:
            return _failure("PERSONA_UPDATE_FAILED", "人格来源模式保存失败")
        return _success(
            "人格来源模式已切换",
            persona=self._public_persona_snapshot(saved),
        )

    async def _activate_persona_profile(self, profile_id: str) -> dict[str, Any]:
        service = getattr(self.plugin, "persona_service", None)
        library_reader = getattr(service, "library_snapshot", None)
        activator = getattr(service, "activate", None)
        if not callable(library_reader) or not callable(activator):
            return _failure("PERSONA_UNAVAILABLE", "人格服务不可用")
        try:
            library = await library_reader()
            profiles = library.get("profiles") if isinstance(library, Mapping) else []
            profile_ids = {
                str(item.get("profile_id") or "")
                for item in profiles or []
                if isinstance(item, Mapping) and item.get("profile_id")
            }
            if profile_id and profile_id not in profile_ids:
                return _failure("PERSONA_PROFILE_NOT_FOUND", "人格档案不存在")
            result = await activator(profile_id)
        except Exception:
            return _failure("PERSONA_ACTIVATE_FAILED", "人格档案切换失败")
        return _success(
            "临专用人格已停用" if not profile_id else "临专用人格已启用",
            persona=self._public_persona_snapshot(result),
        )

    # ---------- 脱敏投影 ----------

    @staticmethod
    def _public_pairing(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = snapshot if isinstance(snapshot, Mapping) else {}
        return {
            "pairing_id": _safe_text(raw.get("pairing_id"), 32),
            "state": _safe_text(raw.get("state"), 32),
            "expires_at": _safe_float(raw.get("expires_at")),
            "remaining_seconds": max(0, _safe_int(raw.get("remaining_seconds"))),
            "consumed_at": (
                _safe_float(raw.get("consumed_at"))
                if raw.get("consumed_at") is not None
                else None
            ),
        }

    def _public_pairing_result(self, result: Any) -> dict[str, Any]:
        expires_at = _safe_float(getattr(result, "expires_at", 0))
        return {
            "pairing_id": _safe_text(getattr(result, "pairing_id", ""), 32),
            "state": "waiting",
            "expires_at": expires_at,
            "remaining_seconds": max(0, int(expires_at - time.time())),
            "consumed_at": None,
        }

    @staticmethod
    def _public_service_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = snapshot if isinstance(snapshot, Mapping) else {}
        sessions = raw.get("sessions") if isinstance(raw.get("sessions"), Mapping) else {}
        capabilities = (
            raw.get("capabilities")
            if isinstance(raw.get("capabilities"), Mapping)
            else {}
        )
        return {
            "enabled": bool(raw.get("enabled", False)),
            "ready": bool(raw.get("ready", False)),
            "status": _safe_text(raw.get("status"), 32),
            "reason": _safe_text(raw.get("reason"), 64),
            "sessions": {
                "active_sessions": max(
                    0, _safe_int(sessions.get("active_sessions"))
                ),
                "attached_streams": max(
                    0, _safe_int(sessions.get("attached_streams"))
                ),
                "queued_events": max(0, _safe_int(sessions.get("queued_events"))),
            },
            "dialogue": {
                "bridge": bool(capabilities.get("bridge", False)),
                "fallback": bool(capabilities.get("direct_provider_fallback", False)),
                "eventbus": False,
            },
        }

    @staticmethod
    def _public_dialogue_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = snapshot if isinstance(snapshot, Mapping) else {}
        return {
            "mode": "bridge",
            "bridge_available": bool(raw.get("bridge_available", False)),
            "allow_direct_provider_fallback": bool(
                raw.get("allow_direct_provider_fallback", False)
            ),
        }

    @staticmethod
    def _public_persona_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = snapshot if isinstance(snapshot, Mapping) else {}
        profiles = raw.get("profiles") if isinstance(raw.get("profiles"), list) else []
        return {
            "mode": _safe_text(raw.get("source_mode"), 32)
            or _safe_text(raw.get("persona_source_mode"), 32),
            "active_quest_persona_id": _safe_text(
                raw.get("active_quest_persona_id"), MAX_PROFILE_ID_CHARS
            ),
            "active_profile_name": _safe_text(raw.get("active_profile_name"), 80),
            "active_available": bool(raw.get("active_available", False)),
            "active_status": _safe_text(raw.get("active_status"), 32),
            "profile_count": len(profiles),
        }

    @staticmethod
    def _pairing_failure(exc: PairingError) -> dict[str, Any]:
        code = _safe_text(getattr(exc, "code", "") or "PAIRING_FAILED", 64).lower()
        message = _PAIRING_ERROR_LABELS.get(code, "配对操作失败")
        return _failure(code.upper(), message)

    @staticmethod
    def _pairing_id(value: Any) -> str | None:
        import re

        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip().lower()
        return normalized if re.fullmatch(PAIRING_ID_PATTERN, normalized) else None

    @staticmethod
    def _profile_id(value: Any) -> str | None:
        import re

        if value in (None, ""):
            return ""
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized:
            return ""
        if len(normalized) > MAX_PROFILE_ID_CHARS:
            return None
        return normalized if re.fullmatch(r"[A-Za-z0-9_.:-]+", normalized) else None
