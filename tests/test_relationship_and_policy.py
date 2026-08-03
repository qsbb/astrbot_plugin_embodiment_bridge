from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_quest_avatar_bridge.adapters.relationship import (
    RelationshipSnapshotAdapter,
)
from astrbot_plugin_quest_avatar_bridge.core.interaction_policy import (
    InteractionPolicy,
)
from astrbot_plugin_quest_avatar_bridge.core.models import (
    InteractionEvent,
    ModelDecision,
    ProposedIntent,
)


class LoggerStub:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, message: str, *args: Any) -> None:
        pass

    def warning(self, message: str, *args: Any) -> None:
        self.warnings.append(message)


class RelationshipProviderStub:
    def __init__(self, version: str = "1.0") -> None:
        self.version = version
        self.calls = 0

    def relationship_snapshot_contract(self) -> dict[str, Any]:
        return {
            "name": "relationship.snapshot",
            "version": self.version,
            "capabilities": ("read_snapshot",),
            "privacy": "derived_only",
        }

    async def get_relationship_snapshot(
        self,
        bot_id: str,
        user_id: str,
        group_id: str | None,
        *,
        relationship_profile_id: str | None,
        person_id: str,
    ) -> dict[str, Any]:
        del bot_id, user_id, group_id, relationship_profile_id, person_id
        self.calls += 1
        return {
            "version": "1.0",
            "mood": "normal",
            "willingness": 80,
            "relationship_tier": "familiar",
            "behavior": {"boundary": "normal"},
            "silence": {"suggested": False},
            "raw_affinity": 99,
        }


class ContextStub:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def get_all_stars(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="astrbot_plugin_relationship",
                activated=True,
                star_cls=self.provider,
            )
        ]


def test_relationship_adapter_requires_explicit_compatible_contract() -> None:
    async def scenario() -> None:
        provider = RelationshipProviderStub()
        adapter = RelationshipSnapshotAdapter(ContextStub(provider), LoggerStub())
        snapshot = await adapter.read(
            bot_id="bot",
            user_id="user",
            group_id="",
            relationship_profile_id="",
        )
        assert snapshot is not None
        assert snapshot["relationship_tier"] == "familiar"
        assert "raw_affinity" not in snapshot
        assert provider.calls == 1

        incompatible = RelationshipProviderStub(version="2.0")
        logger = LoggerStub()
        adapter = RelationshipSnapshotAdapter(ContextStub(incompatible), logger)
        assert (
            await adapter.read(
                bot_id="bot",
                user_id="user",
                group_id="",
                relationship_profile_id="",
            )
            is None
        )
        assert incompatible.calls == 0
        assert logger.warnings

    asyncio.run(scenario())


def test_policy_only_overrides_explicit_high_risk_boundary() -> None:
    policy = InteractionPolicy(gesture_cooldown_seconds=0)
    proposed = ModelDecision(
        should_reply=False,
        reply_text="",
        intent=ProposedIntent(
            emotion="happy",
            gesture="cheek_pinch",
            look_at="user",
            intensity=0.8,
            duration_ms=1500,
            reason_code="model_accepts_touch",
        ),
    )
    gentle = InteractionEvent(
        session_id="s1",
        event_id="e1",
        name="cheek_pinch",
        phase="start",
        strength=0.4,
        hand="right",
    )
    accepted = policy.apply(
        session_id="s1",
        turn_id="t1",
        decision=proposed,
        interaction=gentle,
        relationship={"behavior": {"boundary": "normal"}},
    )
    assert accepted.emotion.value == "happy"
    assert accepted.gesture.value == "cheek_pinch"

    unsafe = policy.apply(
        session_id="s2",
        turn_id="t2",
        decision=proposed,
        interaction=gentle.model_copy(update={"strength": 0.95}),
        relationship={"behavior": {"boundary": "normal"}},
    )
    assert unsafe.emotion.value == "uncomfortable"
    assert unsafe.gesture.value == "refuse"
    assert unsafe.look_at.value == "away"
    assert unsafe.reason_code == "boundary_safety_override"

    prolonged = policy.apply(
        session_id="s3",
        turn_id="t3",
        decision=proposed.model_copy(
            update={
                "intent": proposed.intent.model_copy(update={"gesture": "head_pat"})
            }
        ),
        interaction=gentle.model_copy(
            update={
                "event_id": "e2",
                "name": "head_pat",
                "phase": "update",
                "strength": 0.3,
                "duration_ms": 15_000,
            }
        ),
        relationship={"behavior": {"boundary": "normal"}},
    )
    assert prolonged.gesture.value == "refuse"
    assert prolonged.reason_code == "continuous_touch_limit"
