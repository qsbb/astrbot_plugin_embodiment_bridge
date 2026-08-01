from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from ..adapters.astrbot_llm import DecisionGenerator
from ..adapters.relationship import RelationshipSnapshotAdapter
from ..adapters.stt import AdapterUnavailable, STTAdapter
from ..adapters.tts import (
    OUTPUT_CHANNELS,
    OUTPUT_FORMAT,
    OUTPUT_SAMPLE_RATE,
    TTSAdapter,
)
from .interaction_policy import InteractionPolicy
from .models import (
    PROTOCOL_VERSION,
    InteractionEvent,
    ModelDecision,
    TurnStartRequest,
)
from .session_manager import SessionManager, SessionState, TurnState


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
        output_chunk_ms: int = 50,
    ) -> None:
        self.sessions = sessions
        self.llm = llm
        self.stt = stt
        self.tts = tts
        self.relationship = relationship
        self.policy = policy
        self.logger = logger
        self.output_chunk_ms = min(max(output_chunk_ms, 40), 100)

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
        if request.text:
            await self._launch(
                session,
                turn,
                lambda: self._run_text_turn(session, turn, request.text or ""),
            )
        return turn

    async def finish_audio(self, session: SessionState, turn_id: str) -> TurnState:
        turn, pcm16 = await self.sessions.end_audio(session, turn_id)
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
        turn = await self.sessions.begin_turn(
            session,
            turn_id,
            cancel_previous=True,
        )
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
            await operation()

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
        try:
            text = await self.stt.transcribe(pcm16, sample_rate=16_000)
            if not text.strip():
                await self._emit_error(
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
            raise
        except AdapterUnavailable:
            await self._emit_error(
                session,
                turn,
                "stt_unavailable",
                "PCM16 STT is not configured",
            )
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] STT turn failed: error_type=%s", type(exc).__name__
            )
            await self._emit_error(
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
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] text turn failed: error_type=%s", type(exc).__name__
            )
            await self._emit_error(
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
            await self._emit_error(
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
        history = await self.sessions.history_snapshot(session)
        relationship = await self._read_relationship(session)
        decision = await self.llm.generate(
            user_text=user_text,
            history=history,
            interaction=interaction,
            relationship=relationship,
        )
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
        if text and self.tts.available:
            try:
                async for pcm_chunk in self._normalized_audio_chunks(
                    self.tts.synthesize(text, emotion=intent.emotion.value)
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
                    if not accepted and not self.sessions.is_current(
                        session, turn.turn_id, turn.generation
                    ):
                        return
                    audio_sent = audio_sent or accepted
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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

        await self._emit(
            session,
            turn,
            {
                "type": "reply.end",
                "status": "completed",
                "text_sent": bool(text),
                "audio_sent": audio_sent,
            },
        )

    async def _read_relationship(
        self,
        session: SessionState,
    ) -> dict[str, Any] | None:
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
            return_exceptions=True,
        )
