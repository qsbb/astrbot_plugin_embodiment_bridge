from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


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


class SessionStartRequest(StrictModel):
    type: Literal["session.start"] = "session.start"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    client_id: Identifier
    user_id: OptionalScope
    bot_id: OptionalScope
    group_id: OptionalScope = ""
    relationship_profile_id: OptionalScope = ""

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
    text: str | None = Field(default=None, min_length=1, max_length=8000)
    cancel_previous: bool = True


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


class ModelDecision(StrictModel):
    should_reply: bool
    reply_text: str = Field(default="", max_length=4000)
    intent: ProposedIntent

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
