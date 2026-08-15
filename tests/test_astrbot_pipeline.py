from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from astrbot_plugin_embodiment_bridge.adapters import astrbot_pipeline
from astrbot_plugin_embodiment_bridge.core.avatar_action_tool import (
    QUEST_ACTION_INTENT_EXTRA,
    QUEST_ACTION_PARSE_EXTRA,
    prepare_quest_action_request,
)
from astrbot_plugin_embodiment_bridge.core.avatar_skills import AvatarSkillRegistry


class QueueStub:
    def __init__(self) -> None:
        self.event: Any | None = None
        self.put_count = 0

    def put_nowait(self, event: Any) -> None:
        self.event = event
        self.put_count += 1


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


def test_expired_spatial_context_is_not_captured_for_eventbus() -> None:
    snapshot = SimpleNamespace(model_dump=lambda **_kwargs: {"schema_version": 1})
    session = SimpleNamespace(
        spatial_context=snapshot,
        spatial_context_updated_at=10.0,
    )
    with patch(
        "astrbot_plugin_embodiment_bridge.adapters.astrbot_pipeline.time.monotonic",
        return_value=41.0,
    ):
        assert astrbot_pipeline._session_spatial_context(session) is None

    with patch(
        "astrbot_plugin_embodiment_bridge.adapters.astrbot_pipeline.time.monotonic",
        return_value=39.0,
    ):
        assert astrbot_pipeline._session_spatial_context(session) == {
            "schema_version": 1
        }


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
        self.extras: dict[str, Any] = {}

    async def wait_completed(self) -> None:
        await asyncio.sleep(0)

    def captured_text(self) -> str:
        return self.text

    def get_extra(self, key: str) -> Any:
        if key == "conversation_flow.delivery_plan":
            return self.plan
        return self.extras.get(key)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value

    def is_stopped(self) -> bool:
        return self._stopped


class DiagnosticStub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


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


@pytest.mark.parametrize(
    "action",
    ("dance", "dance_next", "raise_hand", "turn_half", "sit", "lie"),
)
def test_eventbus_action_tool_intent_replaces_fixed_talk_decision(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        event = CaptureEventStub("那我换一支舞。")
        event.set_extra("embodiment_bridge", True)
        event.set_extra(
            QUEST_ACTION_INTENT_EXTRA,
            AvatarSkillRegistry.invoke(action, {"intensity": 0.7}),
        )
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )

        decision = await adapter.generate(session=session(), user_text="换个舞蹈")

        assert decision.reply_text == "那我换一支舞。"
        assert decision.intent.gesture.value == action
        assert decision.intent.reason_code == f"skill_{action}"
        assert context.queue.put_count == 1

    asyncio.run(scenario())


def test_pipeline_logs_selected_dance_without_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        diagnostic = DiagnosticStub()
        event = CaptureEventStub("动作回复")
        event.set_extra("embodiment_bridge", True)
        event.set_extra(
            QUEST_ACTION_INTENT_EXTRA,
            AvatarSkillRegistry.invoke("dance_next", {"intensity": 0.7}),
        )
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            diagnostic,
            platform_id="qq",
        )

        decision = await adapter.generate(session=session(), user_text="换个舞蹈")

        assert decision.intent.gesture.value == "dance_next"
        assert diagnostic.events == [
            (
                "avatar.action.pipeline_outcome",
                {
                    "component": "action",
                    "operation": "dance_next",
                    "status": "selected",
                    "reason_code": "skill_dance_next",
                    "gesture": "dance_next",
                    "action_source": "selected",
                    "motion_selection": "next_imported",
                    "duration_ms": adapter.last_duration_ms,
                },
            )
        ]
        assert "换个舞蹈" not in repr(diagnostic.events)
        assert "动作回复" not in repr(diagnostic.events)

    asyncio.run(scenario())


