from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_embodiment_bridge.core.avatar_action_tool import (
    EXPLICIT_ACTION_SOURCE,
    QUEST_ACTION_INTENT_EXTRA,
    QUEST_ACTION_PROMPT_MARKER,
    QUEST_ACTION_SOURCE_EXTRA,
    QUEST_ACTION_TOOL_NAME,
    execute_quest_action,
    inject_quest_action_tool,
    prepare_quest_action_request,
    read_selected_intent,
    read_selected_source,
    stage_explicit_action,
)
from astrbot_plugin_embodiment_bridge.core.avatar_skills import AvatarSkillRegistry


class EventStub:
    def __init__(self, *, quest: bool, message_str: str = "") -> None:
        self.extras: dict[str, Any] = {}
        self.message_str = message_str
        if quest:
            self.extras["embodiment_bridge"] = True

    def get_extra(self, key: str) -> Any:
        return self.extras.get(key)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value


def test_action_execution_accepts_one_allowlisted_action_and_rejects_replacement() -> (
    None
):
    async def scenario() -> None:
        event = EventStub(quest=True)
        records: list[tuple[str, dict[str, Any]]] = []

        accepted = json.loads(
            await execute_quest_action(
                event,
                action="dance_next",
                emotion="happy",
                intensity=0.8,
                duration_ms=9000,
                look_at="user",
                diagnostic=lambda name, **fields: records.append((name, fields)),
            )
        )
        intent = read_selected_intent(event)
        assert accepted == {"status": "accepted", "code": "dance_next"}
        assert intent is not None
        assert intent.gesture.value == "dance_next"
        assert intent.emotion.value == "happy"
        assert intent.intensity == 0.8
        assert intent.duration_ms == 9000
        assert event.extras[QUEST_ACTION_INTENT_EXTRA] is intent
        assert records[-1][0] == "avatar.action.accepted"
        assert records[-1][1]["operation"] == "dance_next"
        assert records[-1][1]["emotion"] == "happy"
        assert records[-1][1]["gesture"] == "dance_next"
        assert records[-1][1]["look_at"] == "user"
        assert records[-1][1]["intensity"] == 0.8
        assert records[-1][1]["duration_ms"] == 9000
        assert records[-1][1]["motion_selection"] == "next_imported"

        duplicate = json.loads(
            await execute_quest_action(event, action="wave", diagnostic=None)
        )
        assert duplicate == {
            "status": "rejected",
            "code": "action_already_selected",
        }
        assert read_selected_intent(event) is intent

    asyncio.run(scenario())


def test_action_execution_fails_closed_for_non_quest_unknown_and_extra_arguments() -> (
    None
):
    async def scenario() -> None:
        normal_event = EventStub(quest=False)
        denied = json.loads(
            await execute_quest_action(normal_event, action="wave", diagnostic=None)
        )
        assert denied == {"status": "rejected", "code": "quest_event_required"}
        assert read_selected_intent(normal_event) is None

        quest_event = EventStub(quest=True)
        unknown = json.loads(
            await execute_quest_action(quest_event, action="play_file", diagnostic=None)
        )
        assert unknown == {"status": "rejected", "code": "unknown_action"}
        assert read_selected_intent(quest_event) is None

        extra = json.loads(
            await execute_quest_action(
                quest_event,
                action="wave",
                diagnostic=None,
                animation_path="C:/untrusted.vmd",
            )
        )
        assert extra == {"status": "rejected", "code": "unknown_argument"}
        assert read_selected_intent(quest_event) is None

    asyncio.run(scenario())


