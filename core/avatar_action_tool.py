"""Quest-scoped AstrBot function tool for executable avatar actions.

The tool is injected into an individual EventBus ``ProviderRequest`` only after
the Bridge marker has been verified.  It is deliberately not registered in the
global AstrBot tool manager, so ordinary platform events never see or execute it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .avatar_skills import AvatarSkillRegistry
from .explicit_action_parser import ExplicitActionResult, parse_explicit_action
from .models import ProposedIntent
from .plugin_identity import (
    BRIDGE_EVENT_MARKER,
    BRIDGE_SUPPORTED_ACTIONS,
    LEGACY_BRIDGE_EVENT_MARKER,
)


QUEST_EVENT_MARKER = BRIDGE_EVENT_MARKER
LEGACY_QUEST_EVENT_MARKER = LEGACY_BRIDGE_EVENT_MARKER
QUEST_ACTION_INTENT_EXTRA = "embodiment_bridge.avatar_action_intent"
QUEST_ACTION_PARSE_EXTRA = "embodiment_bridge.explicit_action_parse"
QUEST_ACTION_SOURCE_EXTRA = "embodiment_bridge.avatar_action_source"
QUEST_ACTION_TOOL_NAME = "embodiment_avatar_action"
QUEST_ACTION_PROMPT_MARKER = "# 临：具身角色动作工具"
EXPLICIT_ACTION_SOURCE = "explicit_request"
MODEL_TOOL_SOURCE = "model_tool"
NO_ACTION_SOURCE = "none"


def _is_quest_event(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    try:
        return (
            getter(QUEST_EVENT_MARKER) is True
            or getter(LEGACY_QUEST_EVENT_MARKER) is True
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def read_selected_intent(event: Any) -> ProposedIntent | None:
    """Read and strictly validate the action selected during this EventBus turn."""
    if not _is_quest_event(event):
        return None
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        value = getter(QUEST_ACTION_INTENT_EXTRA)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    if isinstance(value, ProposedIntent):
        return value
    if isinstance(value, dict):
        try:
            return ProposedIntent.model_validate(value)
        except (TypeError, ValueError):
            return None
    return None


def read_selected_source(event: Any) -> str:
    """Read the bounded internal source marker for a selected Quest action."""
    if not _is_quest_event(event):
        return ""
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    try:
        value = getter(QUEST_ACTION_SOURCE_EXTRA)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    return value if value in {EXPLICIT_ACTION_SOURCE, MODEL_TOOL_SOURCE} else ""


def stage_explicit_action(event: Any, user_text: str) -> bool:
    """Cache only the bounded parse result from the original Bridge text."""
    if not _is_quest_event(event):
        return False
    setter = getattr(event, "set_extra", None)
    if not callable(setter):
        return False
    try:
        setter(QUEST_ACTION_PARSE_EXTRA, parse_explicit_action(user_text))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    return True


def _read_staged_parse(event: Any) -> ExplicitActionResult | None:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return None
    try:
        value = getter(QUEST_ACTION_PARSE_EXTRA)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    return value if isinstance(value, ExplicitActionResult) else None


def _supported_action_names(event: Any) -> tuple[str, ...]:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return AvatarSkillRegistry.legacy_client_names()
    try:
        declared = getter(BRIDGE_SUPPORTED_ACTIONS)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        declared = None
    if not isinstance(declared, (list, tuple, set, frozenset)):
        return AvatarSkillRegistry.legacy_client_names()
    return AvatarSkillRegistry.supported_names(declared)


def inject_quest_action_tool(
    request: Any,
    event: Any,
    handler: Callable[..., Awaitable[str]],
    diagnostic: Callable[..., None] | None = None,
) -> bool:
    """Inject the action tool into one trusted Quest request.

    Returning ``False`` is a fail-closed result.  Importing the framework types
    lazily keeps plugin discovery compatible with older AstrBot builds.
    """
    if not _is_quest_event(event):
        return False
    supported_actions = _supported_action_names(event)
    if not supported_actions:
        _diagnostic(
            diagnostic,
            "avatar.action.tool_skipped",
            component="action",
            operation="none",
            status="skipped",
            reason_code="client_has_no_supported_actions",
            action_source=MODEL_TOOL_SOURCE,
        )
        return False
    selected = read_selected_intent(event)
    if selected is not None:
        source = read_selected_source(event)
        _diagnostic(
            diagnostic,
            "avatar.action.tool_skipped",
            component="action",
            operation=selected.gesture.value,
            status="skipped",
            reason_code=(
                "explicit_action_preselected"
                if source == EXPLICIT_ACTION_SOURCE
                else "action_already_selected"
            ),
            result=source or "unknown",
            action_source=source or "unknown",
        )
        return False
    try:
        from astrbot.core.agent.tool import FunctionTool, ToolSet
    except (ImportError, ModuleNotFoundError, AttributeError) as exc:
        _diagnostic(
            diagnostic,
            "avatar.action.tool_unavailable",
            component="action",
            operation=QUEST_ACTION_TOOL_NAME,
            status="rejected",
            reason_code="astrbot_tool_api_unavailable",
            error_type=type(exc).__name__,
            action_source=MODEL_TOOL_SOURCE,
        )
        return False

    tool = FunctionTool(
        name=QUEST_ACTION_TOOL_NAME,
        description=(
            "选择一个已允许的具身角色动作。仅在确实要让角色做动作时调用，"
            "每轮最多调用一次；不要传动画文件路径或骨骼名称。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(supported_actions),
                    "description": "动作名称，必须来自枚举",
                },
                "emotion": {
                    "type": "string",
                    "enum": [
                        "neutral",
                        "happy",
                        "shy",
                        "surprised",
                        "concerned",
                        "uncomfortable",
                    ],
                    "description": "动作期间的表情倾向",
                    "default": "neutral",
                },
                "intensity": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "动作强度，范围 0 到 1",
                    "default": 0.45,
                },
                "duration_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 30000,
                    "description": "动作持续时间（毫秒）",
                },
                "look_at": {
                    "type": "string",
                    "enum": ["user", "hand", "away", "none"],
                    "description": "动作期间视线目标",
                    "default": "user",
                },
                "angle_degrees": {
                    "type": "number",
                    "minimum": -180,
                    "maximum": 180,
                    "description": "仅 turn_half 使用的转身角度",
                },
                "depth": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "仅 crouch 使用的下蹲深度",
                },
                "hold_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 30000,
                    "description": "仅 crouch 使用的保持时间",
                },
                "style": {
                    "type": "string",
                    "enum": ["natural", "gentle", "energetic"],
                    "default": "natural",
                },
                "transition_in_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5000,
                },
                "transition_out_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 5000,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handler,
    )
    tool_set = getattr(request, "func_tool", None)
    if tool_set is None:
        tool_set = ToolSet()
        request.func_tool = tool_set
    add_tool = getattr(tool_set, "add_tool", None)
    if not callable(add_tool):
        _diagnostic(
            diagnostic,
            "avatar.action.tool_unavailable",
            component="action",
            operation=QUEST_ACTION_TOOL_NAME,
            status="rejected",
            reason_code="astrbot_tool_set_api_unavailable",
            action_source=MODEL_TOOL_SOURCE,
        )
        return False
    try:
        get_tool = getattr(tool_set, "get_tool", None)
        existing = get_tool(QUEST_ACTION_TOOL_NAME) if callable(get_tool) else None
        if existing is not None and getattr(existing, "handler", None) == handler:
            _inject_action_prompt(request, diagnostic, supported_actions)
            return True
        # Replace a same-name entry rather than trusting a handler supplied by
        # another plugin or a stale plugin instance.
        remove_tool = getattr(tool_set, "remove_tool", None)
        if callable(remove_tool):
            remove_tool(QUEST_ACTION_TOOL_NAME)
        add_tool(tool)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        _diagnostic(
            diagnostic,
            "avatar.action.tool_unavailable",
            component="action",
            operation=QUEST_ACTION_TOOL_NAME,
            status="rejected",
            reason_code="quest_action_tool_injection_failed",
            error_type=type(exc).__name__,
            action_source=MODEL_TOOL_SOURCE,
        )
        return False
    _diagnostic(
        diagnostic,
        "avatar.action.tool_exposed",
        component="action",
        operation=QUEST_ACTION_TOOL_NAME,
        status="ready",
        event_count=len(supported_actions),
        action_source=MODEL_TOOL_SOURCE,
    )
    _inject_action_prompt(request, diagnostic, supported_actions)
    return True


async def prepare_quest_action_request(
    request: Any,
    event: Any,
    handler: Callable[..., Awaitable[str]],
    diagnostic: Callable[..., None] | None = None,
) -> str:
    """Preselect explicit imperatives or expose the bounded model tool."""
    if not _is_quest_event(event):
        return "non_quest"

    parsed = _read_staged_parse(event) or parse_explicit_action(
        str(getattr(event, "message_str", "") or "")
    )
    _diagnostic(
        diagnostic,
        "avatar.action.explicit_parse",
        component="action",
        operation=parsed.action or "none",
        status=parsed.status,
        reason_code=parsed.reason,
        result=("tool_allowed" if parsed.allow_model_tool else "tool_suppressed"),
        action_source=(EXPLICIT_ACTION_SOURCE if parsed.action else NO_ACTION_SOURCE),
    )

    if parsed.action is not None:
        await execute_quest_action(
            event,
            action=parsed.action,
            diagnostic=diagnostic,
            selection_source=EXPLICIT_ACTION_SOURCE,
        )
        if read_selected_intent(event) is not None:
            inject_quest_action_tool(request, event, handler, diagnostic)
            return "preselected"
        _diagnostic(
            diagnostic,
            "avatar.action.tool_skipped",
            component="action",
            operation=parsed.action,
            status="skipped",
            reason_code="explicit_preselection_failed",
            result="tool_suppressed",
            action_source=EXPLICIT_ACTION_SOURCE,
        )
        return "preselection_failed"

    if not parsed.allow_model_tool:
        _diagnostic(
            diagnostic,
            "avatar.action.tool_skipped",
            component="action",
            operation="none",
            status="skipped",
            reason_code=parsed.reason,
            result="unsafe_context",
            action_source=NO_ACTION_SOURCE,
        )
        return "unsafe_context"

    return (
        "tool_exposed"
        if inject_quest_action_tool(request, event, handler, diagnostic)
        else "tool_unavailable"
    )


async def execute_quest_action(
    event: Any,
    action: str = "",
    emotion: str = "neutral",
    intensity: float = 0.45,
    duration_ms: int | None = None,
    look_at: str = "user",
    angle_degrees: float | None = None,
    depth: float | None = None,
    hold_ms: int | None = None,
    style: str = "natural",
    transition_in_ms: int | None = None,
    transition_out_ms: int | None = None,
    *,
    diagnostic: Callable[..., None] | None = None,
    selection_source: str = MODEL_TOOL_SOURCE,
    **extra: Any,
) -> str:
    """Validate and store one action intent on the current Quest event."""
    started = time.perf_counter()
    normalized_action = (
        AvatarSkillRegistry.normalize_action_name(action)
        or str(action or "").strip().lower()
    )
    normalized_source = (
        EXPLICIT_ACTION_SOURCE
        if selection_source == EXPLICIT_ACTION_SOURCE
        else MODEL_TOOL_SOURCE
    )
    try:
        if not _is_quest_event(event):
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="quest_event_required",
                duration_ms=(time.perf_counter() - started) * 1000,
                action_source=normalized_source,
            )
            return _result("rejected", "quest_event_required")
        if set(extra):
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="unknown_argument",
                duration_ms=(time.perf_counter() - started) * 1000,
                action_source=normalized_source,
            )
            return _result("rejected", "unknown_argument")
        if AvatarSkillRegistry.normalize_action_name(normalized_action) is None:
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="unknown_action",
                duration_ms=(time.perf_counter() - started) * 1000,
                action_source=normalized_source,
            )
            return _result("rejected", "unknown_action")
        if normalized_action not in _supported_action_names(event):
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="client_action_unsupported",
                duration_ms=(time.perf_counter() - started) * 1000,
                action_source=normalized_source,
            )
            return _result("rejected", "client_action_unsupported")
        if read_selected_intent(event) is not None:
            existing_source = read_selected_source(event)
            model_override_rejected = bool(
                normalized_source == MODEL_TOOL_SOURCE
                and existing_source == EXPLICIT_ACTION_SOURCE
            )
            _diagnostic(
                diagnostic,
                (
                    "avatar.action.model_override_rejected"
                    if model_override_rejected
                    else "avatar.action.rejected"
                ),
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code=(
                    "explicit_action_preselected"
                    if model_override_rejected
                    else "action_already_selected"
                ),
                result=existing_source or "unknown",
                action_source=normalized_source,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return _result("rejected", "action_already_selected")
        arguments: dict[str, Any] = {
            "emotion": emotion,
            "intensity": intensity,
            "duration_ms": duration_ms,
            "look_at": look_at,
            "style": style,
        }
        for key, value in (
            ("angle_degrees", angle_degrees),
            ("depth", depth),
            ("hold_ms", hold_ms),
            ("transition_in_ms", transition_in_ms),
            ("transition_out_ms", transition_out_ms),
        ):
            if value is not None:
                arguments[key] = value
        intent = AvatarSkillRegistry.invoke(normalized_action, arguments)
        if intent is None:
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="unknown_action",
                duration_ms=(time.perf_counter() - started) * 1000,
                action_source=normalized_source,
            )
            return _result("rejected", "unknown_action")
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            raise RuntimeError("event_extra_unavailable")
        setter(QUEST_ACTION_SOURCE_EXTRA, normalized_source)
        setter(QUEST_ACTION_INTENT_EXTRA, intent)
        catalog_status = AvatarSkillRegistry.catalog_status(normalized_action)
        motion_selection = AvatarSkillRegistry.motion_selection(normalized_action)
        if catalog_status == "not_declared":
            _diagnostic(
                diagnostic,
                "avatar.action.catalog_unavailable",
                component="action",
                operation=normalized_action,
                status="degraded",
                reason_code="action_catalog_not_declared",
                catalog_status=catalog_status,
                action_source=normalized_source,
                motion_selection=motion_selection,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        _diagnostic(
            diagnostic,
            "avatar.action.accepted",
            component="action",
            operation=normalized_action,
            status="accepted",
            reason_code=intent.reason_code,
            emotion=intent.emotion.value,
            gesture=intent.gesture.value,
            look_at=intent.look_at.value,
            intensity=intent.intensity,
            duration_ms=intent.duration_ms,
            result=normalized_source,
            action_source=normalized_source,
            catalog_status=catalog_status,
            motion_selection=motion_selection,
        )
        return _result("accepted", normalized_action)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        _diagnostic(
            diagnostic,
            "avatar.action.failed",
            component="action",
            operation=normalized_action or "unknown",
            status="failed",
            reason_code="action_store_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
            action_source=normalized_source,
        )
        return _result("failed", "action_store_failed")


def _result(status: str, code: str) -> str:
    return json.dumps(
        {"status": status, "code": code}, ensure_ascii=False, separators=(",", ":")
    )


def _inject_action_prompt(
    request: Any,
    diagnostic: Callable[..., None] | None,
    supported_actions: tuple[str, ...],
) -> None:
    current = str(getattr(request, "system_prompt", "") or "")
    if QUEST_ACTION_PROMPT_MARKER in current:
        return
    skills = ", ".join(supported_actions)
    instruction = f"""

