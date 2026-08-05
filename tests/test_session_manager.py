from __future__ import annotations

import asyncio
import base64

import pytest

from astrbot_plugin_quest_avatar_bridge.core.models import (
    AudioChunkRequest,
    InteractionEvent,
    SessionStartRequest,
)
from astrbot_plugin_quest_avatar_bridge.core.session_manager import (
    BoundedEventQueue,
    QueueItem,
    SessionManager,
    SessionConflict,
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