def test_explicit_request_preselects_without_exposing_model_tool() -> None:
    async def scenario() -> None:
        event = EventStub(quest=True, message_str="请随便跳个舞")
        request = SimpleNamespace(func_tool=None, system_prompt="base")
        records: list[tuple[str, dict[str, Any]]] = []
        handler_calls = 0

        async def handler(*_args: Any, **_kwargs: Any) -> str:
            nonlocal handler_calls
            handler_calls += 1
            return "unused"

        result = await prepare_quest_action_request(
            request,
            event,
            handler,
            lambda name, **fields: records.append((name, fields)),
        )

        intent = read_selected_intent(event)
        assert result == "preselected"
        assert intent is not None
        assert intent.gesture.value == "dance"
        assert read_selected_source(event) == EXPLICIT_ACTION_SOURCE
        assert event.extras[QUEST_ACTION_SOURCE_EXTRA] == EXPLICIT_ACTION_SOURCE
        assert request.func_tool is None
        assert request.system_prompt == "base"
        assert handler_calls == 0
        assert [name for name, _fields in records] == [
            "avatar.action.explicit_parse",
            "avatar.action.catalog_unavailable",
            "avatar.action.accepted",
            "avatar.action.tool_skipped",
        ]
        assert records[0][1]["operation"] == "dance"
        assert records[0][1]["status"] == "matched"
        assert records[1][1]["reason_code"] == "action_catalog_not_declared"
        assert records[2][1]["result"] == "explicit_request"
        assert records[3][1]["reason_code"] == "explicit_action_preselected"
        assert "请随便跳个舞" not in repr(records)

    asyncio.run(scenario())


def test_negative_or_non_quest_requests_never_expose_or_select_action() -> None:
    async def scenario() -> None:
        records: list[tuple[str, dict[str, Any]]] = []
        denied_event = EventStub(quest=True, message_str="不要跳舞")
        denied_request = SimpleNamespace(func_tool=None, system_prompt="base")
        denied = await prepare_quest_action_request(
            denied_request,
            denied_event,
            lambda *_args, **_kwargs: asyncio.sleep(0, result="unused"),
            lambda name, **fields: records.append((name, fields)),
        )
        assert denied == "unsafe_context"
        assert read_selected_intent(denied_event) is None
        assert denied_request.func_tool is None
        assert [name for name, _fields in records] == [
            "avatar.action.explicit_parse",
            "avatar.action.tool_skipped",
        ]

        ordinary_event = EventStub(quest=False, message_str="请跳舞")
        ordinary_request = SimpleNamespace(func_tool=None, system_prompt="base")
        assert (
            await prepare_quest_action_request(
                ordinary_request,
                ordinary_event,
                lambda *_args, **_kwargs: asyncio.sleep(0, result="unused"),
                lambda name, **fields: records.append((name, fields)),
            )
            == "non_quest"
        )
        assert ordinary_request.func_tool is None
        assert read_selected_intent(ordinary_event) is None

    asyncio.run(scenario())


def test_model_tool_cannot_override_server_preselected_action() -> None:
    async def scenario() -> None:
        event = EventStub(quest=True, message_str="请挥手")
        records: list[tuple[str, dict[str, Any]]] = []
        await execute_quest_action(
            event,
            action="wave",
            diagnostic=lambda name, **fields: records.append((name, fields)),
            selection_source=EXPLICIT_ACTION_SOURCE,
        )

        duplicate = json.loads(
            await execute_quest_action(
                event,
                action="bow",
                diagnostic=lambda name, **fields: records.append((name, fields)),
            )
        )

        assert duplicate == {
            "status": "rejected",
            "code": "action_already_selected",
        }
        assert read_selected_intent(event).gesture.value == "wave"
        assert records[-1][0] == "avatar.action.model_override_rejected"
        assert records[-1][1]["operation"] == "bow"
        assert records[-1][1]["reason_code"] == "explicit_action_preselected"
        assert records[-1][1]["result"] == "explicit_request"

    asyncio.run(scenario())


def test_staged_original_parse_wins_over_later_event_text_mutation() -> None:
    async def scenario() -> None:
        event = EventStub(quest=True, message_str="请挥手")
        assert stage_explicit_action(event, event.message_str) is True
        event.message_str = "不要挥手"
        request = SimpleNamespace(func_tool=None, system_prompt="base")

        result = await prepare_quest_action_request(
            request,
            event,
            lambda *_args, **_kwargs: asyncio.sleep(0, result="unused"),
            None,
        )

        assert result == "preselected"
        assert read_selected_intent(event).gesture.value == "wave"
        assert request.func_tool is None

    asyncio.run(scenario())


