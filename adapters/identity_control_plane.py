from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from typing import Any

from .provider_utils import find_active_provider


IDENTITY_PLUGIN_NAME = "astrbot_plugin_identity_guardian"
CONTROL_PLANE_NAME = "identity.control_plane"
CONTROL_PLANE_VERSION = "1.0"
CONTROL_PLANE_TIMEOUT_SECONDS = 2.0
_AUTHENTICATED_API_PRINCIPAL_RE = re.compile(
    r"^api_key:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
_PRINCIPAL_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESPONSE_KEYS = {
    "contract_version",
    "status",
    "reason",
    "updated",
    "authorized",
    "config_writable",
    "owner_count",
    "quest_binding_count",
    "grants_platform_action",
}


class IdentityControlPlaneError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authenticated_principal_digest(api_principal: object) -> str:
    """Hash an AstrBot-authenticated API-key principal, never a raw API key."""

    principal = str(api_principal or "").strip()
    if not _AUTHENTICATED_API_PRINCIPAL_RE.fullmatch(principal):
        raise IdentityControlPlaneError(
            "invalid_authenticated_api_principal",
            "AstrBot 未提供有效的 API Key 身份凭据",
        )
    return "sha256:" + hashlib.sha256(principal.encode("utf-8")).hexdigest()


def validate_principal_digest(value: object) -> str:
    digest = str(value or "").strip().lower()
    if not _PRINCIPAL_DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid API principal digest")
    return digest


class IdentityControlPlaneAdapter:
    """Strict optional consumer for the series identity authority."""

    def __init__(self, context: Any, logger: Any) -> None:
        self.context = context
        self.logger = logger

    async def snapshot(self) -> dict[str, Any]:
        provider = find_active_provider(self.context, IDENTITY_PLUGIN_NAME)
        if provider is None:
            return {
                "source": "bridge_local",
                "authoritative": False,
                "status": "ready",
                "reason": "identity_guardian_not_installed",
                "config_writable": True,
                "owner_count": 0,
                "quest_binding_count": 0,
            }
        self._require_contract(provider)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(provider.get_identity_control_plane),
                timeout=CONTROL_PLANE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise IdentityControlPlaneError(
                "identity_control_plane_timeout",
                "统一身份控制面读取超时",
            ) from exc
        except Exception as exc:
            raise IdentityControlPlaneError(
                "identity_control_plane_error",
                "统一身份控制面读取失败",
            ) from exc
        validated = self._validate_response(result)
        return {
            "source": "identity_guardian",
            "authoritative": True,
            **validated,
        }

    async def upsert_quest_owner_binding(
        self,
        *,
        api_principal_digest: str,
        client_id: str,
        platform_id: str,
        bot_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        provider = find_active_provider(self.context, IDENTITY_PLUGIN_NAME)
        if provider is None:
            return {
                "source": "bridge_local",
                "authoritative": False,
                "status": "saved",
                "reason": "saved_to_bridge_local_fallback",
                "updated": True,
                "authorized": True,
                "config_writable": True,
                "owner_count": 1,
                "quest_binding_count": 1,
                "grants_platform_action": False,
            }
        self._require_contract(provider)
        request = {
            "api_principal_digest": validate_principal_digest(
                api_principal_digest
            ),
            "client_id": client_id,
            "platform_id": platform_id,
            "bot_id": bot_id,
            "user_id": user_id,
        }
        try:
            value = provider.upsert_quest_owner_binding(request)
            if inspect.isawaitable(value):
                value = await asyncio.wait_for(
                    value,
                    timeout=CONTROL_PLANE_TIMEOUT_SECONDS,
                )
            result = value
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise IdentityControlPlaneError(
                "identity_control_plane_timeout",
                "统一身份控制面保存超时",
            ) from exc
        except Exception as exc:
            raise IdentityControlPlaneError(
                "identity_control_plane_error",
                "统一身份控制面保存失败",
            ) from exc
        validated = self._validate_response(result)
        if (
            validated["status"] != "saved"
            or validated["updated"] is not True
            or validated["authorized"] is not True
        ):
            raise IdentityControlPlaneError(
                str(validated["reason"] or "identity_control_plane_rejected"),
                "统一身份控制面拒绝了 Quest 身份绑定",
            )
        return {
            "source": "identity_guardian",
            "authoritative": True,
            **validated,
        }

    @staticmethod
    def _require_contract(provider: Any) -> None:
        try:
            contract = provider.identity_control_plane_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise IdentityControlPlaneError(
                "identity_control_plane_incompatible",
                "已安装的“序”不提供兼容的统一身份契约",
            ) from exc
        compatible = bool(
            isinstance(contract, dict)
            and contract.get("name") == CONTROL_PLANE_NAME
            and contract.get("version") == CONTROL_PLANE_VERSION
            and contract.get("plugin") == IDENTITY_PLUGIN_NAME
            and tuple(contract.get("capabilities") or ())
            == (
                "read_status",
                "upsert_quest_owner_binding",
                "authorize_quest_session",
            )
            and tuple(contract.get("methods") or ())
            == (
                "get_identity_control_plane",
                "upsert_quest_owner_binding",
                "authorize_quest_session",
            )
            and contract.get("privacy") == "counts_only"
            and contract.get("principal_storage") == "sha256_digest_only"
            and contract.get("natural_person_grants_permission") is False
            and contract.get("provider_present_fallback") == "deny_without_local_merge"
        )
        if not compatible:
            raise IdentityControlPlaneError(
                "identity_control_plane_incompatible",
                "已安装的“序”不提供兼容的统一身份契约",
            )

    @staticmethod
    def _validate_response(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _RESPONSE_KEYS:
            raise IdentityControlPlaneError(
                "identity_control_plane_invalid_response",
                "统一身份控制面返回了无效响应",
            )
        valid = bool(
            value.get("contract_version") == CONTROL_PLANE_VERSION
            and value.get("status")
            in {"ready", "saved", "rejected", "unavailable", "error"}
            and isinstance(value.get("reason"), str)
            and isinstance(value.get("updated"), bool)
            and isinstance(value.get("authorized"), bool)
            and isinstance(value.get("config_writable"), bool)
            and isinstance(value.get("owner_count"), int)
            and value["owner_count"] >= 0
            and isinstance(value.get("quest_binding_count"), int)
            and value["quest_binding_count"] >= 0
            and value.get("grants_platform_action") is False
        )
        if not valid:
            raise IdentityControlPlaneError(
                "identity_control_plane_invalid_response",
                "统一身份控制面返回了无效响应",
            )
        return dict(value)
