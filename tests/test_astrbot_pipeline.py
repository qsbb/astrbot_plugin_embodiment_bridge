from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_quest_avatar_bridge.adapters import astrbot_pipeline


class QueueStub:
    def __init__(self) -> None:
        self.event: Any | None = None

    def put_nowait(self, event: Any) -> None:
        self.event = event


class ContextStub:
    def __init__(self) -> None:
        self.queue = QueueStub()
        self.platform = SimpleNamespace(meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"))

    def get_event_queue(self) -> QueueStub:
        return self.queue

    def get_platform_inst(self, platform_id: str) -> Any | None:
        return self.platform if platform_id == "qq" else None


class CaptureEventStub:
    def __init__(self, text: str = "", plan: dict[str, Any] | None = None) -> None:
        self.text = text
        self.plan = plan

    async def wait_completed(self) -> None:
        await asyncio.sleep(0)

    def captured_text(self) -> str:
        return self.text

    def get_extra(self, key: str) -> Any:
        return self.plan if key == "conversation_flow.delivery_plan" else None


def session(*, authorized: bool = True) -> Any:
    return SimpleNamespace(
        protected_context_authorized=authorized,
        user_id="user",
        bot_id="bot",
        group_id="",
    )


def test_authorized_text_uses_event_bus_and_returns_talk_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        event = CaptureEventStub("现在是通过正式消息管线得到的回复。")
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )
        decision = await adapter.generate(session=session(), user_text="几点了")
        assert context.queue.event is event
        assert decision.should_reply is True
        assert decision.reply_text == "现在是通过正式消息管线得到的回复。"
        assert decision.intent.gesture.value == "talk"
        assert adapter.status == "ok"

    asyncio.run(scenario())


def test_delivery_plan_recovers_text_when_voice_plugin_removed_plain_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        event = CaptureEventStub(
            "",
            {"version": "1.0", "original_text": "保留的最终正文"},
        )
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )
        decision = await adapter.generate(session=session(), user_text="你好")
        assert decision.reply_text == "保留的最终正文"

    asyncio.run(scenario())


def test_pipeline_rejects_unauthorized_session_before_queueing() -> None:
    async def scenario() -> None:
        context = ContextStub()
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )
        with pytest.raises(
            astrbot_pipeline.MessagePipelineUnavailable,
            match="protected_context_not_authorized",
        ):
            await adapter.generate(session=session(authorized=False), user_text="hello")
        assert context.queue.event is None

    asyncio.run(scenario())


def test_pipeline_reports_precise_platform_availability_and_reconfigures() -> None:
    context = ContextStub()
    adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(context, SimpleNamespace())

    assert adapter.available is False
    assert adapter.availability_reason == "trusted_platform_not_configured"

    adapter.configure_platform("missing")
    assert adapter.available is False
    assert adapter.availability_reason == "trusted_platform_unavailable"

    adapter.configure_platform("qq")
    assert adapter.available is True
    assert adapter.availability_reason == "ready"
    assert adapter.status_snapshot()["availability_reason"] == "ready"

    adapter.enabled = False
    assert adapter.availability_reason == "disabled"


def test_empty_pipeline_reply_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        event = CaptureEventStub()
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )
        with pytest.raises(astrbot_pipeline.MessagePipelineEmpty):
            await adapter.generate(session=session(), user_text="hello")
        assert adapter.status == "empty_reply"

    asyncio.run(scenario())
