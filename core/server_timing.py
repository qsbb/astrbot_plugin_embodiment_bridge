from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Literal


SERVER_TIMING_CONTRACT = "server_timing@1.0"
MAX_TIMING_MS = 86_400_000
DECISION_PATHS = frozenset({"astrbot_event_bus", "direct_provider"})
DecisionPath = Literal["astrbot_event_bus", "direct_provider"]


def _elapsed_ms(started_at: float | None, ended_at: float | None) -> int:
    if started_at is None:
        return 0
    end = monotonic() if ended_at is None else ended_at
    return min(MAX_TIMING_MS, max(0, int(round((end - started_at) * 1000))))


@dataclass(slots=True)
class ServerTimingState:
    """Internal monotonic timing state for one turn.

    Only :meth:`snapshot` is exposed. It contains bounded integer durations and
    a fixed decision-path enum; no provider, identity, or request data crosses
    the protocol boundary.
    """

    started_at: float = field(default_factory=monotonic)
    stt_started_at: float | None = None
    stt_ended_at: float | None = None
    decision_started_at: float | None = None
    decision_ended_at: float | None = None
    decision_path: DecisionPath = "direct_provider"
    tts_started_at: float | None = None
    tts_first_chunk_at: float | None = None
    tts_ended_at: float | None = None
    decision_hooks_ms: int = 0
    decision_provider_ms: int = 0
    event_loop_lag_ms: int = 0

    def start_processing(self) -> None:
        self.started_at = monotonic()

    def start_stt(self) -> None:
        self.stt_started_at = monotonic()

    def finish_stt(self) -> None:
        if self.stt_started_at is not None and self.stt_ended_at is None:
            self.stt_ended_at = monotonic()

    def start_decision(self, path: DecisionPath) -> None:
        if self.decision_started_at is None:
            self.decision_started_at = monotonic()
        self.decision_path = path

    def finish_decision(self) -> None:
        if self.decision_started_at is not None and self.decision_ended_at is None:
            self.decision_ended_at = monotonic()

    def start_tts(self) -> None:
        self.tts_started_at = monotonic()

    def mark_tts_first_chunk(self) -> None:
        if self.tts_first_chunk_at is None:
            self.tts_first_chunk_at = monotonic()

    def finish_tts(self) -> None:
        if self.tts_started_at is not None and self.tts_ended_at is None:
            self.tts_ended_at = monotonic()

    def set_decision_breakdown(self, hooks_ms: int, provider_ms: int) -> None:
        self.decision_hooks_ms = max(0, min(MAX_TIMING_MS, int(hooks_ms or 0)))
        self.decision_provider_ms = max(0, min(MAX_TIMING_MS, int(provider_ms or 0)))

    def set_event_loop_lag_ms(self, lag_ms: int) -> None:
        self.event_loop_lag_ms = max(0, min(MAX_TIMING_MS, int(lag_ms or 0)))

    def snapshot(self) -> dict[str, int | str]:
        """Return the fixed, non-sensitive ``server_timing@1.0`` payload."""

        tts_first_chunk_ms = 0
        if self.tts_started_at is not None and self.tts_first_chunk_at is not None:
            tts_first_chunk_ms = _elapsed_ms(
                self.tts_started_at,
                self.tts_first_chunk_at,
            )
        return {
            "contract": SERVER_TIMING_CONTRACT,
            "stt_ms": _elapsed_ms(self.stt_started_at, self.stt_ended_at),
            "decision_ms": _elapsed_ms(
                self.decision_started_at,
                self.decision_ended_at,
            ),
            "decision_path": (
                self.decision_path
                if self.decision_path in DECISION_PATHS
                else "direct_provider"
            ),
            "tts_first_chunk_ms": tts_first_chunk_ms,
            "tts_total_ms": _elapsed_ms(self.tts_started_at, self.tts_ended_at),
            "decision_hooks_ms": self.decision_hooks_ms,
            "decision_provider_ms": self.decision_provider_ms,
            "event_loop_lag_ms": self.event_loop_lag_ms,
            "turn_total_ms": _elapsed_ms(self.started_at, None),
        }
