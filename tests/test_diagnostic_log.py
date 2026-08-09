from __future__ import annotations

import json
import logging
import asyncio
import threading
from pathlib import Path
from typing import Any

from astrbot_plugin_quest_avatar_bridge.core.diagnostic_log import (
    DiagnosticLog,
    DiagnosticLogSink,
)


def test_disabled_logger_does_not_create_file(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path)

    diagnostic.record("plugin.initialized", component="plugin", status="ok")

    assert not diagnostic.path.exists()


def test_diagnostics_snapshot_keeps_memory_events_when_file_log_disabled(
    tmp_path: Path,
) -> None:
    diagnostic = DiagnosticLog(tmp_path)
    diagnostic.record(
        "session.authorization",
        component="identity",
        status="blocked",
        reason_code="owner_not_configured",
        authorized=False,
    )

    payload = diagnostic.diagnostic_events()

    assert payload["contract"] == "quest_avatar_bridge.diagnostics@1.0"
    assert payload["plugin_id"] == "astrbot_plugin_quest_avatar_bridge"
    assert payload["plugin_name"] == "临"
    assert payload["status"] == "memory_only"
    assert payload["reason"] == "FILE_LOG_DISABLED"
    assert payload["events"][0]["details"] == {
        "component": "identity",
        "status": "blocked",
        "reason_code": "owner_not_configured",
        "authorized": False,
    }
    assert not diagnostic.path.exists()


def test_diagnostics_provider_exposes_bounded_safe_events_and_clear_cursor(
    tmp_path: Path,
) -> None:
    diagnostic = DiagnosticLog(tmp_path, enabled=True)
    diagnostic.record(
        "http.request",
        component="transport",
        status=200,
        operation="session_start",
        user_id="user-secret",
        reply_text="reply-secret",
    )

    payload = diagnostic.diagnostic_events(after_seq=0, limit=10)
    event = payload["events"][0]

    assert payload["status"] == "ready"
    assert payload["reason"] == "READY"
    assert event["seq"] == 1
    assert event["plugin_name"] == "临"
    assert event["level"] == "INFO"
    assert event["code"] == "http.request"
    assert event["details"] == {
        "component": "transport",
        "status": 200,
        "operation": "session_start",
    }
    old_stream_id = payload["stream_id"]

    diagnostic.diagnostic_clear()
    cleared = diagnostic.diagnostic_events()
    assert cleared["events"] == []
    assert cleared["stream_id"] != old_stream_id


def test_diagnostics_provider_reports_unavailable_after_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)
        monkeypatch.setattr(
            diagnostic,
            "_write_line",
            lambda _line: (_ for _ in ()).throw(OSError("disk full")),
        )
        await diagnostic.start()
        diagnostic.record(
            "listener.start_error", component="listener", code="bind_failed"
        )
        assert await diagnostic.flush() is False

        payload = diagnostic.diagnostic_events()
        assert payload["status"] == "memory_only"
        assert payload["reason"] == "FILE_LOG_UNAVAILABLE"
        assert payload["events"][0]["details"]["code"] == "bind_failed"
        await diagnostic.close()

    asyncio.run(scenario())


def test_logger_does_not_attach_root_handler(tmp_path: Path) -> None:
    root = logging.getLogger()
    handlers_before = tuple(root.handlers)
    names_before = set(logging.Logger.manager.loggerDict)

    diagnostic = DiagnosticLog(tmp_path, enabled=True)
    diagnostic.record("http.request", component="transport", status=200)

    assert tuple(root.handlers) == handlers_before
    assert set(logging.Logger.manager.loggerDict) == names_before


def test_component_sink_does_not_forward_message_or_arguments(tmp_path: Path) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)
        await diagnostic.start()
        sink = DiagnosticLogSink(diagnostic)
        sink.error("API key=%s reply=%s", "credential-secret", "reply-secret")
        assert await diagnostic.flush()

        line = diagnostic.path.read_text(encoding="utf-8")
        assert "credential-secret" not in line
        assert "reply-secret" not in line
        assert json.loads(line)["event"] == "component.error"
        await diagnostic.close()

    asyncio.run(scenario())


def test_platform_logger_mirror_is_namespaced_and_redacted(
    tmp_path: Path, caplog: Any
) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True, platform_log_enabled=True)
        caplog.set_level(
            logging.INFO,
            logger="astrbot.plugin.astrbot_plugin_quest_avatar_bridge",
        )
        await diagnostic.start()
        diagnostic.record(
            "turn.failed",
            component="turn",
            status="failed",
            code="provider_error",
            session_id="hidden-session",
            reply_text="hidden-reply",
        )
        assert await diagnostic.flush()
        records = [
            record
            for record in caplog.records
            if record.name == "astrbot.plugin.astrbot_plugin_quest_avatar_bridge"
        ]
        assert records
        rendered = records[-1].getMessage()
        assert "provider_error" in rendered
        assert "hidden-session" not in rendered
        assert "hidden-reply" not in rendered
        assert diagnostic._platform_logger.handlers == []
        await diagnostic.close()

    asyncio.run(scenario())


