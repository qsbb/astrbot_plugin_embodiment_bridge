from __future__ import annotations

import asyncio
import time
import types
import uuid
from typing import Any

from ..core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
    FastActionFeedback,
    VerifiedActionFacts,
)
from ..core.avatar_action_tool import (
    MODEL_TOOL_SOURCE,
    read_selected_intent,
    read_selected_source,
    stage_explicit_action,
)
from ..core.explicit_action_parser import requires_text_reply
from ..core.plugin_identity import (
    BRIDGE_ACTION_FACTS,
    BRIDGE_EVENT_MARKER,
    BRIDGE_FAST_ACTION_ACTIVE,
    BRIDGE_FAST_ACTION_EXPLICIT,
    BRIDGE_FAST_ACTION_FEEDBACK,
    BRIDGE_FAST_ACTION_SELECTED,
    BRIDGE_IDENTITY_CONTEXT,
    BRIDGE_PROTECTED_CONTEXT_AUTHORIZED,
    BRIDGE_SPATIAL_CONTEXT,
    BRIDGE_SUPPORTED_ACTIONS,
    BRIDGE_TEXT_REPLY_REQUIRED,
    LEGACY_BRIDGE_EVENT_MARKER,
    LEGACY_BRIDGE_IDENTITY_CONTEXT,
)
from ..core.session_manager import SPATIAL_CONTEXT_TTL_SECONDS, SessionState


class MessagePipelineUnavailable(RuntimeError):
    pass


class MessagePipelineEmpty(RuntimeError):
    pass


