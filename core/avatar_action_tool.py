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
from .models import ProposedIntent


QUEST_EVENT_MARKER = "quest_avatar_bridge"
QUEST_ACTION_INTENT_EXTRA = "quest_avatar_bridge.avatar_action_intent"
QUEST_ACTION_TOOL_NAME = "quest_avatar_action"
QUEST_ACTION_PROMPT_MARKER = "# 临：Quest 角色动作工具"

def _is_quest_event(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    try:
        return getter(QUEST_EVENT_MARKER) is True
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
        )
        return False

    tool = FunctionTool(
        name=QUEST_ACTION_TOOL_NAME,
        description=(
            "选择一个已允许的 Quest 角色动作。仅在确实要让角色做动作时调用，"
            "每轮最多调用一次；不要传动画文件路径或骨骼名称。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(AvatarSkillRegistry.names()),
                    "description": "动作名称，必须来自枚举",
                },
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "shy", "surprised", "concerned", "uncomfortable"],
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
        )
        return False
    try:
        get_tool = getattr(tool_set, "get_tool", None)
        existing = get_tool(QUEST_ACTION_TOOL_NAME) if callable(get_tool) else None
        if existing is not None and getattr(existing, "handler", None) == handler:
            _inject_action_prompt(request, diagnostic)
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
        )
        return False
    _diagnostic(
        diagnostic,
        "avatar.action.tool_exposed",
        component="action",
        operation=QUEST_ACTION_TOOL_NAME,
        status="ready",
        event_count=len(AvatarSkillRegistry.names()),
    )
    _inject_action_prompt(request, diagnostic)
    return True


async def execute_quest_action(
    event: Any,
    action: str = "",
    emotion: str = "neutral",
    intensity: float = 0.45,
    duration_ms: int | None = None,
    look_at: str = "user",
    *,
    diagnostic: Callable[..., None] | None = None,
    **extra: Any,
) -> str:
    """Validate and store one action intent on the current Quest event."""
    started = time.perf_counter()
    normalized_action = str(action or "").strip().lower()
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
            )
            return _result("rejected", "unknown_argument")
        if read_selected_intent(event) is not None:
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="action_already_selected",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return _result("rejected", "action_already_selected")
        intent = AvatarSkillRegistry.invoke(
            normalized_action,
            {
                "emotion": emotion,
                "intensity": intensity,
                "duration_ms": duration_ms,
                "look_at": look_at,
            },
        )
        if intent is None:
            _diagnostic(
                diagnostic,
                "avatar.action.rejected",
                component="action",
                operation=normalized_action or "unknown",
                status="rejected",
                reason_code="unknown_action",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return _result("rejected", "unknown_action")
        setter = getattr(event, "set_extra", None)
        if not callable(setter):
            raise RuntimeError("event_extra_unavailable")
        setter(QUEST_ACTION_INTENT_EXTRA, intent)
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
        )
        return _result("failed", "action_store_failed")


def _result(status: str, code: str) -> str:
    return json.dumps(
        {"status": status, "code": code}, ensure_ascii=False, separators=(",", ":")
    )


def _inject_action_prompt(
    request: Any,
    diagnostic: Callable[..., None] | None,
) -> None:
    current = str(getattr(request, "system_prompt", "") or "")
    if QUEST_ACTION_PROMPT_MARKER in current:
        return
    skills = ", ".join(AvatarSkillRegistry.names())
    instruction = f"""

{QUEST_ACTION_PROMPT_MARKER}
本轮是 Quest 具身对话。只有确实需要角色执行身体动作时，调用
`{QUEST_ACTION_TOOL_NAME}`，不要只在回复正文中描述“我抬手/转身/跳舞”。
`action` 只能从以下白名单选择：{skills}。
“换一个/另一支舞”使用 dance_next，普通跳舞使用 dance。每轮至多选择一个动作；
普通说话无需调用。不得生成动画路径、文件路径、骨骼名或白名单外动作。
工具返回 rejected/failed 时，不得声称动作已经成功完成。
"""
    request.system_prompt = current + instruction
    _diagnostic(
        diagnostic,
        "avatar.action.prompt_injected",
        component="action",
        operation=QUEST_ACTION_TOOL_NAME,
        status="ready",
        event_count=len(AvatarSkillRegistry.names()),
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
    "QUEST_ACTION_PROMPT_MARKER",
    "QUEST_ACTION_TOOL_NAME",
    "QUEST_EVENT_MARKER",
    "execute_quest_action",
    "inject_quest_action_tool",
    "read_selected_intent",
]
