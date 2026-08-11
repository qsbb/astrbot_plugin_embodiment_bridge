from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_embodiment_bridge.core.data_migration import (
    MIGRATION_MARKER,
    PluginDataMigrationError,
    prepare_plugin_data_dir,
)
from astrbot_plugin_embodiment_bridge.core.plugin_identity import (
    LEGACY_PLUGIN_ID,
    PLUGIN_ID,
)


def data_locator(root: Path):
    return lambda plugin_id: root / "plugin_data" / plugin_id


def test_fresh_install_creates_only_new_data_directory(tmp_path: Path) -> None:
    result = prepare_plugin_data_dir(data_locator(tmp_path))

    assert result == tmp_path / "plugin_data" / PLUGIN_ID
    assert result.is_dir()
    assert not (tmp_path / "plugin_data" / LEGACY_PLUGIN_ID).exists()


def test_legacy_data_is_copied_atomically_and_source_is_preserved(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "plugin_data" / LEGACY_PLUGIN_ID
    (legacy / "persona_profiles").mkdir(parents=True)
    (legacy / "server_identity.json").write_text("legacy", encoding="utf-8")
    (legacy / "persona_profiles" / "profile.json").write_text(
        "profile", encoding="utf-8"
    )

    result = prepare_plugin_data_dir(data_locator(tmp_path))

    assert (result / "server_identity.json").read_text(encoding="utf-8") == "legacy"
    assert (result / "persona_profiles" / "profile.json").read_text(
        encoding="utf-8"
    ) == "profile"
    marker = json.loads((result / MIGRATION_MARKER).read_text(encoding="utf-8"))
    assert marker["source_plugin_id"] == LEGACY_PLUGIN_ID
    assert marker["target_plugin_id"] == PLUGIN_ID
    assert marker["source_preserved"] is True
    assert (legacy / "server_identity.json").read_text(encoding="utf-8") == "legacy"


def test_existing_new_data_is_authoritative_and_never_implicitly_merged(
    tmp_path: Path,
) -> None:
    locate = data_locator(tmp_path)
    legacy = Path(locate(LEGACY_PLUGIN_ID))
    current = Path(locate(PLUGIN_ID))
    legacy.mkdir(parents=True)
    current.mkdir(parents=True)
    (legacy / "legacy-only.json").write_text("legacy", encoding="utf-8")
    (current / "current.json").write_text("current", encoding="utf-8")

    assert prepare_plugin_data_dir(locate) == current
    assert not (current / "legacy-only.json").exists()
    assert (current / "current.json").read_text(encoding="utf-8") == "current"


def test_unexpected_data_directory_name_fails_closed(tmp_path: Path) -> None:
    def malformed_locator(plugin_id: str) -> Path:
        if plugin_id == PLUGIN_ID:
            return tmp_path / "shared"
        return tmp_path / LEGACY_PLUGIN_ID

    with pytest.raises(PluginDataMigrationError, match="plugin_data_path_unexpected"):
        prepare_plugin_data_dir(malformed_locator)


def test_legacy_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    legacy = tmp_path / "plugin_data" / LEGACY_PLUGIN_ID
    legacy.parent.mkdir()
    try:
        legacy.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")

    with pytest.raises(
        PluginDataMigrationError, match="plugin_data_reparse_point_rejected"
    ):
        prepare_plugin_data_dir(data_locator(tmp_path))
