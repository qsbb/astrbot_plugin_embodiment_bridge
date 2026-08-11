from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from .provider_utils import contract_matches, find_active_provider
from .identity_control_plane import validate_principal_digest


IDENTITY_PLUGIN_NAME = "astrbot_plugin_identity_guardian"
IDENTITY_CONTRACT_NAME = "identity.quest_session_authorization"
IDENTITY_CONTRACT_MAJOR = "1"
IDENTITY_CAPABILITY = "authorize_read_only_session"
IDENTITY_METHOD = "authorize_quest_session"
IDENTITY_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProtectedContextDecision:
    authorized: bool
    reason: str


class QuestSessionAuthorizationAdapter:
    """Fail-closed gate for protected relationship context.

    ``api_principal`` comes from AstrBot's authenticated request context.  The
    client and platform identifiers come from Bridge server configuration, never
    from arbitrary extra Unity fields.  The protocol's declared client id must
    match the configured client before the provider is called.
    """

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        trusted_client_id: str,
        trusted_platform_id: str,
        local_api_principal_digest: str = "",
        local_bot_id: str = "",
        local_user_id: str = "",
        local_group_id: str = "",
        relationship_identity_resolver: Any | None = None,
        relationship_person_id: str = "",
        identity_sync_ready: bool = True,
    ) -> None:
        self.context = context
        self.logger = logger
        self.trusted_client_id = trusted_client_id.strip()
        self.trusted_platform_id = trusted_platform_id.strip()
        self._local_principal_fingerprint = _stored_principal_fingerprint(
            local_api_principal_digest
        )
        self.local_bot_id = str(local_bot_id or "").strip()
        self.local_user_id = str(local_user_id or "").strip()
        self.local_group_id = str(local_group_id or "").strip()
        self.relationship_identity_resolver = relationship_identity_resolver
        self.relationship_person_id = str(relationship_person_id or "").strip()
        self.identity_sync_ready = bool(identity_sync_ready)
        self.status = (
            "ready_for_authorization" if self.configured else self.configuration_reason
        )
        self._missing_logged = False
        self._incompatible_logged = False

    @property
    def configured(self) -> bool:
        return self.configuration_reason == "configured"

    @property
    def configuration_reason(self) -> str:
        if not self.trusted_client_id:
            return "trusted_client_id_missing"
        if not self.trusted_platform_id:
            return "trusted_platform_id_missing"
        if (
            "|" in self.trusted_client_id
            or "|" in self.trusted_platform_id
            or len(self.trusted_client_id) > 128
            or len(self.trusted_platform_id) > 128
        ):
            return "trusted_identity_config_invalid"
        return "configured"

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "contract": f"{IDENTITY_CONTRACT_NAME}@1.0",
            "configured": self.configured,
            "status": self.status,
            "default_access": "denied",
            "api_principal_source": "astrbot_authenticated_request",
            "client_id_source": "bridge_server_config",
            "platform_id_source": "bridge_server_config",
            "unity_trusted_source_fields": False,
            "fallback_mode": "exact_local_binding",
            "local_binding_configured": self.local_binding_configured,
        }

    @property
    def local_binding_configured(self) -> bool:
        return bool(
            self._local_principal_fingerprint
            and self.trusted_client_id
            and self.trusted_platform_id
            and self.local_bot_id
            and self.local_user_id
        )

    def configure_local_binding(
        self,
        *,
        api_principal_digest: str,
        client_id: str,
        platform_id: str,
        bot_id: str,
        user_id: str,
        group_id: str,
    ) -> None:
        self._local_principal_fingerprint = validate_principal_digest(
            api_principal_digest
        ).removeprefix("sha256:")
        self.trusted_client_id = str(client_id or "").strip()
        self.trusted_platform_id = str(platform_id or "").strip()
        self.local_bot_id = str(bot_id or "").strip()
        self.local_user_id = str(user_id or "").strip()
        self.local_group_id = str(group_id or "").strip()
        self.status = (
            "ready_for_authorization" if self.configured else self.configuration_reason
        )

    def configure_trusted_platform(self, platform_id: str) -> None:
        self.trusted_platform_id = str(platform_id or "").strip()
        self.status = (
            "ready_for_authorization" if self.configured else self.configuration_reason
        )

    def configure_relationship_person_id(self, person_id: str) -> None:
        self.relationship_person_id = str(person_id or "").strip()

    def configure_sync_ready(self, ready: bool) -> None:
        self.identity_sync_ready = bool(ready)

    def clear_local_binding(self) -> None:
        self._local_principal_fingerprint = ""
        self.local_bot_id = ""
        self.local_user_id = ""
        self.local_group_id = ""
        self.relationship_person_id = ""
        self.identity_sync_ready = True
        self.status = "local_identity_not_configured"

    def canonicalize_session_request(self, value: Any) -> Any:
        """Replace client identity claims with the configured server tuple."""
        if not self.local_bot_id or not self.local_user_id:
            return value
        copier = getattr(value, "model_copy", None)
        if not callable(copier):
            return value
        return copier(
            update={
                "bot_id": self.local_bot_id,
                "user_id": self.local_user_id,
                "group_id": self.local_group_id,
            }
        )

    async def authorize(
        self,
        *,
        api_principal: str,
        declared_client_id: str,
        bot_id: str,
        user_id: str,
        group_id: str,
    ) -> ProtectedContextDecision:
        principal = str(api_principal or "").strip()
        if not self.identity_sync_ready:
            self.status = "identity_sync_pending"
            return ProtectedContextDecision(False, self.status)
        if not self.configured:
            self.status = self.configuration_reason
            return ProtectedContextDecision(False, self.status)
        if not principal:
            self.status = "api_principal_missing"
            return ProtectedContextDecision(False, self.status)
        if str(declared_client_id or "").strip() != self.trusted_client_id:
            self.status = "client_id_mismatch"
            return ProtectedContextDecision(False, self.status)
        relationship_decision = await self._verify_relationship_identity(
            bot_id=bot_id,
            user_id=user_id,
        )
        if relationship_decision is not None:
            self.status = relationship_decision.reason
            return relationship_decision

        provider = find_active_provider(self.context, IDENTITY_PLUGIN_NAME)
        if provider is None:
            decision = self._authorize_local(
                api_principal=principal,
                bot_id=bot_id,
                user_id=user_id,
                group_id=group_id,
            )
            self.status = decision.reason
            if not self._missing_logged:
                self.logger.info(
                    "[embodiment-bridge] identity guardian not installed; using exact local binding fallback"
                )
                self._missing_logged = True
            return decision

        if not self._contract_compatible(provider):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[embodiment-bridge] identity.quest_session_authorization contract is incompatible; protected context disabled"
                )
                self._incompatible_logged = True
            return ProtectedContextDecision(False, self.status)

        request = {
            "api_principal": principal,
            "client_id": self.trusted_client_id,
            "platform_id": self.trusted_platform_id,
            "bot_id": str(bot_id or "").strip(),
            "user_id": str(user_id or "").strip(),
            "group_id": str(group_id) if group_id else None,
        }
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(provider.authorize_quest_session, request),
                timeout=IDENTITY_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "authorization_timeout"
            return ProtectedContextDecision(False, self.status)
        except Exception as exc:
            self.logger.warning(
                "[embodiment-bridge] session authorization failed: error_type=%s",
                type(exc).__name__,
            )
            self.status = "authorization_error"
            return ProtectedContextDecision(False, self.status)

        expected_fields = {
            "contract_version",
            "status",
            "authorized",
            "reason",
            "access",
            "owner_confirmed",
            "grants_platform_action",
        }
        owner_authorized = (
            bool(
                response.get("reason") == "authorized_private_owner_identity"
                and response.get("owner_confirmed") is True
            )
            if isinstance(response, dict)
            else False
        )
        quest_authorized = (
            bool(
                response.get("reason") == "authorized_private_quest_identity"
                and response.get("owner_confirmed") is False
            )
            if isinstance(response, dict)
            else False
        )
        valid = (
            isinstance(response, dict)
            and set(response) == expected_fields
            and str(response.get("contract_version") or "").split(".", 1)[0]
            == IDENTITY_CONTRACT_MAJOR
            and response.get("status") == "authorized"
            and response.get("authorized") is True
            and response.get("access") == "read_only_context"
            and (owner_authorized or quest_authorized)
            and response.get("grants_platform_action") is False
        )
        if not valid:
            reason = (
                str(response.get("reason") or "authorization_denied")
                if isinstance(response, dict)
                else "authorization_invalid_response"
            )
            self.status = reason
            return ProtectedContextDecision(False, reason)

        self.status = "authorized"
        return ProtectedContextDecision(True, str(response["reason"]))

    async def _verify_relationship_identity(
        self,
        *,
        bot_id: str,
        user_id: str,
    ) -> ProtectedContextDecision | None:
        if not self.relationship_person_id:
            return None
        resolver = self.relationship_identity_resolver
        if resolver is None:
            return ProtectedContextDecision(
                False, "relationship_event_identity_resolver_unavailable"
            )
        resolution = await resolver.resolve(
            person_id=self.relationship_person_id,
            platform_candidates=(self.trusted_platform_id,),
        )
        identity = resolution.identity
        if identity is None:
            reason = str(resolution.reason or "unavailable")
            return ProtectedContextDecision(
                False,
                f"relationship_event_identity_{reason}"[:128],
            )
        expected = "\x1f".join(
            (identity.platform_id, identity.bot_id, identity.user_id)
        )
        actual = "\x1f".join(
            (
                self.trusted_platform_id,
                str(bot_id or "").strip(),
                str(user_id or "").strip(),
            )
        )
        if not hmac.compare_digest(expected, actual):
            return ProtectedContextDecision(
                False, "relationship_event_identity_mismatch"
            )
        return None

    def _authorize_local(
        self,
        *,
        api_principal: str,
        bot_id: str,
        user_id: str,
        group_id: str,
    ) -> ProtectedContextDecision:
        if not self.local_binding_configured:
            return ProtectedContextDecision(False, "local_identity_not_configured")
        actual_fingerprint = hashlib.sha256(
            str(api_principal or "").encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            actual_fingerprint,
            self._local_principal_fingerprint,
        ):
            return ProtectedContextDecision(False, "local_api_principal_mismatch")
        expected = (
            self.local_bot_id,
            self.local_user_id,
            self.local_group_id,
        )
        actual = (
            str(bot_id or "").strip(),
            str(user_id or "").strip(),
            str(group_id or "").strip(),
        )
        if not hmac.compare_digest("\x1f".join(actual), "\x1f".join(expected)):
            return ProtectedContextDecision(False, "local_quest_identity_mismatch")
        return ProtectedContextDecision(True, "authorized_local_owner_identity")

    @staticmethod
    def _contract_compatible(provider: Any) -> bool:
        try:
            contract = provider.quest_session_authorization_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
        identity_fields = (
            contract.get("permission_identity_fields")
            if isinstance(contract, dict)
            else None
        )
        return bool(
            contract_matches(
                contract,
                name=IDENTITY_CONTRACT_NAME,
                major=IDENTITY_CONTRACT_MAJOR,
                capability=IDENTITY_CAPABILITY,
                method=IDENTITY_METHOD,
            )
            and contract.get("timeout_ms") == 1000
            and contract.get("permission_identity_mode")
            == "raw_platform_identity_tuple"
            and isinstance(identity_fields, (list, tuple))
            and tuple(identity_fields) == ("platform_id", "bot_id", "user_id")
            and contract.get("cross_platform_inheritance") is False
            and contract.get("grants_platform_action") is False
        )

    async def close(self) -> None:
        return None


def _stored_principal_fingerprint(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64 or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        return ""
    return normalized
