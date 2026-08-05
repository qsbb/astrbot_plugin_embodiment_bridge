from __future__ import annotations

import json
import os
import re
import threading
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
    }
)
_SENSITIVE_NAME_RE = re.compile(
    r"(?:key|token|jwt|auth|secret|password|credential|api|base_url|url|path|body|"
    r"audio|reply|text|session|turn|person|platform|user|bot|group|client|identity)",
    re.IGNORECASE,
)


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
    ) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / self.filename
        self.enabled = bool(enabled)
        self.max_bytes = max(16_384, min(int(max_bytes), 16 * 1_048_576))
        self.backup_count = max(0, min(int(backup_count), 10))
        self._lock = threading.RLock()
        self._write_failures = 0
        self._disabled_due_error = False

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def degraded(self) -> bool:
        return self._disabled_due_error

    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled or self._disabled_due_error:
            return
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": self._safe_token(event, fallback="diagnostic"),
        }
        for name, value in fields.items():
            if name not in _SAFE_FIELD_NAMES or _SENSITIVE_NAME_RE.search(name):
                continue
            safe = self._safe_value(name, value)
            if safe is not None:
                payload[name] = safe
        try:
            line = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
            self._write_line(line)
        except Exception:
            # Diagnostics must never change plugin or request behavior.
            self._write_failures += 1
            self._disabled_due_error = True

    def close(self) -> None:
        return None

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

    @classmethod
    def _safe_value(cls, name: str, value: Any) -> Any:
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
