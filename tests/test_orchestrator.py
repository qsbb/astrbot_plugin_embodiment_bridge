from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.adapters.stt import DisabledSTTAdapter
from astrbot_plugin_embodiment_bridge.adapters.fast_action import FastActionUnavailable
from astrbot_plugin_embodiment_bridge.adapters.astrbot_pipeline import (
    MessagePipelineEmpty,
    MessagePipelineUnavailable,
)
from astrbot_plugin_embodiment_bridge.core.interaction_policy import InteractionPolicy
from astrbot_plugin_embodiment_bridge.core.plugin_identity import (
    BRIDGE_FAST_ACTION_EVENT_SELECTED,
)
from astrbot_plugin_embodiment_bridge.core.models import (
    AudioChunkRequest,
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
from astrbot_plugin_embodiment_bridge.core.session_manager import QueueItem, SessionManager
from astrbot_plugin_embodiment_bridge.core.turn_orchestrator import TurnOrchestrator


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


class DiagnosticStub:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.records.append((event, fields))


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


class STTTextStub:
    available = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        assert pcm16
        assert sample_rate == 16_000
        self.calls += 1
        return self.text

    async def close(self) -> None:
        return None


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


class FastActionStub:
    enabled = True
    available = True

    def __init__(
        self,
        intent: ProposedIntent | None,
        *,
        release: asyncio.Event,
    ) -> None:
        self.intent = intent
        self.release = release
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> ProposedIntent | None:
        self.calls.append(dict(kwargs))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return self.intent

    async def close(self) -> None:
        return None


class FailingFastActionStub:
    enabled = True
    available = True

    async def decide(self, **kwargs: Any) -> ProposedIntent:
        del kwargs
        raise FastActionUnavailable("fast_action_timeout")

    async def close(self) -> None:
        return None


class UnavailableFastActionStub:
    enabled = True
    available = False
    availability_reason = "selected_missing"
    provider_id = "configured-provider"

    async def decide(self, **kwargs: Any) -> ProposedIntent:
        del kwargs
        raise AssertionError("unavailable provider must not be called")

    async def close(self) -> None:
        return None


class DisabledFastActionStub(UnavailableFastActionStub):
    enabled = False
    availability_reason = "disabled"


class UnconfiguredFastActionStub(UnavailableFastActionStub):
    availability_reason = "provider_not_configured"
    provider_id = ""


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
    stt: Any | None = None,
    tts: Any | None = None,
    diagnostic: Any | None = None,
    fast_action: Any | None = None,
    supported_actions: tuple[str, ...] | None = None,
):
    sessions = SessionManager(event_queue_size=queue_size, interaction_debounce_ms=0)
    session = await sessions.start_session(
        SessionStartRequest(
            session_id="s1",
            client_id="quest",
            user_id="user",
            bot_id="bot",
            supported_actions=supported_actions,
        ),
        "owner",
    )
    orchestrator = TurnOrchestrator(
        sessions=sessions,
        llm=llm,
        stt=stt or DisabledSTTAdapter(),
        tts=tts or TTSStub(),
        relationship=RelationshipStub(),
        policy=InteractionPolicy(gesture_cooldown_seconds=0),
        logger=LoggerStub(),
        diagnostic_log=diagnostic,
        fast_action=fast_action,
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
        assert (
            orchestrator._public_pipeline_reason(
                session,
                MessagePipelineEmpty("astrbot_pipeline_reply_required_missing"),
            )
            == "astrbot_pipeline_reply_required_missing"
        )
        assert "明确要求的文字回复" in orchestrator._pipeline_error_message(
            "astrbot_pipeline_reply_required_missing"
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


def test_unity_mock_protocol_sanitizes_main_action_and_keeps_reply_order() -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticStub()
        result = decision(
            Emotion.SHY,
            Gesture.STEP_BACK,
            LookAt.AWAY,
            "boundary_soft_refusal",
            "请轻一点。",
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(result), diagnostic=diagnostic
        )
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
        intent_record = next(
            fields
            for event, fields in diagnostic.records
            if event == "avatar.intent.emitted"
        )
        assert intent_record == {
            "component": "action",
            "operation": "talk",
            "status": "planned",
            "reason_code": "dialogue_only",
            "emotion": "neutral",
            "gesture": "talk",
            "look_at": "user",
            "intensity": 0.38,
            "duration_ms": 1200,
        }
        await orchestrator.close()

    asyncio.run(scenario())


def test_normal_dialogue_does_not_start_fast_action_or_autonomous_intent() -> None:
    """A configured fast-action adapter is idle for an ordinary text turn."""

    async def scenario() -> None:
        diagnostic = DiagnosticStub()
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.DANCE,
                look_at=LookAt.USER,
                intensity=0.7,
                duration_ms=7_000,
                reason_code="must_not_be_used",
            ),
            release=release,
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "dialogue_only",
                    "普通回复",
                )
            ),
            fast_action=fast,
            diagnostic=diagnostic,
            tts=TTSStub(available=False),
        )

        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-dialogue-only", text="你好"),
        )
        events = await collect_until_end(session)

        assert fast.calls == []
        assert fast.started.is_set() is False
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "talk"
        assert intents[0]["reason_code"] == "dialogue_only"
        skipped = [
            fields
            for event, fields in diagnostic.records
            if event == "fast_action.skipped"
        ]
        assert skipped
        assert skipped[-1]["reason_code"] == "autonomous_action_disabled"
        assert not any(
            event in {"fast_action.started", "fast_action.completed"}
            for event, _fields in diagnostic.records
        )
        await orchestrator.close()

    asyncio.run(scenario())


