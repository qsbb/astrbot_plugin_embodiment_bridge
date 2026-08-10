from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from astrbot_plugin_quest_avatar_bridge.adapters.stt import DisabledSTTAdapter
from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_pipeline import (
    MessagePipelineEmpty,
    MessagePipelineUnavailable,
)
from astrbot_plugin_quest_avatar_bridge.core.interaction_policy import InteractionPolicy
from astrbot_plugin_quest_avatar_bridge.core.models import (
    Emotion,
    Gesture,
    InteractionEvent,
    LookAt,
    ModelDecision,
    ProposedIntent,
    SessionStartRequest,
    TurnStartRequest,
    safe_neutral_decision,
)
from astrbot_plugin_quest_avatar_bridge.core.session_manager import SessionManager
from astrbot_plugin_quest_avatar_bridge.core.turn_orchestrator import TurnOrchestrator


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


class RelationshipStub:
    def __init__(self, snapshot: dict[str, Any] | None = None) -> None:
        self.snapshot = snapshot

    async def read(self, **kwargs: Any) -> dict[str, Any] | None:
        return self.snapshot

    async def close(self) -> None:
        pass


class TTSStub:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    async def synthesize(self, text: str, *, emotion: str) -> AsyncIterator[bytes]:
        del text, emotion
        yield b"\x00\x00" * 1_200

    async def close(self) -> None:
        pass


class LateTTSStub(TTSStub):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, text: str, *, emotion: str) -> AsyncIterator[bytes]:
        del text, emotion
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
        yield b"\x00\x00" * 1_200


class DecisionStub:
    def __init__(self, decision: ModelDecision) -> None:
        self.decision = decision

    async def generate(self, **kwargs: Any) -> ModelDecision:
        return self.decision

    async def close(self) -> None:
        pass


class FailingDecisionStub(DecisionStub):
    async def generate(self, **kwargs: Any) -> ModelDecision:
        del kwargs
        raise RuntimeError("provider failed")


class EmptyMessagePipelineStub:
    available = True
    availability_reason = "ready"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def generate(self, **kwargs: Any) -> ModelDecision:
        del kwargs
        raise MessagePipelineEmpty(self.reason)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "last_event_woken": True,
            "last_event_stopped": True,
            "last_send_observed": False,
        }

    async def close(self) -> None:
        return None


class LateDecisionStub(DecisionStub):
    def __init__(self, decision: ModelDecision) -> None:
        super().__init__(decision)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, **kwargs: Any) -> ModelDecision:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
        return self.decision


class ConcurrentInteractionDecisionStub:
    def __init__(self) -> None:
        self.primary_started = asyncio.Event()
        self.primary_release = asyncio.Event()
        self.primary_cancelled = asyncio.Event()

    async def generate(self, **kwargs: Any) -> ModelDecision:
        if kwargs.get("interaction") is not None:
            return decision(
                Emotion.SHY,
                Gesture.STEP_BACK,
                LookAt.AWAY,
                "interaction_reply",
                "interaction",
            )
        self.primary_started.set()
        try:
            await self.primary_release.wait()
        except asyncio.CancelledError:
            self.primary_cancelled.set()
            raise
        return decision(
            Emotion.HAPPY,
            Gesture.TALK,
            LookAt.USER,
            "primary_reply",
            "primary",
        )

    async def close(self) -> None:
        return None


def decision(
    emotion: Emotion,
    gesture: Gesture,
    look_at: LookAt,
    reason: str,
    text: str,
) -> ModelDecision:
    return ModelDecision(
        should_reply=bool(text),
        reply_text=text,
        intent=ProposedIntent(
            emotion=emotion,
            gesture=gesture,
            look_at=look_at,
            intensity=0.6,
            duration_ms=1_200,
            reason_code=reason,
        ),
    )