def test_sensitive_fields_are_omitted_and_values_are_bounded(tmp_path: Path) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)
        await diagnostic.start()
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
        assert await diagnostic.flush()

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
        await diagnostic.close()

    asyncio.run(scenario())


def test_stage_fields_and_aggregate_counters_survive_redaction(tmp_path: Path) -> None:
    diagnostic = DiagnosticLog(tmp_path)

    diagnostic.record(
        "reply.completed",
        component="pipeline",
        phase="eventbus",
        status="completed",
        reason_code="owner_not_configured",
        http_status=202,
        attempt=2,
        queue_depth=3,
        active_sessions=4,
        attached_streams=1,
        event_count=8,
        bytes=32000,
        chunks=7,
        authorized=False,
        text_sent=True,
        audio_sent=False,
        event_woken=True,
        event_stopped=True,
        send_observed=False,
        session_id="hidden-session",
        reply_text="hidden-reply",
    )

    details = diagnostic.diagnostic_events()["events"][0]["details"]
    assert details == {
        "component": "pipeline",
        "phase": "eventbus",
        "status": "completed",
        "reason_code": "owner_not_configured",
        "http_status": 202,
        "attempt": 2,
        "queue_depth": 3,
        "active_sessions": 4,
        "attached_streams": 1,
        "event_count": 8,
        "bytes": 32000,
        "chunks": 7,
        "authorized": False,
        "text_sent": True,
        "audio_sent": False,
        "event_woken": True,
        "event_stopped": True,
        "send_observed": False,
    }


def test_rotation_and_concurrent_writes_keep_valid_jsonl(tmp_path: Path) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(
            tmp_path, enabled=True, max_bytes=16_384, backup_count=2, queue_size=512
        )
        await diagnostic.start()

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

        await asyncio.gather(
            *(asyncio.to_thread(write_batch, item) for item in range(8))
        )
        assert await diagnostic.flush()
        await diagnostic.close()

        files = [diagnostic.path, *sorted(tmp_path.glob("quest_avatar_bridge.log.*"))]
        assert diagnostic.path.exists()
        assert any(path.name.endswith(".1") for path in files)
        for path in files:
            assert path.stat().st_size <= diagnostic.max_bytes
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    asyncio.run(scenario())


def test_write_failure_degrades_without_raising(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)

        def fail(_line: str) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(diagnostic, "_write_line", fail)
        await diagnostic.start()
        diagnostic.record(
            "listener.start_error", component="listener", code="bind_failed"
        )
        assert await diagnostic.flush() is False
        diagnostic.record("listener.closed", component="listener", status="closed")

        assert diagnostic.degraded is True
        assert diagnostic.write_failures == 1
        await diagnostic.close()

    asyncio.run(scenario())


def test_slow_disk_write_does_not_block_event_loop(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)
        entered = threading.Event()
        release = threading.Event()

        def slow_write(_line: str) -> None:
            entered.set()
            release.wait(timeout=1)

        monkeypatch.setattr(diagnostic, "_write_line", slow_write)
        await diagnostic.start()
        diagnostic.record("http.request", component="transport", status=200)
        assert await asyncio.to_thread(entered.wait, 1)

        progressed = False

        async def tick() -> None:
            nonlocal progressed
            await asyncio.sleep(0)
            progressed = True

        await asyncio.wait_for(tick(), timeout=0.1)
        assert progressed is True
        release.set()
        assert await diagnostic.flush()
        await diagnostic.close()

    asyncio.run(scenario())


def test_persona_diagnostics_only_write_boolean_configuration_state(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        diagnostic = DiagnosticLog(tmp_path, enabled=True)
        await diagnostic.start()
        diagnostic.record(
            "persona.status",
            component="persona",
            status="ready",
            persona_source="astrbot_selected",
            persona_status="ready",
            persona_configured=True,
            character_name_configured=False,
            name_configured=False,
            character_name="name-secret",
            persona_text="persona-secret",
            astrbot_persona_id="persona-secret-id",
        )
        assert await diagnostic.flush()
        assert diagnostic.status_snapshot() == {
            "enabled": True,
            "status": "ready",
            "write_failures": 0,
        }

        line = diagnostic.path.read_text(encoding="utf-8")
        payload = json.loads(line)
        assert payload["persona_configured"] is True
        assert payload["character_name_configured"] is False
        assert payload["name_configured"] is False
        assert payload["persona_source"] == "astrbot_selected"
        assert payload["persona_status"] == "ready"
        assert "name-secret" not in line
        assert "persona-secret" not in line
        assert "persona-secret-id" not in line
        await diagnostic.close()

    asyncio.run(scenario())
