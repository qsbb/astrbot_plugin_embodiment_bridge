from __future__ import annotations

import asyncio
from typing import Any

from .provider_utils import contract_matches, find_active_provider


RELATIONSHIP_PLUGIN_NAME = "astrbot_plugin_relationship"
RELATIONSHIP_SNAPSHOT_CONTRACT_NAME = "relationship.snapshot"
RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR = "1"
_RELATIONSHIP_TIERS = {
    "guarded",
    "neutral",
    "familiar",
    "close",
    "inner_circle",
}
_BEHAVIOR_FIELDS = {"tone", "length", "initiative", "boundary", "followup"}
_SILENCE_FIELDS = {"suggested", "reason", "strength"}


class RelationshipSnapshotAdapter:
    """Consume only the explicitly declared relationship.snapshot@1 contract."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        person_id: str = "",
        timeout_seconds: float = 1.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 5.0)
        self.person_id = str(person_id or "").strip()
        self._missing_logged = False
        self._incompatible_logged = False
        self.status = "authorization_gated"

    async def read(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str,
        relationship_profile_id: str,
    ) -> dict[str, Any] | None:
        provider = find_active_provider(self.context, RELATIONSHIP_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] relationship plugin not installed; continuing without snapshot"
                )
                self._missing_logged = True
            return None

        try:
            contract = provider.relationship_snapshot_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not self._compatible_contract(contract):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] relationship.snapshot contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            return None

        try:
            snapshot = await asyncio.wait_for(
                provider.get_relationship_snapshot(
                    bot_id,
                    user_id,
                    group_id or None,
                    relationship_profile_id=relationship_profile_id or None,
                    person_id=self.person_id,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "timeout"
            return None
        except Exception as exc:
            self.status = "error"
            self.logger.warning(
                "[quest-avatar] relationship snapshot failed: error_type=%s",
                type(exc).__name__,
            )
            return None
        try:
            normalized = self._normalize(snapshot)
        except (TypeError, ValueError):
            normalized = None
        if normalized is None:
            self.status = "invalid_response"
            self.logger.warning(
                "[quest-avatar] relationship.snapshot returned an invalid payload"
            )
            return None
        self.status = "ok"
        return normalized

    @staticmethod
    def _compatible_contract(contract: Any) -> bool:
        return bool(
            contract_matches(
                contract,
                name=RELATIONSHIP_SNAPSHOT_CONTRACT_NAME,
                major=RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR,
                capability="read_snapshot",
            )
            and contract.get("privacy") == "derived_only"
        )

    @staticmethod
    def _normalize(snapshot: Any) -> dict[str, Any] | None:
        allowed = {
            "version",
            "mood",
            "willingness",
            "relationship_tier",
            "behavior",
            "silence",
        }
        if not isinstance(snapshot, dict) or set(snapshot) != allowed:
            return None
        version = str(snapshot.get("version") or "")
        behavior = snapshot.get("behavior")
        silence = snapshot.get("silence")
        willingness = snapshot.get("willingness")
        relationship_tier = snapshot.get("relationship_tier")
        if (
            version != "1.0"
            or not isinstance(snapshot.get("mood"), str)
            or isinstance(willingness, bool)
            or not isinstance(willingness, int)
            or not isinstance(relationship_tier, str)
            or relationship_tier not in _RELATIONSHIP_TIERS
            or not isinstance(behavior, dict)
            or set(behavior) != _BEHAVIOR_FIELDS
            or not all(isinstance(behavior[field], str) for field in _BEHAVIOR_FIELDS)
            or not isinstance(silence, dict)
            or set(silence) != _SILENCE_FIELDS
            or not isinstance(silence.get("suggested"), bool)
            or not isinstance(silence.get("reason"), str)
            or isinstance(silence.get("strength"), bool)
            or not isinstance(silence.get("strength"), int)
        ):
            return None
        return {
            "version": version,
            "mood": snapshot["mood"][:128],
            "willingness": max(0, min(100, willingness)),
            "relationship_tier": snapshot["relationship_tier"],
            "behavior": {field: behavior[field][:128] for field in _BEHAVIOR_FIELDS},
            "silence": {
                "suggested": silence["suggested"],
                "reason": silence["reason"][:256],
                "strength": max(0, min(100, silence["strength"])),
            },
        }

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "contract": f"{RELATIONSHIP_SNAPSHOT_CONTRACT_NAME}@1.0",
            "status": self.status,
            "access": "identity_authorized_sessions_only",
            "privacy": "derived_only",
            "person_source": "bridge_server_config",
        }

    def configure_person_id(self, person_id: str) -> None:
        self.person_id = str(person_id or "").strip()

    async def close(self) -> None:
        return None
