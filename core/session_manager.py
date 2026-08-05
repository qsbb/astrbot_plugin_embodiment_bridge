from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable

from .models import AudioChunkRequest, InteractionEvent, SessionStartRequest


CRITICAL_EVENT_TYPES = frozenset(
    {"asr.final", "avatar.intent", "reply.audio.chunk", "reply.end", "error"}
)
DROPPABLE_EVENT_TYPES = frozenset({"asr.partial", "reply.text.delta"})


class BridgeStateError(RuntimeError):
    code = "bridge_state_error"
    status_code = 400


class SessionNotFound(BridgeStateError):
    code = "session_not_found"
    status_code = 404


class SessionOwnershipError(BridgeStateError):
    code = "session_ownership_mismatch"
    status_code = 403


class SessionConflict(BridgeStateError):
    code = "session_conflict"
    status_code = 409


class AudioValidationError(BridgeStateError):
    code = "invalid_audio"
    status_code = 400


class PayloadTooLarge(BridgeStateError):
    code = "payload_too_large"
    status_code = 413


class QueueClosed(RuntimeError):
    pass


@dataclass(slots=True)
class QueueItem:
    payload: dict[str, Any]
    turn_id: str | None
    generation: int | None

    @property
    def event_type(self) -> str:
        return str(self.payload.get("type") or "")


class BoundedEventQueue:
    def __init__(self, capacity: int) -> None:
        if capacity < 4:
            raise ValueError("event queue capacity must be at least 4")
        self.capacity = capacity
        self._items: deque[QueueItem] = deque()
        self._condition = asyncio.Condition()
        self._closed = False

    async def put(
        self,
        item: QueueItem,
        *,
        still_valid: Callable[[], bool] | None = None,
    ) -> bool:
        async with self._condition:
            if self._closed or (still_valid is not None and not still_valid()):
                return False

            if item.event_type == "asr.partial":
                self._items = deque(
                    existing
                    for existing in self._items
                    if not (
                        existing.event_type == "asr.partial"
                        and existing.turn_id == item.turn_id
                    )
                )

            while len(self._items) >= self.capacity:
                if self._discard_oldest_droppable():
                    break
                if item.event_type not in CRITICAL_EVENT_TYPES:
                    return False
                await self._condition.wait()
                if self._closed or (still_valid is not None and not still_valid()):
                    return False

            if still_valid is not None and not still_valid():
                return False
            self._items.append(item)
            self._condition.notify_all()
            return True

    async def get(self) -> QueueItem:
        async with self._condition:
            while not self._items:
                if self._closed:
                    raise QueueClosed
                await self._condition.wait()
            item = self._items.popleft()
            self._condition.notify_all()
            return item

    async def discard_turn(self, turn_id: str) -> None:
        async with self._condition:
            self._items = deque(item for item in self._items if item.turn_id != turn_id)
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()

    def _discard_oldest_droppable(self) -> bool:
        for index, existing in enumerate(self._items):
            if existing.event_type in DROPPABLE_EVENT_TYPES:
                del self._items[index]
                return True
        return False

    @property
    def size(self) -> int:
        return len(self._items)


@dataclass(slots=True)
class TurnState:
    turn_id: str
    generation: int
    interaction: bool = False
    audio: bytearray = field(default_factory=bytearray)
    next_audio_sequence: int = 0
    audio_ended: bool = False
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class SessionState:
    session_id: str
    owner: str
    client_id: str
    user_id: str
    bot_id: str
    group_id: str
    relationship_profile_id: str
    queue: BoundedEventQueue
    protected_context_authorized: bool = False
    context_authorization_reason: str = "not_checked"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0
    current_turn: TurnState | None = None
    interaction_turns: dict[str, TurnState] = field(default_factory=dict)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    stream_attached: bool = False
    closed: bool = False
    history: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=20))
    interactions: deque[InteractionEvent] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    seen_event_ids: set[str] = field(default_factory=set)
    seen_event_order: deque[str] = field(default_factory=lambda: deque(maxlen=512))
    last_interaction_at: dict[tuple[str, str], float] = field(default_factory=dict)


