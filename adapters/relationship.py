from __future__ import annotations

from typing import Any


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
        provider = self._find_provider()
        if provider is None:
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] relationship plugin not installed; continuing without snapshot"
                )
                self._missing_logged = True
            return None

        contract = provider.relationship_snapshot_contract()
        if not self._compatible(contract):
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] relationship.snapshot contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            return None

        snapshot = await provider.get_relationship_snapshot(
            bot_id,
            user_id,
            group_id or None,
            relationship_profile_id=relationship_profile_id or None,
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

    def _find_provider(self) -> Any | None:
        for metadata in self.context.get_all_stars():
            if metadata.name != RELATIONSHIP_PLUGIN_NAME or not metadata.activated:
                continue
            return metadata.star_cls
        return None

    @staticmethod
    def _compatible(contract: Any) -> bool:
        if not isinstance(contract, dict):
            return False
        version = str(contract.get("version") or "")
        capabilities = contract.get("capabilities")
        return bool(
            contract.get("name") == RELATIONSHIP_SNAPSHOT_CONTRACT_NAME
            and version.split(".", 1)[0] == RELATIONSHIP_SNAPSHOT_CONTRACT_MAJOR
            and isinstance(capabilities, (list, tuple))
            and "read_snapshot" in capabilities
        )

    async def close(self) -> None:
        return None
