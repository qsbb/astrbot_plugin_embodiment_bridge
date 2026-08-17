from __future__ import annotations

import asyncio
import base64
import re
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from ..adapters.astrbot_llm import DecisionGenerator
from ..adapters.astrbot_pipeline import (
    AstrBotMessagePipelineAdapter,
    MessagePipelineEmpty,
    MessagePipelineUnavailable,
)
from ..adapters.environment import CachedEnvironmentAdapter
from ..adapters.fast_action import FastActionDecisionAdapter, FastActionUnavailable
from ..adapters.identity import (
    ProtectedContextDecision,
    QuestSessionAuthorizationAdapter,
)
from ..adapters.knowledge import GlobalKnowledgeAdapter
from ..adapters.relationship import RelationshipSnapshotAdapter
from ..adapters.runtime import SeriesRuntimeAdapter
from ..adapters.stt import AdapterUnavailable, STTAdapter
from ..adapters.tts import (
    OUTPUT_CHANNELS,
    OUTPUT_FORMAT,
    OUTPUT_SAMPLE_RATE,
    TTSAdapter,
)
from ..adapters.voice_hub_tts import VoiceHubTTSAdapter
from .avatar_skills import AvatarSkillRegistry
from .explicit_action_parser import parse_explicit_action
from .interaction_policy import InteractionPolicy
from .models import (
    PROTOCOL_VERSION,
    ActionSource,
    AvatarIntent,
    Emotion,
    Gesture,
    InteractionEvent,
    LookAt,
    ModelDecision,
    ProposedIntent,
    SessionStartRequest,
    TurnStartRequest,
)
from .plugin_identity import (
    BRIDGE_FAST_ACTION_EVENT_SELECTED,
    BRIDGE_FAST_ACTION_SELECTED,
)
from .session_manager import SessionManager, SessionState, TurnState


_PUBLIC_PIPELINE_REASONS = frozenset(
    {
        "api_principal_missing",
        "astrbot_event_api_unavailable",
        "astrbot_event_factory_invalid",
        "astrbot_event_factory_unavailable",
        "astrbot_event_queue_unavailable",
        "astrbot_pipeline_empty_reply",
        "astrbot_pipeline_event_stopped",
        "astrbot_pipeline_no_response",
        "astrbot_pipeline_not_woken",
        "astrbot_pipeline_reply_capture_empty",
        "astrbot_pipeline_reply_required_missing",
        "astrbot_pipeline_timeout",
        "authorization_denied",
        "authorization_error",
        "authorization_invalid_response",
        "authorization_timeout",
        "client_id_mismatch",
        "contract_incompatible",
        "identity_adapter_unavailable",
        "invalid_api_principal",
        "invalid_bot_id",
        "invalid_client_id",
        "invalid_platform_id",
        "invalid_user_id",
        "local_api_principal_mismatch",
        "local_identity_not_configured",
        "local_quest_identity_mismatch",
        "message_pipeline_disabled",
        "missing_api_principal",
        "missing_bot_id",
        "missing_client_id",
        "missing_platform_id",
        "missing_user_id",
        "owner_not_configured",
        "provider_unavailable",
        "quest_identity_not_allowlisted",
        "trusted_client_id_missing",
        "trusted_identity_config_invalid",
        "trusted_platform_id_missing",
        "trusted_platform_not_configured",
        "trusted_platform_unavailable",
    }
)

_SAME_TURN_ACTION_COMPLETION_CLAIM = re.compile(
    r"(?:"
    r"我(?:已经|刚刚)?(?:做完|完成|蹲好|蹲完|转完|跳完|挥完)(?:了|啦|咯)?"
    r"|动作(?:已经)?完成(?:了|啦)?"
    r"|(?:i(?:'ve| have)?\s+)?(?:finished|completed|done)(?:\s+it)?\b"
    r")",
    re.IGNORECASE,
)

# Bridge deadline leaves transport/headset margin before the 35-second client budget.
EVENTBUS_TERMINAL_DEADLINE_SECONDS = 29.0


