from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_SAFE_FIELD_NAMES = frozenset(
    {
        "component",
        "operation",
        "status",
        "code",
        "reason",
        "error_type",
        "state",
        "method",
        "route",
        "event_type",
        "enabled",
        "available",
        "ready",
        "degraded",
        "duration_ms",
        "bytes",
        "chunks",
        "sequence",
        "result",
        "rotated",
        "persona_configured",
        "character_name_configured",
        "name_configured",
        "persona_source",
        "persona_status",
    }
)
_SENSITIVE_NAME_RE = re.compile(
    r"(?:key|token|jwt|auth|secret|password|credential|api|base_url|url|path|body|"
    r"audio|reply|text|session|turn|person|platform|user|bot|group|client|identity)",
    re.IGNORECASE,
)
_SAFE_BOOLEAN_STATUS_FIELDS = frozenset(
    {"persona_configured", "character_name_configured", "name_configured"}
)
_SAFE_PERSONA_ENUM_FIELDS = frozenset({"persona_source", "persona_status"})
_SAFE_PERSONA_ENUM_VALUES = frozenset(
    {
        "astrbot_selected",
        "astrbot_default",
        "manual_override",
        "generic",
        "ready",
        "not_checked",
        "selected_missing",
        "default_missing",
        "timeout",
        "unavailable",
        "configuration_invalid",
    }
)

PLUGIN_ID = "astrbot_plugin_quest_avatar_bridge"
PLUGIN_NAME = "临"
DIAGNOSTIC_CONTRACT = "series.diagnostics@1.0"
DIAGNOSTIC_SERIES_ID = "ningxin_suxi"
_MAX_EVENTS = 1000