def test_explicit_action_still_runs_locally_without_calling_provider() -> None:
    """An imperative action is parsed locally and never delegated to a model."""

    async def scenario() -> None:
        diagnostic = DiagnosticStub()
        release = asyncio.Event()
        fast = FastActionStub(None, release=release)
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "dialogue_only",
                    "我会回复你。",
                )
            ),
            fast_action=fast,
            diagnostic=diagnostic,
            tts=TTSStub(available=False),
        )

        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-explicit-wave", text="请挥手"),
        )
        events = await collect_until_end(session)

        assert fast.calls == []
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert intent["gesture"] == "wave"
        assert intent["reason_code"] == "explicit_request"
        assert intent["source"] == "explicit_request"
        assert any(
            event == "fast_action.explicit_selected"
            and fields["action_source"] == "explicit_request"
            for event, fields in diagnostic.records
        )
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="旧测试验证已移除的自主 fast-action 并行路径")
def test_fast_action_runs_beside_reply_and_emits_at_most_one_intent() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.DANCE,
                look_at=LookAt.USER,
                intensity=0.7,
                duration_ms=7000,
                reason_code="skill_dance",
            ),
            release=release,
        )
        slow_reply = LateDecisionStub(
            decision(
                Emotion.NEUTRAL,
                Gesture.TALK,
                LookAt.USER,
                "normal_reply",
                "reply",
            )
        )
        sessions, session, orchestrator = await build_orchestrator(
            slow_reply,
            fast_action=fast,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-fast", text="今天真适合跳舞"),
        )
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        await asyncio.wait_for(slow_reply.started.wait(), timeout=1)

        release.set()
        first = (await asyncio.wait_for(session.queue.get(), timeout=1)).payload
        assert first["type"] == "avatar.intent"
        assert first["gesture"] == "dance"

        slow_reply.release.set()
        events = [first]
        while events[-1]["type"] != "reply.end":
            events.append(
                (await asyncio.wait_for(session.queue.get(), timeout=1)).payload
            )
        assert sum(event["type"] == "avatar.intent" for event in events) == 1
        assert any(event["type"] == "reply.text.delta" for event in events)
        assert events[-1]["status"] == "completed"
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="主 LLM 不再暴露动作工具，旧 EventBus 竞态契约已移除")
def test_eventbus_action_wins_when_fast_selector_has_not_selected() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.WAVE,
                look_at=LookAt.USER,
                intensity=0.6,
                duration_ms=1_800,
                reason_code="skill_wave",
            ),
            release=release,
        )
        reply = LateDecisionStub(
            decision(
                Emotion.NEUTRAL,
                Gesture.CROUCH,
                LookAt.USER,
                "skill_crouch",
                "我现在蹲下。",
            )
        )
        diagnostic = DiagnosticStub()
        sessions, session, orchestrator = await build_orchestrator(
            reply,
            fast_action=fast,
            supported_actions=("talk", "wave", "crouch"),
            tts=TTSStub(available=False),
            diagnostic=diagnostic,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-arbitration", text="我有点紧张"),
        )
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        await asyncio.wait_for(reply.started.wait(), timeout=1)
        assert session.current_turn is not None
        session.current_turn.fast_action_feedback[BRIDGE_FAST_ACTION_EVENT_SELECTED] = (
            "crouch"
        )
        reply.release.set()
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "crouch"
        assert intents[0]["source"] == "eventbus_tool"
        await asyncio.wait_for(fast.cancelled.wait(), timeout=1)
        assert any(
            event == "fast_action.cancelled"
            and fields["reason_code"] == "eventbus_action_selected"
            for event, fields in diagnostic.records
        )
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="主 LLM 不再暴露动作工具，旧 EventBus 竞态契约已移除")
def test_eventbus_action_remains_single_when_fast_selector_finishes_late() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.WAVE,
                look_at=LookAt.USER,
                intensity=0.6,
                duration_ms=1_800,
                reason_code="skill_wave",
            ),
            release=release,
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.CROUCH,
                    LookAt.USER,
                    "skill_crouch",
                    "我现在蹲下。",
                )
            ),
            fast_action=fast,
            supported_actions=("talk", "wave", "crouch"),
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-arbitration", text="我有点紧张"),
        )
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        assert session.current_turn is not None
        session.current_turn.fast_action_feedback[BRIDGE_FAST_ACTION_EVENT_SELECTED] = (
            "crouch"
        )
        release.set()
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "crouch"
        assert intents[0]["source"] == "eventbus_tool"
        await orchestrator.close()

    asyncio.run(scenario())


