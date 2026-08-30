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
)
from ..core.avatar_action_tool import (
    MODEL_TOOL_SOURCE,
    read_selected_intent,
    read_selected_source,
)
from ..core.explicit_action_parser import requires_text_reply
from ..core.plugin_identity import (
    BRIDGE_EVENT_MARKER,
    BRIDGE_FAST_ACTION_SELECTED,
    BRIDGE_CAPTURE_REQUIRED,
    BRIDGE_DELIVERY_OWNER,
    BRIDGE_IDENTITY_CONTEXT,
    BRIDGE_PROTECTED_CONTEXT_AUTHORIZED,
    BRIDGE_SPATIAL_CONTEXT,
    BRIDGE_TEXT_REPLY_REQUIRED,
    LEGACY_BRIDGE_EVENT_MARKER,
    LEGACY_BRIDGE_IDENTITY_CONTEXT,
)
from ..core.session_manager import SPATIAL_CONTEXT_TTL_SECONDS, SessionState
from ..core.timing_trace import TimingTrace, safe_trace_action


class MessagePipelineUnavailable(RuntimeError):
    pass


class MessagePipelineEmpty(RuntimeError):
    pass


DELIVERY_VISIBILITY_VALUES = frozenset(
    {"captured", "result_recovered", "plan_recovered", "action_only", "unobserved"}
)


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
        diagnostic_log: Any | None = None,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = bool(enabled)
        self.platform_id = str(platform_id or "").strip()
        self.timeout_seconds = min(max(float(timeout_seconds), 10.0), 180.0)
        self.diagnostic_log = diagnostic_log
        self.status = "enabled" if self.enabled else "disabled"
        self.last_error = ""
        self.last_duration_ms = 0
        self.last_event_woken: bool | None = None
        self.last_event_wake_match: bool | None = None
        self.last_event_processed: bool | None = None
        self.last_event_stopped: bool | None = None
        self.last_send_observed: bool | None = None
        self.last_event_class = ""
        self.last_captured_chars = 0
        self.last_result_chars = 0
        self.last_delivery_plan_chars = 0
        self.last_result_chain_count = 0
        self.last_selected_intent = "none"
        self.last_delivery_visibility = "unobserved"
        self.last_delivery_visibility_reason = ""
        self.last_event_cleanup_called: bool | None = None
        self.last_event_timing: dict[str, int | str | bool] = {}
        self.last_event_trace: dict[str, int | str | bool] = {}
        self._current_event: Any | None = None
        self._status_generation = 0
        self._status_generation_counter = 0

    def _begin_status_generation(self) -> int:
        self._status_generation_counter += 1
        self._status_generation = self._status_generation_counter
        return self._status_generation

    def _publish_status(self, generation: int, **fields: Any) -> bool:
        if generation != self._status_generation:
            return False
        for name, value in fields.items():
            setattr(self, name, value)
        return True

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
        generation = self._begin_status_generation()

        def publish(**fields: Any) -> bool:
            return self._publish_status(generation, **fields)

        visibility = "unobserved"

        def set_visibility(value: str, source: str = "") -> None:
            nonlocal visibility
            visibility = value if value in DELIVERY_VISIBILITY_VALUES else "unobserved"
            self._set_delivery_visibility(value, source, generation=generation)

        current_turn = getattr(session, "current_turn", None)
        trace_id = str(getattr(current_turn, "trace_id", "") or "")[:16]
        recorder = getattr(self.diagnostic_log, "record", None)
        shared_trace = getattr(current_turn, "timing_trace", None)
        owns_trace = not isinstance(shared_trace, TimingTrace)
        trace = (
            shared_trace
            if isinstance(shared_trace, TimingTrace)
            else TimingTrace(
                self.diagnostic_log,
                enabled=bool(getattr(self.diagnostic_log, "enabled", False)),
                trace_id=trace_id,
            )
        )

        async def close_owned_trace() -> None:
            if owns_trace:
                await trace.close()

        # Keep lifecycle state in this invocation's closure.  The adapter can
        # serve concurrent sessions; a shared timing dictionary would let one
        # event overwrite another event's spans.
        timing: dict[str, int | str | bool] | None = None

        def stage(event: str, *, status: str, **fields: Any) -> None:
            current_timing = timing
            if isinstance(current_timing, dict):
                elapsed_ms = max(
                    0,
                    int(
                        (time.perf_counter() - float(current_timing["started"]))
                        * 1000
                    ),
                )
                fields.setdefault("elapsed_ms", elapsed_ms)
                current_timing[event] = elapsed_ms
                if event == "event_cleanup_called":
                    publish(
                        last_event_cleanup_called=True,
                        last_event_timing=_public_event_timing(current_timing),
                    )
                    trace.finish_eventbus(status="completed")
            if not callable(recorder):
                return
            try:
                recorder(
                    event,
                    component="eventbus",
                    phase="pipeline",
                    status=status,
                    trace_id=trace_id,
                    **fields,
                )
            except Exception:
                return

        publish(
            last_error="",
            last_event_woken=None,
            last_event_wake_match=None,
            last_event_processed=None,
            last_event_stopped=None,
            last_send_observed=None,
            last_event_class="",
            last_captured_chars=0,
            last_result_chars=0,
            last_delivery_plan_chars=0,
            last_result_chain_count=0,
            last_selected_intent="none",
            last_delivery_visibility="unobserved",
            last_delivery_visibility_reason="",
            last_event_cleanup_called=None,
            last_event_timing={},
            last_event_trace={},
        )
        if not self.enabled:
            publish(last_error="message_pipeline_disabled")
            raise MessagePipelineUnavailable("message_pipeline_disabled")
        if not session.protected_context_authorized:
            publish(last_error="protected_context_not_authorized")
            raise MessagePipelineUnavailable("protected_context_not_authorized")
        if not self.platform_id:
            publish(last_error="trusted_platform_not_configured")
            raise MessagePipelineUnavailable("trusted_platform_not_configured")

        try:
            platform_getter = self.context.get_platform_inst
            queue_getter = self.context.get_event_queue
        except AttributeError:
            await close_owned_trace()
            publish(last_error="astrbot_event_api_unavailable")
            raise MessagePipelineUnavailable("astrbot_event_api_unavailable")
        try:
            platform = platform_getter(self.platform_id)
        except BaseException:
            await close_owned_trace()
            raise
        if platform is None:
            await close_owned_trace()
            publish(last_error="trusted_platform_unavailable")
            raise MessagePipelineUnavailable("trusted_platform_unavailable")
        try:
            event_factory = platform.create_event
        except AttributeError as exc:
            await close_owned_trace()
            publish(last_error="astrbot_event_factory_unavailable")
            raise MessagePipelineUnavailable(
                "astrbot_event_factory_unavailable"
            ) from exc
        if not callable(event_factory):
            await close_owned_trace()
            publish(last_error="astrbot_event_factory_unavailable")
            raise MessagePipelineUnavailable("astrbot_event_factory_unavailable")

        # The shared turn trace is started by the orchestrator.  A direct
        # adapter invocation in tests/older callers owns its local trace.
        trace.start_event_loop_monitor()

        try:
            with trace.span("eventbus.event_create", kind="eventbus"):
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
                    # ``action_facts`` remains an accepted compatibility
                    # argument, but verified receipts are local controller
                    # state and never enter a main EventBus request.
                    action_facts=None,
                    # Client capability declarations are consumed by the
                    # local action controller.  Do not copy them into the
                    # synthetic EventBus event where downstream LLM hooks
                    # could treat them as an action prompt.
                    supported_actions=None,
                )
        except BaseException:
            await close_owned_trace()
            raise
        timing = {
            "started": time.perf_counter(),
            "event_class": type(event).__name__[:96],
        }
        event._quest_bridge_stage = stage
        event._quest_bridge_timing = timing
        event._quest_bridge_trace = trace
        event._quest_bridge_metadata = _event_metadata_snapshot(event)
        self._current_event = event
        _install_trace_probe(event, stage)
        stage(
            "event_created",
            status="created",
            event_type="message.event",
            **event._quest_bridge_metadata,
        )
        try:
            queue = queue_getter()
        except BaseException:
            await close_owned_trace()
            raise
        queue_size_before = _queue_size(queue)
        trace.start_named_span(
            "eventbus.queue_wait",
            kind="eventbus",
            category="queue",
        )
        try:
            with trace.span("eventbus.enqueue", kind="eventbus"):
                queue.put_nowait(event)
        except asyncio.CancelledError:
            trace.finish_eventbus(status="cancelled")
            await close_owned_trace()
            raise
        except Exception as exc:
            trace.finish_eventbus(status="error", fallback=True)
            await close_owned_trace()
            publish(status="queue_unavailable")
            publish(last_error="astrbot_event_queue_unavailable")
            raise MessagePipelineUnavailable("astrbot_event_queue_unavailable") from exc
        stage(
            "event_enqueued",
            status="queued",
            event_type="message.event",
            queue_size_before=queue_size_before,
            queue_size_after=_queue_size(queue),
            queue_class=type(queue).__name__[:96],
        )

        publish(status="processing")
        try:
            # This marker separates queue admission from the scheduler's
            # completion path. It lets the client distinguish an EventBus
            # wait from STT/LLM/TTS work when a turn is slow.
            stage("event_wait_started", status="waiting", event_type="message.event")
            await asyncio.wait_for(event.wait_completed(), timeout=self.timeout_seconds)
            publish(
                last_event_cleanup_called=bool(
                    getattr(event, "_quest_cleanup_called", False)
                )
            )
            stage("event_wait_completed", status="completed", event_type="message.event")
            stage("event_woken", status="completed", event_type="message.event")
        except TimeoutError as exc:
            publish(status="timeout")
            publish(last_error="astrbot_pipeline_timeout")
            cleanup_called = bool(getattr(event, "_quest_cleanup_called", False))
            publish(last_event_cleanup_called=cleanup_called)
            stage(
                "event_wait_timeout",
                status="timeout",
                event_type="message.event",
                reason_code=(
                    "pipeline_pending"
                    if cleanup_called
                    else "not_consumed_or_scheduler_missing"
                ),
            )
            trace.finish_eventbus(status="timeout", timeout=True)
            set_visibility("unobserved", "external_direct_send_or_empty")
            raise MessagePipelineUnavailable("astrbot_pipeline_timeout") from exc
        finally:
            stage("event_cleanup_entered", status="entered", event_type="message.event")
            if bool(getattr(event, "_quest_cleanup_called", False)):
                # Some AstrBot releases set the cleanup marker but do not
                # expose a TraceSpan callback.  The marker is still enough to
                # close the queue/processing spans accurately.
                trace.finish_eventbus(status="completed")
            # If the event did not complete cleanly (timeout / cancellation),
            # abort it so that late send() / send_streaming() calls inside
            # AstrBot's event loop do not produce valid replies for a turn
            # that the orchestrator already abandoned.
            if not event._quest_done.is_set():
                _abort_synthetic_event(
                    event,
                    reason="pipeline_timeout_or_cancelled",
                    stage=stage,
                )
            self._current_event = None
            publish(
                last_event_timing=_public_event_timing(timing),
                last_event_trace=_public_event_trace(timing),
            )
            await close_owned_trace()

        event_outcome = self._record_event_outcome(event, generation=generation)
        stage("event_completed", status="ok", event_type="message.event")
        text_reply_required = requires_text_reply(user_text)
        selected_intent = event_outcome["selected_intent"]
        reply = event.captured_text().strip()
        if not reply:
            reply = _event_result_text(event).strip()
            if reply:
                self._record_reply_recovered(source="event_result")
                set_visibility("result_recovered", "event_result")
        if not reply:
            reply = _delivery_plan_text(event).strip()
            if reply:
                self._record_reply_recovered(source="delivery_plan")
                set_visibility("plan_recovered", "delivery_plan")
        if not reply and text_reply_required:
            publish(
                status="empty_reply",
                last_error="astrbot_pipeline_reply_required_missing",
            )
            set_visibility("unobserved", "external_direct_send_or_empty")
            self._record_required_reply_missing()
            raise MessagePipelineEmpty("astrbot_pipeline_reply_required_missing")
        # A tool-only action is still a valid turn. AstrBot may finish after
        # executing the action tool without emitting a textual assistant reply;
        # do not discard the selected dance/turn intent as an empty response.
        if not reply and selected_intent is not None:
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            publish(
                status="ok",
                last_error="",
                last_duration_ms=duration_ms,
                last_event_timing=_public_event_timing(timing),
                last_event_trace=_public_event_trace(timing),
            )
            self._record_action_outcome(
                selected_intent,
                source="selected",
                duration_ms=duration_ms,
            )
            set_visibility("action_only", "selected_action")
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
        if not reply and event_outcome["event_stopped"] is True and fast_action_selected:
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            publish(
                status="ok",
                last_error="",
                last_duration_ms=duration_ms,
                last_event_timing=_public_event_timing(timing),
                last_event_trace=_public_event_trace(timing),
            )
            self._record_fast_action_stopped_outcome(
                duration_ms=duration_ms,
                action_source=(
                    "explicit_request"
                    if fast_action_feedback.get("explicit_action") is True
                    else "fast_provider"
                ),
            )
            set_visibility("action_only", "fast_action_selected")
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
            publish(status="empty_reply")
            set_visibility("unobserved", "external_direct_send_or_empty")
            empty_reason = self._empty_reply_reason(generation=generation)
            publish(last_error=empty_reason)
            raise MessagePipelineEmpty(empty_reason)

        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        publish(
            status="ok",
            last_error="",
            last_duration_ms=duration_ms,
            last_event_timing=_public_event_timing(timing),
            last_event_trace=_public_event_trace(timing),
        )
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
            duration_ms=duration_ms,
        )
        if visibility == "unobserved":
            if event_outcome["send_observed"] is True:
                set_visibility("captured", "event_send")
            else:
                set_visibility(
                    "unobserved", "external_direct_send_or_empty"
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

    def _set_delivery_visibility(
        self,
        visibility: str,
        source: str = "",
        *,
        generation: int | None = None,
    ) -> None:
        """Record only whether this adapter can observe its own delivery.

        External plugins may call platform/context send APIs which this synthetic
        event cannot safely intercept. Those paths remain ``unobserved`` rather
        than being guessed or globally monkeypatched.
        """
        if visibility not in DELIVERY_VISIBILITY_VALUES:
            visibility = "unobserved"
        reason = (
            "external_direct_send_or_empty" if visibility == "unobserved" else ""
        )
        if generation is None:
            self.last_delivery_visibility = visibility
            self.last_delivery_visibility_reason = reason
        else:
            self._publish_status(
                generation,
                last_delivery_visibility=visibility,
                last_delivery_visibility_reason=reason,
            )
        recorder = getattr(self.diagnostic_log, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(
                "message_pipeline.delivery_visibility",
                component="message_pipeline",
                phase="eventbus",
                status=visibility,
                reason_code=reason,
                source=source
                if source
                in {
                    "event_send",
                    "event_result",
                    "delivery_plan",
                    "selected_action",
                    "fast_action_selected",
                    "external_direct_send_or_empty",
                }
                else "none",
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
            "last_event_wake_match": self.last_event_wake_match,
            "last_event_processed": self.last_event_processed,
            "last_event_stopped": self.last_event_stopped,
            "last_send_observed": self.last_send_observed,
            "last_event_class": self.last_event_class,
            "last_captured_chars": self.last_captured_chars,
            "last_result_chars": self.last_result_chars,
            "last_delivery_plan_chars": self.last_delivery_plan_chars,
            "last_result_chain_count": self.last_result_chain_count,
            "last_selected_intent": self.last_selected_intent,
            "last_delivery_visibility": self.last_delivery_visibility,
            "last_delivery_visibility_reason": self.last_delivery_visibility_reason,
            "last_event_cleanup_called": self.last_event_cleanup_called,
            "last_event_timing": dict(self.last_event_timing),
            "last_event_trace": dict(self.last_event_trace),
            "delivery_owner": "embodiment_bridge",
            "capture_required": True,
            "decision_path": "astrbot_event_bus",
            "mode": "astrbot_event_bus",
            "admin_inheritance": False,
            "server_tts_suppressed": True,
        }

    async def close(self) -> None:
        return None

    def abort_current_event(self, reason: str = "aborted") -> None:
        """Terminate the synthetic event currently owned by this adapter.

        Safe to call when no event is in flight (no-op).  The orchestrator
        calls this on EventBus timeout, turn interrupt, and session close
        so that late ``send()`` / ``send_streaming()`` calls from
        AstrBot's event loop do not produce valid replies.
        """
        event = self._current_event
        if event is None:
            return
        _abort_synthetic_event(event, reason=reason)

    def _record_event_outcome(
        self,
        event: Any,
        *,
        generation: int | None = None,
    ) -> dict[str, Any]:
        wake_match = bool(
            getattr(event, "is_wake", False)
            or getattr(event, "is_at_or_wake_command", False)
        )
        # Keep the legacy field for protocol compatibility. It is a wake/@
        # match, not a claim that EventBus processing happened.
        event_class = type(event).__name__[:96]
        stopped = getattr(event, "is_stopped", None)
        try:
            event_stopped = bool(stopped()) if callable(stopped) else False
        except Exception:
            event_stopped = None
        send_observed = bool(getattr(event, "_has_send_oper", False))
        try:
            captured = event.captured_text() if callable(getattr(event, "captured_text", None)) else ""
        except Exception:
            captured = ""
        captured_chars = len(captured.strip()) if isinstance(captured, str) else 0
        result_text = _event_result_text(event)
        plan_text = _delivery_plan_text(event)
        result_chars = len(result_text.strip())
        plan_chars = len(plan_text.strip())
        selected_intent = read_selected_intent(event)
        selected_intent_name = (
            selected_intent.gesture.value
            if selected_intent is not None
            else "none"
        )
        result_chain_count = 0
        try:
            result = event.get_result() if callable(getattr(event, "get_result", None)) else None
            chain = getattr(result, "chain", None)
            result_chain_count = len(chain) if isinstance(chain, (list, tuple)) else 0
        except Exception:
            result_chain_count = 0
        outcome = {
            "event_wake_match": wake_match,
            "event_stopped": event_stopped,
            "send_observed": send_observed,
            "event_class": event_class,
            "captured_chars": captured_chars,
            "result_chars": result_chars,
            "delivery_plan_chars": plan_chars,
            "selected_intent": selected_intent,
            "selected_intent_name": selected_intent_name,
            "result_chain_count": result_chain_count,
            "delivery_visibility": self.last_delivery_visibility,
        }
        if generation is not None:
            self._publish_status(
                generation,
                last_event_wake_match=wake_match,
                last_event_woken=wake_match,
                last_event_processed=True,
                last_event_stopped=event_stopped,
                last_send_observed=send_observed,
                last_event_class=event_class,
                last_captured_chars=captured_chars,
                last_result_chars=result_chars,
                last_delivery_plan_chars=plan_chars,
                last_selected_intent=selected_intent_name,
                last_result_chain_count=result_chain_count,
            )
        return outcome

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

    def _empty_reply_reason(self, *, generation: int | None = None) -> str:
        if generation is not None and generation != self._status_generation:
            return "astrbot_pipeline_empty_reply"
        if self.last_event_stopped is True:
            return (
                "astrbot_pipeline_not_woken"
                if self.last_event_processed is False
                else "astrbot_pipeline_event_stopped"
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
