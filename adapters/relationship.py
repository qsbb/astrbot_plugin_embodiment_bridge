from __future__ import annotations

import asyncio
import time
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
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 5.0)
        self.person_id = str(person_id or "").strip()
        # 同会话内短 TTL 缓存：关系快照变化慢（秒级），同一 (bot,user,group,profile)
        # 在 TTL 内重复 read 直接命中缓存，避免与情插件在 EventBus 路径的注入重复付费。
        # 0 表示关闭缓存（完全回退为每轮契约调用）。
        self.cache_ttl_seconds = min(max(float(cache_ttl_seconds), 0.0), 30.0)
        self._cache: dict[tuple[str, str, str, str, str], tuple[float, dict[str, Any] | None, str]] = {}
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
        cache_key = (
            str(self.person_id or ""),
            str(bot_id or ""),
            str(user_id or ""),
            str(group_id or ""),
            str(relationship_profile_id or ""),
        )
        if self.cache_ttl_seconds > 0:
            now = time.monotonic()
            cached = self._cache.get(cache_key)
            if cached is not None and (now - cached[0]) < self.cache_ttl_seconds:
                self.status = cached[2]
                return cached[1]
        snapshot = await self._read_live(
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            relationship_profile_id=relationship_profile_id,
        )
        if self.cache_ttl_seconds > 0:
            self._cache[cache_key] = (time.monotonic(), snapshot, self.status)
            if len(self._cache) > 256:
                oldest = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest, None)
        return snapshot

    async def _read_live(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str,
        relationship_profile_id: str,
    ) -> dict[str, Any] | None:
        if not self.person_id:
            self.status = "disabled"
            return None
        provider = find_active_provider(self.context, RELATIONSHIP_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[embodiment-bridge] relationship plugin not installed; continuing without snapshot"
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
                    "[embodiment-bridge] relationship.snapshot contract is incompatible; integration disabled"
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
                "[embodiment-bridge] relationship snapshot failed: error_type=%s",
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
                "[embodiment-bridge] relationship.snapshot returned an invalid payload"
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
        if not self.person_id:
            # Clearing the optional natural-person selector must immediately
            # disable relationship reads, even before the next turn arrives.
            self.status = "disabled"
        else:
            self.status = "authorization_gated"

    async def close(self) -> None:
        return None
