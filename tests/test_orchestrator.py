from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from astrbot_plugin_quest_avatar_bridge.adapters.stt import DisabledSTTAdapter
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


class DecisionStub:
    def __init__(self, decision: ModelDecision) -> None:
        self.decision = decision

    async def generate(self, **kwargs: Any) -> ModelDecision:
        return self.decision

    async def close(self) -> None:
        pass


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


async def build_orchestrator(llm: Any, *, queue_size: int = 64):
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
        tts=TTSStub(),
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
        assert types[-1] == "reply.end"
        assert all(event["protocol_version"] == "1.0" for event in events)
        audio = next(event for event in events if event["type"] == "reply.audio.chunk")
        assert audio["format"] == "pcm16"
        assert audio["sample_rate"] == 24000
        assert audio["channels"] == 1
        await orchestrator.close()

    asyncio.run(scenario())
