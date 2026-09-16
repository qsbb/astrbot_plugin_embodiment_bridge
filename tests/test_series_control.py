from __future__ import annotations

import asyncio
import json
from pathlib import Path

from astrbot_plugin_embodiment_bridge.series_control import SeriesControlAdapter

from .http_harness import build_plugin


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


class _FileBackedConfig(dict):
    """模拟 AstrBot 的可写配置：内存 + 原子落盘（写失败可注入）。"""

    def __init__(self, values: dict, path: Path, *, fail: bool = False) -> None:
        super().__init__(values)
        self.path = path
        self.fail = fail

    async def save_config_async(self, changes: dict) -> bool:
        if self.fail:
            self.update(changes)
            raise OSError("disk failed")
        merged = dict(self)
        merged.update(changes)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.path)
        self.update(changes)
        return True


_NATIVE_DEFAULTS = {
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
}


def test_contract_declares_native_read_and_write_capabilities(tmp_path: Path) -> None:
    contract = SeriesControlAdapter(_Plugin(tmp_path)).series_control_contract()

    capabilities = set(contract["capabilities"])
    assert {"read_native", "write_native"} <= capabilities
    assert contract["read_only"] is False


def test_snapshot_exposes_native_values_for_freeze_and_import(tmp_path: Path) -> None:
    adapter = SeriesControlAdapter(_Plugin(tmp_path))
    adapter.apply_series_control_patch({"max_sessions": 2}, expected_revision=0)

    snapshot = adapter.series_control_snapshot()
    item = snapshot["fields"]["max_sessions"]
    assert item["native_value"] == 8
    assert item["effective_value"] == 2
    assert item["managed_configured"] is True
    assert "secret" not in item


def test_native_write_requires_plugin_hook(tmp_path: Path) -> None:
    adapter = SeriesControlAdapter(_Plugin(tmp_path))

    result = asyncio.run(
        adapter.series_control_native_write({"max_sessions": 2}, expected_revision=0)
    )

    assert result["status"] == "error"
    assert result["reason"] == "UNSUPPORTED"


def test_native_write_persists_native_config_and_backs_up(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = build_plugin(monkeypatch, tmp_path)
    plugin = bundle.plugin
    config_path = tmp_path / "astrbot-config.json"
    plugin.config = _FileBackedConfig(dict(_NATIVE_DEFAULTS), config_path)

    result = asyncio.run(
        plugin.series_control_native_write({"max_sessions": 2}, expected_revision=0)
    )

    assert result["status"] == "ok"
    assert result["reason"] == "APPLIED"
    assert result["written"] == ["max_sessions"]
    assert result["backup_id"]

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["max_sessions"] == 2
    assert plugin.config["max_sessions"] == 2
    assert plugin.sessions.max_sessions == 2

    backup = plugin.data_dir / f"native-backup-{result['backup_id']}.json"
    payload = json.loads(backup.read_text(encoding="utf-8"))
    assert payload["native_values"]["max_sessions"] == 8
    assert payload["config"]["max_sessions"] == 8

    snapshot = plugin.series_control_snapshot()
    assert snapshot["fields"]["max_sessions"]["native_value"] == 2


def test_native_write_rejects_unknown_invalid_and_stale_patches(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = build_plugin(monkeypatch, tmp_path)
    plugin = bundle.plugin
    config_path = tmp_path / "astrbot-config.json"
    plugin.config = _FileBackedConfig(dict(_NATIVE_DEFAULTS), config_path)

    unknown = asyncio.run(
        plugin.series_control_native_write({"bridge_api_key": "x"}, expected_revision=0)
    )
    assert unknown["status"] == "error"
    assert unknown["reason"] == "UNKNOWN_FIELD"

    invalid = asyncio.run(
        plugin.series_control_native_write({"max_sessions": True}, expected_revision=0)
    )
    assert invalid["status"] == "error"
    assert invalid["reason"] == "INVALID_VALUE"

    stale = asyncio.run(
        plugin.series_control_native_write({"max_sessions": 2}, expected_revision=9)
    )
    assert stale["status"] == "error"
    assert stale["reason"] == "REVISION_CONFLICT"
    assert not config_path.exists()


def test_native_write_rolls_back_memory_when_persist_fails(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = build_plugin(monkeypatch, tmp_path)
    plugin = bundle.plugin
    config_path = tmp_path / "astrbot-config.json"
    plugin.config = _FileBackedConfig(dict(_NATIVE_DEFAULTS), config_path, fail=True)

    result = asyncio.run(
        plugin.series_control_native_write({"max_sessions": 2}, expected_revision=0)
    )

    assert result["status"] == "error"
    assert result["reason"].startswith("PERSIST_FAILED:")
    assert plugin.config["max_sessions"] == 8
    assert plugin.sessions.max_sessions == 8
    assert not config_path.exists()
