from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable

from .avatar_skills import AvatarSkillRegistry
from .models import (
    ActionResultRequest,
    ActionResultStatus,
    AudioChunkRequest,
    AvatarIntent,
    Gesture,
    InteractionEvent,
    SessionStartRequest,
    SpatialContextRequest,
    SpatialContextSnapshot,
    VerifiedActionFact,
)
from .server_timing import ServerTimingState


CRITICAL_EVENT_TYPES = frozenset(
    {"asr.final", "avatar.intent", "reply.audio.chunk", "reply.end", "error"}
)
DROPPABLE_EVENT_TYPES = frozenset({"asr.partial", "reply.text.delta"})
SPATIAL_CONTEXT_TTL_SECONDS = 30.0
ACTION_LIFECYCLE_TTL_SECONDS = 300.0
ACTION_FACT_TTL_SECONDS = 300.0
MAX_ACTION_LIFECYCLES = 32
MAX_ACTION_FACTS = 8
MAX_ACTION_RECEIPTS_PER_LIFECYCLE = 8
MAX_ACTION_RECEIPTS = MAX_ACTION_LIFECYCLES * MAX_ACTION_RECEIPTS_PER_LIFECYCLE
PASSIVE_GESTURES = frozenset({Gesture.IDLE, Gesture.TALK})
TERMINAL_ACTION_STATUSES = frozenset(
    {
        ActionResultStatus.COMPLETED,
        ActionResultStatus.REJECTED,
        ActionResultStatus.INTERRUPTED,
    }
)


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


class ActionReceiptReplay(BridgeStateError):
    code = "action_receipt_replay"
    status_code = 409


class ActionPlanStale(BridgeStateError):
    code = "action_plan_stale"
    status_code = 409


class ActionMismatch(BridgeStateError):
    code = "action_mismatch"
    status_code = 409


class ActionTransitionInvalid(BridgeStateError):
    code = "action_transition_invalid"
    status_code = 409


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
    server_timing: ServerTimingState = field(default_factory=ServerTimingState)
    audio: bytearray = field(default_factory=bytearray)
    next_audio_sequence: int = 0
    audio_ended: bool = False
    task: asyncio.Task[None] | None = None
    # Optional action-only LLM task. It runs in parallel with the normal
    # reply pipeline and is cancelled with the owning turn.
    fast_action_task: asyncio.Task[None] | None = None
    fast_action_active: bool = False
    fast_action_selected: bool = False
    fast_action_intent: AvatarIntent | None = None
    fast_action_source: str = "fast_provider"
    # A bounded mutable holder is attached to the synthetic EventBus event.
    # Replacing ``snapshot`` and reserving one allowlisted action are atomic on
    # the event loop; it never exposes the task, prompt, or user text.
    fast_action_feedback: dict[str, object] = field(default_factory=dict)
    intent_emitted: bool = False
    primary_intent_gesture: str = ""
    reply_ended: bool = False


@dataclass(slots=True)
class ActionLifecycleRecord:
    action_id: str
    turn_id: str
    action: Gesture
    status: str
    created_at: float
    updated_at: float
    receipt_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ActionReceiptRecord:
    action_id: str
    signature: tuple[str, ...]
    recorded_at: float


@dataclass(frozen=True, slots=True)
class ActionFactRecord:
    action_id: str
    turn_id: str
    fact: VerifiedActionFact
    recorded_at: float


@dataclass(frozen=True, slots=True)
class ActionResultOutcome:
    action_id: str
    turn_id: str
    action: Gesture
    lifecycle_status: ActionResultStatus
    terminal: bool
    idempotent: bool


