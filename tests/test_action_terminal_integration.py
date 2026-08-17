from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from astrbot_plugin_embodiment_bridge.core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
)
from .http_harness import (
    AUTH_HEADERS,
    ASTRBOT_API_KEY_ID,
    LiveHttpServer,
    build_plugin,
    read_sse_frame,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "protocol_v1"


def load_json(name: str) -> dict[str, Any]:
    import json

    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class EventBusFake:
    available = True

    def __init__(self, *, decision: ModelDecision | None = None, hang: bool = False) -> None:
        self.decision = decision
        self.hang = hang
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, **kwargs: Any) -> ModelDecision:
        self.started.set()
        if self.hang:
            await self.release.wait()
        assert kwargs["session"].protected_context_authorized is True
        return self.decision or ModelDecision(
            should_reply=False,
            reply_text="",
            intent=ProposedIntent(
                emotion=Emotion.NEUTRAL,
                gesture=Gesture.DANCE,
                look_at=LookAt.USER,
                intensity=0.55,
                duration_ms=2_000,
                reason_code="skill_dance",
            ),
        )

    async def close(self) -> None:
        self.release.set()


async def _open(bundle: Any, server: Any, client: ClientSession, session_id: str) -> Any:
    request = load_json("session_start.request.json")
    request["session_id"] = session_id
    created = await client.post(
        server.url("/session/start"), headers=AUTH_HEADERS, json=request
    )
    assert created.status == 201
    session = await bundle.plugin.sessions.get_owned(session_id, f"api_key:{ASTRBOT_API_KEY_ID}")
    session.protected_context_authorized = True
    events = await client.get(
        server.url(f"/events/{session_id}"),
        headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
    )
    assert (await read_sse_frame(events)).comment == "connected"
    return events


def test_eventbus_action_only_http_has_one_intent_and_terminal(monkeypatch: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        pipeline = EventBusFake()
        bundle.plugin.message_pipeline = pipeline
        bundle.plugin.orchestrator.message_pipeline = pipeline
        bundle.plugin.orchestrator.allow_direct_provider_fallback = False
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(timeout=ClientTimeout(total=None, connect=2)) as client:
                events = await _open(bundle, server, client, "eventbus-action")
                started = await client.post(
                    server.url("/turn/start"), headers=AUTH_HEADERS,
                    json={"type": "turn.start", "protocol_version": "1.0", "session_id": "eventbus-action", "turn_id": "t1", "text": "跳舞", "cancel_previous": True},
                )
                assert started.status == 202
                frames = []
                while not frames or frames[-1].event != "reply.end":
                    frames.append(await read_sse_frame(events, timeout=2))
                assert [frame.event for frame in frames].count("avatar.intent") == 1
                assert [frame.event for frame in frames].count("reply.end") == 1
                assert frames[-1].data["status"] == "completed"
                events.close()

    asyncio.run(scenario())


def test_eventbus_deadline_emits_error_then_failed_end_once(monkeypatch: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        pipeline = EventBusFake(hang=True)
        bundle.plugin.message_pipeline = pipeline
        bundle.plugin.orchestrator.message_pipeline = pipeline
        bundle.plugin.orchestrator.allow_direct_provider_fallback = False
        bundle.plugin.orchestrator.eventbus_terminal_deadline_seconds = 0.05
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(timeout=ClientTimeout(total=None, connect=2)) as client:
                events = await _open(bundle, server, client, "eventbus-timeout")
                started = await client.post(
                    server.url("/turn/start"), headers=AUTH_HEADERS,
                    json={"type": "turn.start", "protocol_version": "1.0", "session_id": "eventbus-timeout", "turn_id": "t1", "text": "slow pipeline", "cancel_previous": True},
                )
                assert started.status == 202
                pre_terminal = []
                while True:
                    frame = await read_sse_frame(events, timeout=1)
                    pre_terminal.append(frame)
                    if frame.event == "error":
                        error = frame
                        break
                terminal = await read_sse_frame(events, timeout=1)
                assert error.event == "error"
                assert error.data["code"] == "astrbot_pipeline_timeout"
                assert terminal.event == "reply.end"
                assert terminal.data["status"] == "failed"
                assert terminal.data["text_sent"] is False
                assert terminal.data["audio_sent"] is False
                diagnostic = bundle.plugin.diagnostic_log.diagnostic_events(limit=200)
                stage_names = {
                    item["code"] for item in diagnostic["events"]
                }
                assert {"event_enqueued", "event_cleanup_entered", "event_completed", "reply_end_emitted"}.issubset(stage_names)
                serialized = repr(diagnostic)
                assert "slow pipeline" not in serialized
                assert "eventbus-timeout" not in serialized
                try:
                    await read_sse_frame(events, timeout=0.1)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("duplicate terminal frame")
                pipeline.release.set()
                events.close()

    asyncio.run(scenario())


def test_interrupt_late_eventbus_completion_does_not_replay_terminal(monkeypatch: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        pipeline = EventBusFake(hang=True)
        bundle.plugin.message_pipeline = pipeline
        bundle.plugin.orchestrator.message_pipeline = pipeline
        bundle.plugin.orchestrator.allow_direct_provider_fallback = False
        bundle.plugin.orchestrator.eventbus_terminal_deadline_seconds = 0.2
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(timeout=ClientTimeout(total=None, connect=2)) as client:
                events = await _open(bundle, server, client, "eventbus-interrupt")
                started = await client.post(
                    server.url("/turn/start"), headers=AUTH_HEADERS,
                    json={"type": "turn.start", "protocol_version": "1.0", "session_id": "eventbus-interrupt", "turn_id": "t1", "text": "slow pipeline", "cancel_previous": True},
                )
                assert started.status == 202
                await asyncio.wait_for(pipeline.started.wait(), timeout=1)
                interrupted = await client.post(
                    server.url("/interrupt"), headers=AUTH_HEADERS,
                    json={"type": "interrupt", "protocol_version": "1.0", "session_id": "eventbus-interrupt", "turn_id": "t1", "reason": "test"},
                )
                assert interrupted.status == 200
                pipeline.release.set()
                await asyncio.sleep(0.1)
                try:
                    await read_sse_frame(events, timeout=0.1)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("cancelled turn replayed an event")
                events.close()

    asyncio.run(scenario())
