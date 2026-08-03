from __future__ import annotations

import asyncio
from typing import Any

from .provider_utils import contract_matches, find_active_provider


RELATIONSHIP_PLUGIN_NAME = "astrbot_plugin_relationship"
RELATIONSHIP_SNAPSHOT_CONTRACT_NAME = "relationship.snapshot"
RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR = "1"


class RelationshipSnapshotAdapter:
    """Consume only the explicitly declared relationship.snapshot@1 contract."""

    def __init__(self, context: Any, logger: Any) -> None:
        self.context = context
        self.logger = logger
        self._missing_logged = False
        self._incompatible_logged = False

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
        if (
            not contract_matches(
                contract,
                name=RELATIONSHIP_SNAPSHOT_CONTRACT_NAME,
                major=RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR,
                capability="read_snapshot",
            )
            or contract.get("privacy") != "derived_only"
        ):
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] relationship.snapshot contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            return None

        snapshot = await asyncio.wait_for(
            provider.get_relationship_snapshot(
                bot_id,
                user_id,
                group_id or None,
                relationship_profile_id=relationship_profile_id or None,
                person_id="",
            ),
            timeout=1.0,
        )
        if not isinstance(snapshot, dict):
            self.logger.warning(
                "[quest-avatar] relationship.snapshot returned a non-object payload"
            )
            return None
        version = str(snapshot.get("version") or "")
        if version.split(".", 1)[0] != RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR:
            self.logger.warning(
                "[quest-avatar] relationship.snapshot payload version is incompatible"
            )
            return None
        allowed = {
            "version",
            "mood",
            "willingness",
            "relationship_tier",
            "behavior",
            "silence",
        }
        return {key: snapshot[key] for key in allowed if key in snapshot}

    async def close(self) -> None:
        return None
