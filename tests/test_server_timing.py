from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_pipeline import (
    MessagePipelineUnavailable,
)
from astrbot_plugin_quest_avatar_bridge.core.interaction_policy import (
    InteractionPolicy,
)
from astrbot_plugin_quest_avatar_bridge.core.models import (
    AudioChunkRequest,
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
    SessionStartRequest,
    TurnStartRequest,
)
from astrbot_plugin_quest_avatar_bridge.core.server_timing import (
    MAX_TIMING_MS,
    SERVER_TIMING_CONTRACT,
    ServerTimingState,
)
from astrbot_plugin_quest_avatar_bridge.core.session_manager import SessionManager
from astrbot_plugin_quest_avatar_bridge.core.turn_orchestrator import TurnOrchestrator


TIMING_KEYS = {
    "contract",
    "stt_ms",
    "decision_ms",
    "decision_path",
    "tts_first_chunk_ms",
    "tts_total_ms",
    "turn_total_ms",
}
DURATION_KEYS = TIMING_KEYS - {"contract", "decision_path"}


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


class RelationshipStub:
    async def read(self, **kwargs: Any) -> None:
        return None

    async def close(self) -> None:
        pass


class STTStub:
    available = True

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        assert pcm16
        assert sample_rate == 16_000
        await asyncio.sleep(0.002)
        return "recognized"

    async def close(self) -> None:
        pass


class TTSStub:
    available = True

    async def synthesize(self, text: str, *, emotion: str) -> AsyncIterator[bytes]:
        assert text
        assert emotion
        await asyncio.sleep(0.002)
        yield b"\x00\x00" * 1_200

    async def close(self) -> None:
        pass


class DecisionStub:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def generate(self, **kwargs: Any) -> ModelDecision:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider failed")
        await asyncio.sleep(0.002)
        return _decision()

    async def close(self) -> None:
        pass


class MessagePipelineStub:
    available = True
    availability_reason = "ready"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def generate(self, **kwargs: Any) -> ModelDecision:
        self.calls += 1
        await asyncio.sleep(0.002)
        if self.fail:
            raise MessagePipelineUnavailable("astrbot_event_queue_unavailable")
        return _decision()

    async def close(self) -> None:
        pass


def _decision() -> ModelDecision:
    return ModelDecision(
        should_reply=True,
        reply_text="reply",
        intent=ProposedIntent(
            emotion=Emotion.NEUTRAL,
            gesture=Gesture.TALK,
            look_at=LookAt.USER,
            intensity=0.4,
            duration_ms=1_200,
            reason_code="timing_test",
        ),
    )


async def _build(
    *,
    enabled: bool,
    authorized: bool = False,
    llm: Any | None = None,
    message_pipeline: Any | None = None,
    allow_direct_provider_fallback: bool = True,
) -> tuple[SessionManager, Any, TurnOrchestrator, DecisionStub]:
    sessions = SessionManager(interaction_debounce_ms=0)
    session = await sessions.start_session(
        SessionStartRequest(
            session_id="s1",
            client_id="quest",
            user_id="user",
            bot_id="bot",
        ),
        "owner",
        protected_context_authorized=authorized,
        context_authorization_reason="authorized" if authorized else "denied",
    )
    decision = llm or DecisionStub()
    orchestrator = TurnOrchestrator(
        sessions=sessions,
        llm=decision,
        stt=STTStub(),
        tts=TTSStub(),
        relationship=RelationshipStub(),
        policy=InteractionPolicy(gesture_cooldown_seconds=0),
        logger=LoggerStub(),
        message_pipeline=message_pipeline,
        allow_direct_provider_fallback=allow_direct_provider_fallback,
        server_timing_enabled=enabled,
    )
    return sessions, session, orchestrator, decision


