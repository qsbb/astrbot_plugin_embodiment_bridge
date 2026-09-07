from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from astrbot_plugin_embodiment_bridge.adapters import astrbot_pipeline
from astrbot_plugin_embodiment_bridge.core.plugin_identity import (
    BRIDGE_CAPTURE_REQUIRED,
    BRIDGE_DELIVERY_OWNER,
)


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


def test_trace_probe_keeps_action_names_but_drops_trace_fields() -> None:
    class Trace:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def record(self, action: str, **fields: Any) -> None:
            self.calls.append((action, fields))

    trace = Trace()
    event = SimpleNamespace(trace=trace, _quest_bridge_timing={"started": 0.0})
    stages: list[tuple[str, dict[str, Any]]] = []
    astrbot_pipeline._install_trace_probe(
        event,
        lambda name, **fields: stages.append((name, fields)),
    )

    trace.record("astr_agent_complete", resp="private reply", stats={"tokens": 1})

    assert trace.calls[0][0] == "astr_agent_complete"
    assert event._quest_bridge_timing["trace_astr_agent_complete"] >= 0
    assert event._quest_bridge_timing["trace_event_count"] == 1
    assert stages[-1][1]["trace_action"] == "astr_agent_complete"
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

    fast_action_feedback = {
        "snapshot": {
            "status": "processing",
            "action": None,
            "execution_confirmed": False,
        }
    }
    message = astrbot_pipeline._build_capture_event(
        platform=FakePlatform(),
        platform_meta=FakePlatform().meta(),
        user_text="hello",
        user_id="bound-user",
        bot_id="bound-bot",
        group_id="bound-group",
        protected_context_authorized=True,
        fast_action_active=True,
        fast_action_feedback=fast_action_feedback,
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
    assert message.get_extra(BRIDGE_DELIVERY_OWNER) == "embodiment_bridge"
    assert message.get_extra(BRIDGE_CAPTURE_REQUIRED) is True
    identity = message.get_extra("embodiment_bridge.identity_context")
    assert identity["platform_id"] == "trusted-platform"
    assert identity["user_id"] == "bound-user"
    assert identity["trusted"] is True
    assert message.get_extra("embodiment_bridge.protected_context_authorized") is True
    assert message.get_extra("embodiment_bridge.fast_action_active") is None
    assert message.get_extra("embodiment_bridge.fast_action_feedback") is None
    assert message.get_extra("embodiment_bridge.spatial_context")["revision"] == 3
    assert message.get_extra("embodiment_bridge.action_facts") is None


def test_build_capture_event_keeps_action_arbitration_when_context_unauthorized(
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
            self._extras: dict[str, Any] = {}

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
            return FakeAstrMessageEvent(message)

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

    fast_action_feedback = {
        "snapshot": {
            "status": "processing",
            "action": None,
            "execution_confirmed": False,
        }
    }
    event = astrbot_pipeline._build_capture_event(
        platform=FakePlatform(),
        platform_meta=FakePlatform().meta(),
        user_text="hello",
        user_id="bound-user",
        bot_id="bound-bot",
        group_id="",
        protected_context_authorized=False,
        fast_action_active=True,
        fast_action_feedback=fast_action_feedback,
    )

    assert event.get_extra("embodiment_bridge.protected_context_authorized") is False
    assert event.get_extra("embodiment_bridge.fast_action_feedback") is None
    identity = event.get_extra("embodiment_bridge.identity_context")
    assert identity["trusted"] is True
    assert identity["user_id"] == "bound-user"
    assert event.get_extra("embodiment_bridge.spatial_context") is None
