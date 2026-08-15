from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


PROTOCOL_VERSION = "1.0"
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
OptionalScope = Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Emotion(StrEnum):
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SHY = "shy"
    SURPRISED = "surprised"
    CONCERNED = "concerned"
    UNCOMFORTABLE = "uncomfortable"


class Gesture(StrEnum):
    IDLE = "idle"
    TALK = "talk"
    WAVE = "wave"
    BOW = "bow"
    HANDSHAKE = "handshake"
    HEAD_PAT = "head_pat"
    CHEEK_PINCH = "cheek_pinch"
    REFUSE = "refuse"
    STEP_BACK = "step_back"
    DANCE = "dance"
    DANCE_NEXT = "dance_next"
    RAISE_HAND = "raise_hand"
    TURN_HALF = "turn_half"
    SIT = "sit"
    LIE = "lie"
    NOD = "nod"
    SWAY = "sway"


class LookAt(StrEnum):
    USER = "user"
    HAND = "hand"
    AWAY = "away"
    NONE = "none"


class InteractionName(StrEnum):
    HANDSHAKE = "handshake"
    HEAD_PAT = "head_pat"
    CHEEK_PINCH = "cheek_pinch"
    GAZE = "gaze"
    SPEAKING = "speaking"


class InteractionPhase(StrEnum):
    START = "start"
    UPDATE = "update"
    END = "end"
    CANCEL = "cancel"