async def _collect_until_end(session: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        item = await asyncio.wait_for(session.queue.get(), timeout=1)
        events.append(item.payload)
        if item.event_type == "reply.end":
            return events


def _assert_safe_timing(payload: dict[str, Any], path: str) -> None:
    timing = payload["server_timing"]
    assert set(timing) == TIMING_KEYS
    assert timing["contract"] == SERVER_TIMING_CONTRACT
    assert timing["decision_path"] == path
    for key in DURATION_KEYS:
        value = timing[key]
        assert isinstance(value, int) and not isinstance(value, bool)
        assert 0 <= value <= MAX_TIMING_MS
    serialized = json.dumps(timing, ensure_ascii=False)
    assert "contract" in serialized


def test_server_timing_is_absent_by_default() -> None:
    async def scenario() -> None:
        _sessions, session, orchestrator, _decision_stub = await _build(enabled=False)
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = await _collect_until_end(session)
        assert "server_timing" not in events[-1]
        await orchestrator.close()

    asyncio.run(scenario())


def test_server_timing_direct_provider_and_audio_stages_are_safe() -> None:
    async def scenario() -> None:
        _sessions, session, orchestrator, _decision_stub = await _build(enabled=True)
        turn = await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1"),
        )
        await orchestrator.sessions.add_audio_chunk(
            session,
            AudioChunkRequest(
                session_id="s1",
                turn_id="t1",
                sequence=0,
                data="AAAAAA==",
            ),
        )
        await orchestrator.finish_audio(session, turn.turn_id)
        events = await _collect_until_end(session)
        assert [item["type"] for item in events] == [
            "asr.final",
            "avatar.intent",
            "reply.text.delta",
            "reply.audio.chunk",
            "reply.end",
        ]
        _assert_safe_timing(events[-1], "direct_provider")
        timing = events[-1]["server_timing"]
        assert timing["stt_ms"] >= 0
        assert timing["decision_ms"] >= 0
        assert timing["tts_first_chunk_ms"] >= 0
        assert timing["tts_total_ms"] >= timing["tts_first_chunk_ms"]
        assert timing["turn_total_ms"] >= timing["stt_ms"]
        await orchestrator.close()

    asyncio.run(scenario())


def test_server_timing_records_eventbus_and_fallback_paths() -> None:
    async def run_case(
        fail: bool,
        expected: str,
        *,
        allow_fallback: bool = True,
    ) -> None:
        pipeline = MessagePipelineStub(fail=fail)
        _sessions, session, orchestrator, direct = await _build(
            enabled=True,
            authorized=True,
            message_pipeline=pipeline,
            allow_direct_provider_fallback=allow_fallback,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = await _collect_until_end(session)
        _assert_safe_timing(events[-1], expected)
        assert pipeline.calls == 1
        assert direct.calls == (1 if fail and allow_fallback else 0)
        if fail and not allow_fallback:
            assert [item["type"] for item in events] == ["error", "reply.end"]
        await orchestrator.close()

    async def scenario() -> None:
        await run_case(False, "astrbot_event_bus")
        await run_case(True, "direct_provider")
        await run_case(True, "astrbot_event_bus", allow_fallback=False)

    asyncio.run(scenario())


def test_server_timing_is_attached_to_failed_terminal_event() -> None:
    async def scenario() -> None:
        _sessions, session, orchestrator, _decision_stub = await _build(
            enabled=True,
            llm=DecisionStub(fail=True),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = await _collect_until_end(session)
        assert [item["type"] for item in events] == ["error", "reply.end"]
        assert events[-1]["status"] == "failed"
        _assert_safe_timing(events[-1], "direct_provider")
        await orchestrator.close()

    asyncio.run(scenario())


def test_server_timing_states_are_per_turn_and_clamped() -> None:
    old_start = -((MAX_TIMING_MS / 1000) + 1)
    first = ServerTimingState(started_at=old_start)
    second = ServerTimingState(started_at=old_start)
    first.stt_started_at = 1.0
    first.stt_ended_at = 2.0
    first.decision_path = "astrbot_event_bus"

    first_snapshot = first.snapshot()
    second_snapshot = second.snapshot()
    assert first_snapshot["stt_ms"] == 1_000
    assert first_snapshot["decision_path"] == "astrbot_event_bus"
    assert second_snapshot["stt_ms"] == 0
    assert second_snapshot["decision_path"] == "direct_provider"
    assert first_snapshot["turn_total_ms"] == MAX_TIMING_MS
    assert second_snapshot["turn_total_ms"] == MAX_TIMING_MS