class DiagnosticLog:
    """Small plugin-owned JSONL logger with no logging-module integration."""

    filename = "quest_avatar_bridge.log"

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: bool = False,
        max_bytes: int = 1_048_576,
        backup_count: int = 3,
        queue_size: int = 256,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / self.filename
        self.enabled = bool(enabled)
        self.max_bytes = max(16_384, min(int(max_bytes), 16 * 1_048_576))
        self.backup_count = max(0, min(int(backup_count), 10))
        self._lock = threading.RLock()
        self._write_failures = 0
        self._disabled_due_error = False
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._pending: deque[str] = deque(maxlen=max(16, min(queue_size, 4096)))
        self._stream_id = uuid.uuid4().hex
        self._sequence = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closing = False
        self._writing = False

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def degraded(self) -> bool:
        return self._disabled_due_error

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if not self.enabled:
                status = "disabled"
            elif self._disabled_due_error:
                status = "unavailable"
            else:
                status = "ready"
            return {
                "enabled": self.enabled,
                "status": status,
                "write_failures": self._write_failures,
            }

    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled or self._disabled_due_error or self._closing:
            return
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": self._safe_token(event, fallback="diagnostic"),
        }
        for name, value in fields.items():
            if name not in _SAFE_FIELD_NAMES or (
                _SENSITIVE_NAME_RE.search(name)
                and name not in _SAFE_BOOLEAN_STATUS_FIELDS
                and name not in _SAFE_PERSONA_ENUM_FIELDS
            ):
                continue
            safe = self._safe_value(name, value)
            if safe is not None:
                payload[name] = safe
        try:
            line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
            with self._lock:
                self._append_event_unlocked(payload)
                self._pending.append(line)
                loop = self._loop
                wake = self._wake
            if loop is not None and wake is not None:
                loop.call_soon_threadsafe(wake.set)
        except Exception:
            # Diagnostics must never change plugin or request behavior.
            self._write_failures += 1
            self._disabled_due_error = True

    async def start(self) -> None:
        if (
            not self.enabled
            or self._disabled_due_error
            or self._writer_task is not None
        ):
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._writer_task = asyncio.create_task(
            self._writer(), name="quest-avatar:diagnostic-writer"
        )
        with self._lock:
            has_pending = bool(self._pending)
        if has_pending:
            self._wake.set()

    async def flush(self, timeout: float = 2.0) -> bool:
        if not self.enabled or self._writer_task is None:
            return True

        async def wait_until_drained() -> None:
            while True:
                with self._lock:
                    drained = not self._pending and not self._writing
                if drained or self._disabled_due_error:
                    return
                await asyncio.sleep(0.01)

        try:
            async with asyncio.timeout(max(0.05, timeout)):
                await wait_until_drained()
            return not self._disabled_due_error
        except TimeoutError:
            return False

    async def close(self, timeout: float = 2.0) -> None:
        if self._closing:
            return
        self._closing = True
        task = self._writer_task
        if task is None:
            return
        wake = self._wake
        if wake is not None:
            wake.set()
        try:
            async with asyncio.timeout(max(0.05, timeout)):
                await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            self._writer_task = None
            self._loop = None
            self._wake = None

    async def _writer(self) -> None:
        wake = self._wake
        if wake is None:
            return
        while True:
            await wake.wait()
            while True:
                with self._lock:
                    if not self._pending:
                        self._writing = False
                        wake.clear()
                        should_close = self._closing
                        break
                    line = self._pending.popleft()
                    self._writing = True
                try:
                    await asyncio.to_thread(self._write_line, line)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    with self._lock:
                        self._write_failures += 1
                        self._disabled_due_error = True
                        self._pending.clear()
                        self._writing = False
                    return
            if should_close:
                return

    def _write_line(self, line: str) -> None:
        encoded_size = len(line.encode("utf-8"))
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            rotated = False
            try:
                current_size = self.path.stat().st_size
            except FileNotFoundError:
                current_size = 0
            if current_size and current_size + encoded_size > self.max_bytes:
                self._rotate()
                rotated = True
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            if rotated:
                # A rotation marker is intentionally omitted: it would create
                # another write and could recurse when the filesystem is full.
                return

    def _rotate(self) -> None:
        if self.backup_count <= 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self._backup_path(self.backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                os.replace(source, self._backup_path(index + 1))
        if self.path.exists():
            os.replace(self.path, self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _append_event(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._append_event_unlocked(payload)

    def _append_event_unlocked(self, payload: dict[str, Any]) -> None:
        self._sequence += 1
        details = {
            key: value for key, value in payload.items() if key not in {"ts", "event"}
        }
        self._events.append(
            {
                "seq": self._sequence,
                "timestamp": str(payload.get("ts") or ""),
                "plugin_id": PLUGIN_ID,
                "plugin_name": PLUGIN_NAME,
                "level": self._event_level(str(payload.get("event") or "")),
                "code": str(payload.get("event") or "diagnostic"),
                "summary": str(payload.get("event") or "diagnostic"),
                "details": details,
            }
        )

    @staticmethod
    def _event_level(event: str) -> str:
        lowered = event.lower()
        if "error" in lowered or "failed" in lowered:
            return "ERROR"
        if "warning" in lowered or "warn" in lowered:
            return "WARNING"
        return "INFO"

    def diagnostic_events(
        self, *, after_seq: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        after = max(0, int(after_seq or 0))
        size = min(_MAX_EVENTS, max(1, int(limit or 200)))
        with self._lock:
            first = self._events[0]["seq"] if self._events else self._sequence + 1
            base = {
                "contract": DIAGNOSTIC_CONTRACT,
                "plugin_id": PLUGIN_ID,
                "plugin_name": PLUGIN_NAME,
                "stream_id": self._stream_id,
                "events": [],
                "next_seq": self._sequence,
                "dropped_before": max(0, first - 1),
            }
            if not self.enabled:
                base.update(status="disabled", reason="DIAGNOSTIC_DISABLED")
                return base
            if self._disabled_due_error:
                base.update(status="unavailable", reason="DIAGNOSTIC_UNAVAILABLE")
                return base
            base["status"] = "ready"
            base["reason"] = "READY"
            base["events"] = [
                dict(event) for event in self._events if event["seq"] > after
            ][-size:]
            return base

    def diagnostic_clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._stream_id = uuid.uuid4().hex

    @classmethod
    def _safe_value(cls, name: str, value: Any) -> Any:
        if name in _SAFE_BOOLEAN_STATUS_FIELDS:
            return value if isinstance(value, bool) else None
        if name in _SAFE_PERSONA_ENUM_FIELDS:
            return value if value in _SAFE_PERSONA_ENUM_VALUES else None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return max(-1_000_000_000, min(value, 1_000_000_000))
        if isinstance(value, float):
            return round(max(-1_000_000_000.0, min(value, 1_000_000_000.0)), 3)
        if isinstance(value, str):
            if name in {"reason", "error_type"}:
                return cls._safe_token(value, fallback="redacted")
            return cls._safe_token(value, fallback="redacted")
        return None

    @staticmethod
    def _safe_token(value: str, *, fallback: str = "redacted") -> str:
        candidate = str(value or "")[:96]
        return candidate if _TOKEN_RE.fullmatch(candidate) else fallback


class DiagnosticLogSink:
    """Logger-shaped sink for existing components; never forwards messages."""

    def __init__(self, diagnostic_log: DiagnosticLog) -> None:
        self.diagnostic_log = diagnostic_log

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        self.diagnostic_log.record("component.info", component="plugin", status="info")

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        self.diagnostic_log.record(
            "component.warning", component="plugin", status="warning"
        )

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        self.diagnostic_log.record(
            "component.error", component="plugin", status="error"
        )


__all__ = ["DiagnosticLog", "DiagnosticLogSink"]
