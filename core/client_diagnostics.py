"""Per-session in-memory ring buffer for bounded client diagnostics reports.

Quest clients push two low-priority report kinds here:

- ``perf``: a flat performance snapshot at a low cadence (client gated on
  detailed sampling);
- ``spans``: one turn's client-side span bundle at the turn boundary.

These are transient observations. They are never written to the persistent
diagnostic log; they only leave this process through the operator projection
(:meth:`ClientDiagnosticsStore.snapshot`). Every field is bounded by the
``diagnostics@1.0`` request schema, and the store adds its own session /
count / rate limits so a misbehaving client cannot grow memory.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any

CLIENT_DIAGNOSTICS_CONTRACT = "diagnostics@1.0"
MAX_EVENTS_PER_SESSION = 120
MAX_SESSIONS = 8
MIN_REPORT_INTERVAL_SECONDS = 0.4
SNAPSHOT_EVENTS_PER_SESSION = 40
_PERF_FLOAT_FIELDS = (
    "fps",
    "frame_p50_ms",
    "frame_p95_ms",
    "frame_max_ms",
    "compositor_dropped_session",
    "physics_dropped_s",
    "xr_cpu_ms",
    "xr_gpu_ms",
    "cpu_util",
    "gpu_util",
    "mmd_solver_ms",
    "mmd_physics_ms",
    "mmd_bone_ik_ms",
    "mmd_sdef_ms",
    "mmd_flush_ms",
    "hand_contact_ms",
    "target_fps",
    "render_scale",
)
_PERF_INT_FIELDS = (
    "physics_dropped_frames",
    "mem_alloc_bytes",
    "mem_pss_bytes",
    "gc0",
    "gc1",
    "gc2",
    "model_renderer",
    "model_material",
    "model_texture",
    "model_vertex",
    "model_tri",
    "model_bone",
    "model_rigid",
    "model_joint",
    "physics_hz",
    "physics_substeps",
)
_PERF_TEXT_FIELDS = ("thermal_state", "active_action")


class _SessionBuffer:
    __slots__ = (
        "session_id",
        "events",
        "first_monotonic",
        "last_monotonic",
        "last_report_monotonic",
        "sequence",
        "perf_count",
        "span_count",
        "rejected_count",
        "fps_sum",
        "fps_samples",
        "physics_dropped_max_s",
        "latest_perf",
    )

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS_PER_SESSION)
        self.first_monotonic = time.monotonic()
        self.last_monotonic = self.first_monotonic
        self.last_report_monotonic = 0.0
        self.sequence = 0
        self.perf_count = 0
        self.span_count = 0
        self.rejected_count = 0
        self.fps_sum = 0.0
        self.fps_samples = 0
        self.physics_dropped_max_s = 0.0
        self.latest_perf: dict[str, Any] | None = None


class ClientDiagnosticsStore:
    """Bounded, session-isolated sink for client diagnostics reports."""

    def __init__(
        self,
        *,
        max_events_per_session: int = MAX_EVENTS_PER_SESSION,
        max_sessions: int = MAX_SESSIONS,
        min_report_interval_seconds: float = MIN_REPORT_INTERVAL_SECONDS,
    ) -> None:
        self._sessions: dict[str, _SessionBuffer] = {}
        self._lock = RLock()
        self._max_events = max(1, int(max_events_per_session))
        self._max_sessions = max(1, int(max_sessions))
        self._min_interval = max(0.0, float(min_report_interval_seconds))
        self._accepted = 0
        self._rejected = 0

    def record_report(self, owner: str, report: Any) -> dict[str, Any]:
        """Store one validated report; returns an acceptance summary."""

        session_id = str(getattr(report, "session_id", "") or "")[:64]
        kind = str(getattr(report, "kind", "") or "")
        if not session_id or kind not in {"perf", "spans"}:
            self._rejected += 1
            return {"accepted": False, "reason": "invalid_report"}

        now = time.monotonic()
        with self._lock:
            buffer = self._sessions.get(session_id)
            if buffer is None:
                self._evict_expired_sessions_locked()
                if len(self._sessions) >= self._max_sessions:
                    oldest = min(
                        self._sessions.values(),
                        key=lambda item: item.last_monotonic,
                    )
                    self._sessions.pop(oldest.session_id, None)
                buffer = _SessionBuffer(session_id)
                self._sessions[session_id] = buffer

            if (
                buffer.last_report_monotonic
                and now - buffer.last_report_monotonic < self._min_interval
            ):
                buffer.rejected_count += 1
                self._rejected += 1
                return {
                    "accepted": False,
                    "reason": "rate_limited",
                    "min_interval_ms": int(self._min_interval * 1000),
                }

            buffer.last_report_monotonic = now
            buffer.last_monotonic = now
            buffer.sequence += 1
            entry: dict[str, Any] = {
                "seq": buffer.sequence,
                "owner_bound": bool(owner),
                "kind": kind,
                "ts_ms": int(getattr(report, "ts_ms", 0) or 0),
                "turn_id": str(getattr(report, "turn_id", "") or "")[:64],
                "trace_id": str(getattr(report, "trace_id", "") or "")[:64],
                "received_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds"),
                "offset_ms": int((now - buffer.first_monotonic) * 1000),
            }
            if kind == "perf":
                perf = self._project_perf(report)
                entry["perf"] = perf
                buffer.perf_count += 1
                buffer.latest_perf = perf
                if perf.get("fps", -1) >= 0:
                    buffer.fps_sum += float(perf["fps"])
                    buffer.fps_samples += 1
                dropped = float(perf.get("physics_dropped_s", 0) or 0)
                if dropped > buffer.physics_dropped_max_s:
                    buffer.physics_dropped_max_s = dropped
            else:
                entry["spans"] = [
                    {
                        "component": span.get("component", ""),
                        "stage": span.get("stage", ""),
                        "status": span.get("status", ""),
                        "code": span.get("code", ""),
                        "start_offset_ms": int(span.get("start_offset_ms", 0) or 0),
                        "end_offset_ms": int(span.get("end_offset_ms", 0) or 0),
                        "duration_ms": int(span.get("duration_ms", -1) or -1),
                        "chunks": int(span.get("chunks", 0) or 0),
                    }
                    for span in report.model_dump(include={"spans"}).get("spans", [])
                ]
                buffer.span_count += len(entry["spans"])
            buffer.events.append(entry)
            while len(buffer.events) > self._max_events:
                buffer.events.popleft()
            self._accepted += 1
            return {
                "accepted": True,
                "kind": kind,
                "sequence": buffer.sequence,
                "buffered_events": len(buffer.events),
            }

    def forget(self, session_id: str) -> None:
        """Drop one session's buffer (called when the session closes)."""

        key = str(session_id or "")[:64]
        if not key:
            return
        with self._lock:
            self._sessions.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        """Operator-facing projection; bounded and sanitized."""

        with self._lock:
            sessions = sorted(
                self._sessions.values(),
                key=lambda item: item.last_monotonic,
                reverse=True,
            )
            projected: list[dict[str, Any]] = []
            now = time.monotonic()
            for buffer in sessions:
                events = list(buffer.events)[-SNAPSHOT_EVENTS_PER_SESSION:]
                aggregates = {
                    "report_count": buffer.perf_count + buffer.span_count,
                    "perf_count": buffer.perf_count,
                    "span_events": buffer.span_count,
                    "rejected_count": buffer.rejected_count,
                    "avg_fps": (
                        round(buffer.fps_sum / buffer.fps_samples, 1)
                        if buffer.fps_samples
                        else -1
                    ),
                    "physics_dropped_max_s": round(
                        buffer.physics_dropped_max_s, 3
                    ),
                    "age_seconds": round(max(0.0, now - buffer.last_monotonic), 1),
                    "window_seconds": round(
                        max(0.0, buffer.last_monotonic - buffer.first_monotonic), 1
                    ),
                }
                projected.append(
                    {
                        "session_id": buffer.session_id,
                        "aggregates": aggregates,
                        "latest_perf": buffer.latest_perf,
                        "events": events,
                    }
                )
            return {
                "contract": CLIENT_DIAGNOSTICS_CONTRACT,
                "status": "ready" if projected else "empty",
                "sessions": projected,
                "totals": {
                    "sessions": len(projected),
                    "accepted": self._accepted,
                    "rejected": self._rejected,
                },
            }

    @staticmethod
    def _project_perf(report: Any) -> dict[str, Any]:
        perf: dict[str, Any] = {}
        for name in _PERF_FLOAT_FIELDS:
            perf[name] = round(float(getattr(report, name, -1) or -1), 3)
        for name in _PERF_INT_FIELDS:
            perf[name] = int(getattr(report, name, -1) or -1)
        for name in _PERF_TEXT_FIELDS:
            perf[name] = str(getattr(report, name, "") or "")[:48]
        worn = getattr(report, "headset_worn", None)
        perf["headset_worn"] = bool(worn) if worn is not None else None
        return perf

    def _evict_expired_sessions_locked(self) -> None:
        if len(self._sessions) < self._max_sessions:
            return
        # Called right before inserting a brand-new session: drop the stalest
        # buffer instead of refusing telemetry from a newly bound session.
        oldest = min(self._sessions.values(), key=lambda item: item.last_monotonic)
        self._sessions.pop(oldest.session_id, None)
