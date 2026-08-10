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
from .interaction_policy import InteractionPolicy
from .models import (
    PROTOCOL_VERSION,
    InteractionEvent,
    ModelDecision,
    SessionStartRequest,
    TurnStartRequest,
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
        allow_direct_provider_fallback: bool = True,
        output_chunk_ms: int = 50,
        diagnostic_log: Any | None = None,
        server_timing_enabled: bool = False,
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
        self.allow_direct_provider_fallback = bool(allow_direct_provider_fallback)
        self.diagnostic_log = diagnostic_log
        self.server_timing_enabled = bool(server_timing_enabled)
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
            name=f"quest-avatar:{session.session_id}:{turn.turn_id}",
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
            await self._decide_and_deliver(session, turn, text, interaction=None)
        except asyncio.CancelledError:
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
                "[quest-avatar] STT turn failed: error_type=%s", type(exc).__name__
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
            await self._decide_and_deliver(session, turn, text, interaction=None)
        except asyncio.CancelledError:
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
                "[quest-avatar] AstrBot message pipeline unavailable: reason=%s",
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
                "[quest-avatar] text turn failed: error_type=%s", type(exc).__name__
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
            raise
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] interaction turn failed: error_type=%s",
                type(exc).__name__,
            )
            await self._emit_terminal_error(
                session,
                turn,
                "interaction_failed",
                "Interaction decision failed",
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
                    decision = await self.message_pipeline.generate(
                        session=session,
                        user_text=user_text,
                    )
                    operation = "astrbot_event_bus"
                    self._diagnostic(
                        "message_pipeline.completed",
                        component="message_pipeline",
                        phase="eventbus",
                        status="ok",
                        duration_ms=(time.perf_counter() - llm_started) * 1000,
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
        intent = self.policy.apply(
            session_id=session.session_id,
            turn_id=turn.turn_id,
            decision=decision,
            interaction=interaction,
            relationship=relationship,
        )
        if not await self._emit(session, turn, intent.model_dump(mode="json")):
            return

        text = decision.reply_text.strip() if decision.should_reply else ""
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
                    "[quest-avatar] TTS failed: error_type=%s", type(exc).__name__
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

        reply_end_payload = {
            "type": "reply.end",
            "status": "completed",
            "text_sent": bool(text),
            "audio_sent": audio_sent,
        }
        if self.server_timing_enabled:
            reply_end_payload["server_timing"] = turn.server_timing.snapshot()
        await self._emit(
            session,
            turn,
            reply_end_payload,
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
            return "Quest 客户端 ID 与服务端可信配置不匹配"
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
            "[quest-avatar] AstrBot message pipeline returned no reply: reason=%s",
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
                "[quest-avatar] relationship snapshot failed: error_type=%s",
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
        return await self._emit(
            session,
            turn,
            reply_end_payload,
        )

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

        producer = asyncio.create_task(produce(), name="quest-avatar:tts-producer")
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
