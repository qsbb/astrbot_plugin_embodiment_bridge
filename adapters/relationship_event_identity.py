from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .provider_utils import contract_matches, find_active_provider


RELATIONSHIP_PLUGIN_NAME = "astrbot_plugin_relationship"
QUEST_EVENT_IDENTITY_CONTRACT_NAME = "relationship.quest_event_identity"
QUEST_EVENT_IDENTITY_CONTRACT_MAJOR = "1"
QUEST_EVENT_IDENTITY_METHOD = "resolve_quest_event_identity"
_PRIVATE_UMO_MESSAGE_TYPES = frozenset(
    {"friendmessage", "privatemessage", "directmessage"}
)
_UNAVAILABLE_REASONS = frozenset(
    {
        "invalid_request",
        "active_platform_required",
        "active_platform_api_unavailable",
        "active_platform_not_available",
        "person_not_found",
        "identity_transaction_pending",
        "private_account_not_found",
        "private_account_ambiguous",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class QuestEventIdentity:
    platform_id: str
    bot_id: str
    user_id: str
    session_id: str


@dataclass(frozen=True, slots=True, repr=False)
class QuestEventIdentityResolution:
    status: str
    reason: str
    identity: QuestEventIdentity | None = None


class RelationshipQuestEventIdentityAdapter:
    """Resolve raw event identity only through the server-side formal contract."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.timeout_seconds = min(max(float(timeout_seconds), 0.05), 5.0)
        self.status = "not_checked"
        self.reason = ""
        self._missing_logged = False
        self._incompatible_logged = False

    async def resolve(
        self,
        *,
        person_id: str,
        platform_candidates: tuple[str, ...],
    ) -> QuestEventIdentityResolution:
        provider = find_active_provider(self.context, RELATIONSHIP_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            self.reason = "relationship_plugin_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[embodiment-bridge] relationship plugin not installed; event identity unavailable"
                )
                self._missing_logged = True
            return QuestEventIdentityResolution(self.status, self.reason)

        try:
            contract = provider.quest_event_identity_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not self._compatible_contract(contract):
            self.status = "contract_unavailable"
            self.reason = "relationship_quest_event_identity_contract_unavailable"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[embodiment-bridge] relationship.quest_event_identity contract unavailable; private registry fallback is forbidden"
                )
                self._incompatible_logged = True
            return QuestEventIdentityResolution(self.status, self.reason)

        try:
            payload = await asyncio.wait_for(
                provider.resolve_quest_event_identity(
                    person_id=person_id,
                    platform_candidates=list(platform_candidates),
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "timeout"
            self.reason = "relationship_quest_event_identity_timeout"
            return QuestEventIdentityResolution(self.status, self.reason)
        except Exception as exc:
            self.status = "error"
            self.reason = "relationship_quest_event_identity_error"
            self.logger.warning(
                "[embodiment-bridge] relationship event identity failed: error_type=%s",
                type(exc).__name__,
            )
            return QuestEventIdentityResolution(self.status, self.reason)

        identity, status, reason = self._normalize(payload, platform_candidates)
        self.status = status
        self.reason = reason
        if status == "invalid_response":
            self.logger.warning(
                "[embodiment-bridge] relationship.quest_event_identity returned an invalid payload"
            )
        return QuestEventIdentityResolution(status, reason, identity)

    @staticmethod
    def _compatible_contract(contract: Any) -> bool:
        return bool(
            contract_matches(
                contract,
                name=QUEST_EVENT_IDENTITY_CONTRACT_NAME,
                major=QUEST_EVENT_IDENTITY_CONTRACT_MAJOR,
                capability="resolve_private_event_identity",
                method=QUEST_EVENT_IDENTITY_METHOD,
            )
            and contract.get("plugin") == RELATIONSHIP_PLUGIN_NAME
            and contract.get("privacy") == "server_only_raw_account"
            and contract.get("browser_exposed") is False
            and contract.get("exposes_raw_account_ids") is True
            and contract.get("grants_permission") is False
            and contract.get("active_platform_match_required") is True
            and contract.get("private_session_required") is True
        )

    @staticmethod
    def _normalize(
        payload: Any,
        platform_candidates: tuple[str, ...],
    ) -> tuple[QuestEventIdentity | None, str, str]:
        if not isinstance(payload, dict) or set(payload) != {
            "contract_version",
            "status",
            "reason",
            "identity",
        }:
            return None, "invalid_response", "invalid_response"
        if payload.get("contract_version") != "1.0":
            return None, "invalid_response", "invalid_response"
        status = payload.get("status")
        reason = payload.get("reason")
        if status == "unavailable":
            if (
                reason not in _UNAVAILABLE_REASONS
                or payload.get("identity") is not None
            ):
                return None, "invalid_response", "invalid_response"
            return None, "unavailable", str(reason)
        if status != "ok" or reason != "resolved_unique_active_private_account":
            return None, "invalid_response", "invalid_response"

        value = payload.get("identity")
        if not isinstance(value, dict) or set(value) != {
            "platform_id",
            "bot_id",
            "user_id",
            "session_id",
        }:
            return None, "invalid_response", "invalid_response"
        fields = {
            key: str(value.get(key) or "").strip()
            for key in ("platform_id", "bot_id", "user_id", "session_id")
        }
        if any(
            not item
            or len(item) > (240 if key == "session_id" else 120)
            or "|" in item
            or any(char.isspace() or ord(char) < 33 for char in item)
            for key, item in fields.items()
        ):
            return None, "invalid_response", "invalid_response"
        allowed_platforms = {
            str(item).strip().casefold()
            for item in platform_candidates
            if str(item).strip()
        }
        if fields["platform_id"].casefold() not in allowed_platforms:
            return None, "invalid_response", "invalid_response"
        parts = fields["session_id"].split(":", 2)
        if not (
            len(parts) == 3
            and parts[0].casefold() == fields["platform_id"].casefold()
            and parts[1].casefold() in _PRIVATE_UMO_MESSAGE_TYPES
            and parts[2] == fields["user_id"]
        ):
            return None, "invalid_response", "invalid_response"
        return QuestEventIdentity(**fields), "ok", str(reason)

    async def close(self) -> None:
        return None