def test_tool_only_dance_is_not_lost_as_empty_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        diagnostic = DiagnosticStub()
        event = CaptureEventStub("", stopped=False, send_observed=False)
        event.set_extra("embodiment_bridge", True)
        event.set_extra(
            QUEST_ACTION_INTENT_EXTRA,
            AvatarSkillRegistry.invoke("dance", {}),
        )
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            diagnostic,
            platform_id="qq",
        )

        decision = await adapter.generate(session=session(), user_text="跳舞")

        assert decision.should_reply is False
        assert decision.reply_text == ""
        assert decision.intent.gesture.value == "dance"
        assert diagnostic.events[0][0] == "avatar.action.pipeline_outcome"
        assert diagnostic.events[0][1]["operation"] == "dance"
        assert diagnostic.events[0][1]["action_source"] == "selected"
        assert diagnostic.events[0][1]["motion_selection"] == "recommended_imported"

    asyncio.run(scenario())


def test_pipeline_logs_default_talk_fallback_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        diagnostic = DiagnosticStub()
        event = CaptureEventStub("普通回复")
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            diagnostic,
            platform_id="qq",
        )

        decision = await adapter.generate(session=session(), user_text="普通问题")

        assert decision.intent.gesture.value == "talk"
        assert diagnostic.events[0][1]["status"] == "fallback"
        assert diagnostic.events[0][1]["action_source"] == "default_talk"
        assert "普通问题" not in repr(diagnostic.events)
        assert "普通回复" not in repr(diagnostic.events)

    asyncio.run(scenario())


def test_explicit_action_keeps_one_eventbus_model_pass_without_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        model_calls = 0
        tool_handler_calls = 0

        class ProcessingEvent(CaptureEventStub):
            def __init__(self) -> None:
                super().__init__("")
                self.message_str = "请随便跳个舞"
                self.set_extra("embodiment_bridge", True)
                self.request = SimpleNamespace(func_tool=None, system_prompt="base")

            async def wait_completed(self) -> None:
                nonlocal model_calls, tool_handler_calls

                async def tool_handler(*_args: Any, **_kwargs: Any) -> str:
                    nonlocal tool_handler_calls
                    tool_handler_calls += 1
                    return "unused"

                await prepare_quest_action_request(
                    self.request,
                    self,
                    tool_handler,
                    None,
                )
                model_calls += 1
                self.text = "EventBus reply"

        event = ProcessingEvent()
        monkeypatch.setattr(astrbot_pipeline, "_build_capture_event", lambda **_: event)
        adapter = astrbot_pipeline.AstrBotMessagePipelineAdapter(
            context,
            SimpleNamespace(),
            platform_id="qq",
        )

        decision = await adapter.generate(
            session=session(),
            user_text="请随便跳个舞",
        )

        assert context.queue.put_count == 1
        assert model_calls == 1
        assert tool_handler_calls == 0
        assert event.request.func_tool is None
        assert decision.intent.gesture.value == "dance"
        assert decision.intent.reason_code == "skill_dance"

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
        with pytest.raises(
            astrbot_pipeline.MessagePipelineEmpty, match=expected_reason
        ):
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
        protected_context_authorized=True,
        fast_action_active=True,
        spatial_context={
            "schema_version": 1,
            "revision": 3,
            "floor_count": 1,
            "seat_count": 1,
            "bed_count": 0,
            "table_count": 1,
            "wall_count": 4,
            "door_count": 1,
            "window_count": 1,
            "scene_capture_available": True,
            "occlusion_available": False,
        },
        action_facts=[
            {
                "action": "wave",
                "status": "completed",
                "reason_code": "completed",
                "duration_ms": 1_250,
            }
        ],
    )

    assert isinstance(message, FakeAstrMessageEvent)
    assert message.native_factory is True
    assert message.get_extra("_api_key_allow_admin_role") is False
    staged_action = message.get_extra(QUEST_ACTION_PARSE_EXTRA)
    assert staged_action.status == "not_explicit"
    assert staged_action.action is None
    identity = message.get_extra("embodiment_bridge.identity_context")
    assert identity["platform_id"] == "trusted-platform"
    assert identity["user_id"] == "bound-user"
    assert identity["trusted"] is True
    assert (
        message.get_extra("embodiment_bridge.protected_context_authorized") is True
    )
    assert message.get_extra("embodiment_bridge.fast_action_active") is True
    assert message.get_extra("embodiment_bridge.spatial_context")["revision"] == 3
    assert message.get_extra("embodiment_bridge.action_facts") == [
        {
            "action": "wave",
            "status": "completed",
            "reason_code": "completed",
            "duration_ms": 1_250,
        }
    ]
