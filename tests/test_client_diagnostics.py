"""Tests for the bounded Quest→bridge client diagnostics channel.

Covers the ``diagnostics@1.0`` request schema, the per-session in-memory ring
buffer (rate limit, session eviction, forgetting on close), and the fact that
client reports never reach the persistent diagnostic log.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.core.client_diagnostics import (
    ClientDiagnosticsStore,
)
from astrbot_plugin_embodiment_bridge.core.models import DiagnosticReportRequest

SESSION_ID = "session-diag-1"
TURN_ID = "turn-diag-1"
TRACE_ID = "tracef9d3fe6a6114"


def _perf_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "diagnostics.report",
        "protocol_version": "1.0",
        "kind": "perf",
        "session_id": SESSION_ID,
        "turn_id": "",
        "trace_id": "",
        "ts_ms": 12_345,
        "fps": 71.5,
        "frame_p50_ms": 13.2,
        "frame_p95_ms": 19.8,
        "frame_max_ms": 32.4,
        "compositor_dropped_session": 5.0,
        "physics_dropped_s": 0.375,
        "physics_dropped_frames": 6,
        "xr_cpu_ms": 8.25,
        "xr_gpu_ms": 9.5,
        "cpu_util": 41.0,
        "gpu_util": 57.0,
        "mmd_solver_ms": 2.0,
        "mmd_physics_ms": 5.5,
        "mmd_bone_ik_ms": 1.25,
        "mmd_sdef_ms": 3.75,
        "mmd_flush_ms": 0.5,
        "hand_contact_ms": 0.25,
        "mem_alloc_bytes": 524_288_000,
        "mem_pss_bytes": 1_048_576_000,
        "gc0": 4,
        "gc1": 2,
        "gc2": 1,
        "thermal_state": "nominal",
        "model_renderer": 14,
        "model_material": 32,
        "model_texture": 41,
        "model_vertex": 70_000,
        "model_tri": 68_000,
        "model_bone": 150,
        "model_rigid": 90,
        "model_joint": 80,
        "target_fps": 72.0,
        "render_scale": 1.0,
        "headset_worn": True,
        "active_action": "idle",
        "physics_hz": 30,
        "physics_substeps": 1,
    }
    payload.update(overrides)
    return payload


def _spans_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "diagnostics.report",
        "protocol_version": "1.0",
        "kind": "spans",
        "session_id": SESSION_ID,
        "turn_id": TURN_ID,
        "trace_id": TRACE_ID,
        "ts_ms": 88_000,
        "spans": [
            {
                "component": "capture",
                "stage": "capture",
                "status": "completed",
                "code": "first_chunk",
                "start_offset_ms": 0,
                "end_offset_ms": 120,
                "duration_ms": -1,
                "chunks": 0,
            },
            {
                "component": "playback",
                "stage": "playback",
                "status": "completed",
                "code": "playback_start",
                "start_offset_ms": 5200,
                "end_offset_ms": 5320,
                "duration_ms": 120,
                "chunks": 194,
            },
        ],
    }
    payload.update(overrides)
    return payload


def _parse(payload: dict[str, Any]) -> DiagnosticReportRequest:
    return DiagnosticReportRequest.model_validate_json(json.dumps(payload))


def test_perf_report_parses_with_flat_metrics() -> None:
    report = _parse(_perf_payload())
    assert report.kind == "perf"
    assert report.fps == pytest.approx(71.5)
    assert report.physics_hz == 30
    assert report.active_action == "idle"
    assert report.spans == []


def test_spans_report_parses_and_requires_turn_id() -> None:
    report = _parse(_spans_payload())
    assert report.kind == "spans"
    assert report.trace_id == TRACE_ID
    assert len(report.spans) == 2

    with pytest.raises(Exception):
        _parse(_spans_payload(turn_id=""))


def test_report_schema_rejects_invalid_payloads() -> None:
    with pytest.raises(Exception):
        _parse(_perf_payload(fps=-1))
    with pytest.raises(Exception):
        _parse(_spans_payload(spans=[]))
    with pytest.raises(Exception):
        _parse(_perf_payload(kind="spans"))
    with pytest.raises(Exception):
        _parse(_perf_payload(unexpected_field=1))


def test_store_records_reports_and_projects_snapshot() -> None:
    store = ClientDiagnosticsStore(min_report_interval_seconds=0.0)

    accepted_perf = store.record_report("owner", _parse(_perf_payload()))
    assert accepted_perf["accepted"] is True
    accepted_spans = store.record_report("owner", _parse(_spans_payload()))
    assert accepted_spans["accepted"] is True

    snapshot = store.snapshot()
    assert snapshot["contract"] == "diagnostics@1.0"
    assert snapshot["status"] == "ready"
    assert snapshot["totals"]["accepted"] == 2
    assert len(snapshot["sessions"]) == 1
    session = snapshot["sessions"][0]
    assert session["session_id"] == SESSION_ID
    assert session["aggregates"]["perf_count"] == 1
    assert session["aggregates"]["span_events"] == 2
    assert session["aggregates"]["avg_fps"] == pytest.approx(71.5, abs=0.11)
    assert session["aggregates"]["physics_dropped_max_s"] == pytest.approx(0.375)
    assert session["latest_perf"]["fps"] == pytest.approx(71.5)
    kinds = [event["kind"] for event in session["events"]]
    assert kinds == ["perf", "spans"]


def test_store_enforces_rate_limit_per_session() -> None:
    store = ClientDiagnosticsStore(min_report_interval_seconds=60.0)

    first = store.record_report("owner", _parse(_perf_payload()))
    assert first["accepted"] is True
    second = store.record_report("owner", _parse(_perf_payload(fps=60.0)))
    assert second["accepted"] is False
    assert second["reason"] == "rate_limited"
    snapshot = store.snapshot()
    assert snapshot["totals"]["rejected"] == 1
    assert snapshot["sessions"][0]["aggregates"]["rejected_count"] == 1
    # 被拒的快照不得污染最新性能数据。
    assert snapshot["sessions"][0]["latest_perf"]["fps"] == pytest.approx(71.5)


def test_store_ring_buffer_is_bounded_and_drops_oldest() -> None:
    store = ClientDiagnosticsStore(
        max_events_per_session=4,
        min_report_interval_seconds=0.0,
    )
    for index in range(6):
        report = _parse(_perf_payload(fps=60.0 + index, ts_ms=index))
        assert store.record_report("owner", report)["accepted"] is True
    session = store.snapshot()["sessions"][0]
    assert len(session["events"]) == 4
    assert [event["ts_ms"] for event in session["events"]] == [2, 3, 4, 5]


def test_store_evicts_stale_sessions_beyond_capacity() -> None:
    store = ClientDiagnosticsStore(max_sessions=2, min_report_interval_seconds=0.0)
    for index in range(3):
        assert store.record_report(
            "owner",
            _parse(_perf_payload(session_id=f"session-{index}")),
        )["accepted"] is True
    snapshot = store.snapshot()
    assert snapshot["totals"]["sessions"] == 2
    assert {item["session_id"] for item in snapshot["sessions"]} == {
        "session-1",
        "session-2",
    }


def test_forget_drops_session_buffer() -> None:
    store = ClientDiagnosticsStore(min_report_interval_seconds=0.0)
    store.record_report("owner", _parse(_perf_payload()))
    store.forget(SESSION_ID)
    assert store.snapshot()["status"] == "empty"
    store.forget("")
    store.forget("unknown-session")


def test_store_projection_carries_no_free_text() -> None:
    # The request schema is the sanitization boundary: overlong or free-text
    # fields never even reach the store, so the projection stays bounded.
    with pytest.raises(Exception):
        _parse(_spans_payload(trace_id="x" * 200))
    with pytest.raises(Exception):
        _parse(_spans_payload(turn_id="y" * 200))
    with pytest.raises(Exception):
        _parse(
            _spans_payload(
                spans=[
                    {
                        "component": "capture",
                        "stage": "capture",
                        "code": "z" * 500,
                        "start_offset_ms": 0,
                        "end_offset_ms": 1,
                    }
                ]
            )
        )
    store = ClientDiagnosticsStore(min_report_interval_seconds=0.0)
    store.record_report("owner", _parse(_spans_payload()))
    entry = next(
        event
        for event in store.snapshot()["sessions"][0]["events"]
        if event["kind"] == "spans"
    )
    assert len(entry["trace_id"]) <= 64
    assert len(entry["turn_id"]) <= 64
    assert "user_text" not in entry
