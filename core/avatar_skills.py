from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import Emotion, Gesture, LookAt, ProposedIntent


@dataclass(frozen=True, slots=True)
class AvatarSkill:
    """One allowlisted avatar capability exposed to the model."""

    name: str
    gesture: Gesture
    description: str
    default_look_at: LookAt = LookAt.USER
    default_intensity: float = 0.45
    default_duration_ms: int = 2_000


class AvatarSkillRegistry:
    """Resolve model action calls into safe protocol intents."""

    _skills = (
        AvatarSkill("idle", Gesture.IDLE, "Return to the natural idle pose", LookAt.USER, 0.2, 1_200),
        AvatarSkill("talk", Gesture.TALK, "Use the conversational speaking pose", LookAt.USER, 0.35, 2_000),
        AvatarSkill("wave", Gesture.WAVE, "Perform a natural feminine wave", LookAt.USER, 0.5, 2_400),
        AvatarSkill("bow", Gesture.BOW, "Perform a short polite bow", LookAt.USER, 0.45, 1_800),
        AvatarSkill("dance", Gesture.DANCE, "Play the selected dance motion", LookAt.USER, 0.7, 8_000),
        AvatarSkill("nod", Gesture.NOD, "Nod naturally in agreement", LookAt.USER, 0.35, 1_300),
        AvatarSkill("sway", Gesture.SWAY, "Use a subtle idle sway", LookAt.USER, 0.25, 2_000),
        AvatarSkill("handshake", Gesture.HANDSHAKE, "Respond to a detected handshake", LookAt.HAND, 0.45, 2_000),
        AvatarSkill("head_pat", Gesture.HEAD_PAT, "Respond to a detected head pat", LookAt.HAND, 0.45, 2_000),
        AvatarSkill("cheek_pinch", Gesture.CHEEK_PINCH, "Respond to a detected cheek pinch", LookAt.HAND, 0.45, 2_000),
        AvatarSkill("refuse", Gesture.REFUSE, "Set a clear but non-aggressive boundary", LookAt.AWAY, 0.55, 1_800),
        AvatarSkill("step_back", Gesture.STEP_BACK, "Take a small boundary-respecting step back", LookAt.AWAY, 0.55, 1_800),
    )
    _by_name = {skill.name: skill for skill in _skills}

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return tuple(skill.name for skill in cls._skills)

    @classmethod
    def prompt_contract(cls) -> str:
        skills = ", ".join(
            f"{skill.name}: {skill.description}" for skill in cls._skills
        )
        return (
            "Avatar skills are allowlisted methods, not free-form animation commands. "
            "When an action is needed, set action to "
            '{"name":"<skill>","arguments":{"intensity":0.0,"duration_ms":0,"look_at":"user|hand|away|none"}} '
            "and keep intent consistent. Available skills: "
            + skills
            + ". Use null action for a normal reply."
        )

    @classmethod
    def invoke(cls, name: Any, arguments: Any = None) -> ProposedIntent | None:
        skill = cls._by_name.get(str(name or "").strip().lower())
        if skill is None:
            return None
        args = arguments if isinstance(arguments, dict) else {}
        if any(str(key) not in {"emotion", "intensity", "duration_ms", "look_at"} for key in args):
            return None
        emotion = cls._emotion(args.get("emotion"), Emotion.NEUTRAL)
        look_at = cls._look_at(args.get("look_at"), skill.default_look_at)
        intensity = cls._bounded_float(args.get("intensity"), skill.default_intensity)
        duration_ms = cls._bounded_int(args.get("duration_ms"), skill.default_duration_ms)
        return ProposedIntent(
            emotion=emotion,
            gesture=skill.gesture,
            look_at=look_at,
            intensity=intensity,
            duration_ms=duration_ms,
            reason_code=f"skill_{skill.name}",
        )

    @staticmethod
    def _bounded_float(value: Any, default: float) -> float:
        if value is None or isinstance(value, bool):
            return default
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _bounded_int(value: Any, default: int) -> int:
        if value is None or isinstance(value, bool):
            return default
        try:
            return max(0, min(30_000, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _emotion(value: Any, default: Emotion) -> Emotion:
        try:
            return Emotion(str(value).strip().lower()) if value else default
        except ValueError:
            return default

    @staticmethod
    def _look_at(value: Any, default: LookAt) -> LookAt:
        try:
            return LookAt(str(value).strip().lower()) if value else default
        except ValueError:
            return default
