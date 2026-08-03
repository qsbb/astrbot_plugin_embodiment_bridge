from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .provider_utils import contract_matches, find_active_provider


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
    ) -> None:
        self.context = context
        self.logger = logger
        self.trusted_client_id = trusted_client_id.strip()
        self.trusted_platform_id = trusted_platform_id.strip()
        self.status = "not_checked"
        self._missing_logged = False
        self._incompatible_logged = False

    @property
    def configured(self) -> bool:
        return bool(self.trusted_client_id and self.trusted_platform_id)

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
        if not self.configured:
            self.status = "consumer_not_configured"
            return ProtectedContextDecision(False, self.status)
        if not principal:
            self.status = "api_principal_missing"
            return ProtectedContextDecision(False, self.status)
        if str(declared_client_id or "").strip() != self.trusted_client_id:
            self.status = "client_id_mismatch"
            return ProtectedContextDecision(False, self.status)

        provider = find_active_provider(self.context, IDENTITY_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] identity guardian not installed; protected context disabled"
                )
                self._missing_logged = True
            return ProtectedContextDecision(False, self.status)

        try:
            contract = provider.quest_session_authorization_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not contract_matches(
            contract,
            name=IDENTITY_CONTRACT_NAME,
            major=IDENTITY_CONTRACT_MAJOR,
            capability=IDENTITY_CAPABILITY,
            method=IDENTITY_METHOD,
        ):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] identity.quest_session_authorization contract is incompatible; protected context disabled"
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
                "[quest-avatar] Quest session authorization failed: error_type=%s",
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
        valid = (
            isinstance(response, dict)
            and set(response) == expected_fields
            and str(response.get("contract_version") or "").split(".", 1)[0]
            == IDENTITY_CONTRACT_MAJOR
            and response.get("status") == "authorized"
            and response.get("authorized") is True
            and response.get("reason") == "authorized_private_owner_identity"
            and response.get("access") == "read_only_context"
            and response.get("owner_confirmed") is True
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
        return ProtectedContextDecision(True, "authorized_private_owner_identity")

    async def close(self) -> None:
        return None