@dataclass(slots=True)
class SessionState:
    session_id: str
    owner: str
    client_id: str
    user_id: str
    bot_id: str
    group_id: str
    relationship_profile_id: str
    supported_actions: tuple[str, ...]
    supported_actions_declared: bool
    queue: BoundedEventQueue
    protected_context_authorized: bool = False
    context_authorization_reason: str = "not_checked"
    spatial_context: SpatialContextSnapshot | None = None
    spatial_context_updated_at: float = 0.0
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
    action_lifecycles: dict[str, ActionLifecycleRecord] = field(
        default_factory=dict
    )
    action_receipts: dict[str, ActionReceiptRecord] = field(default_factory=dict)
    action_facts: deque[ActionFactRecord] = field(default_factory=deque)


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
        self._accepting = True
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
            if not self._accepting:
                raise SessionConflict("bridge is not accepting new sessions")
            existing = self._sessions.get(request.session_id)
            if existing is not None:
                if not self._same_session_identity(existing, request, owner):
                    raise SessionConflict("session already exists")
                async with existing.lock:
                    if existing.closed:
                        raise SessionConflict("session is closed")
                    existing.protected_context_authorized = bool(
                        protected_context_authorized
                    )
                    existing.context_authorization_reason = str(
                        context_authorization_reason or "not_checked"
                    )[:128]
                return existing
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
                supported_actions=AvatarSkillRegistry.supported_names(
                    request.supported_actions
                ),
                supported_actions_declared=request.supported_actions is not None,
                queue=BoundedEventQueue(self.event_queue_size),
                protected_context_authorized=protected_context_authorized,
                context_authorization_reason=str(
                    context_authorization_reason or "not_checked"
                )[:128],
            )
            self._sessions[request.session_id] = session
            return session

    async def set_accepting(self, accepting: bool) -> None:
        """Atomically gate session creation during service lifecycle changes."""
        async with self._lock:
            self._accepting = bool(accepting) and not self._terminated

    @staticmethod
    def _same_session_identity(
        session: SessionState,
        request: SessionStartRequest,
        owner: str,
    ) -> bool:
        return bool(
            session.owner == owner
            and session.client_id == request.client_id
            and session.user_id == request.user_id
            and session.bot_id == request.bot_id
            and session.group_id == request.group_id
            and session.relationship_profile_id == request.relationship_profile_id
            and session.supported_actions
            == AvatarSkillRegistry.supported_names(request.supported_actions)
            and session.supported_actions_declared
            == (request.supported_actions is not None)
        )

    @staticmethod
    def supports_action(session: SessionState, action: Gesture | str) -> bool:
        normalized = AvatarSkillRegistry.normalize_action_name(action)
        return normalized is not None and normalized in session.supported_actions

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
            if old_turn is not None and old_turn.fast_action_task is not None:
                old_turn.fast_action_task.cancel()
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

    async def plan_action(
        self,
        session: SessionState,
        *,
        turn_id: str,
        action: Gesture,
    ) -> str | None:
        if action in PASSIVE_GESTURES:
            return None
        now = monotonic()
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            self._prune_action_state_unlocked(session, now)
            action_id = self._new_action_id(session)
            session.action_lifecycles[action_id] = ActionLifecycleRecord(
                action_id=action_id,
                turn_id=turn_id,
                action=action,
                status="planned",
                created_at=now,
                updated_at=now,
            )
            self._enforce_action_lifecycle_bound_unlocked(session)
            return action_id

    async def discard_action_plan(
        self,
        session: SessionState,
        action_id: str | None,
    ) -> None:
        if not action_id:
            return
        async with session.lock:
            record = session.action_lifecycles.get(action_id)
            if record is None or record.status != "planned":
                return
            self._remove_action_lifecycle_unlocked(session, action_id)

    async def record_action_result(
        self,
        session: SessionState,
        request: ActionResultRequest,
    ) -> ActionResultOutcome:
        signature = self._action_receipt_signature(request)
        now = monotonic()
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            self._prune_action_state_unlocked(session, now)
            if request.session_id != session.session_id:
                raise ActionMismatch("action result belongs to another session")
            previous_receipt = session.action_receipts.get(request.receipt_id)
            if previous_receipt is not None:
                if previous_receipt.signature != signature:
                    raise ActionReceiptReplay("receipt_id was already used")
                record = session.action_lifecycles.get(previous_receipt.action_id)
                if record is None:
                    raise ActionPlanStale("action plan is no longer available")
                return ActionResultOutcome(
                    action_id=record.action_id,
                    turn_id=record.turn_id,
                    action=record.action,
                    lifecycle_status=request.status,
                    terminal=request.status in TERMINAL_ACTION_STATUSES,
                    idempotent=True,
                )

            record = session.action_lifecycles.get(request.action_id)
            if record is None:
                raise ActionPlanStale("action plan is unknown or expired")
            if record.turn_id != request.turn_id or record.action is not request.action:
                raise ActionMismatch("action result does not match the planned action")
            if len(record.receipt_ids) >= MAX_ACTION_RECEIPTS_PER_LIFECYCLE:
                raise ActionTransitionInvalid("action receipt limit reached")
            if not self._action_transition_allowed(record.status, request.status):
                raise ActionTransitionInvalid("action lifecycle transition is invalid")

            record.status = request.status.value
            record.updated_at = now
            record.receipt_ids.add(request.receipt_id)
            session.action_receipts[request.receipt_id] = ActionReceiptRecord(
                action_id=request.action_id,
                signature=signature,
                recorded_at=now,
            )
            self._enforce_action_receipt_bound_unlocked(session)
            terminal = request.status in TERMINAL_ACTION_STATUSES
            if terminal:
                session.action_facts.append(
                    ActionFactRecord(
                        action_id=request.action_id,
                        turn_id=request.turn_id,
                        fact=VerifiedActionFact(
                            action=request.action,
                            status=request.status.value,
                            reason_code=request.reason_code,
                            duration_ms=request.duration_ms,
                        ),
                        recorded_at=now,
                    )
                )
                while len(session.action_facts) > MAX_ACTION_FACTS:
                    session.action_facts.popleft()
            return ActionResultOutcome(
                action_id=record.action_id,
                turn_id=record.turn_id,
                action=record.action,
                lifecycle_status=request.status,
                terminal=terminal,
                idempotent=False,
            )

    async def action_facts_snapshot(
        self,
        session: SessionState,
        *,
        exclude_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = monotonic()
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            self._prune_action_state_unlocked(session, now)
            return [
                item.fact.model_dump(mode="json")
                for item in session.action_facts
                if not exclude_turn_id or item.turn_id != exclude_turn_id
            ]

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
            if turn.fast_action_task is not None:
                turn.fast_action_task.cancel()
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

    async def update_spatial_context(
        self,
        session: SessionState,
        request: SpatialContextRequest,
    ) -> tuple[str, SpatialContextSnapshot]:
        if request.session_id != session.session_id:
            raise SessionConflict("spatial context belongs to another session")
        incoming = SpatialContextSnapshot.from_request(request)
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            current = session.spatial_context
            if current is None or incoming.revision > current.revision:
                session.spatial_context = incoming
                session.spatial_context_updated_at = monotonic()
                return "updated", incoming
            if incoming.revision == current.revision and incoming == current:
                session.spatial_context_updated_at = monotonic()
                return "unchanged", current
            if incoming.revision == current.revision:
                raise SessionConflict("spatial context revision content conflicts")
            raise SessionConflict("spatial context revision is stale")

    async def read_spatial_context(
        self,
        session: SessionState,
    ) -> SpatialContextSnapshot | None:
        async with session.lock:
            if session.closed:
                raise SessionNotFound("session is closed")
            if (
                session.spatial_context is not None
                and monotonic() - session.spatial_context_updated_at
                > SPATIAL_CONTEXT_TTL_SECONDS
            ):
                session.spatial_context = None
                session.spatial_context_updated_at = 0.0
            return session.spatial_context

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
                if turn.fast_action_task is not None:
                    turn.fast_action_task.cancel()
                    tasks.append(turn.fast_action_task)
            for interaction_turn in interaction_turns:
                interaction_turn.audio.clear()
                if interaction_turn.fast_action_task is not None:
                    interaction_turn.fast_action_task.cancel()
                    tasks.append(interaction_turn.fast_action_task)
            session.history.clear()
            session.spatial_context = None
            session.spatial_context_updated_at = 0.0
            session.interactions.clear()
            session.seen_event_ids.clear()
            session.seen_event_order.clear()
            session.action_lifecycles.clear()
            session.action_receipts.clear()
            session.action_facts.clear()
        await session.queue.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all_sessions(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            await self.close_session(session)

    async def terminate(self) -> None:
        async with self._lock:
            self._accepting = False
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
                    if turn.fast_action_task is not None:
                        turn.fast_action_task.cancel()
                        tasks.add(turn.fast_action_task)
                for interaction_turn in interaction_turns:
                    interaction_turn.audio.clear()
                    if interaction_turn.fast_action_task is not None:
                        interaction_turn.fast_action_task.cancel()
                        tasks.add(interaction_turn.fast_action_task)
                session.history.clear()
                session.spatial_context = None
                session.spatial_context_updated_at = 0.0
                session.interactions.clear()
                session.action_lifecycles.clear()
                session.action_receipts.clear()
                session.action_facts.clear()
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

    @staticmethod
    def _new_action_id(session: SessionState) -> str:
        while True:
            action_id = "a_" + secrets.token_hex(12)
            if action_id not in session.action_lifecycles:
                return action_id

    @staticmethod
    def _action_receipt_signature(request: ActionResultRequest) -> tuple[str, ...]:
        return (
            request.protocol_version,
            request.session_id,
            request.turn_id,
            request.action_id,
            request.receipt_id,
            request.action.value,
            request.status.value,
            request.reason_code.value,
            str(request.duration_ms),
        )

    @staticmethod
    def _action_transition_allowed(
        current: str,
        requested: ActionResultStatus,
    ) -> bool:
        if current == "planned":
            return requested in {
                ActionResultStatus.ACCEPTED,
                ActionResultStatus.REJECTED,
                ActionResultStatus.INTERRUPTED,
            }
        if current == ActionResultStatus.ACCEPTED.value:
            return requested in {
                ActionResultStatus.STARTED,
                ActionResultStatus.REJECTED,
                ActionResultStatus.INTERRUPTED,
            }
        if current == ActionResultStatus.STARTED.value:
            return requested in TERMINAL_ACTION_STATUSES
        return False

    def _prune_action_state_unlocked(
        self,
        session: SessionState,
        now: float,
    ) -> None:
        stale_ids = [
            action_id
            for action_id, record in session.action_lifecycles.items()
            if now - record.updated_at > ACTION_LIFECYCLE_TTL_SECONDS
        ]
        for action_id in stale_ids:
            self._remove_action_lifecycle_unlocked(session, action_id)
        stale_receipt_ids = [
            receipt_id
            for receipt_id, receipt in session.action_receipts.items()
            if now - receipt.recorded_at > ACTION_LIFECYCLE_TTL_SECONDS
        ]
        for receipt_id in stale_receipt_ids:
            session.action_receipts.pop(receipt_id, None)
        session.action_facts = deque(
            item
            for item in session.action_facts
            if now - item.recorded_at <= ACTION_FACT_TTL_SECONDS
        )
        while len(session.action_facts) > MAX_ACTION_FACTS:
            session.action_facts.popleft()

    def _enforce_action_lifecycle_bound_unlocked(
        self,
        session: SessionState,
    ) -> None:
        while len(session.action_lifecycles) > MAX_ACTION_LIFECYCLES:
            oldest_action_id = next(iter(session.action_lifecycles))
            self._remove_action_lifecycle_unlocked(session, oldest_action_id)

    @staticmethod
    def _enforce_action_receipt_bound_unlocked(session: SessionState) -> None:
        while len(session.action_receipts) > MAX_ACTION_RECEIPTS:
            receipt_id = next(
                (
                    candidate
                    for candidate, receipt in session.action_receipts.items()
                    if receipt.action_id not in session.action_lifecycles
                ),
                next(iter(session.action_receipts)),
            )
            session.action_receipts.pop(receipt_id, None)

    @staticmethod
    def _remove_action_lifecycle_unlocked(
        session: SessionState,
        action_id: str,
    ) -> None:
        session.action_lifecycles.pop(action_id, None)

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            sessions = list(self._sessions.values())
        return {
            "active_sessions": len(sessions),
            "attached_streams": sum(1 for item in sessions if item.stream_attached),
            "queued_events": sum(item.queue.size for item in sessions),
        }