class AstrBotMessagePipelineAdapter:
    """Submit an authorized embodied-client utterance to AstrBot's EventBus."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        platform_id: str = "",
        timeout_seconds: float = 90.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = bool(enabled)
        self.platform_id = str(platform_id or "").strip()
        self.timeout_seconds = min(max(float(timeout_seconds), 10.0), 180.0)
        self.status = "enabled" if self.enabled else "disabled"
        self.last_error = ""
        self.last_duration_ms = 0
        self.last_event_woken: bool | None = None
        self.last_event_stopped: bool | None = None
        self.last_send_observed: bool | None = None

    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.platform_id:
            return "trusted_platform_not_configured"
        try:
            self.context.get_event_queue
            platform_getter = self.context.get_platform_inst
        except AttributeError:
            return "astrbot_event_api_unavailable"
        try:
            platform = platform_getter(self.platform_id)
            if platform is None:
                return "trusted_platform_unavailable"
            if not callable(platform.create_event):
                return "astrbot_event_factory_unavailable"
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return "astrbot_event_factory_unavailable"
        return "ready"

    def configure_platform(self, platform_id: str) -> None:
        self.platform_id = str(platform_id or "").strip()
        self.status = "enabled" if self.enabled else "disabled"

    async def generate(
        self,
        *,
        session: SessionState,
        user_text: str,
        fast_action_active: bool = False,
        fast_action_feedback: dict[str, object] | None = None,
        action_facts: list[dict[str, Any]] | None = None,
    ) -> ModelDecision:
        started = time.perf_counter()
        self.last_error = ""
        self.last_event_woken = None
        self.last_event_stopped = None
        self.last_send_observed = None
        if not self.enabled:
            self.last_error = "message_pipeline_disabled"
            raise MessagePipelineUnavailable("message_pipeline_disabled")
        if not session.protected_context_authorized:
            self.last_error = "protected_context_not_authorized"
            raise MessagePipelineUnavailable("protected_context_not_authorized")
        if not self.platform_id:
            self.last_error = "trusted_platform_not_configured"
            raise MessagePipelineUnavailable("trusted_platform_not_configured")

        try:
            platform_getter = self.context.get_platform_inst
            queue_getter = self.context.get_event_queue
        except AttributeError:
            self.last_error = "astrbot_event_api_unavailable"
            raise MessagePipelineUnavailable("astrbot_event_api_unavailable")
        platform = platform_getter(self.platform_id)
        if platform is None:
            self.last_error = "trusted_platform_unavailable"
            raise MessagePipelineUnavailable("trusted_platform_unavailable")
        try:
            event_factory = platform.create_event
        except AttributeError as exc:
            self.last_error = "astrbot_event_factory_unavailable"
            raise MessagePipelineUnavailable(
                "astrbot_event_factory_unavailable"
            ) from exc
        if not callable(event_factory):
            self.last_error = "astrbot_event_factory_unavailable"
            raise MessagePipelineUnavailable("astrbot_event_factory_unavailable")

        event = _build_capture_event(
            platform=platform,
            platform_meta=platform.meta(),
            user_text=user_text,
            user_id=session.user_id,
            bot_id=session.bot_id,
            group_id=session.group_id,
            protected_context_authorized=session.protected_context_authorized,
            spatial_context=_session_spatial_context(session),
            fast_action_active=fast_action_active,
            fast_action_feedback=fast_action_feedback,
            action_facts=action_facts,
            supported_actions=getattr(session, "supported_actions", None),
        )
        try:
            queue_getter().put_nowait(event)
        except (AttributeError, asyncio.QueueFull, RuntimeError) as exc:
            self.status = "queue_unavailable"
            self.last_error = "astrbot_event_queue_unavailable"
            raise MessagePipelineUnavailable("astrbot_event_queue_unavailable") from exc

        self.status = "processing"
        try:
            await asyncio.wait_for(event.wait_completed(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            self.status = "timeout"
            self.last_error = "astrbot_pipeline_timeout"
            raise MessagePipelineUnavailable("astrbot_pipeline_timeout") from exc

        self._record_event_outcome(event)
        text_reply_required = requires_text_reply(user_text)
        selected_intent = read_selected_intent(event)
        reply = event.captured_text().strip()
        if not reply:
            reply = _event_result_text(event).strip()
            if reply:
                self._record_reply_recovered(source="event_result")
        if not reply:
            reply = _delivery_plan_text(event).strip()
            if reply:
                self._record_reply_recovered(source="delivery_plan")
        if not reply and text_reply_required:
            self.status = "empty_reply"
            self.last_error = "astrbot_pipeline_reply_required_missing"
            self._record_required_reply_missing()
            raise MessagePipelineEmpty(self.last_error)
        # A tool-only action is still a valid turn. AstrBot may finish after
        # executing the action tool without emitting a textual assistant reply;
        # do not discard the selected dance/turn intent as an empty response.
        if not reply and selected_intent is not None:
            self.status = "ok"
            self.last_error = ""
            self.last_duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            self._record_action_outcome(
                selected_intent,
                source="selected",
                duration_ms=self.last_duration_ms,
            )
            self._record_eventbus_action_outcome(event, selected_intent)
            return ModelDecision(
                should_reply=False,
                reply_text="",
                intent=selected_intent,
            )
        fast_action_selected = bool(
            isinstance(fast_action_feedback, dict)
            and isinstance(
                fast_action_feedback.get(BRIDGE_FAST_ACTION_SELECTED),
                str,
            )
        )
        # ``stop_event()`` is also used by post-processing plugins to claim
        # delivery ownership or intentionally suppress text. If the parallel
        # fast-action path already reserved a valid action, an empty stopped
        # EventBus result is a legitimate action-only turn rather than a
        # dialogue transport failure. The orchestrator owns and delivers the
        # reserved intent; this placeholder is never emitted as a second one.
        if not reply and self.last_event_stopped is True and fast_action_selected:
            self.status = "ok"
            self.last_error = ""
            self.last_duration_ms = max(
                0, int((time.perf_counter() - started) * 1000)
            )
            self._record_fast_action_stopped_outcome(
                duration_ms=self.last_duration_ms,
                action_source=(
                    "explicit_request"
                    if fast_action_feedback.get("explicit_action") is True
                    else "fast_provider"
                ),
            )
            return ModelDecision(
                should_reply=False,
                reply_text="",
                intent=ProposedIntent(
                    emotion=Emotion.NEUTRAL,
                    gesture=Gesture.TALK,
                    look_at=LookAt.USER,
                    intensity=0.38,
                    duration_ms=1_200,
                    reason_code="fast_action_selected",
                ),
            )
        if not reply:
            self.status = "empty_reply"
            self.last_error = self._empty_reply_reason()
            raise MessagePipelineEmpty(self.last_error)

        self.status = "ok"
        self.last_error = ""
        self.last_duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        reply = reply[:4000]
        intent = selected_intent or ProposedIntent(
            emotion=Emotion.NEUTRAL,
            gesture=Gesture.TALK,
            look_at=LookAt.USER,
            intensity=0.38,
            duration_ms=min(8_000, max(1_200, len(reply) * 85)),
            reason_code="astrbot_message_pipeline",
        )
        self._record_action_outcome(
            intent,
            source="selected" if selected_intent is not None else "default_talk",
            duration_ms=self.last_duration_ms,
        )
        self._record_eventbus_action_outcome(event, selected_intent)
        return ModelDecision(
            should_reply=True,
            reply_text=reply,
            intent=intent,
        )

    def _record_reply_recovered(self, *, source: str) -> None:
        recorder = getattr(self.logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(
                "message_pipeline.reply_recovered",
                component="message_pipeline",
                phase="eventbus",
                status="recovered",
                result=source,
            )
        except Exception:
            return

    def _record_required_reply_missing(self) -> None:
        recorder = getattr(self.logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(
                "message_pipeline.required_reply_missing",
                component="message_pipeline",
                phase="eventbus",
                status="error",
                reason_code="astrbot_pipeline_reply_required_missing",
                reply_required=True,
            )
        except Exception:
            return

    def _record_fast_action_stopped_outcome(
        self,
        *,
        duration_ms: int,
        action_source: str,
    ) -> None:
        recorder = getattr(self.logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(
                "message_pipeline.stopped_after_fast_action",
                component="message_pipeline",
                phase="eventbus",
                status="completed",
                reason_code="fast_action_selected",
                action_source=action_source,
                duration_ms=duration_ms,
            )
        except Exception:
            return

    def _record_action_outcome(
        self,
        intent: ProposedIntent,
        *,
        source: str,
        duration_ms: int,
    ) -> None:
        recorder = getattr(self.logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(
                "avatar.action.pipeline_outcome",
                component="action",
                operation=intent.gesture.value,
                status="selected" if source == "selected" else "fallback",
                reason_code=intent.reason_code,
                gesture=intent.gesture.value,
                action_source=source,
                motion_selection=(
                    "recommended_imported"
                    if intent.gesture.value == "dance"
                    else "next_imported"
                    if intent.gesture.value == "dance_next"
                    else "none"
                ),
                duration_ms=duration_ms,
            )
        except Exception:
            return

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "availability_reason": self.availability_reason,
            "status": self.status,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
            "last_event_woken": self.last_event_woken,
            "last_event_stopped": self.last_event_stopped,
            "last_send_observed": self.last_send_observed,
            "mode": "astrbot_event_bus",
            "admin_inheritance": False,
            "server_tts_suppressed": True,
        }

    async def close(self) -> None:
        return None

    def _record_event_outcome(self, event: Any) -> None:
        self.last_event_woken = bool(
            getattr(event, "is_wake", False)
            or getattr(event, "is_at_or_wake_command", False)
        )
        stopped = getattr(event, "is_stopped", None)
        try:
            self.last_event_stopped = bool(stopped()) if callable(stopped) else False
        except Exception:
            self.last_event_stopped = None
        self.last_send_observed = bool(getattr(event, "_has_send_oper", False))

    def _record_eventbus_action_outcome(
        self,
        event: Any,
        intent: ProposedIntent | None,
    ) -> None:
        recorder = getattr(self.logger, "record", None)
        if not callable(recorder):
            return
        try:
            selected_source = read_selected_source(event)
            recorder(
                "avatar.action.eventbus_outcome",
                component="action",
                phase="eventbus",
                status="selected" if selected_source else "no_action",
                eventbus_tool_called=selected_source == MODEL_TOOL_SOURCE,
                action_source=selected_source or "none",
                operation=intent.gesture.value if intent is not None else "none",
            )
        except Exception:
            return

    def _empty_reply_reason(self) -> str:
        if self.last_event_stopped is True:
            return (
                "astrbot_pipeline_event_stopped"
                if self.last_event_woken
                else "astrbot_pipeline_not_woken"
            )
        if self.last_send_observed is True:
            return "astrbot_pipeline_reply_capture_empty"
        if self.last_event_stopped is False:
            return "astrbot_pipeline_no_response"
        return "astrbot_pipeline_empty_reply"


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
    original_cleanup = event.cleanup_temporary_local_files

    async def send(self: Any, outgoing: Any) -> None:
        self._has_send_oper = True
        _capture_message(self, outgoing, streaming=False)

    async def send_streaming(
        self: Any,
        generator: Any,
        use_fallback: bool = False,
    ) -> None:
        del use_fallback
        self._has_send_oper = True
        async for outgoing in generator:
            _capture_message(self, outgoing, streaming=True)

    async def send_typing(self: Any) -> None:
        return None

    async def stop_typing(self: Any) -> None:
        return None

    def cleanup(self: Any) -> None:
        try:
            original_cleanup()
        finally:
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
    if supported_actions is not None:
        event.set_extra(BRIDGE_SUPPORTED_ACTIONS, tuple(supported_actions))
    event.set_extra(BRIDGE_FAST_ACTION_ACTIVE, bool(fast_action_active))
    event.set_extra(BRIDGE_TEXT_REPLY_REQUIRED, requires_text_reply(user_text))
    if (
        fast_action_active
        and isinstance(fast_action_feedback, dict)
        and fast_action_feedback.get("explicit_action") is True
    ):
        event.set_extra(BRIDGE_FAST_ACTION_EXPLICIT, True)
    event.set_extra(
        BRIDGE_PROTECTED_CONTEXT_AUTHORIZED,
        bool(protected_context_authorized),
    )
    if fast_action_active and isinstance(fast_action_feedback, dict):
        snapshot = fast_action_feedback.get("snapshot")
        try:
            FastActionFeedback.model_validate(snapshot)
        except (TypeError, ValueError):
            pass
        else:
            # Keep the bounded holder by reference for same-turn action
            # arbitration. This internal coordination is independent from
            # protected persona/relationship context authorization; only the
            # prompt overlay below remains authorization-gated.
            event.set_extra(BRIDGE_FAST_ACTION_FEEDBACK, fast_action_feedback)
    if protected_context_authorized and spatial_context is not None:
        event.set_extra(BRIDGE_SPATIAL_CONTEXT, dict(spatial_context))
    if protected_context_authorized and action_facts:
        try:
            verified_facts = VerifiedActionFacts.model_validate(
                {"facts": action_facts}
            )
        except (TypeError, ValueError):
            verified_facts = None
        if verified_facts is not None and verified_facts.facts:
            event.set_extra(
                BRIDGE_ACTION_FACTS,
                verified_facts.model_dump(mode="json")["facts"],
            )
    # Deprecated compatibility markers are emitted for one major release so
    # existing series plugins can migrate without losing authorized context.
    event.set_extra(LEGACY_BRIDGE_EVENT_MARKER, True)
    stage_explicit_action(event, user_text)
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