def test_fast_action_failure_uses_strict_explicit_request_not_main_reply_action() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.HAPPY,
                    Gesture.WAVE,
                    LookAt.USER,
                    "main_action_fallback",
                    "hello",
                )
            ),
            fast_action=FailingFastActionStub(),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-fallback", text="wave"),
        )
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "wave"
        assert intents[0]["reason_code"] == "explicit_request"
        assert any(event["type"] == "reply.text.delta" for event in events)
        await orchestrator.close()

    asyncio.run(scenario())


def test_fast_action_failure_does_not_guess_negated_or_discussed_action() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "dialogue_only",
                    "hello",
                )
            ),
            fast_action=FailingFastActionStub(),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-negated-fallback",
                text="不要挥手，只是讨论一下挥手动作",
            ),
        )
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "talk"
        assert intents[0]["reason_code"] == "dialogue_only"
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="普通轮次不再使用自主社会动作 fallback")
def test_fast_action_timeout_uses_conservative_autonomous_social_fallback() -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticStub()
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.IDLE,
                    LookAt.NONE,
                    "main_reply_without_action",
                    "你好呀。",
                )
            ),
            fast_action=FailingFastActionStub(),
            diagnostic=diagnostic,
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-autonomous-fallback",
                text="心夏，你好呀",
            ),
        )
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "wave"
        assert intents[0]["reason_code"] == "autonomous_greeting"
        assert intents[0]["source"] == "fallback"
        fallback = next(
            fields
            for event, fields in diagnostic.records
            if event == "fast_action.local_fallback_selected"
        )
        assert fallback["operation"] == "wave"
        assert fallback["provider_status"] == "unavailable"
        assert any(event["type"] == "reply.text.delta" for event in events)
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="普通轮次不再调用 fast-action Provider 或本地自主 fallback")
def test_unavailable_fast_provider_uses_local_social_fallback_but_disabled_does_not() -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticStub()
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "你好。",
                )
            ),
            fast_action=UnavailableFastActionStub(),
            diagnostic=diagnostic,
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-provider-unavailable",
                text="你好",
            ),
        )
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert intent["gesture"] == "wave"
        assert intent["reason_code"] == "autonomous_greeting"
        assert intent["source"] == "fallback"
        assert any(
            event == "fast_action.provider_unavailable"
            and fields["reason_code"] == "fast_action_selected_missing"
            for event, fields in diagnostic.records
        )
        await orchestrator.close()

        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "你好。",
                )
            ),
            fast_action=DisabledFastActionStub(),
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-provider-disabled",
                text="你好",
            ),
        )
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert intent["gesture"] == "talk"
        assert intent["reason_code"] == "main_reply"
        await orchestrator.close()

        unconfigured_diagnostic = DiagnosticStub()
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "你好。",
                )
            ),
            fast_action=UnconfiguredFastActionStub(),
            tts=TTSStub(available=False),
            diagnostic=unconfigured_diagnostic,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-provider-not-configured",
                text="你好",
            ),
        )
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert intent["gesture"] == "talk"
        assert intent["reason_code"] == "main_reply"
        assert not any(
            event in {
                "fast_action.started",
                "fast_action.local_fallback_selected",
            }
            for event, _fields in unconfigured_diagnostic.records
        )
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="由新的 dialogue-only 测试覆盖主模型动作字段隔离")
def test_fast_action_no_action_ignores_main_reply_action_fields() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(None, release=release)
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.DANCE,
                    LookAt.AWAY,
                    "main_reply",
                    "done",
                )
            ),
            fast_action=fast,
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(
                session_id="s1",
                turn_id="t-no-action",
                text="现在几点",
            ),
        )
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        release.set()
        events = await collect_until_end(session)
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "talk"
        assert intents[0]["emotion"] == "neutral"
        assert intents[0]["look_at"] == "user"
        assert intents[0]["reason_code"] == "fast_action_no_action"
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="普通轮次不再启动并行 fast-action 任务")
def test_main_reply_delivers_text_while_fast_action_is_pending() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.DANCE,
                look_at=LookAt.USER,
                intensity=0.7,
                duration_ms=7000,
                reason_code="skill_dance",
            ),
            release=release,
        )
        sessions, session, orchestrator = await build_orchestrator(
            LateDecisionStub(
                decision(
                    Emotion.CONCERNED,
                    Gesture.WAVE,
                    LookAt.AWAY,
                    "main_action_must_be_ignored",
                    "done",
                )
            ),
            fast_action=fast,
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-main-first", text="今天真适合跳舞"),
        )
        reply = orchestrator.llm
        assert isinstance(reply, LateDecisionStub)
        await asyncio.wait_for(reply.started.wait(), timeout=1)
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        reply.release.set()
        await asyncio.sleep(0)
        assert session.queue.size >= 1

        release.set()
        events = await collect_until_end(session)
        assert sum(event["type"] == "avatar.intent" for event in events) == 1
        assert next(
            event for event in events if event["type"] == "avatar.intent"
        )["gesture"] == "dance"
        assert events[-1]["type"] == "reply.end"
        await orchestrator.close()

    asyncio.run(scenario())


