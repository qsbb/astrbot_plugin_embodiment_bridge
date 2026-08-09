from __future__ import annotations

import asyncio
import sys
import types
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
        self.platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="qq", name="aiocqhttp"),
            create_event=lambda message: message,
        )

    def get_event_queue(self) -> QueueStub:
        return self.queue

    def get_platform_inst(self, platform_id: str) -> Any | None:
        return self.platform if platform_id == "qq" else None


class CaptureEventStub:
    def __init__(
        self,
        text: str = "",
        plan: dict[str, Any] | None = None,
        *,
        woken: bool = True,
        stopped: bool = False,
        send_observed: bool = False,
    ) -> None:
        self.text = text
        self.plan = plan
        self.is_wake = woken
        self.is_at_or_wake_command = woken
        self._stopped = stopped
        self._has_send_oper = send_observed

    async def wait_completed(self) -> None:
        await asyncio.sleep(0)

    def captured_text(self) -> str:
        return self.text

    def get_extra(self, key: str) -> Any:
        return self.plan if key == "conversation_flow.delivery_plan" else None

    def is_stopped(self) -> bool:
        return self._stopped


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


@pytest.mark.parametrize(
    ("event", "expected_reason"),
    (
        (
            CaptureEventStub(woken=False, stopped=True),
            "astrbot_pipeline_not_woken",
        ),
        (
            CaptureEventStub(woken=True, stopped=True),
            "astrbot_pipeline_event_stopped",
        ),
        (
            CaptureEventStub(send_observed=True),
            "astrbot_pipeline_reply_capture_empty",
        ),
        (CaptureEventStub(), "astrbot_pipeline_no_response"),
    ),
)
def test_empty_pipeline_reply_preserves_precise_event_outcome(
    monkeypatch: pytest.MonkeyPatch,
    event: CaptureEventStub,
    expected_reason: str,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )
        with pytest.raises(astrbot_pipeline.MessagePipelineEmpty, match=expected_reason):
            await adapter.generate(session=session(), user_text="hello")
        assert adapter.status == "empty_reply"
        assert adapter.last_error == expected_reason
        assert adapter.status_snapshot()["last_event_woken"] is event.is_wake
        assert adapter.status_snapshot()["last_event_stopped"] is event._stopped
        assert adapter.status_snapshot()["last_send_observed"] is event._has_send_oper

    asyncio.run(scenario())


def test_build_capture_event_uses_public_platform_factory_and_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePlain:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAstrBotMessage:
        def __init__(self) -> None:
            self.group = None

    class FakeEvent:
        def __init__(self, message: Any) -> None:
            self.message_obj = message
            self.session_id = message.session_id
            self._extras: dict[str, Any] = {}
            self._has_send_oper = False

        def set_extra(self, key: str, value: Any) -> None:
            self._extras[key] = value

        def get_extra(self, key: str, default: Any = None) -> Any:
            return self._extras.get(key, default)

        def cleanup_temporary_local_files(self) -> None:
            return None

    class FakeAstrMessageEvent(FakeEvent):
        pass

    class FakePlatform:
        def meta(self) -> Any:
            return types.SimpleNamespace(id="trusted-platform", name="aiocqhttp")

        def create_event(self, message: Any) -> FakeEvent:
            event = FakeAstrMessageEvent(message)
            event.native_factory = True
            return event

    class FakeMessageMember:
        def __init__(self, user_id: str, nickname: str) -> None:
            self.user_id = user_id
            self.nickname = nickname

    class FakeGroup:
        def __init__(self, group_id: str) -> None:
            self.group_id = group_id

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.Plain = FakePlain
    platform_module = types.ModuleType("astrbot.api.platform")
    platform_module.AstrBotMessage = FakeAstrBotMessage
    platform_module.Group = FakeGroup
    platform_module.MessageMember = FakeMessageMember
    platform_module.MessageType = types.SimpleNamespace(
        GROUP_MESSAGE="group", FRIEND_MESSAGE="friend"
    )
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = FakeAstrMessageEvent
    monkeypatch.setitem(
        sys.modules, "astrbot.api.message_components", message_components
    )
    monkeypatch.setitem(sys.modules, "astrbot.api.platform", platform_module)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event_module)

    message = astrbot_pipeline._build_capture_event(
        platform=FakePlatform(),
        platform_meta=FakePlatform().meta(),
        user_text="hello",
        user_id="bound-user",
        bot_id="bound-bot",
        group_id="bound-group",
    )

    assert isinstance(message, FakeAstrMessageEvent)
    assert message.native_factory is True
    assert message.get_extra("_api_key_allow_admin_role") is False
    identity = message.get_extra("quest_avatar_bridge.identity_context")
    assert identity["platform_id"] == "trusted-platform"
    assert identity["user_id"] == "bound-user"
    assert identity["trusted"] is True