class TurnOrchestrator:
    def __init__(
        self,
        *,
        sessions: SessionManager,
        llm: DecisionGenerator,
        stt: STTAdapter,
        tts: TTSAdapter,
        relationship: RelationshipSnapshotAdapter,
        policy: InteractionPolicy,
        logger: Any,
        identity: QuestSessionAuthorizationAdapter | None = None,
        knowledge: GlobalKnowledgeAdapter | None = None,
        environment: CachedEnvironmentAdapter | None = None,
        runtime: SeriesRuntimeAdapter | None = None,
        voice_audio: VoiceHubTTSAdapter | None = None,
        message_pipeline: AstrBotMessagePipelineAdapter | None = None,
        fast_action: FastActionDecisionAdapter | None = None,
        allow_direct_provider_fallback: bool = True,
        output_chunk_ms: int = 50,
        diagnostic_log: Any | None = None,
        server_timing_enabled: bool = False,
        eventbus_terminal_deadline_seconds: float = EVENTBUS_TERMINAL_DEADLINE_SECONDS,
    ) -> None:
        self.sessions = sessions
        self.llm = llm
        self.stt = stt
        self.tts = tts
        self.relationship = relationship
        self.policy = policy
        self.logger = logger
        self.identity = identity
        self.knowledge = knowledge
        self.environment = environment
        self.runtime = runtime
        self.voice_audio = voice_audio
        self.message_pipeline = message_pipeline
        self.fast_action = fast_action
        self.allow_direct_provider_fallback = bool(allow_direct_provider_fallback)
        self.diagnostic_log = diagnostic_log
        self.server_timing_enabled = bool(server_timing_enabled)
        self.eventbus_terminal_deadline_seconds = min(
            34.0, max(0.01, float(eventbus_terminal_deadline_seconds))
        )
        self.output_chunk_ms = min(max(output_chunk_ms, 40), 100)

    async def authorize_session(
        self,
        owner: str,
        request: SessionStartRequest,
    ) -> ProtectedContextDecision:
        started = time.perf_counter()
        try:
            if self.identity is None:
                decision = ProtectedContextDecision(
                    False, "identity_adapter_unavailable"
                )
            else:
                decision = await self.identity.authorize(
                    api_principal=owner,
                    declared_client_id=request.client_id,
                    bot_id=request.bot_id,
                    user_id=request.user_id,
                    group_id=request.group_id,
                )
            self._diagnostic(
                "session.authorization",
                component="identity",
                phase="session_start",
                status="authorized" if decision.authorized else "blocked",
                authorized=decision.authorized,
                reason_code=decision.reason,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return decision
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._diagnostic(
                "session.authorization_error",
                component="identity",
                phase="session_start",
                status="error",
                reason_code="authorization_error",
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise

    async def start_turn(
        self,
        session: SessionState,
        request: TurnStartRequest,
    ) -> TurnState:
        turn = await self.sessions.begin_turn(
            session,
            request.turn_id,
            cancel_previous=request.cancel_previous,
        )
        self._diagnostic(
            "turn.accepted",
            component="turn",
            phase="text" if request.text else "audio",
            status="processing" if request.text else "awaiting_audio",
        )
        if request.text:
            await self._launch(
                session,
                turn,
                lambda: self._run_text_turn(session, turn, request.text or ""),
            )
        return turn

    async def finish_audio(self, session: SessionState, turn_id: str) -> TurnState:
        turn, pcm16 = await self.sessions.end_audio(session, turn_id)
        self._diagnostic(
            "audio.received",
            component="audio_input",
            phase="upload_complete",
            status="ok",
            bytes=len(pcm16),
        )
        await self._launch(
            session,
            turn,
            lambda: self._run_audio_turn(session, turn, pcm16),
        )
        return turn

    async def submit_interaction(
        self,
        session: SessionState,
        interaction: InteractionEvent,
    ) -> TurnState | None:
        accepted = await self.sessions.record_interaction(session, interaction)
        if not accepted:
            return None
        turn_id = f"i:{interaction.event_id}"[:64]
        turn = await self.sessions.begin_interaction_turn(session, turn_id)
        await self._launch(
            session,
            turn,
            lambda: self._run_interaction_turn(session, turn, interaction),
        )
        return turn

    async def _launch(
        self,
        session: SessionState,
        turn: TurnState,
        operation: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        gate = asyncio.Event()

        async def runner() -> None:
            await gate.wait()
            turn.server_timing.start_processing()
            try:
                await operation()
            finally:
                await self.sessions.complete_turn(session, turn)

        task = asyncio.create_task(
            runner(),
            name=f"embodiment-bridge:{session.session_id}:{turn.turn_id}",
        )
        assigned = await self.sessions.assign_task(session, turn, task)
        gate.set()
        if not assigned:
            await asyncio.gather(task, return_exceptions=True)

    async def _run_audio_turn(
        self,
        session: SessionState,
        turn: TurnState,
        pcm16: bytes,
    ) -> None:
        started = time.perf_counter()
        turn.server_timing.start_stt()
        try:
            self._diagnostic(
                "stt.started",
                component="stt",
                phase="transcribe",
                status="processing",
                bytes=len(pcm16),
            )
            text = await self.stt.transcribe(pcm16, sample_rate=16_000)
            turn.server_timing.finish_stt()
            self._diagnostic(
                "stt.completed",
                component="stt",
                status="ok",
                available=True,
                duration_ms=(time.perf_counter() - started) * 1000,
                bytes=len(pcm16),
            )
            if not text.strip():
                await self._emit_terminal_error(
                    session, turn, "stt_empty", "Speech was not recognized"
                )
                return
            emitted = await self._emit(
                session,
                turn,
                {
                    "type": "asr.final",
                    "text": text,
                },
            )
            if not emitted:
                return
            await self._run_reply_with_fast_action(
                session,
                turn,
                text,
                lambda: self._decide_and_deliver(
                    session, turn, text, interaction=None
                ),
            )
        except asyncio.CancelledError:
            self._diagnostic(
                "turn_cancelled", component="turn", phase="audio", status="cancelled", trace_id=turn.trace_id
            )
            turn.server_timing.finish_stt()
            raise
        except MessagePipelineEmpty as exc:
            await self._emit_pipeline_empty_error(session, turn, exc, phase="voice")
        except MessagePipelineUnavailable as exc:
            reason = self._public_pipeline_reason(session, exc)
            self._diagnostic(
                "message_pipeline.blocked",
                component="message_pipeline",
                status="blocked",
                reason_code=reason,
            )
            await self._emit_terminal_error(
                session,
                turn,
                reason,
                self._pipeline_error_message(reason),
            )
        except AdapterUnavailable:
            turn.server_timing.finish_stt()
            self._diagnostic(
                "stt.error",
                component="stt",
                code="stt_unavailable",
                error_type="AdapterUnavailable",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            await self._emit_terminal_error(
                session,
                turn,
                "stt_unavailable",
                "PCM16 STT is not configured",
            )
        except Exception as exc:
            turn.server_timing.finish_stt()
            self._diagnostic(
                "stt.error",
                component="stt",
                code="stt_failed",
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            self.logger.warning(
                "[embodiment-bridge] STT turn failed: error_type=%s",
                type(exc).__name__,
            )
            await self._emit_terminal_error(
                session, turn, "stt_failed", "Speech recognition failed"
            )

    async def _run_text_turn(
        self,
        session: SessionState,
        turn: TurnState,
        text: str,
    ) -> None:
        try:
            await self._run_reply_with_fast_action(
                session,
                turn,
                text,
                lambda: self._decide_and_deliver(
                    session, turn, text, interaction=None
                ),
            )
        except asyncio.CancelledError:
            self._diagnostic(
                "turn_cancelled",
                component="turn",
                phase="text",
                status="cancelled",
                trace_id=turn.trace_id,
            )
            raise
        except MessagePipelineEmpty as exc:
            await self._emit_pipeline_empty_error(session, turn, exc, phase="text")
        except MessagePipelineUnavailable as exc:
            reason = self._public_pipeline_reason(session, exc)
            self._diagnostic(
                "message_pipeline.blocked",
                component="message_pipeline",
                phase="eventbus",
                status="blocked",
                reason_code=reason,
            )
            self.logger.warning(
                "[embodiment-bridge] AstrBot message pipeline unavailable: reason=%s",
                reason,
            )
            await self._emit_terminal_error(
                session,
                turn,
                reason,
                self._pipeline_error_message(reason),
            )
        except Exception as exc:
            self._diagnostic(
                "turn.error",
                component="turn",
                phase="text",
                status="error",
                code="turn_failed",
                error_type=type(exc).__name__,
            )
            self.logger.warning(
                "[embodiment-bridge] text turn failed: error_type=%s",
                type(exc).__name__,
            )
            await self._emit_terminal_error(
                session, turn, "turn_failed", "Turn generation failed"
            )

    async def _run_interaction_turn(
        self,
        session: SessionState,
        turn: TurnState,
        interaction: InteractionEvent,
    ) -> None:
        fact = (
            f"interaction={interaction.name.value}; phase={interaction.phase.value}; "
            f"strength={interaction.strength:.3f}; duration_ms={interaction.duration_ms}; "
            f"hand={interaction.hand.value}"
        )
        try:
            await self._decide_and_deliver(
                session,
                turn,
                fact,
                interaction=interaction,
            )
        except asyncio.CancelledError:
            self._diagnostic(
                "turn_cancelled",
                component="turn",
                phase="interaction",
                status="cancelled",
                trace_id=turn.trace_id,
            )
            raise
        except Exception as exc:
            self.logger.warning(
                "[embodiment-bridge] interaction turn failed: error_type=%s",
                type(exc).__name__,
            )
            await self._emit_terminal_error(
                session,
                turn,
                "interaction_failed",
                "Interaction decision failed",
            )

    async def _run_reply_with_fast_action(
        self,
        session: SessionState,
        turn: TurnState,
        user_text: str,
        operation: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Run the reply path while an optional action-only LLM runs beside it.

        The action task never owns reply text, EventBus dispatch, history, STT,
        or TTS. When it is active, it races the request-scoped EventBus action
        tool through a one-action reservation; the regular reply path keeps
        streaming while the selector races, then settles one intent before
        ``reply.end``.
        """
        fast_task: asyncio.Task[None] | None = None
        adapter = self.fast_action
        explicit = parse_explicit_action(user_text)
        explicit_action = (
            explicit.action
            if explicit.status == "matched" and explicit.action is not None
            else None
        )
        autonomous_fallback = AvatarSkillRegistry.autonomous_fallback(
            user_text,
            session.supported_actions,
        )
        fast_provider_configured = bool(
            str(getattr(adapter, "provider_id", "") or "").strip()
        )
        if explicit_action is not None:
            turn.fast_action_active = True
            turn.fast_action_feedback["explicit_action"] = True
            if self.sessions.supports_action(session, explicit_action):
                self._set_fast_action_feedback(turn, status="processing")
                fast_task = asyncio.create_task(
                    self._run_fast_action(
                        session,
                        turn,
                        user_text,
                        explicit_action=explicit_action,
                    ),
                    name=(
                        "embodiment-bridge:explicit-action:"
                        f"{session.session_id}:{turn.turn_id}"
                    ),
                )
                turn.fast_action_task = fast_task
            else:
                self._set_fast_action_feedback(
                    turn,
                    status="unsupported",
                    action=Gesture(explicit_action),
                )
                self._diagnostic(
                    "fast_action.explicit_unsupported",
                    component="action",
                    phase="capability",
                    status="rejected",
                    operation=explicit_action,
                    reason_code="client_action_unsupported",
                    action_source="explicit_request",
                )
        elif (
            adapter is not None
            and bool(getattr(adapter, "enabled", False))
            and (
                adapter.available
                or (
                    fast_provider_configured
                    and autonomous_fallback is not None
                )
            )
        ):
            turn.fast_action_active = True
            self._set_fast_action_feedback(turn, status="processing")
            fast_task = asyncio.create_task(
                self._run_fast_action(
                    session,
                    turn,
                    user_text,
                    autonomous_fallback=autonomous_fallback,
                ),
                name=f"embodiment-bridge:fast-action:{session.session_id}:{turn.turn_id}",
            )
            turn.fast_action_task = fast_task
        completed = False
        try:
            await operation()
            completed = True
        finally:
            # A failed/cancelled reply must not leave an action behind. On a
            # successful reply, the detached task is allowed to finish while
            # the same session remains current; its errors are self-contained.
            if fast_task is not None and not completed and not fast_task.done():
                fast_task.cancel()
                await asyncio.gather(fast_task, return_exceptions=True)

    async def _run_fast_action(
        self,
        session: SessionState,
        turn: TurnState,
        user_text: str,
        *,
        explicit_action: str | None = None,
        autonomous_fallback: ProposedIntent | None = None,
    ) -> None:
        adapter = self.fast_action
        fast_provider_configured = bool(
            str(getattr(adapter, "provider_id", "") or "").strip()
        )
        if (
            explicit_action is None
            and (adapter is None or not adapter.available)
            and (
                autonomous_fallback is None
                or not fast_provider_configured
            )
        ):
            return
        started = time.perf_counter()
        self._diagnostic(
            "fast_action.started",
            component="action",
            phase="parallel",
            status="processing",
        )
        try:
            explicit = parse_explicit_action(user_text)
            explicit_intent = (
                AvatarSkillRegistry.invoke(explicit_action, {})
                if explicit_action is not None
                else None
            )
            provider_failure = ""
            action_source = "fast_provider"
            if explicit_intent is not None:
                proposed = explicit_intent.model_copy(
                    update={"reason_code": "explicit_request"}
                )
                action_source = "explicit_request"
                self._diagnostic(
                    "fast_action.explicit_selected",
                    component="action",
                    phase="parallel",
                    status="selected",
                    operation=proposed.gesture.value,
                    reason_code=explicit.reason,
                    action_source=action_source,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            elif explicit.status in {"rejected", "ambiguous"}:
                proposed = None
                action_source = "none"
                self._diagnostic(
                    "fast_action.explicit_rejected",
                    component="action",
                    phase="parallel",
                    status="no_action",
                    reason_code=explicit.reason,
                    action_source=action_source,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            else:
                if adapter is None:
                    return
                if not adapter.available:
                    availability = str(
                        getattr(adapter, "availability_reason", "unavailable")
                        or "unavailable"
                    )[:48]
                    provider_failure = f"fast_action_{availability}"
                    self._diagnostic(
                        "fast_action.provider_unavailable",
                        component="action",
                        phase="parallel",
                        status="unavailable",
                        reason_code=provider_failure,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                    proposed = None
                else:
                    history = await self.sessions.history_snapshot(session)
                    try:
                        proposed = await adapter.decide(
                            user_text=user_text,
                            history=history,
                            supported_actions=session.supported_actions,
                        )
                    except FastActionUnavailable as exc:
                        provider_failure = (
                            str(exc)[:64] or "fast_action_unavailable"
                        )
                        self._diagnostic(
                            "fast_action.provider_unavailable",
                            component="action",
                            phase="parallel",
                            status="unavailable",
                            reason_code=provider_failure,
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )
                        proposed = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        provider_failure = "fast_action_failed"
                        self._diagnostic(
                            "fast_action.provider_error",
                            component="action",
                            phase="parallel",
                            status="error",
                            reason_code=provider_failure,
                            error_type=type(exc).__name__,
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )
                        proposed = None
                if proposed is None:
                    local_fallback = autonomous_fallback or (
                        AvatarSkillRegistry.autonomous_fallback(
                            user_text,
                            session.supported_actions,
                        )
                    )
                    if local_fallback is not None:
                        proposed = local_fallback
                        action_source = "local_context_fallback"
                        self._diagnostic(
                            "fast_action.local_fallback_selected",
                            component="action",
                            phase="fallback",
                            status="selected",
                            operation=proposed.gesture.value,
                            reason_code=proposed.reason_code,
                            action_source=action_source,
                            provider_status=(
                                "unavailable" if provider_failure else "no_action"
                            ),
                            duration_ms=(time.perf_counter() - started) * 1000,
                        )
            duration_ms = (time.perf_counter() - started) * 1000
            if proposed is None:
                self._set_fast_action_feedback(
                    turn,
                    status="unavailable" if provider_failure else "no_action",
                )
                self._diagnostic(
                    "fast_action.completed",
                    component="action",
                    phase="parallel",
                    status="no_action",
                    reason_code=provider_failure or "no_action",
                    duration_ms=duration_ms,
                )
                return
            if not self.sessions.supports_action(session, proposed.gesture):
                self._set_fast_action_feedback(
                    turn,
                    status="unsupported",
                    action=proposed.gesture,
                )
                self._diagnostic(
                    "fast_action.unsupported",
                    component="action",
                    phase="capability",
                    status="rejected",
                    operation=proposed.gesture.value,
                    reason_code="client_action_unsupported",
                    action_source=action_source,
                    duration_ms=duration_ms,
                )
                return
            if not self.sessions.is_current(session, turn.turn_id, turn.generation):
                self._diagnostic(
                    "fast_action.skipped",
                    component="action",
                    phase="parallel",
                    status="cancelled",
                    reason_code="turn_not_current",
                    operation=proposed.gesture.value,
                    duration_ms=duration_ms,
                )
                return
            if turn.reply_ended:
                self._diagnostic(
                    "fast_action.skipped",
                    component="action",
                    phase="parallel",
                    status="superseded",
                    reason_code="reply_already_completed",
                    operation=proposed.gesture.value,
                    duration_ms=duration_ms,
                )
                return
            # Text/TTS may already be streaming. EventBus and the fast selector
            # coordinate through the shared reservation holder below.
            if turn.intent_emitted:
                self._diagnostic(
                    "fast_action.skipped",
                    component="action",
                    phase="parallel",
                    status="superseded",
                    reason_code="reply_path_selected",
                    operation=proposed.gesture.value,
                    duration_ms=duration_ms,
                )
                return
            eventbus_action = turn.fast_action_feedback.get(
                BRIDGE_FAST_ACTION_EVENT_SELECTED
            )
            if isinstance(eventbus_action, str):
                self._diagnostic(
                    "fast_action.deferred",
                    component="action",
                    phase="arbitration",
                    status="superseded",
                    reason_code="eventbus_action_selected",
                    operation=proposed.gesture.value,
                    result=eventbus_action,
                    duration_ms=duration_ms,
                    action_source=action_source,
                )
                return
            # EventBus tool execution and this reservation are synchronous on
            # the owning event loop. Whichever source reserves first wins; the
            # other source fails closed before any avatar.intent is emitted.
            turn.fast_action_feedback[BRIDGE_FAST_ACTION_SELECTED] = (
                proposed.gesture.value
            )
            turn.fast_action_selected = True
            turn.fast_action_source = action_source
            turn.intent_emitted = True
            turn.primary_intent_gesture = proposed.gesture.value
            decision = ModelDecision(
                should_reply=False,
                reply_text="",
                intent=proposed,
            )
            intent = self.policy.apply(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                decision=decision,
                interaction=None,
                relationship=None,
                action_source=(
                    ActionSource.EXPLICIT_REQUEST
                    if action_source == "explicit_request"
                    else ActionSource.FALLBACK
                    if action_source == "local_context_fallback"
                    else ActionSource.FAST_PROVIDER
                ),
            )
            intent, emitted = await self._emit_avatar_intent(session, turn, intent)
            turn.fast_action_intent = intent
            self._diagnostic(
                "avatar.intent.emitted" if emitted else "avatar.intent.dropped",
                component="action",
                phase="delivery",
                status="planned" if emitted else "dropped",
                operation=intent.gesture.value,
                reason_code=(intent.reason_code if emitted else "turn_not_current"),
                action_source=action_source,
                gesture=intent.gesture.value,
            )
            if not emitted:
                turn.fast_action_feedback.pop(BRIDGE_FAST_ACTION_SELECTED, None)
                turn.fast_action_selected = False
                turn.intent_emitted = False
                turn.fast_action_intent = None
                self._set_fast_action_feedback(turn, status="error")
            else:
                self._set_fast_action_feedback(
                    turn,
                    status="planned",
                    action=intent.gesture,
                )
            self._diagnostic(
                "fast_action.completed",
                component="action",
                phase="parallel",
                status="selected" if emitted else "dropped",
                operation=intent.gesture.value,
                reason_code=intent.reason_code if emitted else "intent_emit_failed",
                duration_ms=duration_ms,
                action_source=action_source,
            )
        except asyncio.CancelledError:
            self._diagnostic(
                "turn_cancelled", component="turn", phase="action", status="cancelled", trace_id=turn.trace_id
            )
            raise
        except Exception as exc:
            self._set_fast_action_feedback(turn, status="error")
            self._diagnostic(
                "fast_action.error",
                component="action",
                phase="parallel",
                status="error",
                reason_code="fast_action_failed",
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    async def _decide_and_deliver(
        self,
        session: SessionState,
        turn: TurnState,
        user_text: str,
        *,
        interaction: InteractionEvent | None,
    ) -> None:
        if not self.sessions.is_current(session, turn.turn_id, turn.generation):
            return
        turn.server_timing.start_decision("direct_provider")
        history = await self.sessions.history_snapshot(session)
        use_message_pipeline = bool(
            interaction is None
            and session.protected_context_authorized
            and self.message_pipeline is not None
            and self.message_pipeline.available
        )
        if use_message_pipeline:
            selected_phase = "eventbus"
            selected_status = "ready"
            selected_reason = "ready"
        elif interaction is not None:
            selected_phase = "direct_provider"
            selected_status = "fallback"
            selected_reason = "interaction_policy"
        else:
            selected_phase = "direct_provider"
            selected_status = "unavailable"
            selected_reason = self._public_pipeline_reason(
                session,
                MessagePipelineUnavailable("protected_context_not_authorized")
                if not session.protected_context_authorized
                else MessagePipelineUnavailable(
                    self.message_pipeline.availability_reason
                    if self.message_pipeline is not None
                    else "astrbot_event_api_unavailable"
                ),
            )
        turn.server_timing.start_decision(
            "astrbot_event_bus" if use_message_pipeline else "direct_provider"
        )
        self._diagnostic(
            "message_pipeline.selected",
            component="message_pipeline",
            phase=selected_phase,
            status=selected_status,
            authorized=session.protected_context_authorized,
            reason_code=selected_reason,
        )
        pipeline_required = (
            interaction is None and not self.allow_direct_provider_fallback
        )
        if pipeline_required and not use_message_pipeline:
            reason = (
                self.message_pipeline.availability_reason
                if self.message_pipeline is not None
                else "astrbot_event_api_unavailable"
            )
            self._diagnostic(
                "message_pipeline.blocked",
                component="message_pipeline",
                status="blocked",
                reason_code=reason,
            )
            raise MessagePipelineUnavailable(reason)
        relationship = await self._read_relationship(session)
        knowledge: list[dict[str, Any]] = []
        environment: dict[str, Any] | None = None
        if not use_message_pipeline:
            knowledge, environment = await asyncio.gather(
                self._read_knowledge(user_text, interaction),
                self._read_environment(),
            )
        llm_started = time.perf_counter()
        try:
            operation = "direct_provider"
            if use_message_pipeline and self.message_pipeline is not None:
                try:
                    self._diagnostic(
                        "message_pipeline.started",
                        component="message_pipeline",
                        phase="eventbus",
                        status="processing",
                    )
                    action_facts = await self.sessions.action_facts_snapshot(
                        session,
                        exclude_turn_id=turn.turn_id,
                    )
                    self._diagnostic(
                        "event_enqueued",
                        component="eventbus",
                        phase="pipeline",
                        status="queued",
                        event_type="message.event",
                        trace_id=turn.trace_id,
                    )
                    try:
                        decision = await asyncio.wait_for(
                            self.message_pipeline.generate(
                                session=session,
                                user_text=user_text,
                                fast_action_active=turn.fast_action_active,
                                fast_action_feedback=turn.fast_action_feedback,
                                action_facts=action_facts,
                            ),
                            timeout=self.eventbus_terminal_deadline_seconds,
                        )
                        self._diagnostic(
                            "event_woken",
                            component="eventbus",
                            phase="pipeline",
                            status="completed",
                            event_type="message.event",
                            trace_id=turn.trace_id,
                        )
                    except TimeoutError as exc:
                        self._diagnostic(
                            "event_completed",
                            component="eventbus",
                            phase="terminal",
                            status="timeout",
                            reason_code="astrbot_pipeline_timeout",
                            deadline_ms=self.eventbus_terminal_deadline_seconds * 1000,
                            trace_id=turn.trace_id,
                        )
                        raise MessagePipelineUnavailable("astrbot_pipeline_timeout") from exc
                    finally:
                        self._diagnostic(
                            "event_cleanup_entered",
                            component="eventbus",
                            phase="pipeline",
                            status="entered",
                            event_type="message.event",
                            trace_id=turn.trace_id,
                        )
                    operation = "astrbot_event_bus"
                    self._diagnostic(
                        "message_pipeline.completed",
                        component="message_pipeline",
                        phase="eventbus",
                        status="ok",
                        duration_ms=(time.perf_counter() - llm_started) * 1000,
                    )
                    self._diagnostic(
                        "decision_ready",
                        component="eventbus",
                        phase="decision",
                        status="ready",
                        trace_id=turn.trace_id,
                    )
                except MessagePipelineUnavailable as exc:
                    self._diagnostic(
                        "message_pipeline.fallback",
                        component="message_pipeline",
                        status=(
                            "fallback"
                            if self.allow_direct_provider_fallback
                            else "blocked"
                        ),
                        reason_code=str(exc)[:64] or "unknown",
                    )
                    if not self.allow_direct_provider_fallback:
                        raise
                    turn.server_timing.start_decision("direct_provider")
                    knowledge, environment = await asyncio.gather(
                        self._read_knowledge(user_text, interaction),
                        self._read_environment(),
                    )
                    decision = await self.llm.generate(
                        user_text=user_text,
                        history=history,
                        interaction=interaction,
                        relationship=relationship,
                        knowledge=knowledge,
                        environment=environment,
                    )
                except MessagePipelineEmpty:
                    raise
            else:
                decision = await self.llm.generate(
                    user_text=user_text,
                    history=history,
                    interaction=interaction,
                    relationship=relationship,
                    knowledge=knowledge,
                    environment=environment,
                )
            turn.server_timing.finish_decision()
            self._diagnostic(
                "llm.completed",
                component="llm",
                operation=operation,
                status="ok",
                available=True,
                duration_ms=(time.perf_counter() - llm_started) * 1000,
            )
            self._diagnostic(
                "decision.completed",
                component="llm",
                status="ok",
                result="reply" if decision.should_reply else "silent",
            )
        except (MessagePipelineUnavailable, MessagePipelineEmpty):
            turn.server_timing.finish_decision()
            raise
        except Exception as exc:
            turn.server_timing.finish_decision()
            self._diagnostic(
                "llm.error",
                component="llm",
                code="llm_failed",
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - llm_started) * 1000,
            )
            raise
        if not self.sessions.is_current(session, turn.turn_id, turn.generation):
            return
        await self.sessions.append_history(session, "user", user_text)
        await self._deliver_decision(
            session,
            turn,
            decision,
            interaction=interaction,
            relationship=relationship,
        )

    async def _deliver_decision(
        self,
        session: SessionState,
        turn: TurnState,
        decision: ModelDecision,
        *,
        interaction: InteractionEvent | None,
        relationship: dict[str, Any] | None,
    ) -> None:
        feedback_snapshot = turn.fast_action_feedback.get("snapshot")
        if (
            isinstance(feedback_snapshot, dict)
            and feedback_snapshot.get("status") == "unsupported"
        ):
            decision = decision.model_copy(
                update={
                    "should_reply": True,
                    "reply_text": "这个动作当前客户端还不支持。",
                }
            )
            self._diagnostic(
                "avatar.action.reply_corrected",
                component="action",
                phase="reply",
                status="corrected",
                operation=str(feedback_snapshot.get("action") or "unknown"),
                reason_code="client_action_unsupported",
                action_source="capability_gate",
            )
        fast_task = turn.fast_action_task
        fast_task_pending = bool(
            turn.fast_action_active
            and fast_task is not None
            and not fast_task.done()
        )
        eventbus_action_selected = isinstance(
            turn.fast_action_feedback.get(BRIDGE_FAST_ACTION_EVENT_SELECTED),
            str,
        )
        if (
            eventbus_action_selected
            and fast_task is not None
            and not fast_task.done()
        ):
            fast_task.cancel()
            fast_task_pending = False
            self._diagnostic(
                "fast_action.cancelled",
                component="action",
                phase="arbitration",
                status="superseded",
                reason_code="eventbus_action_selected",
                action_source="eventbus_tool",
            )
        if turn.fast_action_selected and turn.fast_action_intent is not None:
            # The parallel action task already emitted the only intent event.
            # Keep the regular model's reply text and TTS, but do not send a
            # second action that could overwrite the fast selection in Unity.
            intent = turn.fast_action_intent
            intent_emitted = True
            self._diagnostic(
                "avatar.intent.skipped",
                component="action",
                operation="main_reply_action",
                status="superseded",
                reason_code="fast_action_selected",
                action_source="main_reply_suppressed",
            )
        else:
            action_decision = (
                self._fast_action_reply_fallback(
                    decision,
                    reason_code=(
                        "client_action_unsupported"
                        if isinstance(
                            turn.fast_action_feedback.get("snapshot"), dict
                        )
                        and turn.fast_action_feedback["snapshot"].get("status")
                        == "unsupported"
                        else "fast_action_no_action"
                    ),
                )
                if turn.fast_action_active and not eventbus_action_selected
                else decision
            )
            intent = self.policy.apply(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                decision=action_decision,
                interaction=interaction,
                relationship=relationship,
                action_source=(
                    ActionSource.FALLBACK
                    if turn.fast_action_active and not eventbus_action_selected
                    else ActionSource.INTERACTION_POLICY
                    if interaction is not None
                    else ActionSource.EVENTBUS_TOOL
                    if eventbus_action_selected
                    or decision.intent.reason_code.startswith("skill_")
                    else ActionSource.DIRECT_MODEL
                ),
            )
            # A pending fast selector must not delay text or TTS. Keep this
            # semantic fallback only as the delivery voice/gesture metadata;
            # reserve the single avatar.intent slot after the audio pipeline
            # gives the selector a chance to finish.
            if fast_task_pending and not eventbus_action_selected:
                intent_emitted = False
                self._diagnostic(
                    "avatar.action.main_delivery_parallel",
                    component="action",
                    phase="reply",
                    status="processing",
                    reason_code="fast_action_pending",
                    action_source="fast_provider_pending",
                )
            else:
                turn.intent_emitted = True
                turn.primary_intent_gesture = intent.gesture.value
                intent, intent_emitted = await self._emit_avatar_intent(
                    session,
                    turn,
                    intent,
                )
                self._diagnostic(
                    "avatar.intent.emitted"
                    if intent_emitted
                    else "avatar.intent.dropped",
                    component="action",
                    operation=intent.gesture.value,
                    status="planned" if intent_emitted else "cancelled",
                    reason_code=(
                        intent.reason_code if intent_emitted else "turn_not_current"
                    ),
                    emotion=intent.emotion.value,
                    gesture=intent.gesture.value,
                    look_at=intent.look_at.value,
                    intensity=intent.intensity,
                    duration_ms=intent.duration_ms,
                    **(
                        {"action_source": "eventbus_tool_fallback"}
                        if eventbus_action_selected
                        else {"action_source": "fast_provider_fallback"}
                        if turn.fast_action_active
                        else {}
                    ),
                )
        if not intent_emitted and not (
            turn.fast_action_active
            and fast_task is not None
            and not fast_task.done()
        ):
            return

        text = decision.reply_text.strip() if decision.should_reply else ""
        if intent.action_id is not None and _SAME_TURN_ACTION_COMPLETION_CLAIM.search(
            text
        ):
            text = "我现在开始。"
            self._diagnostic(
                "avatar.action.reply_corrected",
                component="action",
                phase="reply",
                status="corrected",
                operation=intent.gesture.value,
                reason_code="same_turn_completion_claim",
                action_source="completion_guard",
            )
        if text:
            for chunk in self._text_chunks(text):
                accepted = await self._emit(
                    session,
                    turn,
                    {"type": "reply.text.delta", "text": chunk},
                )
                if not accepted and not self.sessions.is_current(
                    session, turn.turn_id, turn.generation
                ):
                    return
            await self.sessions.append_history(session, "assistant", text)

        audio_sent = False
        audio_bytes = 0
        audio_chunks = 0
        if text and self.tts.available:
            turn.server_timing.start_tts()
            tts_started = time.perf_counter()
            try:
                async for pcm_chunk in self._tts_pipeline(
                    text,
                    emotion=intent.emotion.value,
                ):
                    accepted = await self._emit(
                        session,
                        turn,
                        {
                            "type": "reply.audio.chunk",
                            "format": OUTPUT_FORMAT,
                            "sample_rate": OUTPUT_SAMPLE_RATE,
                            "channels": OUTPUT_CHANNELS,
                            "data": base64.b64encode(pcm_chunk).decode("ascii"),
                        },
                    )
                    if accepted:
                        turn.server_timing.mark_tts_first_chunk()
                    if not accepted and not self.sessions.is_current(
                        session, turn.turn_id, turn.generation
                    ):
                        return
                    audio_sent = audio_sent or accepted
                    if accepted:
                        audio_bytes += len(pcm_chunk)
                        audio_chunks += 1
                self._diagnostic(
                    "tts.completed",
                    component="tts",
                    status="ok",
                    available=True,
                    duration_ms=(time.perf_counter() - tts_started) * 1000,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._diagnostic(
                    "tts.error",
                    component="tts",
                    code="tts_failed",
                    error_type=type(exc).__name__,
                    duration_ms=(time.perf_counter() - tts_started) * 1000,
                )
                self.logger.warning(
                    "[embodiment-bridge] TTS failed: error_type=%s",
                    type(exc).__name__,
                )
                if not await self._emit_error(
                    session,
                    turn,
                    "tts_failed",
                    "Speech synthesis failed; text reply remains available",
                ):
                    return

            finally:
                turn.server_timing.finish_tts()

        if not intent_emitted and turn.fast_action_active:
            if fast_task is not None and not fast_task.done():
                self._diagnostic(
                    "avatar.action.reply_wait_for_arbitration",
                    component="action",
                    phase="reply_end",
                    status="processing",
                    reason_code="fast_action_deadline",
                    action_source="fast_provider_pending",
                )
                await fast_task
            if turn.fast_action_selected and turn.fast_action_intent is not None:
                intent = turn.fast_action_intent
                intent_emitted = True
                self._diagnostic(
                    "avatar.action.arbitration_winner",
                    component="action",
                    phase="arbitration",
                    status="selected",
                    operation=intent.gesture.value,
                    reason_code="fast_action_selected",
                    action_source=turn.fast_action_source,
                )
            elif self.sessions.is_current(session, turn.turn_id, turn.generation):
                fallback = self._fast_action_reply_fallback(decision)
                turn.intent_emitted = True
                turn.primary_intent_gesture = fallback.intent.gesture.value
                intent, intent_emitted = await self._emit_avatar_intent(
                    session,
                    turn,
                    self.policy.apply(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        decision=fallback,
                        interaction=interaction,
                        relationship=relationship,
                        action_source=ActionSource.FALLBACK,
                    ),
                )
                self._diagnostic(
                    "avatar.action.arbitration_winner",
                    component="action",
                    phase="arbitration",
                    status="fallback" if intent_emitted else "dropped",
                    operation=intent.gesture.value,
                    reason_code=(
                        "fast_action_no_action" if intent_emitted else "turn_not_current"
                    ),
                    action_source="fast_provider_fallback",
                )
        if not intent_emitted:
            return

        reply_end_payload = {
            "type": "reply.end",
            "status": "completed",
            "text_sent": bool(text),
            "audio_sent": audio_sent,
        }
        if self.server_timing_enabled:
            reply_end_payload["server_timing"] = turn.server_timing.snapshot()
        async with turn.terminal_lock:
            if turn.reply_ended:
                self._diagnostic(
                    "reply_end_dropped",
                    component="reply",
                    phase="terminal",
                    status="duplicate",
                    event_type="reply.end",
                    trace_id=turn.trace_id,
                )
                return
            turn.reply_ended = True
            emitted = await self._emit(session, turn, reply_end_payload)
        self._diagnostic(
            "reply_end_emitted" if emitted else "reply_end_dropped",
            component="reply",
            phase="terminal",
            status="emitted" if emitted else "dropped",
            event_type="reply.end",
            trace_id=turn.trace_id,
        )
        self._diagnostic(
            "reply.completed",
            component="reply",
            phase="delivery",
            status="completed",
            text_sent=bool(text),
            audio_sent=audio_sent,
            bytes=audio_bytes,
            chunks=audio_chunks,
        )

    @staticmethod
    def _fast_action_reply_fallback(
        decision: ModelDecision,
        *,
        reason_code: str = "fast_action_no_action",
    ) -> ModelDecision:
        """Build a non-model action when the fast selector chose no action.

        The normal reply text is preserved, but its action fields are never
        consumed while the dedicated action provider owns the turn.
        """
        text = decision.reply_text.strip() if decision.should_reply else ""
        speaking = bool(text)
        duration_ms = min(8_000, max(1_200, len(text) * 85)) if speaking else 0
        return ModelDecision(
            should_reply=decision.should_reply,
            reply_text=decision.reply_text,
            intent=ProposedIntent(
                emotion=Emotion.NEUTRAL,
                gesture=Gesture.TALK if speaking else Gesture.IDLE,
                look_at=LookAt.USER if speaking else LookAt.NONE,
                intensity=0.38 if speaking else 0.0,
                duration_ms=duration_ms,
                reason_code=reason_code,
            ),
        )

    @staticmethod
    def _public_pipeline_reason(
        session: SessionState,
        error: MessagePipelineUnavailable,
    ) -> str:
        reason = str(error or "").strip()
        if reason == "protected_context_not_authorized":
            reason = str(session.context_authorization_reason or "").strip()
        return (
            reason
            if reason in _PUBLIC_PIPELINE_REASONS
            else "astrbot_message_pipeline_unavailable"
        )

    @staticmethod
    def _pipeline_error_message(reason: str) -> str:
        if reason in {
            "local_identity_not_configured",
            "local_quest_identity_mismatch",
            "owner_not_configured",
            "quest_identity_not_allowlisted",
        }:
            return "Quest 原始用户、机器人与序的五段绑定未完成"
        if reason == "local_api_principal_mismatch":
            return "Quest 使用的 AstrBot API Key 与服务端身份绑定不一致"
        if reason in {
            "invalid_bot_id",
            "invalid_user_id",
            "missing_bot_id",
            "missing_user_id",
        }:
            return "Quest 配对身份无效，请使用真实用户与机器人 ID 重新绑定"
        if reason in {
            "client_id_mismatch",
            "invalid_client_id",
            "missing_client_id",
            "trusted_client_id_missing",
        }:
            return "具身客户端 ID 与服务端可信配置不匹配"
        if reason in {
            "missing_platform_id",
            "trusted_platform_id_missing",
            "trusted_platform_not_configured",
            "trusted_platform_unavailable",
        }:
            return "AstrBot 可信平台未配置或当前不可用"
        if reason == "astrbot_pipeline_not_woken":
            return "AstrBot 消息事件未被唤醒规则接受"
        if reason == "astrbot_pipeline_event_stopped":
            return "AstrBot 消息事件在唤醒后被白名单、会话状态或插件中止"
        if reason == "astrbot_pipeline_reply_required_missing":
            return "AstrBot 消息链未产生本轮明确要求的文字回复"
        if reason == "astrbot_pipeline_reply_capture_empty":
            return "AstrBot 已执行发送，但临未捕获到可用文字"
        if reason in {
            "astrbot_pipeline_empty_reply",
            "astrbot_pipeline_no_response",
        }:
            return "AstrBot 消息链已完成，但没有产生可用回复"
        return "AstrBot 消息链路不可用，请检查临的独立日志"

    async def _emit_pipeline_empty_error(
        self,
        session: SessionState,
        turn: TurnState,
        error: MessagePipelineEmpty,
        *,
        phase: str,
    ) -> None:
        reason = self._public_pipeline_reason(session, error)
        snapshot = (
            self.message_pipeline.status_snapshot()
            if self.message_pipeline is not None
            else {}
        )
        self._diagnostic(
            "message_pipeline.empty",
            component="message_pipeline",
            phase=phase,
            status="failed",
            reason_code=reason,
            error_type=type(error).__name__,
            event_woken=snapshot.get("last_event_woken"),
            event_stopped=snapshot.get("last_event_stopped"),
            send_observed=snapshot.get("last_send_observed"),
        )
        self.logger.warning(
            "[embodiment-bridge] AstrBot message pipeline returned no reply: reason=%s",
            reason,
        )
        await self._emit_terminal_error(
            session,
            turn,
            reason,
            self._pipeline_error_message(reason),
        )

    async def _read_relationship(
        self,
        session: SessionState,
    ) -> dict[str, Any] | None:
        if not session.protected_context_authorized:
            return None
        try:
            return await self.relationship.read(
                bot_id=session.bot_id,
                user_id=session.user_id,
                group_id=session.group_id,
                relationship_profile_id=session.relationship_profile_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "[embodiment-bridge] relationship snapshot failed: error_type=%s",
                type(exc).__name__,
            )
            return None

    async def _read_knowledge(
        self,
        user_text: str,
        interaction: InteractionEvent | None,
    ) -> list[dict[str, Any]]:
        if self.knowledge is None or interaction is not None:
            return []
        return await self.knowledge.recall(user_text)

    async def _read_environment(self) -> dict[str, Any] | None:
        if self.environment is None:
            return None
        return await self.environment.read()

    async def refresh_runtime_diagnostics(self) -> dict[str, Any]:
        if self.runtime is None:
            return {
                "contract": "update_manager.series_runtime@1.0",
                "status": "disabled",
                "reason": "",
                "members": [],
                "healthy": 0,
                "total": 0,
            }
        return await self.runtime.refresh()

    def integration_status(self) -> dict[str, Any]:
        return {
            "identity": self.identity.status_snapshot()
            if self.identity is not None
            else {
                "configured": False,
                "status": "adapter_unavailable",
                "default_access": "denied",
            },
            "knowledge": self.knowledge.status_snapshot()
            if self.knowledge is not None
            else {"enabled": False, "status": "disabled", "scope": "global"},
            "relationship": self.relationship.status_snapshot(),
            "environment": self.environment.status_snapshot()
            if self.environment is not None
            else {"enabled": False, "status": "disabled", "mode": "cached_only"},
            "voice_audio_output": self.voice_audio.status_snapshot()
            if self.voice_audio is not None
            else {"enabled": False, "available": False, "status": "disabled"},
            "astrbot_message_pipeline": self.message_pipeline.status_snapshot()
            if self.message_pipeline is not None
            else {"enabled": False, "available": False, "status": "disabled"},
            "fast_action": self._fast_action_status(),
            "runtime": self.runtime.snapshot
            if self.runtime is not None
            else {"status": "disabled"},
            "not_consumed": {
                "conversation_proactive_delivery": True,
                "orchestration_hub_resolver": True,
                "knowledge_private_scope": True,
                "environment_realtime_private_methods": True,
            },
        }

    def _fast_action_status(self) -> dict[str, Any]:
        adapter = self.fast_action
        if adapter is None:
            return {
                "enabled": False,
                "available": False,
                "availability_reason": "adapter_unavailable",
                "status": "disabled",
                "model_selected": False,
                "last_duration_ms": 0,
            }
        snapshot = adapter.snapshot()
        return {
            "enabled": snapshot.get("enabled") is True,
            "available": snapshot.get("available") is True,
            "availability_reason": str(
                snapshot.get("availability_reason") or "unavailable"
            )[:64],
            "status": str(snapshot.get("status") or "unknown")[:32],
            "model_selected": snapshot.get("selected") is True,
            "last_duration_ms": max(
                0, min(15_000, int(snapshot.get("last_duration_ms") or 0))
            ),
        }

    @staticmethod
    def _set_fast_action_feedback(
        turn: TurnState,
        *,
        status: str,
        action: Gesture | None = None,
    ) -> None:
        turn.fast_action_feedback["snapshot"] = {
            "status": status,
            "action": action.value if action is not None else None,
            "execution_confirmed": False,
        }

    async def _emit(
        self,
        session: SessionState,
        turn: TurnState,
        payload: dict[str, Any],
    ) -> bool:
        event = {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session.session_id,
            "turn_id": turn.turn_id,
            **payload,
        }
        return await self.sessions.emit(
            session,
            turn_id=turn.turn_id,
            generation=turn.generation,
            payload=event,
        )

    async def _emit_avatar_intent(
        self,
        session: SessionState,
        turn: TurnState,
        intent: AvatarIntent,
    ) -> tuple[AvatarIntent, bool]:
        if (
            intent.gesture not in {Gesture.IDLE, Gesture.TALK}
            and not self.sessions.supports_action(session, intent.gesture)
        ):
            fallback_gesture = Gesture.TALK
            parameters, transition = AvatarSkillRegistry.defaults_for(
                fallback_gesture
            )
            self._diagnostic(
                "avatar.action.unsupported",
                component="action",
                phase="capability",
                status="rejected",
                operation=intent.gesture.value,
                reason_code="client_action_unsupported",
                action_source=intent.source.value,
            )
            intent = intent.model_copy(
                update={
                    "gesture": fallback_gesture,
                    "method": fallback_gesture,
                    "parameters": parameters,
                    "transition": transition,
                    "source": ActionSource.FALLBACK,
                    "reason_code": "client_action_unsupported",
                }
            )
        action_id = await self.sessions.plan_action(
            session,
            turn_id=turn.turn_id,
            action=intent.gesture,
        )
        if action_id is not None:
            intent = intent.model_copy(update={"action_id": action_id})
        payload = intent.model_dump(mode="json")
        if action_id is None:
            payload.pop("action_id", None)
        emitted = await self._emit(session, turn, payload)
        self._diagnostic(
            "avatar_intent_emitted" if emitted else "avatar_intent_dropped",
            component="action",
            phase="delivery",
            status="emitted" if emitted else "dropped",
            event_type="avatar.intent",
            trace_id=turn.trace_id,
        )
        if not emitted:
            await self.sessions.discard_action_plan(session, action_id)
        return intent, emitted

    async def _emit_error(
        self,
        session: SessionState,
        turn: TurnState,
        code: str,
        message: str,
    ) -> bool:
        return await self._emit(
            session,
            turn,
            {"type": "error", "code": code, "message": message},
        )

    async def _emit_terminal_error(
        self,
        session: SessionState,
        turn: TurnState,
        code: str,
        message: str,
    ) -> bool:
        self._diagnostic(
            "reply.failed",
            component="reply",
            phase="terminal",
            status="failed",
            code=code,
            reason_code=code,
            text_sent=False,
            audio_sent=False,
        )
        async with turn.terminal_lock:
            if turn.reply_ended:
                self._diagnostic(
                    "reply_end_dropped",
                    component="reply",
                    phase="terminal",
                    status="duplicate",
                    event_type="reply.end",
                    trace_id=turn.trace_id,
                )
                return False
            turn.reply_ended = True
            if not await self._emit_error(session, turn, code, message):
                return False
            reply_end_payload = {
                "type": "reply.end",
                "status": "failed",
                "text_sent": False,
                "audio_sent": False,
            }
            if self.server_timing_enabled:
                reply_end_payload["server_timing"] = turn.server_timing.snapshot()
            emitted = await self._emit(session, turn, reply_end_payload)
        self._diagnostic(
            "reply_end_emitted" if emitted else "reply_end_dropped",
            component="reply",
            phase="terminal",
            status="emitted" if emitted else "dropped",
            event_type="reply.end",
            trace_id=turn.trace_id,
        )
        return emitted

    async def _normalized_audio_chunks(
        self,
        source: AsyncIterator[bytes],
    ) -> AsyncIterator[bytes]:
        bytes_per_chunk = (
            OUTPUT_SAMPLE_RATE * OUTPUT_CHANNELS * 2 * self.output_chunk_ms // 1000
        )
        buffer = bytearray()
        async for chunk in source:
            if not isinstance(chunk, bytes):
                raise TypeError("TTS adapter must yield bytes")
            buffer.extend(chunk)
            while len(buffer) >= bytes_per_chunk:
                yield bytes(buffer[:bytes_per_chunk])
                del buffer[:bytes_per_chunk]
        if len(buffer) % 2:
            raise ValueError("TTS adapter returned incomplete PCM16 samples")
        if buffer:
            yield bytes(buffer)

    async def _tts_pipeline(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        failure: list[Exception] = []

        async def produce() -> None:
            send_sentinel = True
            try:
                for segment in self._speech_segments(text):
                    source = self.tts.synthesize(segment, emotion=emotion)
                    async for chunk in self._normalized_audio_chunks(source):
                        await queue.put(chunk)
            except asyncio.CancelledError:
                send_sentinel = False
                raise
            except Exception as exc:
                failure.append(exc)
            finally:
                if send_sentinel:
                    await queue.put(None)

        producer = asyncio.create_task(
            produce(), name="embodiment-bridge:tts-producer"
        )
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    if failure:
                        raise failure[0]
                    return
                yield chunk
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    @staticmethod
    def _speech_segments(text: str, max_chars: int = 240) -> list[str]:
        segments: list[str] = []
        for match in re.finditer(r".+?(?:[。！？!?；;.\n]+|$)", text, re.DOTALL):
            sentence = match.group(0).strip()
            while sentence:
                segments.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
        return segments or ([text.strip()] if text.strip() else [])

    @staticmethod
    def _text_chunks(text: str, size: int = 80) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)]

    async def close(self) -> None:
        await self.sessions.terminate()
        await asyncio.gather(
            self.llm.close(),
            self.stt.close(),
            self.tts.close(),
            self.relationship.close(),
            *(
                adapter.close()
                for adapter in (
                    self.identity,
                    self.knowledge,
                    self.environment,
                    self.runtime,
                    self.message_pipeline,
                    self.fast_action,
                )
                if adapter is not None
            ),
            return_exceptions=True,
        )

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return

    def canonicalize_session_request(self, value: Any) -> Any:
        canonicalize = getattr(self.identity, "canonicalize_session_request", None)
        return canonicalize(value) if callable(canonicalize) else value