async def build_orchestrator(
    llm: Any,
    *,
    queue_size: int = 64,
    tts: Any | None = None,
):
    sessions = SessionManager(event_queue_size=queue_size, interaction_debounce_ms=0)
    session = await sessions.start_session(
        SessionStartRequest(
            session_id="s1",
            client_id="quest",
            user_id="user",
            bot_id="bot",
        ),
        "owner",
    )
    orchestrator = TurnOrchestrator(
        sessions=sessions,
        llm=llm,
        stt=DisabledSTTAdapter(),
        tts=tts or TTSStub(),
        relationship=RelationshipStub(),
        policy=InteractionPolicy(gesture_cooldown_seconds=0),
        logger=LoggerStub(),
    )
    return sessions, session, orchestrator


async def collect_until_end(session: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        item = await asyncio.wait_for(session.queue.get(), timeout=1)
        events.append(item.payload)
        if item.event_type in {"reply.end", "error"}:
            return events


def test_pipeline_error_uses_safe_session_authorization_reason() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(safe_neutral_decision("test"))
        )
        session.context_authorization_reason = "quest_identity_not_allowlisted"

        assert (
            orchestrator._public_pipeline_reason(
                session,
                MessagePipelineUnavailable("protected_context_not_authorized"),
            )
            == "quest_identity_not_allowlisted"
        )
        assert "五段绑定" in orchestrator._pipeline_error_message(
            "quest_identity_not_allowlisted"
        )
        assert (
            orchestrator._public_pipeline_reason(
                session,
                MessagePipelineUnavailable("private_internal_detail"),
            )
            == "astrbot_message_pipeline_unavailable"
        )
        await sessions.terminate()

    asyncio.run(scenario())


def test_accept_and_refuse_are_model_decisions_not_touch_mappings() -> None:
    async def scenario() -> None:
        accepted = decision(
            Emotion.HAPPY,
            Gesture.HEAD_PAT,
            LookAt.USER,
            "touch_accepted",
            "可以呀。",
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(accepted)
        )
        event = InteractionEvent(
            session_id="s1",
            event_id="e1",
            name="head_pat",
            phase="start",
            strength=0.5,
            hand="right",
        )
        await orchestrator.submit_interaction(session, event)
        accepted_events = await collect_until_end(session)
        assert accepted_events[0]["emotion"] == "happy"
        assert accepted_events[0]["gesture"] == "head_pat"
        await orchestrator.close()

        refused = decision(
            Emotion.UNCOMFORTABLE,
            Gesture.STEP_BACK,
            LookAt.AWAY,
            "boundary_soft_refusal",
            "先别这样。",
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(refused)
        )
        await orchestrator.submit_interaction(
            session, event.model_copy(update={"event_id": "e2"})
        )
        refused_events = await collect_until_end(session)
        assert refused_events[0]["emotion"] == "uncomfortable"
        assert refused_events[0]["gesture"] == "step_back"
        await orchestrator.close()

    asyncio.run(scenario())


def test_invalid_llm_structure_has_safe_neutral_fallback() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(safe_neutral_decision())
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = await collect_until_end(session)
        assert events[0]["type"] == "avatar.intent"
        assert events[0]["emotion"] == "neutral"
        assert events[0]["gesture"] == "idle"
        assert events[0]["look_at"] == "none"
        assert events[-1]["type"] == "reply.end"
        await orchestrator.close()

    asyncio.run(scenario())


