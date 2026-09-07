"""桥接合成事件与投递助手（1.3.0 起仅保留共享助手）。

历史版本的本模块含 ``AstrBotMessagePipelineAdapter``（把具身客户端的话语提交
进 AstrBot 共享事件总线）。2026-09-07 起主消息链路径移除：具身对话全部走
临专属链路（``quest_enriched_pipeline``），该适配器已删除。本文件保留两条
链路共用的合成事件构造/中止/空间上下文助手与诊断常量。
"""

from __future__ import annotations

import asyncio
import time
import types
import uuid
from typing import Any

from ..core.explicit_action_parser import requires_text_reply
from ..core.plugin_identity import (
    BRIDGE_EVENT_MARKER,
    BRIDGE_CAPTURE_REQUIRED,
    BRIDGE_DELIVERY_OWNER,
    BRIDGE_IDENTITY_CONTEXT,
    BRIDGE_PROTECTED_CONTEXT_AUTHORIZED,
    BRIDGE_SPATIAL_CONTEXT,
    BRIDGE_TEXT_REPLY_REQUIRED,
    LEGACY_BRIDGE_EVENT_MARKER,
    LEGACY_BRIDGE_IDENTITY_CONTEXT,
)
from ..core.session_manager import SPATIAL_CONTEXT_TTL_SECONDS
from ..core.timing_trace import TimingTrace, safe_trace_action


class MessagePipelineUnavailable(RuntimeError):
    pass


class MessagePipelineEmpty(RuntimeError):
    pass


DELIVERY_VISIBILITY_VALUES = frozenset(
    {"captured", "result_recovered", "plan_recovered", "action_only", "unobserved"}
)


