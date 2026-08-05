from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from astrbot_plugin_quest_avatar_bridge.core.diagnostic_log import (
    DiagnosticLog,
    DiagnosticLogSink,
)


def test_disabled_logger_does_not_create_file(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path)

    diagnostic.record("plugin.initialized", component="plugin", status="ok")

    assert not diagnostic.path.exists()


def test_logger_does_not_attach_root_handler(tmp_path: Path) -> None:
    root = logging.getLogger()
    handlers_before = tuple(root.handlers)
    names_before = set(logging.Logger.manager.loggerDict)

    diagnostic = DiagnosticLog(tmp_path, enabled=True)
    diagnostic.record("http.request", component="transport", status=200)

    assert tuple(root.handlers) == handlers_before
    assert set(logging.Logger.manager.loggerDict) == names_before


def test_component_sink_does_not_forward_message_or_arguments(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path, enabled=True)
    sink = DiagnosticLogSink(diagnostic)

    sink.error("API key=%s reply=%s", "credential-secret", "reply-secret")

    line = diagnostic.path.read_text(encoding="utf-8")
    assert "credential-secret" not in line
    assert "reply-secret" not in line
    assert json.loads(line)["event"] == "component.error"


def test_sensitive_fields_are_omitted_and_values_are_bounded(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path, enabled=True)
    diagnostic.record(
        "http.session_start",
        component="transport",
        status=201,
        session_id="session-secret",
        turn_id="turn-secret",
        user_id="user-secret",
        api_key="api-secret",
        reply_text="reply-secret",
        error_type="ValueError",
        duration_ms=12.3456,
    )

    line = diagnostic.path.read_text(encoding="utf-8")
    payload = json.loads(line)
    assert payload["event"] == "http.session_start"
    assert payload["status"] == 201
    assert payload["duration_ms"] == 12.346
    assert all(
        secret not in line
        for secret in (
            "session-secret",
            "turn-secret",
            "user-secret",
            "api-secret",
            "reply-secret",
        )
    )
    assert "session_id" not in payload
    assert "turn_id" not in payload
    assert "api_key" not in payload


def test_rotation_and_concurrent_writes_keep_valid_jsonl(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path, enabled=True, max_bytes=16_384, backup_count=2)

    def write_batch(offset: int) -> None:
        for index in range(40):
            diagnostic.record(
                "http.request",
                component="transport",
                operation="session_start",
                status=201,
                duration_ms=index,
                reason=f"batch_{offset}",
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_batch, range(8)))

    files = [diagnostic.path, *sorted(tmp_path.glob("quest_avatar_bridge.log.*"))]
    assert diagnostic.path.exists()
    assert any(path.name.endswith(".1") for path in files)
    for path in files:
        assert path.stat().st_size <= diagnostic.max_bytes
        for line in path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_write_failure_degrades_without_raising(tmp_path: Path, monkeypatch) -> None:
    diagnostic = DiagnosticLog(tmp_path, enabled=True)

    def fail(_line: str) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(diagnostic, "_write_line", fail)
    diagnostic.record("listener.start_error", component="listener", code="bind_failed")
    diagnostic.record("listener.closed", component="listener", status="closed")

    assert diagnostic.degraded is True
    assert diagnostic.write_failures == 1
