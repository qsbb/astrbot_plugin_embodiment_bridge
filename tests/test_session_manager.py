from __future__ import annotations

import asyncio
import base64
from unittest.mock import patch

import pytest

from astrbot_plugin_embodiment_bridge.core.models import (
    AudioChunkRequest,
    InteractionEvent,
    SessionStartRequest,
    SpatialContextRequest,
)
from astrbot_plugin_embodiment_bridge.core.session_manager import (
    BoundedEventQueue,
    QueueItem,
    SessionManager,
    SessionConflict,
    SessionNotFound,
    SessionOwnershipError,
)


def session_request(session_id: str, client_id: str = "quest-a") -> SessionStartRequest:
    return SessionStartRequest(
        session_id=session_id,
        client_id=client_id,
        user_id="user-1",
        bot_id="bot-1",
    )


def interaction(event_id: str) -> InteractionEvent:
    return InteractionEvent(
        session_id="s1",
        event_id=event_id,
        name="head_pat",
        phase="start",
        strength=0.7,
        duration_ms=0,
        hand="right",
    )


def spatial_context(
    revision: int,
    *,
    session_id: str = "s1",
    seat_count: int = 1,
) -> SpatialContextRequest:
    return SpatialContextRequest(
        session_id=session_id,
        schema_version=1,
        revision=revision,
        floor_count=1,
        seat_count=seat_count,
        bed_count=0,
        table_count=1,
        wall_count=4,
        door_count=1,
        window_count=1,
        scene_capture_available=True,
        occlusion_available=False,
    )


def test_session_isolation_and_ownership() -> None:
    async def scenario() -> None:
        manager = SessionManager(max_sessions=2)
        first = await manager.start_session(session_request("s1"), "api_key:one")
        second = await manager.start_session(
            session_request("s2", "quest-b"), "api_key:two"
        )
        assert (await manager.get_owned("s1", "api_key:one")) is first
        assert (await manager.get_owned("s2", "api_key:two")) is second
        with pytest.raises(SessionOwnershipError):
            await manager.get_owned("s1", "api_key:two")
        await manager.terminate()

    asyncio.run(scenario())


def test_session_action_capabilities_are_explicit_and_legacy_safe() -> None:
    async def scenario() -> None:
        manager = SessionManager(max_sessions=2)
        legacy = await manager.start_session(session_request("legacy"), "api_key:one")
        assert manager.supports_action(legacy, "wave") is True
        assert manager.supports_action(legacy, "crouch") is False
        assert manager.supports_action(legacy, "raise_leg") is False
        assert legacy.supported_actions_declared is False

        declared_request = session_request("declared", "quest-b").model_copy(
            update={"supported_actions": ("wave", "crouch")}
        )
        declared = await manager.start_session(declared_request, "api_key:two")
        assert declared.supported_actions == ("wave", "crouch")
        assert declared.supported_actions_declared is True
        assert manager.supports_action(declared, "crouch") is True
        assert manager.supports_action(declared, "dance") is False
        await manager.terminate()

    asyncio.run(scenario())


def test_identical_session_start_refreshes_authorization_but_identity_changes_conflict() -> (
    None
):
    async def scenario() -> None:
        manager = SessionManager(max_sessions=2)
        request = session_request("s1")
        first = await manager.start_session(
            request,
            "api_key:one",
            protected_context_authorized=False,
            context_authorization_reason="trusted_platform_id_missing",
        )

        refreshed = await manager.start_session(
            request,
            "api_key:one",
            protected_context_authorized=True,
            context_authorization_reason="authorized_private_owner_identity",
        )

        assert refreshed is first
        assert refreshed.protected_context_authorized is True
        assert (
            refreshed.context_authorization_reason
            == "authorized_private_owner_identity"
        )
        assert (await manager.stats())["active_sessions"] == 1

        for changed, owner in (
            (request.model_copy(update={"user_id": "other-user"}), "api_key:one"),
            (request, "api_key:two"),
        ):
            with pytest.raises(SessionConflict, match="already exists"):
                await manager.start_session(changed, owner)

        await manager.terminate()

    asyncio.run(scenario())


def test_interaction_dedupe_and_debounce() -> None:
    async def scenario() -> None:
        manager = SessionManager(interaction_debounce_ms=500)
        session = await manager.start_session(session_request("s1"), "owner")
        assert await manager.record_interaction(session, interaction("e1")) is True
        assert await manager.record_interaction(session, interaction("e1")) is False
        assert await manager.record_interaction(session, interaction("e2")) is False
        assert len(session.interactions) == 1
        await manager.terminate()

    asyncio.run(scenario())


