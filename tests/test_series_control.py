from __future__ import annotations

from pathlib import Path

from astrbot_plugin_embodiment_bridge.series_control import SeriesControlAdapter


class _Config(dict):
    pass


class _Plugin:
    def __init__(self, tmp_path: Path) -> None:
        self.data_dir = tmp_path
        self.config = _Config(
            {
                "diagnostic_log_enabled": False,
                "diagnostic_platform_log_enabled": False,
                "server_timing_enabled": False,
                "max_sessions": 8,
                "event_queue_size": 64,
                "max_audio_seconds": 60,
                "max_audio_chunk_bytes": 16000,
                "interaction_debounce_ms": 250,
                "output_chunk_ms": 50,
                "sse_heartbeat_seconds": 15,
                "max_tts_audio_seconds": 120,
                "bridge_api_key": "must-never-be-exposed",
                "chat_provider_id": "must-never-be-exposed",
            }
        )
        self.applied: list[dict[str, object]] = []

    def _apply_series_control_runtime(self, values: dict[str, object]) -> None:
        self.applied.append(dict(values))


def test_contract_exposes_only_non_secret_runtime_fields(tmp_path: Path) -> None:
    adapter = SeriesControlAdapter(_Plugin(tmp_path))

    contract = adapter.series_control_contract()
    schema = adapter.series_control_schema()

    assert contract["name"] == "series.control@1.0"
    assert contract["plugin_id"] == "astrbot_plugin_embodiment_bridge"
    assert contract["secrets_in_response"] is False
    assert "bridge_api_key" not in schema["fields"]
    assert "chat_provider_id" not in schema["fields"]
    assert all(field["secret"] is False for field in schema["fields"].values())
    assert schema["fields"]["max_sessions"]["maximum"] == 64


def test_apply_persists_and_applies_runtime_values(tmp_path: Path) -> None:
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)

    result = adapter.apply_series_control_patch(
        {
            "max_sessions": 2,
            "event_queue_size": 32,
            "diagnostic_log_enabled": True,
            "output_chunk_ms": 80,
        },
        expected_revision=0,
    )

    assert result["status"] == "ok"
    assert result["revision"] == 1
    assert plugin.applied[-1]["max_sessions"] == 2
    assert plugin.applied[-1]["diagnostic_log_enabled"] is True
    assert (tmp_path / "series-control.json").exists()

    restarted = SeriesControlAdapter(_Plugin(tmp_path))
    assert restarted.series_control_snapshot()["revision"] == 1
    assert restarted.series_control_snapshot()["fields"]["max_sessions"][
        "effective_source"
    ] == "managed"
    assert restarted.effective_config()["max_sessions"] == 2


def test_native_mode_restores_native_values_without_deleting_overlay(tmp_path: Path) -> None:
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    adapter.apply_series_control_patch({"max_sessions": 3}, expected_revision=0)

    adapter.series_control_set_mode("native")
    snapshot = adapter.series_control_snapshot()
    assert snapshot["fields"]["max_sessions"]["managed_configured"] is True
    assert snapshot["fields"]["max_sessions"]["effective_source"] == "plugin"
    assert adapter.effective_config()["max_sessions"] == 8


def test_validation_fails_closed_for_unknown_invalid_and_stale_patches(tmp_path: Path) -> None:
    adapter = SeriesControlAdapter(_Plugin(tmp_path))

    assert adapter.validate_series_control_patch(
        {"bridge_api_key": "x"}, expected_revision=0
    )["reason"] == "UNKNOWN_FIELD"
    assert adapter.validate_series_control_patch(
        {"max_sessions": True}, expected_revision=0
    )["reason"] == "INVALID_VALUE"
    assert adapter.validate_series_control_patch(
        {"max_sessions": 65}, expected_revision=0
    )["reason"] == "INVALID_VALUE"
    assert adapter.apply_series_control_patch(
        {"max_sessions": 2}, expected_revision=1
    )["reason"] == "REVISION_CONFLICT"


def test_reset_restores_native_and_rolls_back_on_persist_failure(
    tmp_path: Path, monkeypatch
) -> None:
    plugin = _Plugin(tmp_path)
    adapter = SeriesControlAdapter(plugin)
    adapter.apply_series_control_patch({"max_sessions": 3}, expected_revision=0)

    original = adapter._persist

    def fail_persist() -> None:
        raise OSError("read-only")

    monkeypatch.setattr(adapter, "_persist", fail_persist)
    failed = adapter.reset_series_control_override(expected_revision=1)
    assert failed["reason"] == "APPLY_FAILED_ROLLED_BACK"
    assert adapter.effective_config()["max_sessions"] == 3
    assert adapter.series_control_snapshot()["revision"] == 1

    monkeypatch.setattr(adapter, "_persist", original)
    reset = adapter.reset_series_control_override(expected_revision=1)
    assert reset["status"] == "ok"
    assert adapter.effective_config()["max_sessions"] == 8