def test_tool_is_injected_only_into_quest_request(monkeypatch: Any) -> None:
    class FakeFunctionTool:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class FakeToolSet:
        def __init__(self) -> None:
            self.tools: list[Any] = []

        def add_tool(self, tool: Any) -> None:
            self.remove_tool(tool.name)
            self.tools.append(tool)

        def remove_tool(self, name: str) -> None:
            self.tools = [tool for tool in self.tools if tool.name != name]

    astrbot = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    tool_module = types.ModuleType("astrbot.core.agent.tool")
    tool_module.FunctionTool = FakeFunctionTool
    tool_module.ToolSet = FakeToolSet
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.core", core)
    monkeypatch.setitem(sys.modules, "astrbot.core.agent", agent)
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.tool", tool_module)

    async def handler(_event: Any, **kwargs: Any) -> str:
        return await execute_quest_action(_event, diagnostic=None, **kwargs)

    ordinary_request = SimpleNamespace(func_tool=None)
    assert (
        inject_quest_action_tool(
            ordinary_request, EventStub(quest=False), handler, None
        )
        is False
    )
    assert ordinary_request.func_tool is None

    quest_request = SimpleNamespace(func_tool=None)
    assert (
        inject_quest_action_tool(quest_request, EventStub(quest=True), handler, None)
        is True
    )
    assert len(quest_request.func_tool.tools) == 1
    tool = quest_request.func_tool.tools[0]
    assert tool.name == QUEST_ACTION_TOOL_NAME
    assert tool.handler is handler
    assert tool.parameters["additionalProperties"] is False
    assert tool.parameters["properties"]["action"]["enum"] == list(
        AvatarSkillRegistry.names()
    )
    assert QUEST_ACTION_PROMPT_MARKER in quest_request.system_prompt
    assert "dance_next" in quest_request.system_prompt
    assert not hasattr(ordinary_request, "system_prompt")

    # The injected FunctionTool must execute the same guarded handler that
    # stores the protocol intent; exposing a schema alone is not sufficient.
    execution_event = EventStub(quest=True)
    execution_result = asyncio.run(
        tool.handler(execution_event, action="dance_next", intensity=0.7)
    )
    assert json.loads(execution_result) == {
        "status": "accepted",
        "code": "dance_next",
    }
    assert read_selected_intent(execution_event).gesture.value == "dance_next"
    assert (
        inject_quest_action_tool(quest_request, EventStub(quest=True), handler, None)
        is True
    )
    assert len(quest_request.func_tool.tools) == 1
    assert quest_request.system_prompt.count(QUEST_ACTION_PROMPT_MARKER) == 1

    ambiguous_event = EventStub(
        quest=True,
        message_str="先跳舞，然后挥手",
    )
    ambiguous_request = SimpleNamespace(func_tool=None, system_prompt="base")
    records: list[tuple[str, dict[str, Any]]] = []
    assert (
        asyncio.run(
            prepare_quest_action_request(
                ambiguous_request,
                ambiguous_event,
                handler,
                lambda name, **fields: records.append((name, fields)),
            )
        )
        == "tool_exposed"
    )
    assert len(ambiguous_request.func_tool.tools) == 1
    assert read_selected_intent(ambiguous_event) is None
    assert records[0][0] == "avatar.action.explicit_parse"
    assert records[0][1]["status"] == "ambiguous"
    assert records[-2][0] == "avatar.action.tool_exposed"
    assert records[-1][0] == "avatar.action.prompt_injected"


def test_action_allowlist_contains_current_protocol_capabilities() -> None:
    assert {
        "dance",
        "dance_next",
        "raise_hand",
        "turn_half",
        "sit",
        "lie",
    }.issubset(AvatarSkillRegistry.names())
