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
    RAISE_LEG = "raise_leg"
    TURN_HALF = "turn_half"
    SIT = "sit"
    LIE = "lie"
    NOD = "nod"
    SWAY = "sway"
    CROUCH = "crouch"


class ActionStyle(StrEnum):
    NATURAL = "natural"
    GENTLE = "gentle"
    ENERGETIC = "energetic"


class ActionSource(StrEnum):
    EXPLICIT_REQUEST = "explicit_request"
    FAST_PROVIDER = "fast_provider"
    EVENTBUS_TOOL = "eventbus_tool"
    DIRECT_MODEL = "direct_model"
    INTERACTION_POLICY = "interaction_policy"
    FALLBACK = "fallback"


class ActionParameters(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    angle_degrees: float | None = Field(default=None, ge=-180.0, le=180.0)
    depth: float | None = Field(default=None, ge=0.0, le=1.0)
    hold_ms: int | None = Field(default=None, ge=0, le=30_000)
    style: ActionStyle = ActionStyle.NATURAL


class ActionTransition(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enter_ms: int = Field(default=350, ge=0, le=5_000)
    exit_ms: int = Field(default=350, ge=0, le=5_000)
    easing: Literal["smoothstep", "ease_in_out"] = "smoothstep"


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
    supported_actions: tuple[Gesture, ...] | None = Field(
        default=None,
        max_length=32,
    )

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
        if self.supported_actions is not None and len(set(self.supported_actions)) != len(
            self.supported_actions
        ):
            raise ValueError("supported_actions must not contain duplicates")
        return self


class TurnImageAttachment(StrictModel):
    """摄像头单帧附件（手机端明确请求时随 turn/start 上送）。

    隐私治理红线（照抄 reality_companion 治理设计）：单帧、明确用途、
    不落盘；仅随本轮对话消费，不写入会话历史。空载荷（Unity JsonUtility
    会把 null 对象序列化成默认字段形状）一律归一化为 None，保证旧客户端
    语义不变。
    """

    mime: Literal["image/jpeg"] = "image/jpeg"
    data_base64: str = Field(min_length=100, max_length=6_000_000)
    purpose: str = Field(default="", max_length=200)

    @field_validator("data_base64")
    @classmethod
    def _validate_jpeg_frame(cls, value: str) -> str:
        import base64
        import binascii

        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image data_base64 is not valid base64") from exc
        if not raw.startswith(b"\xff\xd8"):
            raise ValueError("image payload must start with a JPEG SOI marker")
        return value


class TurnStartRequest(StrictModel):
    type: Literal["turn.start"] = "turn.start"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    text: str | None = Field(default=None, min_length=1, max_length=8192)
    # 摄像头单帧（可选字段，向后兼容）：手机端用户明确请求时随文本上送；
    # 旧客户端不带该字段，行为不变。
    image: TurnImageAttachment | None = None
    cancel_previous: bool = True

    @field_validator("image", mode="before")
    @classmethod
    def _empty_image_becomes_none(cls, value: object) -> object:
        # Unity JsonUtility 无法表达“字段不存在”：null 嵌套对象会序列化成
        # 默认字段形状（{"data_base64":"","purpose":""}）。空载荷一律归一化
        # 为 None，保证旧客户端/纯文本轮语义不变。
        if isinstance(value, dict) and not str(value.get("data_base64") or "").strip():
            return None
        return value

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
    # Optional protocol-1.1 streaming metadata (absent from older clients).
    # byte_offset is this chunk's start offset in the turn PCM byte stream;
    # capture_elapsed_ms is the client's elapsed time since turn start when the
    # audio finished capture, used to estimate upload/queue age without relying
    # on a shared wall clock.
    byte_offset: int | None = Field(default=None, ge=0)
    capture_elapsed_ms: int | None = Field(default=None, ge=0, le=86_400_000)


class AudioEndRequest(StrictModel):
    type: Literal["audio.end"] = "audio.end"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    # Optional protocol-1.1 end-of-audio completeness metadata.
    last_sequence: int | None = Field(default=None, ge=0, le=1_000_000)
    total_bytes: int | None = Field(default=None, ge=0, le=4_000_000)


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


class PlaybackReceiptRequest(StrictModel):
    """A bounded device playback fact, never a conversational input."""

    type: Literal["playback.receipt"] = "playback.receipt"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    session_id: Identifier
    turn_id: Identifier
    speech_id: Identifier
    event_name: Literal[
        "playback.started",
        "playback.progress",
        "playback.ended",
        "playback.interrupted",
    ]
    played_ms: int = Field(default=0, ge=0, le=3_600_000)
    buffered_ms: int = Field(default=0, ge=0, le=3_600_000)
    underflow_count: int = Field(default=0, ge=0, le=100_000)
    reason_code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] = ""


class ClientSpanEvent(StrictModel):
    """One bounded client-side span of the ``diagnostics@1.0`` contract.

    Offsets are relative to the client turn start; no user text, provider
    name, or request body ever appears in these fields.
    """

    component: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=48),
    ]
    stage: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=48),
    ]
    status: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] = (
        "completed"
    )
    code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""
    start_offset_ms: int = Field(default=0, ge=0, le=3_600_000)
    end_offset_ms: int = Field(default=0, ge=0, le=3_600_000)
    duration_ms: int = Field(default=-1, ge=-1, le=3_600_000)
    chunks: int = Field(default=0, ge=0, le=1_000_000)