def _build_capture_event(
    *,
    platform: Any,
    platform_meta: Any,
    user_text: str,
    user_id: str,
    bot_id: str,
    group_id: str,
    protected_context_authorized: bool = False,
    spatial_context: dict[str, Any] | None = None,
    fast_action_active: bool = False,
    fast_action_feedback: dict[str, object] | None = None,
    action_facts: list[dict[str, Any]] | None = None,
    supported_actions: tuple[str, ...] | None = None,
    image: Any | None = None,
) -> Any:
    # Imports stay lazy so plugin discovery still degrades cleanly on older
    # AstrBot builds that do not expose the complete EventBus ABI.
    from astrbot.api.message_components import Plain
    from astrbot.api.platform import (
        AstrBotMessage,
        Group,
        MessageMember,
        MessageType,
    )

    message = AstrBotMessage()
    message.self_id = str(bot_id)
    message.sender = MessageMember(str(user_id), "Embodied Client")
    message.type = MessageType.GROUP_MESSAGE if group_id else MessageType.FRIEND_MESSAGE
    message.session_id = str(group_id or user_id)
    message_id = "embodiment-" + uuid.uuid4().hex
    message.message_id = message_id
    message.message = [Plain(str(user_text))]
    if image is not None:
        # 摄像头单帧（可选、单帧、不落盘）：以 Image 组件进入合成事件链，
        # 走 AstrBot 原生多模态路径。组件构造失败则整轮失败（诚实回执，
        # 不静默丢弃——静默丢弃会让模型在无图情况下回答"看到了什么"）。
        from astrbot.api.message_components import Image

        message.message.append(Image.fromBase64(str(image.data_base64)))
    message.message_str = str(user_text)
    message.raw_message = _bridge_raw_message(
        platform_name=str(getattr(platform_meta, "name", "") or ""),
        user_text=user_text,
        user_id=str(user_id),
        bot_id=str(bot_id),
        group_id=str(group_id or ""),
        message_id=message_id,
    )
    message.timestamp = int(time.time())
    if group_id:
        message.group = Group(group_id=str(group_id))

    # Platform.create_event is AstrBot's public factory. It preserves the
    # concrete adapter event type and its normal MessageSession/UMO setup.
    event = platform.create_event(message)
    from astrbot.api.event import AstrMessageEvent

    if not isinstance(event, AstrMessageEvent):
        raise MessagePipelineUnavailable("astrbot_event_factory_invalid")

    event._quest_done = asyncio.Event()
    event._quest_messages = []
    event._quest_stream = ""
    event._quest_cleanup_called = False
    event._quest_bridge_aborted = False
    event._quest_bridge_abort_reason = ""
    event._quest_bridge_late_event_count = 0
    original_cleanup = event.cleanup_temporary_local_files

    async def send(self: Any, outgoing: Any) -> None:
        if self._quest_bridge_aborted:
            self._quest_bridge_late_event_count += 1
            return
        self._has_send_oper = True
        _capture_message(self, outgoing, streaming=False)

    async def send_streaming(
        self: Any,
        generator: Any,
        use_fallback: bool = False,
    ) -> None:
        del use_fallback
        if self._quest_bridge_aborted:
            self._quest_bridge_late_event_count += 1
            return
        self._has_send_oper = True
        async for outgoing in generator:
            if self._quest_bridge_aborted:
                self._quest_bridge_late_event_count += 1
                return
            _capture_message(self, outgoing, streaming=True)

    async def send_typing(self: Any) -> None:
        return None

    async def stop_typing(self: Any) -> None:
        return None

    def cleanup(self: Any) -> None:
        if self._quest_cleanup_called:
            return
        self._quest_cleanup_called = True
        try:
            original_cleanup()
        finally:
            callback = getattr(self, "_quest_bridge_stage", None)
            if callable(callback):
                try:
                    callback(
                        "event_cleanup_called",
                        status="completed",
                        event_type="message.event",
                    )
                except Exception:
                    pass
            self._quest_done.set()

    async def wait_completed(self: Any) -> None:
        await self._quest_done.wait()

    def captured_text(self: Any) -> str:
        values = [value for value in self._quest_messages if value.strip()]
        if self._quest_stream.strip():
            values.append(self._quest_stream)
        deduplicated: list[str] = []
        for value in values:
            cleaned = value.strip()
            if cleaned and (not deduplicated or deduplicated[-1] != cleaned):
                deduplicated.append(cleaned)
        return "\n".join(deduplicated)

    event.send = types.MethodType(send, event)
    event.send_streaming = types.MethodType(send_streaming, event)
    event.send_typing = types.MethodType(send_typing, event)
    event.stop_typing = types.MethodType(stop_typing, event)
    event.cleanup_temporary_local_files = types.MethodType(cleanup, event)
    event.wait_completed = types.MethodType(wait_completed, event)
    event.captured_text = types.MethodType(captured_text, event)
    # An embodiment bridge session can never inherit AstrBot administrator role from
    # the bound raw account. Authorization remains the identity plugin's job.
    event.set_extra("_api_key_allow_admin_role", False)
    event.set_extra(BRIDGE_EVENT_MARKER, True)
    event.set_extra(BRIDGE_TEXT_REPLY_REQUIRED, requires_text_reply(user_text))
    event.set_extra(BRIDGE_DELIVERY_OWNER, "embodiment_bridge")
    event.set_extra(BRIDGE_CAPTURE_REQUIRED, True)
    event.set_extra(
        BRIDGE_PROTECTED_CONTEXT_AUTHORIZED,
        bool(protected_context_authorized),
    )
    if protected_context_authorized and spatial_context is not None:
        event.set_extra(BRIDGE_SPATIAL_CONTEXT, dict(spatial_context))
    # Deprecated compatibility markers are emitted for one major release so
    # existing series plugins can migrate without losing authorized context.
    event.set_extra(LEGACY_BRIDGE_EVENT_MARKER, True)
    identity_context = {
        "platform_id": str(platform_meta.id),
        "bot_id": str(bot_id),
        "user_id": str(user_id),
        "group_id": str(group_id or ""),
        "session_id": str(message.session_id),
        "trusted": True,
    }
    event.set_extra(
        BRIDGE_IDENTITY_CONTEXT,
        identity_context,
    )
    event.set_extra(LEGACY_BRIDGE_IDENTITY_CONTEXT, identity_context)
    # The client streams TTS through Protocol 1.0 after the text decision. Mark the
    # synthetic event handled so voice_hub does not synthesize the same reply.
    event.set_extra("mimo_tts_handled", True)
    return event


def _session_spatial_context(session: Any) -> dict[str, Any] | None:
    snapshot = getattr(session, "spatial_context", None)
    if snapshot is None:
        return None
    updated_at = float(getattr(session, "spatial_context_updated_at", 0.0) or 0.0)
    if updated_at <= 0.0 or time.monotonic() - updated_at > SPATIAL_CONTEXT_TTL_SECONDS:
        return None
    dump = getattr(snapshot, "model_dump", None)
    if not callable(dump):
        return None
    value = dump(mode="json")
    return dict(value) if isinstance(value, dict) else None


