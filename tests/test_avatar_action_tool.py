from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_quest_avatar_bridge.core.avatar_action_tool import (
    QUEST_ACTION_INTENT_EXTRA,
    QUEST_ACTION_PROMPT_MARKER,
    QUEST_ACTION_TOOL_NAME,
    execute_quest_action,
    inject_quest_action_tool,
    read_selected_intent,
)
from astrbot_plugin_quest_avatar_bridge.core.avatar_skills import AvatarSkillRegistry


class EventStub:
    def __init__(self, *, quest: bool) -> None:
        self.extras: dict[str, Any] = {}
        if quest:
            self.extras["quest_avatar_bridge"] = True

    def get_extra(self, key: str) -> Any:
        return self.extras.get(key)

    def set_extra(self, key: str, value: Any) -> None:
        self.extras[key] = value


def test_action_execution_accepts_one_allowlisted_action_and_rejects_replacement() -> None:
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

        duplicate = json.loads(
            await execute_quest_action(event, action="wave", diagnostic=None)
        )
        assert duplicate == {
            "status": "rejected",
            "code": "action_already_selected",
        }
        assert read_selected_intent(event) is intent

    asyncio.run(scenario())


def test_action_execution_fails_closed_for_non_quest_unknown_and_extra_arguments() -> None:
    async def scenario() -> None:
        normal_event = EventStub(quest=False)
        denied = json.loads(
            await execute_quest_action(normal_event, action="wave", diagnostic=None)
        )
        assert denied == {"status": "rejected", "code": "quest_event_required"}
        assert read_selected_intent(normal_event) is None

        quest_event = EventStub(quest=True)
        unknown = json.loads(
            await execute_quest_action(
                quest_event, action="play_file", diagnostic=None
            )
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

    async def handler(_event: Any, **_kwargs: Any) -> str:
        return "ok"

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
        inject_quest_action_tool(
            quest_request, EventStub(quest=True), handler, None
        )
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
    assert (
        inject_quest_action_tool(
            quest_request, EventStub(quest=True), handler, None
        )
        is True
    )
    assert len(quest_request.func_tool.tools) == 1
    assert quest_request.system_prompt.count(QUEST_ACTION_PROMPT_MARKER) == 1


def test_action_allowlist_contains_current_protocol_capabilities() -> None:
    assert {
        "dance",
        "dance_next",
        "raise_hand",
        "turn_half",
        "sit",
        "lie",
    }.issubset(AvatarSkillRegistry.names())