{QUEST_ACTION_PROMPT_MARKER}
本轮是可信具身客户端对话。只有确实需要角色执行身体动作时，调用
`{QUEST_ACTION_TOOL_NAME}`，不要只在回复正文中描述“我抬手/转身/跳舞”。
`action` 只能从以下白名单选择：{skills}。
“换一个/另一支舞”使用 dance_next，普通跳舞使用 dance。每轮至多选择一个动作；
普通说话无需调用。不得生成动画路径、文件路径、骨骼名或白名单外动作。
工具返回 rejected/failed 时，不得声称动作已经成功完成。工具返回 accepted 也只
表示动作已计划并发给客户端；同轮只能说“我试试/现在开始”，不能说动作已经完成。
只有后续认证回执中的 completed 才证明身体实际完成。
"""
    request.system_prompt = current + instruction
    _diagnostic(
        diagnostic,
        "avatar.action.prompt_injected",
        component="action",
        operation=QUEST_ACTION_TOOL_NAME,
        status="ready",
        event_count=len(supported_actions),
    )


def _diagnostic(
    diagnostic: Callable[..., None] | None,
    event: str,
    **fields: Any,
) -> None:
    if diagnostic is None:
        return
    try:
        diagnostic(event, **fields)
    except Exception:
        return


__all__ = [
    "QUEST_ACTION_INTENT_EXTRA",
    "QUEST_ACTION_PARSE_EXTRA",
    "QUEST_ACTION_PROMPT_MARKER",
    "QUEST_ACTION_SOURCE_EXTRA",
    "QUEST_ACTION_TOOL_NAME",
    "QUEST_EVENT_MARKER",
    "EXPLICIT_ACTION_SOURCE",
    "MODEL_TOOL_SOURCE",
    "NO_ACTION_SOURCE",
    "execute_quest_action",
    "inject_quest_action_tool",
    "prepare_quest_action_request",
    "read_selected_intent",
    "read_selected_source",
    "stage_explicit_action",
]
