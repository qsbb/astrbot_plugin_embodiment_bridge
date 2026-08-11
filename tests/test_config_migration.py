from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.core.config_migration import (
    MIGRATION_CONFIG_KEY,
    PluginConfigMigrationError,
    load_legacy_config_changes,
)
from astrbot_plugin_embodiment_bridge.core.plugin_identity import (
    LEGACY_PLUGIN_ID,
    PLUGIN_ID,
)


class ConfigStub(dict[str, Any]):
    def __init__(self, path: Path, values: dict[str, Any]) -> None:
        defaults = {
            MIGRATION_CONFIG_KEY: False,
            "bridge_service_enabled": True,
            "pairing_listener_port": 8520,
            "bridge_api_key": "",
            "pairing_listener_public_url": "",
        }
        super().__init__(defaults | values)
        self.config_path = str(path)
        self.default_config = defaults


def write_legacy(path: Path, payload: object) -> Path:
    legacy = path.with_name(f"{LEGACY_PLUGIN_ID}_config.json")
    legacy.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return legacy


def test_known_legacy_values_fill_only_new_defaults_and_set_marker(
    tmp_path: Path,
) -> None:
    current = tmp_path / f"{PLUGIN_ID}_config.json"
    current.write_text("{}", encoding="utf-8")
    legacy = write_legacy(
        current,
        {
            "bridge_service_enabled": False,
            "pairing_listener_port": 9000,
            "bridge_api_key": "legacy-secret",
            "unknown_key": "must-not-migrate",
            "pairing_listener_public_url": (
                "https://bridge.example/api/v1/plugins/extensions/"
                f"{LEGACY_PLUGIN_ID}/pairing/exchange"
            ),
        },
    )
    config = ConfigStub(current, {"pairing_listener_port": 9100})

    changes = load_legacy_config_changes(config)

    assert changes == {
        "bridge_service_enabled": False,
        "bridge_api_key": "legacy-secret",
        "pairing_listener_public_url": (
            "https://bridge.example/api/v1/plugins/extensions/"
            f"{PLUGIN_ID}/pairing/exchange"
        ),
        MIGRATION_CONFIG_KEY: True,
    }
    assert json.loads(legacy.read_text(encoding="utf-8"))["bridge_api_key"] == (
        "legacy-secret"
    )


def test_marker_prevents_reimport_and_wrong_config_filename_is_ignored(
    tmp_path: Path,
) -> None:
    current = tmp_path / f"{PLUGIN_ID}_config.json"
    current.write_text("{}", encoding="utf-8")
    write_legacy(current, {"bridge_service_enabled": False})

    assert load_legacy_config_changes(
        ConfigStub(current, {MIGRATION_CONFIG_KEY: True})
    ) == {}
    assert load_legacy_config_changes(ConfigStub(tmp_path / "other.json", {})) == {}


def test_invalid_or_oversized_legacy_config_fails_closed(tmp_path: Path) -> None:
    current = tmp_path / f"{PLUGIN_ID}_config.json"
    current.write_text("{}", encoding="utf-8")
    legacy = current.with_name(f"{LEGACY_PLUGIN_ID}_config.json")
    legacy.write_text("not-json", encoding="utf-8")

    with pytest.raises(PluginConfigMigrationError, match="legacy_plugin_config_invalid"):
        load_legacy_config_changes(ConfigStub(current, {}))


def test_values_with_incompatible_types_are_not_imported(tmp_path: Path) -> None:
    current = tmp_path / f"{PLUGIN_ID}_config.json"
    current.write_text("{}", encoding="utf-8")
    write_legacy(
        current,
        {
            "bridge_service_enabled": "false",
            "pairing_listener_port": True,
            "bridge_api_key": 123,
        },
    )

    assert load_legacy_config_changes(ConfigStub(current, {})) == {
        MIGRATION_CONFIG_KEY: True
    }