def _abort_synthetic_event(
    event: Any,
    *,
    reason: str = "aborted",
    stage: Any = None,
) -> None:
    """Idempotently terminate a synthetic EventBus event owned by the bridge.

    After this call the event will no longer produce valid text or audio
    replies, and late ``send()`` / ``send_streaming()`` calls become no-ops.
    """
    if getattr(event, "_quest_bridge_aborted", False):
        return
    event._quest_bridge_aborted = True
    event._quest_bridge_abort_reason = str(reason or "aborted")[:128]
    event.set_extra("agent_stop_requested", True)
    stopper = getattr(event, "stop_event", None)
    if callable(stopper):
        try:
            stopper()
        except Exception:
            pass
    cleaner = getattr(event, "cleanup_temporary_local_files", None)
    if callable(cleaner):
        try:
            cleaner()
        except Exception:
            pass
    if callable(stage):
        try:
            stage(
                "event_aborted",
                status="aborted",
                event_type="message.event",
                abort_reason=event._quest_bridge_abort_reason,
                late_event_count=event._quest_bridge_late_event_count,
            )
        except Exception:
            pass


def _capture_message(event: Any, message: Any, *, streaming: bool) -> None:
    if message is None:
        return
    getter = getattr(message, "get_plain_text", None)
    text = str(getter() if callable(getter) else "")
    if not text:
        return
    if not streaming:
        event._quest_messages.append(text)
        return
    if text.startswith(event._quest_stream):
        event._quest_stream = text
    elif not event._quest_stream.endswith(text):
        event._quest_stream += text


def _bridge_raw_message(
    *,
    platform_name: str,
    user_text: str,
    user_id: str,
    bot_id: str,
    group_id: str,
    message_id: str,
) -> dict[str, Any]:
    """Provide stable, adapter-neutral metadata for generic plugin hooks.

    This is not treated as a native platform payload. Native hooks should use
    AstrMessageEvent's public accessors; the fields below keep common
    post-processing integrations able to resolve the authorized sender.
    """
    message_type = "group" if group_id else "private"
    raw: dict[str, Any] = {
        "source": "embodiment_bridge",
        "platform": platform_name,
        "post_type": "message",
        "message_type": message_type,
        "self_id": bot_id,
        "user_id": user_id,
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": user_text}}],
        "sender": {"user_id": user_id, "nickname": "Embodied Client"},
    }
    if group_id:
        raw["group_id"] = group_id
    return raw


def _delivery_plan_text(event: Any) -> str:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    plan = getter("conversation_flow.delivery_plan")
    if isinstance(plan, dict) and plan.get("version") == "1.0":
        return str(plan.get("original_text") or "")
    request_context = getter("ningxin.request_context.v1")
    if not isinstance(request_context, dict):
        return ""
    artifacts = request_context.get("artifacts")
    if not isinstance(artifacts, dict):
        return ""
    conversation_flow = artifacts.get("conversation_flow")
    if not isinstance(conversation_flow, dict):
        return ""
    plan = conversation_flow.get("delivery_plan")
    return str(plan.get("original_text") or "") if isinstance(plan, dict) else ""


def _event_result_text(event: Any) -> str:
    """Read the final EventBus result when a plugin claimed default delivery.

    ``stop_event()`` controls further EventBus propagation; it does not erase a
    result that was already produced. Reading only captured ``send()`` calls
    therefore loses valid post-processed replies from plugins that stop default
    delivery after updating ``MessageEventResult``.
    """
    getter = getattr(event, "get_result", None)
    if not callable(getter):
        return ""
    try:
        result = getter()
    except Exception:
        return ""
    plain_text = getattr(result, "get_plain_text", None)
    if not callable(plain_text):
        return ""
    try:
        value = plain_text()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _queue_size(queue: Any) -> int | None:
    """Read a bounded queue size without depending on a private queue API."""
    getter = getattr(queue, "qsize", None)
    if not callable(getter):
        return None
    try:
        value = int(getter())
    except (TypeError, ValueError, RuntimeError):
        return None
    return max(0, min(value, 100_000))