def test_spatial_context_revision_update_idempotency_and_conflicts() -> None:
    async def scenario() -> None:
        manager = SessionManager()
        session = await manager.start_session(session_request("s1"), "owner")

        status, initial = await manager.update_spatial_context(
            session,
            spatial_context(1),
        )
        assert status == "updated"
        assert (await manager.read_spatial_context(session)) is initial

        status, unchanged = await manager.update_spatial_context(
            session,
            spatial_context(1),
        )
        assert status == "unchanged"
        assert unchanged is initial

        with pytest.raises(SessionConflict, match="content conflicts"):
            await manager.update_spatial_context(
                session,
                spatial_context(1, seat_count=2),
            )
        with pytest.raises(SessionConflict, match="stale"):
            await manager.update_spatial_context(session, spatial_context(0))

        status, latest = await manager.update_spatial_context(
            session,
            spatial_context(2, seat_count=2),
        )
        assert status == "updated"
        assert latest.revision == 2
        assert latest.seat_count == 2
        await manager.terminate()

    asyncio.run(scenario())


def test_spatial_context_is_session_owned_isolated_and_destroyed_on_close() -> None:
    async def scenario() -> None:
        manager = SessionManager(max_sessions=2)
        first = await manager.start_session(session_request("s1"), "owner-one")
        second = await manager.start_session(
            session_request("s2", "quest-b"),
            "owner-two",
        )
        with pytest.raises(SessionOwnershipError):
            await manager.get_owned("s1", "owner-two")
        with pytest.raises(SessionConflict, match="another session"):
            await manager.update_spatial_context(
                first, spatial_context(1, session_id="s2")
            )

        await manager.update_spatial_context(first, spatial_context(1))
        assert await manager.read_spatial_context(second) is None

        await manager.close_session(first)
        assert first.spatial_context is None
        with pytest.raises(SessionNotFound, match="closed"):
            await manager.read_spatial_context(first)
        with pytest.raises(SessionNotFound):
            await manager.get_owned("s1", "owner-one")
        await manager.terminate()

    asyncio.run(scenario())


def test_spatial_context_expires_instead_of_becoming_stale_room_truth() -> None:
    async def scenario() -> None:
        manager = SessionManager()
        session = await manager.start_session(session_request("s1"), "owner")
        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=10.0,
        ):
            await manager.update_spatial_context(session, spatial_context(1))
        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=41.0,
        ):
            assert await manager.read_spatial_context(session) is None
        assert session.spatial_context_updated_at == 0.0
        await manager.terminate()

    asyncio.run(scenario())


def test_interaction_turns_are_bounded_and_do_not_replace_primary() -> None:
    async def scenario() -> None:
        manager = SessionManager(max_concurrent_interactions=2)
        session = await manager.start_session(session_request("s1"), "owner")
        primary = await manager.begin_turn(session, "t1", cancel_previous=True)
        first = await manager.begin_interaction_turn(session, "i:e1")
        second = await manager.begin_interaction_turn(session, "i:e2")

        assert manager.is_current(session, primary.turn_id, primary.generation)
        assert manager.is_current(session, first.turn_id, first.generation)
        assert manager.is_current(session, second.turn_id, second.generation)
        with pytest.raises(SessionConflict, match="concurrency limit"):
            await manager.begin_interaction_turn(session, "i:e3")

        await manager.complete_turn(session, first)
        assert not manager.is_current(session, first.turn_id, first.generation)
        assert manager.is_current(session, primary.turn_id, primary.generation)
        await manager.terminate()

    asyncio.run(scenario())


