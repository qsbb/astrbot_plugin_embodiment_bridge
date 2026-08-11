from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .plugin_identity import LEGACY_PLUGIN_ID, PLUGIN_ID


MIGRATION_MARKER = f".migrated-from-{LEGACY_PLUGIN_ID}.json"


class PluginDataMigrationError(RuntimeError):
    pass


def prepare_plugin_data_dir(get_data_dir: Callable[[str], object]) -> Path:
    """Return the new data directory after a bounded, non-destructive migration.

    The legacy tree is never modified. A complete copy is staged beside the new
    directory, checked for reparse points, and atomically promoted. Existing new
    data is authoritative and is never merged with legacy data implicitly.
    """

    # StarTools.get_data_dir() creates the requested directory. Resolve only the
    # new ID through that API; deriving the legacy sibling avoids creating a
    # misleading empty legacy directory on fresh installations.
    target = Path(os.path.abspath(os.fspath(get_data_dir(PLUGIN_ID))))
    source = target.parent / LEGACY_PLUGIN_ID
    if target == source:
        raise PluginDataMigrationError("plugin_data_paths_are_not_distinct")
    _require_safe_root(target, expected_name=PLUGIN_ID)
    _require_safe_root(source, expected_name=LEGACY_PLUGIN_ID)

    if target.exists():
        _require_plain_directory(target, "new_plugin_data_not_plain_directory")
        if any(target.iterdir()):
            return target
        target.rmdir()

    if not source.exists():
        target.mkdir(parents=True, exist_ok=False)
        return target

    _require_plain_directory(source, "legacy_plugin_data_not_plain_directory")
    _validate_tree(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{PLUGIN_ID}.migration-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging, symlinks=False)
        marker = {
            "schema_version": 1,
            "source_plugin_id": LEGACY_PLUGIN_ID,
            "target_plugin_id": PLUGIN_ID,
            "copied_at": datetime.now(timezone.utc).isoformat(),
            "source_preserved": True,
        }
        (staging / MIGRATION_MARKER).write_text(
            json.dumps(marker, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(staging, target)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        raise PluginDataMigrationError("legacy_plugin_data_migration_failed") from exc
    return target


def _require_safe_root(path: Path, *, expected_name: str) -> None:
    if path.name != expected_name or path.parent == path:
        raise PluginDataMigrationError("plugin_data_path_unexpected")
    if _is_reparse_point(path):
        raise PluginDataMigrationError("plugin_data_reparse_point_rejected")


def _require_plain_directory(path: Path, code: str) -> None:
    if _is_reparse_point(path) or not path.is_dir():
        raise PluginDataMigrationError(code)


def _validate_tree(root: Path) -> None:
    for current_root, directories, files in os.walk(root, followlinks=False):
        current = Path(current_root)
        if _is_reparse_point(current):
            raise PluginDataMigrationError("legacy_plugin_data_reparse_point_rejected")
        for name in (*directories, *files):
            candidate = current / name
            if _is_reparse_point(candidate):
                raise PluginDataMigrationError(
                    "legacy_plugin_data_reparse_point_rejected"
                )


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