@pytest.mark.skip(reason="普通轮次不再启动 fast-action 任务；保留队列测试待重写")
def test_fast_action_stays_before_reply_end_under_critical_queue_backpressure() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.WAVE,
                look_at=LookAt.USER,
                intensity=0.6,
                duration_ms=1800,
                reason_code="skill_wave",
            ),
            release=release,
        )
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "hello",
                )
            ),
            queue_size=4,
            fast_action=fast,
            tts=TTSStub(available=False),
        )
        for index in range(4):
            await session.queue.put(
                QueueItem(
                    {"type": "error", "code": f"prefill-{index}"},
                    "prefill",
                    0,
                )
            )

        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-backpressure", text="今天真适合挥手"),
        )
        await asyncio.wait_for(fast.started.wait(), timeout=1)
        release.set()
        await asyncio.sleep(0)
        assert session.current_turn is not None
        assert session.current_turn.fast_action_task is not None
        assert session.current_turn.fast_action_task.done() is False

        events: list[dict[str, Any]] = []
        while not events or events[-1].get("type") != "reply.end":
            events.append(
                (await asyncio.wait_for(session.queue.get(), timeout=1)).payload
            )
        turn_events = [
            event
            for event in events
            if event.get("turn_id") == "t-backpressure"
        ]
        event_types = [event["type"] for event in turn_events]
        assert event_types.count("avatar.intent") == 1
        assert event_types.index("avatar.intent") < event_types.index("reply.end")
        assert event_types[-1] == "reply.end"
        await orchestrator.close()

    asyncio.run(scenario())


def test_finish_audio_preselects_explicit_action_and_orders_one_intent_before_reply() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        release.set()
        fast = FastActionStub(
            ProposedIntent(
                emotion=Emotion.HAPPY,
                gesture=Gesture.WAVE,
                look_at=LookAt.USER,
                intensity=0.6,
                duration_ms=1800,
                reason_code="skill_wave",
            ),
            release=release,
        )
        stt = STTTextStub("请自然地挥挥手，并同时简短回复我。")
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.CONCERNED,
                    Gesture.DANCE,
                    LookAt.AWAY,
                    "main_action_must_be_ignored",
                    "你好。",
                )
            ),
            stt=stt,
            fast_action=fast,
        )
        turn = await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-audio-fast"),
        )
        await sessions.add_audio_chunk(
            session,
            AudioChunkRequest(
                session_id="s1",
                turn_id=turn.turn_id,
                sequence=0,
                data="AAAAAA==",
            ),
        )
        await orchestrator.finish_audio(session, turn.turn_id)

        events = await collect_until_end(session)
        event_types = [event["type"] for event in events]
        assert stt.calls == 1
        # A bounded explicit imperative is resolved locally so a configured
        # fast-action provider cannot add a needless model round trip.
        assert len(fast.calls) == 0
        assert event_types == [
            "asr.final",
            "avatar.intent",
            "reply.text.delta",
            "reply.audio.chunk",
            "reply.end",
        ]
        intents = [event for event in events if event["type"] == "avatar.intent"]
        assert len(intents) == 1
        assert intents[0]["gesture"] == "wave"
        intent_index = event_types.index("avatar.intent")
        assert all(
            intent_index < event_types.index(event_type)
            for event_type in ("reply.text.delta", "reply.audio.chunk", "reply.end")
        )
        await orchestrator.close()

    asyncio.run(scenario())


