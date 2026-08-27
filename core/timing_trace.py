from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator


_MAX_MS = 3_600_000
_LAG_SAMPLE_SECONDS = 0.05
_MAX_SPANS = 512
_TRACE_ACTION_NAMES = frozenset(
    {
        "provider.request_sent",
        "provider.request_start",
        "provider.queue_wait",
        "provider.queued",
        "provider.wait",
        "provider.first_token",
        "provider.first_chunk",
        "provider.last_token",
        "provider.completed",
        "provider.complete",
        "provider.end",
        "astr_agent_prepare",
        "astr_agent_complete",
        "agent.prepare",
        "agent.complete",
    }
)
_TRACE_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _bounded_ms(value: float | int | None) -> int:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(_MAX_MS, int(round(numeric * 1000))))


@dataclass(slots=True)
class _SpanState:
    span_id: str
    name: str
    kind: str
    started: float
    parent_id: str = ""
    category: str = ""
    child_intervals: list[tuple[float, float]] = field(default_factory=list)
    child_wait_ms: dict[str, float] = field(default_factory=dict)
    ended: float = 0.0
    finished: bool = False


class TimingTrace:
    """A bounded, plugin-owned timing tree for one Bridge turn.

    The trace intentionally records durations and fixed enums only. It is
    attached to the synthetic AstrBot event at runtime and never crosses the
    public HTTP/SSE protocol. Child intervals are unioned before calculating
    ``active_ms`` so concurrent awaits are not double-counted.
    """

    def __init__(
        self,
        diagnostic_log: Any,
        *,
        enabled: bool,
        trace_id: str = "",
    ) -> None:
        self.diagnostic_log = diagnostic_log
        self.enabled = bool(enabled)
        self.trace_id = str(trace_id or "")[:32]
        self.started = time.perf_counter()
        self._spans: dict[str, _SpanState] = {}
        self._named_spans: dict[str, str] = {}
        self._sequence = 0
        self._lag_max_ms = 0.0
        self._provider_markers: dict[str, dict[str, float]] = {}
        self._lag_task: asyncio.Task[None] | None = None
        self._lag_stop: asyncio.Event | None = None
        self._closed = False

    @property
    def event_loop_lag_ms(self) -> int:
        return _bounded_ms(self._lag_max_ms)

    def span_wall_ms(self, *names: str) -> int:
        """Return the largest wall_ms among finished spans matching any name.

        Used to surface a fixed, non-sensitive sub-phase breakdown (e.g. the
        decision hooks vs. the LLM provider) alongside the ``server_timing``
        summary without exposing the internal span tree. Returns ``0`` when the
        trace is disabled or no matching span finished.
        """

        if not self.enabled:
            return 0
        wanted = {str(name or "")[:96] for name in names}
        best = 0
        for state in self._spans.values():
            if (
                state.finished
                and state.name in wanted
                and state.ended > state.started
            ):
                best = max(best, _bounded_ms(state.ended - state.started))
        return best

    def start_span(
        self,
        name: str,
        *,
        kind: str = "unknown",
        parent_id: str = "",
        category: str = "",
    ) -> str:
        self._sequence += 1
        span_id = f"s{self._sequence:x}"[:32]
        if not self.enabled or self._closed or len(self._spans) >= _MAX_SPANS:
            return span_id
        state = _SpanState(
            span_id=span_id,
            name=str(name or "unknown")[:96],
            kind=str(kind or "unknown")[:48],
            started=time.perf_counter(),
            parent_id=str(parent_id or "")[:32],
            category=str(category or "")[:32],
        )
        self._spans[span_id] = state
        if state.category == "provider" or state.kind in {
            "provider",
            "llm_provider",
            "stt_provider",
            "agent_provider",
            "tts",
        }:
            self._provider_markers[span_id] = {"request": state.started}
        return span_id

    def start_named_span(
        self,
        name: str,
        *,
        kind: str = "unknown",
        parent_id: str = "",
        category: str = "",
    ) -> str:
        existing = self._named_spans.get(name)
        if existing and existing in self._spans and not self._spans[existing].finished:
            return existing
        span_id = self.start_span(
            name,
            kind=kind,
            parent_id=parent_id,
            category=category,
        )
        if self.enabled:
            self._named_spans[name] = span_id
        return span_id

    def finish_named_span(self, name: str, **fields: Any) -> bool:
        span_id = self._named_spans.get(name)
        if not span_id:
            return False
        return self.finish_span(span_id, **fields)

    def finish_span(
        self,
        span_id: str,
        *,
        status: str = "ok",
        queue_wait_ms: float = 0.0,
        lock_wait_ms: float = 0.0,
        provider_wait_ms: float = 0.0,
        cache_hit: bool | None = None,
        retry_count: int | None = None,
        timeout: bool | None = None,
        fallback: bool | None = None,
        **fields: Any,
    ) -> bool:
        """Finish a span; explicit ``*_wait_ms`` arguments are milliseconds."""
        state = self._spans.get(span_id)
        if not self.enabled or self._closed or state is None or state.finished:
            return False
        ended = time.perf_counter()
        state.finished = True
        state.ended = ended
        wall_seconds = max(0.0, ended - state.started)
        # A wait-category span is itself the measured wait.  Callers can pass
        # an explicit value when only part of the span waited, but the default
        # keeps queue/lock/provider spans useful without duplicated stopwatch
        # code at every call site.
        if state.category == "queue" and not queue_wait_ms:
            queue_wait_ms = wall_seconds * 1000.0
        elif state.category == "lock" and not lock_wait_ms:
            lock_wait_ms = wall_seconds * 1000.0
        elif state.category == "provider" and not provider_wait_ms:
            provider_wait_ms = wall_seconds * 1000.0
        child_seconds = self._union_seconds(state.child_intervals)
        explicit_wait_seconds = max(
            0.0,
            (
                float(queue_wait_ms or 0.0)
                + float(lock_wait_ms or 0.0)
                + float(provider_wait_ms or 0.0)
            )
            / 1000.0,
        )
        active_seconds = max(0.0, wall_seconds - child_seconds - explicit_wait_seconds)
        payload: dict[str, Any] = {
            "component": "timing",
            "phase": "span",
            "status": str(status or "ok")[:32],
            "span_id": state.span_id,
            "parent_span_id": state.parent_id,
            "span_name": state.name,
            "span_kind": state.kind,
            "wall_ms": _bounded_ms(wall_seconds),
            "active_ms": _bounded_ms(active_seconds),
            "queue_wait_ms": _bounded_ms(float(queue_wait_ms or 0.0) / 1000.0),
            "lock_wait_ms": _bounded_ms(float(lock_wait_ms or 0.0) / 1000.0),
            "provider_wait_ms": _bounded_ms(
                float(provider_wait_ms or 0.0) / 1000.0
            ),
            "start_offset_ms": _bounded_ms(state.started - self.started),
            "end_offset_ms": _bounded_ms(ended - self.started),
            "event_loop_lag_ms": self.event_loop_lag_ms,
            "active_ms_estimated": True,
            "cache_hit": bool(cache_hit) if cache_hit is not None else False,
            "retry_count": max(0, int(retry_count or 0)),
            "timeout": bool(timeout) if timeout is not None else False,
            "fallback": bool(fallback) if fallback is not None else False,
            "trace_id": self.trace_id,
        }
        if state.kind in {
            "provider",
            "llm_provider",
            "stt_provider",
            "agent_provider",
            "tts",
        }:
            markers = self._provider_markers.get(state.span_id, {})
            request_at = markers.get("request")
            first_at = markers.get("first_token")
            end_at = markers.get("end") or ended
            payload.update(
                {
                    "provider_request_offset_ms": _bounded_ms(
                        (request_at - self.started) if request_at is not None else 0
                    ),
                    "provider_first_token_offset_ms": _bounded_ms(
                        (first_at - self.started) if first_at is not None else 0
                    ),
                    "provider_end_offset_ms": _bounded_ms(
                        (end_at - self.started) if end_at is not None else 0
                    ),
                    "provider_first_token_ms": _bounded_ms(
                        (first_at - request_at)
                        if request_at is not None and first_at is not None
                        else 0
                    ),
                    "provider_total_ms": _bounded_ms(
                        (end_at - request_at)
                        if request_at is not None and end_at is not None
                        else 0
                    ),
                }
            )
        payload.update(fields)
        self._record("timing.span.completed", **payload)
        if state.parent_id:
            parent = self._spans.get(state.parent_id)
            if parent is not None and not parent.finished:
                parent.child_intervals.append((state.started, ended))
                if state.category:
                    parent.child_wait_ms[state.category] = (
                        parent.child_wait_ms.get(state.category, 0.0) + wall_seconds
                    )
        return True

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "unknown",
        parent_id: str = "",
        category: str = "",
        **finish_fields: Any,
    ) -> Iterator[str]:
        span_id = self.start_span(
            name,
            kind=kind,
            parent_id=parent_id,
            category=category,
        )
        try:
            yield span_id
        except asyncio.CancelledError:
            self.finish_span(span_id, status="cancelled", **finish_fields)
            raise
        except BaseException:
            self.finish_span(span_id, status="error", **finish_fields)
            raise
        else:
            self.finish_span(span_id, status="ok", **finish_fields)

    @contextlib.asynccontextmanager
    async def async_span(
        self,
        name: str,
        *,
        kind: str = "unknown",
        parent_id: str = "",
        category: str = "",
        **finish_fields: Any,
    ) -> Any:
        """Time one awaited operation without changing its cancellation rules."""
        span_id = self.start_span(
            name,
            kind=kind,
            parent_id=parent_id,
            category=category,
        )
        try:
            yield span_id
        except asyncio.CancelledError:
            self.finish_span(span_id, status="cancelled", **finish_fields)
            raise
        except BaseException:
            self.finish_span(span_id, status="error", **finish_fields)
            raise
        else:
            self.finish_span(span_id, status="ok", **finish_fields)

    def mark_event_consumed(self, *, parent_id: str = "") -> str:
        if self._closed:
            return ""
        span_id = self.finish_named_span(
            "eventbus.queue_wait",
            status="consumed",
        )
        if span_id:
            return self.start_named_span(
                "eventbus.processing",
                kind="eventbus",
                parent_id=parent_id,
            )
        return self._named_spans.get("eventbus.processing", "")

    def finish_eventbus(
        self,
        *,
        status: str = "completed",
        timeout: bool = False,
        fallback: bool = False,
    ) -> None:
        """Close EventBus queue/processing spans without double-finishing.

        AstrBot versions differ in whether a trace probe is available.  The
        bridge therefore closes the queue span on timeout/error and, when the
        event was consumed, closes both the queue and processing spans from
        the lifecycle callback or the ``wait_completed`` path.
        """
        if self._closed:
            return
        normalized = str(status or "completed")[:32]
        if normalized in {"completed", "consumed"}:
            processing = self.mark_event_consumed()
            if processing:
                self.finish_span(
                    processing,
                    status="completed",
                    timeout=timeout,
                    fallback=fallback,
                )
            return
        # If a probe already moved the event into processing, finish that
        # span too; otherwise only the queue wait exists and is closed here.
        self.finish_named_span(
            "eventbus.queue_wait",
            status=normalized,
            timeout=timeout,
            fallback=fallback,
        )
        self.finish_named_span(
            "eventbus.processing",
            status=normalized,
            timeout=timeout,
            fallback=fallback,
        )

    def start_event_loop_monitor(self) -> None:
        if not self.enabled or self._closed or self._lag_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._lag_stop = asyncio.Event()
        self._lag_task = loop.create_task(
            self._monitor_event_loop(),
            name="embodiment-bridge:timing-loop-lag",
        )

    async def close(self) -> None:
        if self._closed:
            return
        task = self._lag_task
        stop = self._lag_stop
        self._lag_task = None
        self._lag_stop = None
        # Finish outstanding spans synchronously before yielding to the
        # monitor task.  This preserves an explicit ``abandoned`` record while
        # the closed gate prevents any later callback from appending data.
        if self.enabled:
            for span_id, state in list(self._spans.items()):
                if not state.finished:
                    self.finish_span(span_id, status="abandoned")
        self._closed = True
        if stop is not None:
            stop.set()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _monitor_event_loop(self) -> None:
        stop = self._lag_stop
        if stop is None:
            return
        loop = asyncio.get_running_loop()
        expected = loop.time() + _LAG_SAMPLE_SECONDS
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_LAG_SAMPLE_SECONDS)
            except asyncio.TimeoutError:
                now = loop.time()
                lag = max(0.0, now - expected)
                self._lag_max_ms = max(self._lag_max_ms, lag)
                expected = now + _LAG_SAMPLE_SECONDS

    def trace_point(self, action: str, *, status: str = "observed", **fields: Any) -> None:
        if not self.enabled or self._closed:
            return
        now = time.perf_counter()
        safe_action = safe_trace_action(action)
        lowered = safe_action.lower()
        active_provider = self._active_provider_span()
        if active_provider:
            markers = self._provider_markers.setdefault(active_provider, {})
            if any(marker in lowered for marker in ("request_sent", "request_start")):
                markers.setdefault("request", now)
            elif any(marker in lowered for marker in ("first_token", "first_chunk")):
                markers.setdefault("first_token", now)
            elif any(marker in lowered for marker in ("last_token", "complete", "completed", "end")):
                markers.setdefault("end", now)
        self._record(
            "timing.trace_point",
            component="timing",
            phase="trace",
            status=status,
            span_name=safe_action,
            start_offset_ms=_bounded_ms(now - self.started),
            end_offset_ms=_bounded_ms(now - self.started),
            event_loop_lag_ms=self.event_loop_lag_ms,
            trace_id=self.trace_id,
            **fields,
        )

    def _active_provider_span(self) -> str:
        active = [
            state
            for state in self._spans.values()
            if not state.finished
            and (
                state.category == "provider"
                or state.kind
                in {
                    "provider",
                    "llm_provider",
                    "stt_provider",
                    "agent_provider",
                    "tts",
                }
            )
        ]
        if not active:
            return ""
        return max(active, key=lambda state: state.started).span_id

    def _record(self, event: str, **fields: Any) -> None:
        recorder = getattr(self.diagnostic_log, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(event, **fields)
        except Exception:
            return

    @staticmethod
    def _union_seconds(intervals: list[tuple[float, float]]) -> float:
        if not intervals:
            return 0.0
        ordered = sorted((start, end) for start, end in intervals if end >= start)
        if not ordered:
            return 0.0
        total = 0.0
        current_start, current_end = ordered[0]
        for start, end in ordered[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
                continue
            total += current_end - current_start
            current_start, current_end = start, end
        return total + current_end - current_start


def safe_trace_action(action: Any) -> str:
    """Allow only fixed lifecycle tokens in the public diagnostic trace."""
    value = str(action or "").strip().lower()
    if value in _TRACE_ACTION_NAMES:
        return value
    if _TRACE_ACTION_RE.fullmatch(value) and value.startswith("provider."):
        return value if value in _TRACE_ACTION_NAMES else "provider.other"
    return "other"


__all__ = ["TimingTrace", "safe_trace_action"]