def _install_trace_probe(event: Any, stage: Any) -> None:
    """Observe public AstrBot TraceSpan actions without retaining their fields."""
    trace = getattr(event, "trace", None)
    recorder = getattr(trace, "record", None)
    if not callable(recorder):
        return
    timing = getattr(event, "_quest_bridge_timing", None)
    if not isinstance(timing, dict):
        return

    def record(action: str, **_fields: Any) -> None:
        try:
            bridge_trace = getattr(event, "_quest_bridge_trace", None)
            if isinstance(bridge_trace, TimingTrace):
                bridge_trace.mark_event_consumed()
                safe_action = safe_trace_action(action)
                if safe_action in {"astr_agent_prepare", "agent.prepare"}:
                    bridge_trace.start_named_span(
                        "agent.provider",
                        kind="agent_provider",
                        category="provider",
                    )
                    bridge_trace.trace_point("provider.request_sent")
                elif safe_action in {"astr_agent_complete", "agent.complete"}:
                    bridge_trace.trace_point("provider.completed")
                    bridge_trace.finish_named_span(
                        "agent.provider",
                        status="completed",
                    )
                elif "provider" in safe_action and any(
                    marker in safe_action for marker in ("queue", "queued", "wait")
                ):
                    bridge_trace.start_named_span(
                        "provider.queue_wait",
                        kind="provider_queue",
                        category="queue",
                    )
                elif "provider" in safe_action and any(
                    marker in safe_action
                    for marker in ("first_token", "first_chunk", "request_sent")
                ):
                    bridge_trace.finish_named_span(
                        "provider.queue_wait",
                        status="completed",
                    )
                bridge_trace.trace_point(
                    safe_action,
                    status="observed",
                )
            elapsed_ms = max(
                0,
                int((time.perf_counter() - float(timing["started"])) * 1000),
            )
            safe_action = safe_trace_action(action)
            timing[f"trace_{safe_action}"] = elapsed_ms
            timing["trace_event_count"] = int(timing.get("trace_event_count", 0)) + 1
            stage(
                "event_trace",
                status="observed",
                event_type="message.event",
                trace_action=safe_action,
                trace_event_count=timing["trace_event_count"],
            )
        except Exception:
            pass
        try:
            recorder(action, **_fields)
        except Exception:
            return

    try:
        trace.record = record
    except (AttributeError, TypeError):
        return


def _event_metadata_snapshot(event: Any) -> dict[str, Any]:
    """Return routing facts without exposing platform/user/session identifiers."""
    try:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:
        origin = ""
    parts = origin.split(":", 2) if origin else []
    return {
        "event_class": type(event).__name__[:96],
        "platform_id_configured": bool(getattr(event, "platform_meta", None)),
        "umo_parts": len(parts),
        "umo_shape_valid": len(parts) == 3 and all(bool(part) for part in parts),
        "message_type": str(parts[1])[:48] if len(parts) > 1 else "",
        "session_id_present": bool(parts[2]) if len(parts) > 2 else False,
    }


def _public_event_timing(timing: Any) -> dict[str, int | str | bool]:
    """Project monotonic lifecycle data to safe, bounded diagnostic fields."""
    if not isinstance(timing, dict):
        return {}
    result: dict[str, int | str | bool] = {}
    for key, value in timing.items():
        if key == "started":
            continue
        if isinstance(value, bool | str):
            result[key] = value[:96] if isinstance(value, str) else value
        elif isinstance(value, int | float):
            result[key] = max(0, min(int(value), 3_600_000))
    return result


def _public_event_trace(timing: Any) -> dict[str, int | str | bool]:
    if not isinstance(timing, dict):
        return {}
    result: dict[str, int | str | bool] = {}
    for key, value in timing.items():
        if key == "trace_event_count":
            if isinstance(value, int | float):
                result[key] = max(0, min(int(value), 10_000))
            continue
        if not key.startswith("trace_"):
            continue
        action = safe_trace_action(key[6:])
        safe_key = f"trace_{action}"
        if isinstance(value, bool | str):
            result[safe_key] = value[:96] if isinstance(value, str) else value
        elif isinstance(value, int | float):
            result[safe_key] = max(0, min(int(value), 3_600_000))
    return result