def test_fast_action_task_is_cancelled_by_every_turn_lifecycle_boundary() -> None:
    async def blocker(started: asyncio.Event, cancelled: asyncio.Event) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def attach(turn: object) -> tuple[asyncio.Task[None], asyncio.Event]:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        task = asyncio.create_task(blocker(started, cancelled))
        turn.fast_action_task = task
        await asyncio.wait_for(started.wait(), timeout=1)
        return task, cancelled

    async def scenario() -> None:
        manager = SessionManager(max_sessions=4)
        session = await manager.start_session(session_request("s1"), "owner")

        replaced = await manager.begin_turn(session, "replace-me", cancel_previous=True)
        replaced_task, replaced_cancelled = await attach(replaced)
        current = await manager.begin_turn(session, "cancel-me", cancel_previous=True)
        await asyncio.wait_for(replaced_cancelled.wait(), timeout=1)
        await asyncio.gather(replaced_task, return_exceptions=True)

        current_task, current_cancelled = await attach(current)
        assert await manager.cancel_current(session, current.turn_id) is True
        await asyncio.wait_for(current_cancelled.wait(), timeout=1)
        await asyncio.gather(current_task, return_exceptions=True)

        close_session = await manager.start_session(
            session_request("s2", "quest-b"), "owner-two"
        )
        close_turn = await manager.begin_turn(
            close_session, "close-me", cancel_previous=True
        )
        close_task, close_cancelled = await attach(close_turn)
        await manager.close_session(close_session)
        assert close_cancelled.is_set()
        assert close_task.cancelled()

        terminate_session = await manager.start_session(
            session_request("s3", "quest-c"), "owner-three"
        )
        terminate_turn = await manager.begin_turn(
            terminate_session, "terminate-me", cancel_previous=True
        )
        terminate_task, terminate_cancelled = await attach(terminate_turn)
        await manager.terminate()
        assert terminate_cancelled.is_set()
        assert terminate_task.cancelled()

    asyncio.run(scenario())


def test_audio_sequence_size_and_format_are_enforced() -> None:
    async def scenario() -> None:
        manager = SessionManager(max_audio_bytes=6_400, max_audio_chunk_bytes=3_200)
        session = await manager.start_session(session_request("s1"), "owner")
        await manager.begin_turn(session, "t1", cancel_previous=True)
        chunk = AudioChunkRequest(
            session_id="s1",
            turn_id="t1",
            sequence=0,
            data=base64.b64encode(b"\x00\x00" * 800).decode("ascii"),
        )
        assert await manager.add_audio_chunk(session, chunk) == 1_600
        with pytest.raises(Exception):
            await manager.add_audio_chunk(
                session,
                chunk.model_copy(update={"sequence": 2}),
            )
        await manager.terminate()

    asyncio.run(scenario())


def test_slow_client_keeps_critical_events_and_coalesces_partial() -> None:
    async def scenario() -> None:
        queue = BoundedEventQueue(4)
        await queue.put(QueueItem({"type": "reply.end"}, "t1", 1))
        await queue.put(QueueItem({"type": "error"}, "t1", 1))
        await queue.put(QueueItem({"type": "avatar.intent"}, "t1", 1))
        await queue.put(QueueItem({"type": "asr.partial", "text": "a"}, "t1", 1))
        assert await queue.put(
            QueueItem({"type": "avatar.intent", "emotion": "happy"}, "t1", 1)
        )
        types = [(await queue.get()).event_type for _ in range(4)]
        assert types.count("avatar.intent") == 2
        assert "reply.end" in types
        assert "error" in types
        assert "asr.partial" not in types
        await queue.close()

    asyncio.run(scenario())


def test_noncritical_event_drops_when_only_critical_events_fill_queue() -> None:
    async def scenario() -> None:
        queue = BoundedEventQueue(4)
        for index in range(4):
            await queue.put(QueueItem({"type": "error", "code": str(index)}, "t1", 1))
        accepted = await queue.put(
            QueueItem({"type": "reply.text.delta", "text": "late"}, "t1", 1)
        )
        assert accepted is False
        await queue.close()

    asyncio.run(scenario())


def test_audio_backpressure_keeps_critical_events_and_bounds_queue() -> None:
    async def scenario() -> None:
        queue = BoundedEventQueue(4)
        await queue.put(QueueItem({"type": "avatar.intent"}, "t1", 1))
        await queue.put(QueueItem({"type": "error"}, "t1", 1))
        await queue.put(
            QueueItem({"type": "reply.audio.chunk", "sequence": 0}, "t1", 1)
        )
        await queue.put(
            QueueItem({"type": "reply.audio.chunk", "sequence": 1}, "t1", 1)
        )

        blocked = asyncio.create_task(
            queue.put(QueueItem({"type": "reply.audio.chunk", "sequence": 2}, "t1", 1))
        )
        await asyncio.sleep(0)
        assert blocked.done() is False
        assert queue.size == 4

        assert (await queue.get()).event_type == "avatar.intent"
        assert await asyncio.wait_for(blocked, timeout=1) is True
        remaining = [(await queue.get()).payload for _ in range(4)]
        assert [item["type"] for item in remaining] == [
            "error",
            "reply.audio.chunk",
            "reply.audio.chunk",
            "reply.audio.chunk",
        ]
        assert [item["sequence"] for item in remaining[1:]] == [0, 1, 2]
        await queue.close()

    asyncio.run(scenario())