def test_voice_explicit_action_survives_fast_provider_timeout() -> None:
    async def scenario() -> None:
        stt = STTTextStub("请转个身")
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "好。",
                )
            ),
            stt=stt,
            fast_action=FailingFastActionStub(),
            tts=TTSStub(available=False),
        )
        turn = await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-audio-timeout"),
        )
        await sessions.add_audio_chunk(
            session,
            AudioChunkRequest(
                session_id="s1",
                turn_id=turn.turn_id,
                sequence=0,
                data="AAAAAA==",
            ),
        )
        await orchestrator.finish_audio(session, turn.turn_id)
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert stt.calls == 1
        assert intent["gesture"] == "turn_half"
        assert intent["reason_code"] == "explicit_request"
        assert sum(event["type"] == "avatar.intent" for event in events) == 1
        await orchestrator.close()

    asyncio.run(scenario())


def test_explicit_crouch_bypasses_fast_provider_and_emits_bounded_method() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        fast = FastActionStub(None, release=release)
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "我现在开始。",
                )
            ),
            fast_action=fast,
            supported_actions=("talk", "crouch"),
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-crouch", text="请蹲一下"),
        )
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert fast.calls == []
        assert intent["gesture"] == "crouch"
        assert intent["method"] == "crouch"
        assert intent["parameters"] == {
            "angle_degrees": None,
            "depth": 0.55,
            "hold_ms": 900,
            "style": "natural",
        }
        assert intent["transition"] == {
            "enter_ms": 550,
            "exit_ms": 650,
            "easing": "ease_in_out",
        }
        assert intent["source"] == "explicit_request"
        await orchestrator.close()

    asyncio.run(scenario())


def test_same_turn_action_completion_claim_is_hard_corrected() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "我已经蹲好了。",
                )
            ),
            supported_actions=("talk", "crouch"),
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-crouch-claim", text="请蹲一下"),
        )
        events = await collect_until_end(session)
        text = "".join(
            event["text"] for event in events if event["type"] == "reply.text.delta"
        )
        intent = next(event for event in events if event["type"] == "avatar.intent")

        assert intent["gesture"] == "crouch"
        assert intent.get("action_id")
        assert text == "我现在开始。"
        await orchestrator.close()

    asyncio.run(scenario())


def test_legacy_client_cannot_receive_new_crouch_method() -> None:
    async def scenario() -> None:
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.CROUCH,
                    LookAt.USER,
                    "main_reply",
                    "这个客户端还不能下蹲。",
                )
            ),
            fast_action=None,
            tts=TTSStub(available=False),
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-legacy", text="请蹲下"),
        )
        events = await collect_until_end(session)
        intent = next(event for event in events if event["type"] == "avatar.intent")
        assert intent["gesture"] == "talk"
        assert intent["reason_code"] == "client_action_unsupported"
        assert "action_id" not in intent
        text = "".join(
            event["text"] for event in events if event["type"] == "reply.text.delta"
        )
        assert text == "这个动作当前客户端还不支持。"
        await orchestrator.close()

    asyncio.run(scenario())


def test_finish_audio_with_capture_elapsed_ms_does_not_nameerror() -> None:
    """Regression for the 2026-08-28 voice outage: real Quest clients send
    capture_elapsed_ms on every audio chunk, which makes audio_chunk_ages_ms
    non-empty and forces finish_audio's diagnostic to evaluate _percentile().
    The helper was missing, raising NameError inside audio/end → HTTP 500."""

    async def scenario() -> None:
        stt = STTTextStub("你好")
        sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(
                decision(
                    Emotion.NEUTRAL,
                    Gesture.TALK,
                    LookAt.USER,
                    "main_reply",
                    "你好。",
                )
            ),
            stt=stt,
            tts=TTSStub(available=False),
        )
        turn = await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-audio-ages"),
        )
        for seq in range(2):
            await sessions.add_audio_chunk(
                session,
                AudioChunkRequest(
                    session_id="s1",
                    turn_id=turn.turn_id,
                    sequence=seq,
                    data="AAAAAA==",
                    capture_elapsed_ms=seq * 64,
                ),
            )
        finished = await orchestrator.finish_audio(session, turn.turn_id)
        assert finished.turn_id == turn.turn_id
        events = await collect_until_end(session)
        assert events[-1]["type"] == "reply.end"
        assert stt.calls == 1
        await orchestrator.close()

    asyncio.run(scenario())