class Hand(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    BOTH = "both"
    NONE = "none"


class ActionResultStatus(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    COMPLETED = "completed"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"


class ActionResultReason(StrEnum):
    ACCEPTED = "accepted"
    STARTED = "started"
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"
    BUSY = "busy"
    BLOCKED = "blocked"
    TRACKING_LOST = "tracking_lost"
    ASSET_MISSING = "asset_missing"
    INVALID_STATE = "invalid_state"
    SUPERSEDED = "superseded"
    USER_INTERRUPTED = "user_interrupted"
    SYSTEM_INTERRUPTED = "system_interrupted"


class SessionStartRequest(StrictModel):
    type: Literal["session.start"] = "session.start"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    client_id: Identifier
    user_id: OptionalScope
    bot_id: OptionalScope
    group_id: OptionalScope = ""
    relationship_profile_id: OptionalScope = ""

    @field_validator("group_id", mode="before")
    @classmethod
    def reject_whitespace_only_group_id(cls, value: object) -> object:
        if isinstance(value, str) and value and not value.strip():
            raise ValueError("group_id must be an exact empty string or a real scope")
        return value

    @model_validator(mode="after")
    def require_relationship_scope(self) -> SessionStartRequest:
        if not self.user_id or not self.bot_id:
            raise ValueError("user_id and bot_id are required")
        return self


class TurnStartRequest(StrictModel):
    type: Literal["turn.start"] = "turn.start"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    text: str | None = Field(default=None, min_length=1, max_length=8192)
    cancel_previous: bool = True

    @field_validator("text", mode="before")
    @classmethod
    def normalize_unity_audio_turn_text(cls, value: object) -> object:
        # Unity JsonUtility versions differ on whether a null string is omitted,
        # emitted as null, or represented as an empty string. All three shapes
        # mean "await PCM audio"; whitespace-only user text remains invalid.
        if value == "":
            return None
        return value


class AudioChunkRequest(StrictModel):
    type: Literal["audio.chunk"] = "audio.chunk"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    sequence: int = Field(ge=0, le=1_000_000)
    format: Literal["pcm16"] = "pcm16"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    data: str = Field(min_length=4, max_length=90_000)


class AudioEndRequest(StrictModel):
    type: Literal["audio.end"] = "audio.end"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier


class InteractionEvent(StrictModel):
    type: Literal["interaction"] = "interaction"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    event_id: Identifier
    name: InteractionName
    phase: InteractionPhase
    strength: float = Field(ge=0.0, le=1.0)
    duration_ms: int = Field(default=0, ge=0, le=600_000)
    hand: Hand = Hand.NONE


class InterruptRequest(StrictModel):
    type: Literal["interrupt"] = "interrupt"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] = (
        "user_interrupt"
    )


class SessionCloseRequest(StrictModel):
    type: Literal["session.close"] = "session.close"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier


class ActionResultRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    type: Literal["action.result"] = "action.result"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    action_id: Identifier
    receipt_id: Identifier
    action: Gesture
    status: ActionResultStatus
    reason_code: ActionResultReason
    duration_ms: int = Field(ge=0, le=600_000)

    @field_validator("duration_ms", mode="before")
    @classmethod
    def require_exact_duration_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("duration_ms must be an integer")
        return value

    @model_validator(mode="after")
    def align_status_and_reason(self) -> ActionResultRequest:
        exact_reasons = {
            ActionResultStatus.ACCEPTED: ActionResultReason.ACCEPTED,
            ActionResultStatus.STARTED: ActionResultReason.STARTED,
            ActionResultStatus.COMPLETED: ActionResultReason.COMPLETED,
        }
        exact = exact_reasons.get(self.status)
        if exact is not None and self.reason_code is not exact:
            raise ValueError(f"{self.status.value} requires reason_code={exact.value}")
        rejected_reasons = {
            ActionResultReason.UNSUPPORTED,
            ActionResultReason.BUSY,
            ActionResultReason.BLOCKED,
            ActionResultReason.TRACKING_LOST,
            ActionResultReason.ASSET_MISSING,
            ActionResultReason.INVALID_STATE,
            ActionResultReason.SUPERSEDED,
        }
        interrupted_reasons = {
            ActionResultReason.TRACKING_LOST,
            ActionResultReason.SUPERSEDED,
            ActionResultReason.USER_INTERRUPTED,
            ActionResultReason.SYSTEM_INTERRUPTED,
        }
        if self.status is ActionResultStatus.REJECTED:
            if self.reason_code not in rejected_reasons:
                raise ValueError("rejected status requires a rejection reason_code")
        elif self.status is ActionResultStatus.INTERRUPTED:
            if self.reason_code not in interrupted_reasons:
                raise ValueError("interrupted status requires an interruption reason_code")
        return self


class VerifiedActionFact(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )

    action: Gesture
    status: Literal["completed", "rejected", "interrupted"]
    reason_code: ActionResultReason
    duration_ms: int = Field(ge=0, le=600_000)

    @model_validator(mode="after")
    def align_terminal_status_and_reason(self) -> VerifiedActionFact:
        if self.status == "completed":
            if self.reason_code is not ActionResultReason.COMPLETED:
                raise ValueError("completed fact requires reason_code=completed")
        elif self.status == "rejected":
            if self.reason_code not in {
                ActionResultReason.UNSUPPORTED,
                ActionResultReason.BUSY,
                ActionResultReason.BLOCKED,
                ActionResultReason.TRACKING_LOST,
                ActionResultReason.ASSET_MISSING,
                ActionResultReason.INVALID_STATE,
                ActionResultReason.SUPERSEDED,
            }:
                raise ValueError("rejected fact requires a rejection reason_code")
        elif self.reason_code not in {
            ActionResultReason.TRACKING_LOST,
            ActionResultReason.SUPERSEDED,
            ActionResultReason.USER_INTERRUPTED,
            ActionResultReason.SYSTEM_INTERRUPTED,
        }:
            raise ValueError("interrupted fact requires an interruption reason_code")
        return self


class VerifiedActionFacts(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[VerifiedActionFact, ...] = Field(max_length=8)


class SpatialContextRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        strict=True,
    )

    session_id: Identifier
    schema_version: Literal[1]
    revision: int = Field(ge=0)
    floor_count: int = Field(ge=0, le=64)
    seat_count: int = Field(ge=0, le=64)
    bed_count: int = Field(ge=0, le=64)
    table_count: int = Field(ge=0, le=64)
    wall_count: int = Field(ge=0, le=64)
    door_count: int = Field(ge=0, le=64)
    window_count: int = Field(ge=0, le=64)
    scene_capture_available: bool
    occlusion_available: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version_type(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value


class SpatialContextSnapshot(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        strict=True,
        frozen=True,
    )

    schema_version: Literal[1]
    revision: int = Field(ge=0)
    floor_count: int = Field(ge=0, le=64)
    seat_count: int = Field(ge=0, le=64)
    bed_count: int = Field(ge=0, le=64)
    table_count: int = Field(ge=0, le=64)
    wall_count: int = Field(ge=0, le=64)
    door_count: int = Field(ge=0, le=64)
    window_count: int = Field(ge=0, le=64)
    scene_capture_available: bool
    occlusion_available: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version_type(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @classmethod
    def from_request(cls, request: SpatialContextRequest) -> SpatialContextSnapshot:
        return cls.model_validate(request.model_dump(exclude={"session_id"}))


class ProposedIntent(StrictModel):
    emotion: Emotion = Emotion.NEUTRAL
    gesture: Gesture = Gesture.IDLE
    look_at: LookAt = LookAt.NONE
    intensity: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_ms: int = Field(default=0, ge=0, le=30_000)
    reason_code: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ] = "model_decision"


class AvatarActionCall(StrictModel):
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ]
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelDecision(StrictModel):
    should_reply: bool
    reply_text: str = Field(default="", max_length=4000)
    intent: ProposedIntent
    action: AvatarActionCall | None = None

    @model_validator(mode="after")
    def align_reply_flag(self) -> ModelDecision:
        if self.should_reply and not self.reply_text.strip():
            raise ValueError("reply_text is required when should_reply is true")
        if not self.should_reply and self.reply_text:
            self.reply_text = ""
        return self


class AvatarIntent(StrictModel):
    type: Literal["avatar.intent"] = "avatar.intent"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    action_id: Identifier | None = None
    in_reply_to_event_id: Identifier | None = None
    emotion: Emotion
    gesture: Gesture
    look_at: LookAt
    intensity: float = Field(ge=0.0, le=1.0)
    duration_ms: int = Field(ge=0, le=30_000)
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


def safe_neutral_decision(reason_code: str = "invalid_model_output") -> ModelDecision:
    return ModelDecision(
        should_reply=False,
        reply_text="",
        intent=ProposedIntent(
            emotion=Emotion.NEUTRAL,
            gesture=Gesture.IDLE,
            look_at=LookAt.NONE,
            intensity=0.0,
            duration_ms=0,
            reason_code=reason_code,
        ),
    )
