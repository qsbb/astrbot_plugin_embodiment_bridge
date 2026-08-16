from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from .avatar_skills import AvatarSkillRegistry
from .models import (
    ActionSource,
    AvatarIntent,
    Emotion,
    Gesture,
    InteractionEvent,
    InteractionName,
    InteractionPhase,
    LookAt,
    ModelDecision,
)


@dataclass(slots=True)
class InteractionPolicy:
    max_intensity: float = 0.85
    max_duration_ms: int = 8_000
    max_continuous_touch_ms: int = 15_000
    gesture_cooldown_seconds: float = 0.35
    _last_gesture_at: dict[tuple[str, Gesture], float] = field(default_factory=dict)

    def apply(
        self,
        *,
        session_id: str,
        turn_id: str,
        decision: ModelDecision,
        interaction: InteractionEvent | None,
        relationship: dict[str, Any] | None,
        action_source: ActionSource | str = ActionSource.DIRECT_MODEL,
    ) -> AvatarIntent:
        proposed = decision.intent
        emotion = proposed.emotion
        gesture = proposed.gesture
        look_at = proposed.look_at
        intensity = min(max(float(proposed.intensity), 0.0), self.max_intensity)
        duration_ms = min(max(int(proposed.duration_ms), 0), self.max_duration_ms)
        reason_code = proposed.reason_code
        try:
            source = ActionSource(action_source)
        except ValueError:
            source = ActionSource.FALLBACK

        boundary = self._relationship_boundary(relationship)
        continuous_touch_limit = self._exceeds_continuous_touch_limit(interaction)
        if continuous_touch_limit or self._requires_boundary_override(
            interaction, boundary
        ):
            emotion = Emotion.UNCOMFORTABLE
            gesture = Gesture.REFUSE
            look_at = LookAt.AWAY
            intensity = min(max(interaction.strength, 0.35), self.max_intensity)
            duration_ms = min(max(duration_ms, 900), 2_500)
            reason_code = (
                "continuous_touch_limit"
                if continuous_touch_limit
                else "boundary_safety_override"
            )
            source = ActionSource.INTERACTION_POLICY

        now = monotonic()
        cooldown_key = (session_id, gesture)
        last_at = self._last_gesture_at.get(cooldown_key, 0.0)
        if (
            gesture not in {Gesture.IDLE, Gesture.TALK}
            and now - last_at < self.gesture_cooldown_seconds
        ):
            gesture = Gesture.IDLE
            duration_ms = min(duration_ms, 600)
            reason_code = "gesture_cooldown"
            source = ActionSource.INTERACTION_POLICY
        elif gesture not in {Gesture.IDLE, Gesture.TALK}:
            self._last_gesture_at[cooldown_key] = now

        default_parameters, default_transition = AvatarSkillRegistry.defaults_for(
            gesture
        )
        parameters = (
            proposed.action_parameters
            if gesture is proposed.gesture and proposed.action_parameters is not None
            else default_parameters
        )
        transition = (
            proposed.transition
            if gesture is proposed.gesture and proposed.transition is not None
            else default_transition
        )
        return AvatarIntent(
            session_id=session_id,
            turn_id=turn_id,
            in_reply_to_event_id=interaction.event_id if interaction else None,
            emotion=emotion,
            gesture=gesture,
            look_at=look_at,
            intensity=intensity,
            duration_ms=duration_ms,
            reason_code=reason_code,
            method=gesture,
            parameters=parameters,
            transition=transition,
            source=source,
        )

    def _exceeds_continuous_touch_limit(
        self,
        interaction: InteractionEvent | None,
    ) -> bool:
        if interaction is None:
            return False
        tactile = interaction.name in {
            InteractionName.HANDSHAKE,
            InteractionName.HEAD_PAT,
            InteractionName.CHEEK_PINCH,
        }
        active = interaction.phase in {InteractionPhase.START, InteractionPhase.UPDATE}
        return (
            tactile
            and active
            and interaction.duration_ms >= self.max_continuous_touch_ms
        )

    @staticmethod
    def _relationship_boundary(relationship: dict[str, Any] | None) -> str:
        if not isinstance(relationship, dict):
            return ""
        behavior = relationship.get("behavior")
        if not isinstance(behavior, dict):
            return ""
        return str(behavior.get("boundary") or "").strip().lower()

    @staticmethod
    def _requires_boundary_override(
        interaction: InteractionEvent | None,
        boundary: str,
    ) -> bool:
        if interaction is None:
            return False
        if interaction.name != InteractionName.CHEEK_PINCH:
            return False
        if interaction.strength >= 0.9:
            return True
        return boundary in {"strict", "closed", "avoid_touch", "拒绝接触"}
