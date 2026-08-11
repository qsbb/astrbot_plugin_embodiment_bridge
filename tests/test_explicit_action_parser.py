from __future__ import annotations

import pytest

from astrbot_plugin_embodiment_bridge.core.explicit_action_parser import (
    MAX_COMMAND_CHARS,
    ExplicitActionResult,
    parse_explicit_action,
)


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("请随便跳个舞", "dance"),
        ("能不能跳个舞？", "dance"),
        ("请跳舞", "dance"),
        ("跳下一支舞吧", "dance_next"),
        ("换个舞蹈", "dance_next"),
        ("请随便换一个舞", "dance_next"),
        ("把手举起来", "raise_hand"),
        ("现在转半圈", "turn_half"),
        ("向我挥手", "wave"),
        ("鞠个躬吧", "bow"),
        ("请坐", "sit"),
        ("躺下！", "lie"),
    ],
)
def test_matches_bounded_whole_message_chinese_imperatives(
    text: str,
    action: str,
) -> None:
    result = parse_explicit_action(text)

    assert result == ExplicitActionResult(
        action=action,
        status="matched",
        reason="explicit_imperative",
        allow_model_tool=False,
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("不要跳舞", "negated"),
        ("别挥手", "negated"),
        ("如果方便就鞠躬", "hypothetical"),
        ("假设你现在坐下", "hypothetical"),
        ("他说‘跳舞’", "quoted_or_cited"),
        ("请复述跳舞", "reported_speech"),
        ("讨论一下怎么转半圈", "discussion_context"),
        ("我喜欢看你挥手", "discussion_context"),
        ("你会跳舞吗？", "discussion_context"),
    ],
)
def test_rejects_non_command_contexts(text: str, reason: str) -> None:
    result = parse_explicit_action(text)

    assert result.action is None
    assert result.status == "rejected"
    assert result.reason == reason
    assert result.allow_model_tool is False


def test_multiple_actions_are_ambiguous_and_left_for_bounded_model_tool() -> None:
    result = parse_explicit_action("先跳舞，然后挥手")

    assert result == ExplicitActionResult(
        action=None,
        status="ambiguous",
        reason="multiple_actions",
        allow_model_tool=True,
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("你好，今天过得怎么样", "no_action_mentioned"),
        ("今天真适合跳舞", "not_full_match"),
        ("", "empty_input"),
    ],
)
def test_ordinary_or_incomplete_expressions_allow_bounded_model_tool(
    text: str,
    reason: str,
) -> None:
    result = parse_explicit_action(text)

    assert result.action is None
    assert result.status == "not_explicit"
    assert result.reason == reason
    assert result.allow_model_tool is True


def test_overlong_input_fails_closed_before_matching() -> None:
    result = parse_explicit_action("请" * MAX_COMMAND_CHARS + "跳舞")

    assert result == ExplicitActionResult(
        action=None,
        status="rejected",
        reason="input_too_long",
        allow_model_tool=False,
    )


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("Please dance!", "dance"),
        ("switch to the next dance", "dance_next"),
        ("raise your hand", "raise_hand"),
        ("turn 180 degrees", "turn_half"),
        ("wave at me", "wave"),
        ("take a bow", "bow"),
        ("sit down", "sit"),
        ("lie down", "lie"),
    ],
)
def test_matches_clear_english_imperatives(text: str, action: str) -> None:
    result = parse_explicit_action(text)

    assert result.action == action
    assert result.status == "matched"
    assert result.reason == "explicit_imperative"
    assert result.allow_model_tool is False
