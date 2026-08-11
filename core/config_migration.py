from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .plugin_identity import (
    LEGACY_PLUGIN_ID,
    LEGACY_PUBLIC_API_PREFIX,
    PLUGIN_ID,
    PUBLIC_API_PREFIX,
)


MAX_LEGACY_CONFIG_BYTES = 1_048_576
MIGRATION_CONFIG_KEY = "legacy_plugin_id_migrated"


class PluginConfigMigrationError(RuntimeError):
    pass


def load_legacy_config_changes(config: Any) -> dict[str, Any]:
    """Read one exact legacy config file and return a conservative merge.

    AstrBot derives plugin config filenames from the installation directory. A
    renamed plugin therefore receives a new default config before its constructor
    runs. This helper reads only the exact old sibling file and imports values
    only where the new config still equals its schema default. It never writes or
    removes the legacy file and never imports unknown keys.
    """

    config_path_raw = getattr(config, "config_path", "")
    defaults = getattr(config, "default_config", None)
    if not config_path_raw or not isinstance(defaults, Mapping):
        return {}
    if config.get(MIGRATION_CONFIG_KEY) is True:
        return {}
    current_path = Path(os.path.abspath(os.fspath(config_path_raw)))
    if current_path.name != f"{PLUGIN_ID}_config.json":
        return {}
    legacy_path = current_path.with_name(f"{LEGACY_PLUGIN_ID}_config.json")
    if not legacy_path.exists():
        return {}
    if _is_reparse_point(current_path) or _is_reparse_point(legacy_path):
        raise PluginConfigMigrationError("plugin_config_reparse_point_rejected")
    try:
        size = legacy_path.stat().st_size
    except OSError as exc:
        raise PluginConfigMigrationError("legacy_plugin_config_unreadable") from exc
    if size <= 0 or size > MAX_LEGACY_CONFIG_BYTES:
        raise PluginConfigMigrationError("legacy_plugin_config_size_invalid")
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginConfigMigrationError("legacy_plugin_config_invalid") from exc
    if not isinstance(legacy, dict):
        raise PluginConfigMigrationError("legacy_plugin_config_invalid")

    changes: dict[str, Any] = {}
    for key, default in defaults.items():
        if key == MIGRATION_CONFIG_KEY:
            continue
        if key not in legacy or key not in config:
            continue
        if config.get(key) == default and _compatible_value(legacy[key], default):
            changes[str(key)] = _migrate_known_value(str(key), legacy[key])
    changes[MIGRATION_CONFIG_KEY] = True
    return changes


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _compatible_value(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _migrate_known_value(key: str, value: Any) -> Any:
    if key not in {
        "pairing_listener_public_url",
        "pairing_public_url",
        "pairing_exchange_proxy_url",
    } or not isinstance(value, str):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.path == LEGACY_PUBLIC_API_PREFIX:
        path = PUBLIC_API_PREFIX
    elif parsed.path.startswith(f"{LEGACY_PUBLIC_API_PREFIX}/"):
        path = PUBLIC_API_PREFIX + parsed.path[len(LEGACY_PUBLIC_API_PREFIX) :]
    else:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