class SessionManager:
    def __init__(
        self,
        *,
        max_sessions: int = 8,
        event_queue_size: int = 64,
        max_audio_bytes: int = 1_920_000,
        max_audio_chunk_bytes: int = 16_000,
        interaction_debounce_ms: int = 250,
        max_concurrent_interactions: int = 2,
    ) -> None:
        self.max_sessions = max(1, max_sessions)
        self.event_queue_size = max(4, event_queue_size)
        self.max_audio_bytes = max(3_200, max_audio_bytes)
        self.max_audio_chunk_bytes = max(3_200, max_audio_chunk_bytes)
        self.interaction_debounce_seconds = max(0, interaction_debounce_ms) / 1000
        self.max_concurrent_interactions = max(1, min(max_concurrent_interactions, 8))
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._terminated = False

    async def start_session(
        self,
        request: SessionStartRequest,
        owner: str,
        *,
        protected_context_authorized: bool = False,
        context_authorization_reason: str = "not_checked",
    ) -> SessionState:
        async with self._lock:
            if self._terminated:
                raise SessionConflict("bridge is terminating")
            if request.session_id in self._sessions:
                raise SessionConflict("session already exists")
            if len(self._sessions) >= self.max_sessions:
                raise SessionConflict("session limit reached")
            session = SessionState(
                session_id=request.session_id,
                owner=owner,
                client_id=request.client_id,
                user_id=request.user_id,
                bot_id=request.bot_id,
                group_id=request.group_id,
                relationship_profile_id=request.relationship_profile_id,
                queue=BoundedEventQueue(self.event_queue_size),
                protected_context_authorized=protected_context_authorized,
                context_authorization_reason=str(
                    context_authorization_reason or "not_checked"
                )[:128],
            )
            self._sessions[request.session_id] = session
            return session

    async def get_owned(self, session_id: str, owner: str) -> SessionState:
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed:
            raise SessionNotFound("session not found")
        if session.owner != owner:
            raise SessionOwnershipError("session owner does not match")
        return session

    async def begin_turn(
        self,
        session: SessionState,
        turn_id: str,
        *,
        cancel_previous: bool,
    ) -> TurnState:
        old_turn: TurnState | None
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            old_turn = session.current_turn
            if (
                old_turn is not None and old_turn.turn_id == turn_id
            ) or turn_id in session.interaction_turns:
                raise SessionConflict("turn already exists")
            if old_turn is not None and not cancel_previous:
                raise SessionConflict("another turn is active")
            session.generation += 1
            turn = TurnState(turn_id=turn_id, generation=session.generation)
            session.current_turn = turn
            if old_turn is not None and old_turn.task is not None:
                old_turn.task.cancel()
        if old_turn is not None:
            await session.queue.discard_turn(old_turn.turn_id)
        return turn

    async def begin_interaction_turn(
        self,
        session: SessionState,
        turn_id: str,
    ) -> TurnState:
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            if (
                session.current_turn is not None
                and session.current_turn.turn_id == turn_id
            ) or turn_id in session.interaction_turns:
                raise SessionConflict("turn already exists")
            if len(session.interaction_turns) >= self.max_concurrent_interactions:
                raise SessionConflict("interaction concurrency limit reached")
            session.generation += 1
            turn = TurnState(
                turn_id=turn_id,
                generation=session.generation,
                interaction=True,
            )
            session.interaction_turns[turn_id] = turn
            return turn

    async def assign_task(
        self,
        session: SessionState,
        turn: TurnState,
        task: asyncio.Task[None],
    ) -> bool:
        async with session.lock:
            if not self._is_current_unlocked(session, turn.turn_id, turn.generation):
                task.cancel()
                return False
            turn.task = task
            session.tasks.add(task)
            task.add_done_callback(session.tasks.discard)
            return True

    async def add_audio_chunk(
        self,
        session: SessionState,
        request: AudioChunkRequest,
    ) -> int:
        try:
            decoded = base64.b64decode(request.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AudioValidationError("audio data is not valid base64") from exc
        if not decoded or len(decoded) % 2:
            raise AudioValidationError("PCM16 data must contain complete samples")
        if len(decoded) > self.max_audio_chunk_bytes:
            raise PayloadTooLarge("audio chunk exceeds configured limit")

        async with session.lock:
            turn = session.current_turn
            if turn is None or turn.turn_id != request.turn_id:
                raise SessionConflict("audio belongs to a stale turn")
            if turn.audio_ended:
                raise SessionConflict("audio stream already ended")
            if request.sequence != turn.next_audio_sequence:
                raise AudioValidationError("audio sequence is not contiguous")
            if len(turn.audio) + len(decoded) > self.max_audio_bytes:
                raise PayloadTooLarge("turn audio exceeds configured limit")
            turn.audio.extend(decoded)
            turn.next_audio_sequence += 1
            return len(turn.audio)

    async def end_audio(
        self, session: SessionState, turn_id: str
    ) -> tuple[TurnState, bytes]:
        async with session.lock:
            turn = session.current_turn
            if turn is None or turn.turn_id != turn_id:
                raise SessionConflict("audio belongs to a stale turn")
            if turn.audio_ended:
                raise SessionConflict("audio stream already ended")
            if not turn.audio:
                raise AudioValidationError("audio stream is empty")
            turn.audio_ended = True
            audio = bytes(turn.audio)
            turn.audio.clear()
            return turn, audio

    async def record_interaction(
        self,
        session: SessionState,
        interaction: InteractionEvent,
    ) -> bool:
        async with session.lock:
            if interaction.event_id in session.seen_event_ids:
                return False
            key = (interaction.name.value, interaction.phase.value)
            now = monotonic()
            last_at = session.last_interaction_at.get(key, 0.0)
            if now - last_at < self.interaction_debounce_seconds:
                return False
            if len(session.seen_event_order) == session.seen_event_order.maxlen:
                expired = session.seen_event_order.popleft()
                session.seen_event_ids.discard(expired)
            session.seen_event_order.append(interaction.event_id)
            session.seen_event_ids.add(interaction.event_id)
            session.last_interaction_at[key] = now
            session.interactions.append(interaction)
            return True

    async def emit(
        self,
        session: SessionState,
        *,
        turn_id: str,
        generation: int,
        payload: dict[str, Any],
    ) -> bool:
        def still_valid() -> bool:
            return self._is_current_unlocked(session, turn_id, generation)

        if not still_valid():
            return False
        return await session.queue.put(
            QueueItem(payload=payload, turn_id=turn_id, generation=generation),
            still_valid=still_valid,
        )

    async def cancel_current(
        self,
        session: SessionState,
        requested_turn_id: str | None = None,
    ) -> bool:
        async with session.lock:
            turn = session.current_turn
            if requested_turn_id:
                if turn is None or requested_turn_id != turn.turn_id:
                    turn = session.interaction_turns.pop(requested_turn_id, None)
                else:
                    session.current_turn = None
            elif turn is not None:
                session.current_turn = None
            if turn is None:
                return False
            if turn.task is not None:
                turn.task.cancel()
            turn.audio.clear()
        await session.queue.discard_turn(turn.turn_id)
        return True

    async def complete_turn(
        self,
        session: SessionState,
        turn: TurnState,
    ) -> None:
        if not turn.interaction:
            return
        async with session.lock:
            current = session.interaction_turns.get(turn.turn_id)
            if current is turn:
                session.interaction_turns.pop(turn.turn_id, None)

    async def attach_stream(self, session: SessionState) -> bool:
        async with session.lock:
            if session.stream_attached or session.closed:
                return False
            session.stream_attached = True
            return True

    async def detach_stream(self, session: SessionState) -> None:
        async with session.lock:
            session.stream_attached = False

    async def append_history(self, session: SessionState, role: str, text: str) -> None:
        if role not in {"user", "assistant"} or not text:
            return
        async with session.lock:
            session.history.append({"role": role, "text": text[:8000]})

    async def history_snapshot(self, session: SessionState) -> list[dict[str, str]]:
        async with session.lock:
            return list(session.history)

    async def close_session(self, session: SessionState) -> None:
        async with self._lock:
            self._sessions.pop(session.session_id, None)
        async with session.lock:
            session.closed = True
            turn = session.current_turn
            session.current_turn = None
            interaction_turns = list(session.interaction_turns.values())
            session.interaction_turns.clear()
            tasks = list(session.tasks)
            for task in tasks:
                task.cancel()
            if turn is not None:
                turn.audio.clear()
            for interaction_turn in interaction_turns:
                interaction_turn.audio.clear()
            session.history.clear()
            session.interactions.clear()
            session.seen_event_ids.clear()
            session.seen_event_order.clear()
        await session.queue.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def terminate(self) -> None:
        async with self._lock:
            self._terminated = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        tasks: set[asyncio.Task[None]] = set()
        for session in sessions:
            async with session.lock:
                session.closed = True
                turn = session.current_turn
                session.current_turn = None
                interaction_turns = list(session.interaction_turns.values())
                session.interaction_turns.clear()
                session_tasks = list(session.tasks)
                for task in session_tasks:
                    task.cancel()
                    tasks.add(task)
                if turn is not None:
                    turn.audio.clear()
                for interaction_turn in interaction_turns:
                    interaction_turn.audio.clear()
                session.history.clear()
                session.interactions.clear()
            await session.queue.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def is_current(self, session: SessionState, turn_id: str, generation: int) -> bool:
        return self._is_current_unlocked(session, turn_id, generation)

    @staticmethod
    def _is_current_unlocked(
        session: SessionState,
        turn_id: str,
        generation: int,
    ) -> bool:
        if session.closed:
            return False
        current = session.current_turn
        if (
            current is not None
            and current.turn_id == turn_id
            and current.generation == generation
        ):
            return True
        interaction = session.interaction_turns.get(turn_id)
        return bool(interaction is not None and interaction.generation == generation)

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            sessions = list(self._sessions.values())
        return {
            "active_sessions": len(sessions),
            "attached_streams": sum(1 for item in sessions if item.stream_attached),
            "queued_events": sum(item.queue.size for item in sessions),
        }