def _metric_field(*, le: float) -> Any:
    """Bounded performance-metric field; ``-1`` means "not available"."""

    return Field(default=-1, ge=-1, le=le)


class DiagnosticReportRequest(StrictModel):
    """Bounded client diagnostics upload (``diagnostics@1.0``).

    ``kind="perf"`` carries the flat Quest performance snapshot at a low
    cadence (client gates it on detailed sampling). ``kind="spans"`` carries
    one turn's client-side span bundle at the turn boundary so the operator
    page can join it onto the server timeline via ``trace_id``.
    """

    type: Literal["diagnostics.report"] = "diagnostics.report"
    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    kind: Literal["perf", "spans"]
    session_id: Identifier
    turn_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""
    trace_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""
    ts_ms: int = Field(default=0, ge=0, le=10_000_000_000_000)
    spans: list[ClientSpanEvent] = Field(default_factory=list, max_length=8)

    # Flat Quest performance snapshot (populated only with kind="perf").
    fps: float = _metric_field(le=1_000)
    frame_p50_ms: float = _metric_field(le=10_000)
    frame_p95_ms: float = _metric_field(le=10_000)
    frame_max_ms: float = _metric_field(le=10_000)
    compositor_dropped_session: float = _metric_field(le=1_000_000)
    physics_dropped_s: float = _metric_field(le=1_000_000)
    physics_dropped_frames: int = Field(default=-1, ge=-1, le=10_000_000)
    xr_cpu_ms: float = _metric_field(le=10_000)
    xr_gpu_ms: float = _metric_field(le=10_000)
    cpu_util: float = _metric_field(le=1_000)
    gpu_util: float = _metric_field(le=1_000)
    mmd_solver_ms: float = _metric_field(le=10_000)
    mmd_physics_ms: float = _metric_field(le=10_000)
    mmd_bone_ik_ms: float = _metric_field(le=10_000)
    mmd_sdef_ms: float = _metric_field(le=10_000)
    mmd_flush_ms: float = _metric_field(le=10_000)
    hand_contact_ms: float = _metric_field(le=10_000)
    mem_alloc_bytes: int = Field(default=-1, ge=-1, le=10**13)
    mem_pss_bytes: int = Field(default=-1, ge=-1, le=10**13)
    gc0: int = Field(default=-1, ge=-1, le=1_000_000)
    gc1: int = Field(default=-1, ge=-1, le=1_000_000)
    gc2: int = Field(default=-1, ge=-1, le=1_000_000)
    thermal_state: Annotated[str, StringConstraints(strip_whitespace=True, max_length=32)] = ""
    model_renderer: int = Field(default=-1, ge=-1, le=100_000)
    model_material: int = Field(default=-1, ge=-1, le=100_000)
    model_texture: int = Field(default=-1, ge=-1, le=100_000)
    model_vertex: int = Field(default=-1, ge=-1, le=10_000_000)
    model_tri: int = Field(default=-1, ge=-1, le=10_000_000)
    model_bone: int = Field(default=-1, ge=-1, le=1_000_000)
    model_rigid: int = Field(default=-1, ge=-1, le=1_000_000)
    model_joint: int = Field(default=-1, ge=-1, le=1_000_000)
    target_fps: float = _metric_field(le=1_000)
    render_scale: float = Field(default=0, ge=0, le=4)
    headset_worn: bool | None = None
    active_action: Annotated[str, StringConstraints(strip_whitespace=True, max_length=48)] = ""
    physics_hz: int = Field(default=0, ge=0, le=1_000)
    physics_substeps: int = Field(default=0, ge=0, le=16)

    @model_validator(mode="after")
    def _check_kind_payload(self) -> "DiagnosticReportRequest":
        if self.kind == "perf" and self.fps < 0:
            raise ValueError("perf report requires a valid fps sample")
        if self.kind == "spans":
            if not self.spans:
                raise ValueError("spans report requires at least one span")
            if not self.turn_id:
                raise ValueError("spans report requires the turn id")
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


class FastActionFeedback(StrictModel):
    """Bounded, non-authoritative state shared with the same-turn reply model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "processing",
        "planned",
        "unsupported",
        "no_action",
        "unavailable",
        "error",
    ]
    action: Gesture | None = None
    execution_confirmed: bool = False

    @field_validator("execution_confirmed", mode="before")
    @classmethod
    def require_unconfirmed(cls, value: Any) -> bool:
        if value is not False:
            raise ValueError("same-turn feedback can never confirm execution")
        return False

    @model_validator(mode="after")
    def align_status_and_action(self) -> FastActionFeedback:
        action_statuses = {"planned", "unsupported"}
        if self.status in action_statuses and self.action is None:
            raise ValueError("action-bearing feedback requires an action")
        if self.status not in action_statuses and self.action is not None:
            raise ValueError("only action-bearing feedback can include an action")
        return self


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
    action_parameters: ActionParameters | None = None
    transition: ActionTransition | None = None


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
    method: Gesture
    parameters: ActionParameters = Field(default_factory=ActionParameters)
    transition: ActionTransition = Field(default_factory=ActionTransition)
    source: ActionSource

    @model_validator(mode="after")
    def align_method_and_gesture(self) -> AvatarIntent:
        if self.method is not self.gesture:
            raise ValueError("method must match gesture")
        return self


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