def test_text_and_interaction_failures_emit_terminal_reply_end() -> None:
    async def collect_failure(session: Any) -> list[dict[str, Any]]:
        return [
            (await asyncio.wait_for(session.queue.get(), timeout=1)).payload
            for _ in range(2)
        ]

    async def scenario() -> None:
        failed = decision(Emotion.NEUTRAL, Gesture.IDLE, LookAt.NONE, "unused", "")
        sessions, session, orchestrator = await build_orchestrator(
            FailingDecisionStub(failed)
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        text_events = await collect_failure(session)
        assert [event["type"] for event in text_events] == ["error", "reply.end"]
        assert text_events[0]["code"] == "turn_failed"
        assert text_events[1] == {
            "type": "reply.end",
            "protocol_version": "1.0",
            "session_id": "s1",
            "turn_id": "t1",
            "status": "failed",
            "text_sent": False,
            "audio_sent": False,
        }

        interaction = InteractionEvent(
            session_id="s1",
            event_id="e1",
            name="head_pat",
            phase="start",
            strength=0.5,
            hand="right",
        )
        await orchestrator.submit_interaction(session, interaction)
        interaction_events = await collect_failure(session)
        assert [event["type"] for event in interaction_events] == [
            "error",
            "reply.end",
        ]
        assert interaction_events[0]["code"] == "interaction_failed"
        assert interaction_events[1]["status"] == "failed"
        await orchestrator.close()

    asyncio.run(scenario())


def test_eventbus_empty_reply_is_not_compressed_to_turn_failed() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(safe_neutral_decision("unused"))
        )
        session.protected_context_authorized = True
        orchestrator.message_pipeline = EmptyMessagePipelineStub(
            "astrbot_pipeline_event_stopped"
        )
        orchestrator.allow_direct_provider_fallback = False

        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = [
            (await asyncio.wait_for(session.queue.get(), timeout=1)).payload
            for _ in range(2)
        ]
        assert [event["type"] for event in events] == ["error", "reply.end"]
        assert events[0]["code"] == "astrbot_pipeline_event_stopped"
        assert "白名单" in events[0]["message"]
        assert events[1]["status"] == "failed"
        await orchestrator.close()

    asyncio.run(scenario())


def test_late_old_turn_cannot_pollute_new_turn() -> None:
    async def scenario() -> None:
        late = LateDecisionStub(
            decision(Emotion.HAPPY, Gesture.WAVE, LookAt.USER, "late", "old")
        )
        sessions, session, orchestrator = await build_orchestrator(late)
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="old"),
        )
        await late.started.wait()
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t2"),
        )
        late.release.set()
        await asyncio.sleep(0.05)
        assert session.queue.size == 0
        await orchestrator.close()

    asyncio.run(scenario())


def test_interrupt_prevents_all_old_turn_events() -> None:
    async def scenario() -> None:
        late = LateDecisionStub(
            decision(Emotion.HAPPY, Gesture.WAVE, LookAt.USER, "late", "old")
        )
        sessions, session, orchestrator = await build_orchestrator(late)
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="old"),
        )
        await late.started.wait()
        assert await sessions.cancel_current(session, "t1") is True
        late.release.set()
        await asyncio.sleep(0.05)
        assert session.queue.size == 0
        await orchestrator.close()

    asyncio.run(scenario())


def test_late_tts_after_interrupt_cannot_emit_audio_error_or_end() -> None:
    async def scenario() -> None:
        late_tts = LateTTSStub()
        result = decision(
            Emotion.HAPPY,
            Gesture.WAVE,
            LookAt.USER,
            "reply",
            "hello",
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(result),
            tts=late_tts,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        await asyncio.wait_for(late_tts.started.wait(), timeout=1)
        assert await sessions.cancel_current(session, "t1") is True
        await asyncio.wait_for(late_tts.cancelled.wait(), timeout=1)
        late_tts.release.set()
        await asyncio.sleep(0.05)
        assert session.queue.size == 0
        await orchestrator.close()

    asyncio.run(scenario())


def test_interaction_runs_independently_without_cancelling_primary_turn() -> None:
    async def scenario() -> None:
        llm = ConcurrentInteractionDecisionStub()
        sessions, session, orchestrator = await build_orchestrator(llm)
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="primary"),
        )
        await asyncio.wait_for(llm.primary_started.wait(), timeout=1)

        interaction = InteractionEvent(
            session_id="s1",
            event_id="e1",
            name="head_pat",
            phase="start",
            strength=0.5,
            hand="right",
        )
        interaction_turn = await orchestrator.submit_interaction(session, interaction)
        assert interaction_turn is not None

        interaction_events: list[dict[str, Any]] = []
        while True:
            item = await asyncio.wait_for(session.queue.get(), timeout=1)
            if item.turn_id == interaction_turn.turn_id:
                interaction_events.append(item.payload)
                if item.event_type == "reply.end":
                    break
        assert llm.primary_cancelled.is_set() is False
        assert session.current_turn is not None
        assert session.current_turn.turn_id == "t1"
        assert [event["type"] for event in interaction_events] == [
            "avatar.intent",
            "reply.text.delta",
            "reply.audio.chunk",
            "reply.end",
        ]

        llm.primary_release.set()
        primary_events = await collect_until_end(session)
        assert primary_events[-1]["turn_id"] == "t1"
        assert primary_events[-1]["type"] == "reply.end"
        await orchestrator.close()

    asyncio.run(scenario())


def test_tts_uses_ordered_sentence_segments() -> None:
    class SegmentTTSStub(TTSStub):
        def __init__(self) -> None:
            super().__init__()
            self.segments: list[str] = []

        async def synthesize(self, text: str, *, emotion: str) -> AsyncIterator[bytes]:
            del emotion
            self.segments.append(text)
            yield b"\x00\x00" * 1_200

    async def scenario() -> None:
        tts = SegmentTTSStub()
        result = decision(
            Emotion.HAPPY,
            Gesture.TALK,
            LookAt.USER,
            "reply",
            "first sentence. second sentence! third sentence?",
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(result), tts=tts
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        events = await collect_until_end(session)
        assert tts.segments == [
            "first sentence.",
            "second sentence!",
            "third sentence?",
        ]
        assert [event["type"] for event in events][-1] == "reply.end"
        await orchestrator.close()

    asyncio.run(scenario())


def test_tts_pipeline_prefetch_is_bounded() -> None:
    class BurstTTSStub(TTSStub):
        def __init__(self) -> None:
            super().__init__()
            self.produced = 0

        async def synthesize(self, text: str, *, emotion: str) -> AsyncIterator[bytes]:
            del text, emotion
            for _ in range(20):
                self.produced += 1
                yield b"\x00\x00" * 1_200

    async def scenario() -> None:
        tts = BurstTTSStub()
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(Emotion.HAPPY, Gesture.TALK, LookAt.USER, "reply", "text")
            ),
            tts=tts,
        )
        stream = orchestrator._tts_pipeline("text", emotion="happy")
        first = await anext(stream)
        assert first
        await asyncio.sleep(0.02)
        assert tts.produced <= 4
        await stream.aclose()
        await orchestrator.close()

    asyncio.run(scenario())


def test_unity_mock_protocol_contains_text_audio_intent_and_end() -> None:
    async def scenario() -> None:
        result = decision(
            Emotion.SHY,
            Gesture.STEP_BACK,
            LookAt.AWAY,
            "boundary_soft_refusal",
            "请轻一点。",
        )
        sessions, session, orchestrator = await build_orchestrator(DecisionStub(result))
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t3", text="你好"),
        )
        events = await collect_until_end(session)
        types = [event["type"] for event in events]
        assert types[0] == "avatar.intent"
        assert "reply.text.delta" in types
        assert "reply.audio.chunk" in types
        assert types.index("reply.text.delta") < types.index("reply.audio.chunk")
        assert types[-1] == "reply.end"
        assert all(event["protocol_version"] == "1.0" for event in events)
        audio = next(event for event in events if event["type"] == "reply.audio.chunk")
        assert audio["format"] == "pcm16"
        assert audio["sample_rate"] == 24000
        assert audio["channels"] == 1
        await orchestrator.close()

    asyncio.run(scenario())
