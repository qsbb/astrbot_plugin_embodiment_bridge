"""``series.control@1.0`` adapter for safe bridge runtime controls.

The kernel may override only bounded, non-secret runtime policy fields.  The
bridge keeps the overlay in its own data directory and continues to own its
native AstrBot configuration.  A missing, malformed, or incompatible overlay
therefore falls back to the native values without exposing credentials or
identity configuration through the series contract.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CONTRACT_NAME = "series.control@1.0"
PLUGIN_ID = "astrbot_plugin_embodiment_bridge"
SERIES_ID = "ningxin_suxi"

_FIELDS: dict[str, dict[str, Any]] = {
    "diagnostic_log_enabled": {
        "type": "bool",
        "default": False,
        "minimum": None,
        "maximum": None,
    },
    "diagnostic_platform_log_enabled": {
        "type": "bool",
        "default": False,
        "minimum": None,
        "maximum": None,
    },
    "server_timing_enabled": {
        "type": "bool",
        "default": False,
        "minimum": None,
        "maximum": None,
    },
    "max_sessions": {
        "type": "int",
        "default": 8,
        "minimum": 1,
        "maximum": 64,
    },
    "event_queue_size": {
        "type": "int",
        "default": 64,
        "minimum": 8,
        "maximum": 512,
    },
    "max_audio_seconds": {
        "type": "int",
        "default": 60,
        "minimum": 1,
        "maximum": 120,
    },
    "max_audio_chunk_bytes": {
        "type": "int",
        "default": 16000,
        "minimum": 3200,
        "maximum": 65536,
    },
    "interaction_debounce_ms": {
        "type": "int",
        "default": 250,
        "minimum": 0,
        "maximum": 2000,
    },
    "output_chunk_ms": {
        "type": "int",
        "default": 50,
        "minimum": 40,
        "maximum": 100,
    },
    "sse_heartbeat_seconds": {
        "type": "int",
        "default": 15,
        "minimum": 5,
        "maximum": 60,
    },
    "max_tts_audio_seconds": {
        "type": "int",
        "default": 120,
        "minimum": 1,
        "maximum": 300,
    },
}


class SeriesControlAdapter:
    """Implement the plugin-owned side of ``series.control@1.0``."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._path = Path(plugin.data_dir) / "series-control.json"
        self._overlay: dict[str, Any] = {}
        self._revision = 0
        self._mode = "native"
        self._load()

    def _load(self) -> None:
        """Load only schema-valid overrides; malformed state fails closed."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            self._overlay, self._revision = {}, 0
            return
        if not isinstance(raw, dict):
            self._overlay, self._revision = {}, 0
            return
        revision = raw.get("revision", 0)
        self._revision = (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and revision >= 0
            else 0
        )
        overrides = raw.get("overrides")
        self._overlay = self._clean_values(
            overrides if isinstance(overrides, dict) else {}
        )
        if self._overlay:
            self._mode = "managed"

    @staticmethod
    def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for name, value in values.items():
            spec = _FIELDS.get(name)
            if spec is None:
                continue
            if spec["type"] == "bool":
                if isinstance(value, bool):
                    clean[name] = value
                continue
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and spec["minimum"] <= value <= spec["maximum"]
            ):
                clean[name] = value
        return clean

    def _persist(self) -> None:
        """Persist the overlay with a same-directory fsync and atomic replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="series-control-", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema_version": 1,
                        "revision": self._revision,
                        "overrides": self._overlay,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _native(self, field: str) -> Any:
        config = getattr(self.plugin, "config", {})
        if isinstance(config, dict):
            return config.get(field, _FIELDS[field]["default"])
        getter = getattr(config, "get", None)
        if callable(getter):
            return getter(field, _FIELDS[field]["default"])
        return _FIELDS[field]["default"]

    def _native_configured(self, field: str) -> bool:
        config = getattr(self.plugin, "config", {})
        try:
            return field in config
        except (TypeError, AttributeError):
            return False

    def effective_value(self, field: str, *, force_overlay: bool = False) -> Any:
        if field not in _FIELDS:
            raise KeyError(field)
        if (force_overlay or self._mode == "managed") and field in self._overlay:
            return self._overlay[field]
        return self._native(field)

    def effective_config(self, *, force_overlay: bool = False) -> dict[str, Any]:
        return {
            field: self.effective_value(field, force_overlay=force_overlay)
            for field in _FIELDS
        }

    def series_control_set_mode(self, mode: str) -> dict[str, Any]:
        self._mode = mode if mode in {"native", "managed"} else "native"
        self.sync_runtime()
        return {"success": True, "mode": self._mode}

    def sync_runtime(self, *, force_overlay: bool = False) -> None:
        hook = getattr(self.plugin, "_apply_series_control_runtime", None)
        if callable(hook):
            hook(self.effective_config(force_overlay=force_overlay))

    def series_control_contract(self) -> dict[str, Any]:
        return {
            "name": CONTRACT_NAME,
            "version": "1.0",
            "series_id": SERIES_ID,
            "plugin_id": PLUGIN_ID,
            "plugin_name": "临",
            "capabilities": [
                "read_schema",
                "read_snapshot",
                "validate_patch",
                "apply_patch",
                "reset_override",
            ],
            "read_only": False,
            "secrets_in_response": False,
            "max_patch_fields": len(_FIELDS),
        }

    def series_control_schema(self) -> dict[str, Any]:
        fields: dict[str, dict[str, Any]] = {}
        for name, spec in _FIELDS.items():
            field = {
                "type": spec["type"],
                "default": spec["default"],
                "control": "overrideable",
                "secret": False,
                "restart_required": False,
            }
            if spec["minimum"] is not None:
                field["minimum"] = spec["minimum"]
                field["maximum"] = spec["maximum"]
            fields[name] = field
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": "1.0",
            "plugin_id": PLUGIN_ID,
            "revision": self._revision,
            "fields": fields,
        }

    def series_control_snapshot(self) -> dict[str, Any]:
        fields: dict[str, dict[str, Any]] = {}
        for name in _FIELDS:
            managed = name in self._overlay and self._mode == "managed"
            fields[name] = {
                "native_configured": self._native_configured(name),
                "managed_configured": name in self._overlay,
                "effective_source": "managed" if managed else "plugin",
                "effective_value": self.effective_value(name),
            }
        return {"status": "ok", "revision": self._revision, "fields": fields}

    def _validate(self, patch: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        if expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        if not isinstance(patch, dict) or not patch or len(patch) > len(_FIELDS):
            return {
                "status": "error",
                "reason": "PATCH_INVALID",
                "revision": self._revision,
            }
        for name in patch:
            if name not in _FIELDS:
                return {"status": "error", "reason": "UNKNOWN_FIELD", "field": str(name)}
        clean = self._clean_values(patch)
        if len(clean) != len(patch):
            for name, value in patch.items():
                if name not in clean:
                    spec = _FIELDS[name]
                    reason = (
                        "INVALID_TYPE"
                        if spec["type"] == "bool" and not isinstance(value, bool)
                        else "INVALID_VALUE"
                    )
                    return {"status": "error", "reason": reason, "field": name}
        return {"status": "ok", "reason": "VALID", "revision": self._revision, "patch": clean}

    def validate_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        return self._validate(patch, expected_revision)

    def _restore(self, overlay: dict[str, Any], revision: int) -> None:
        self._overlay = dict(overlay)
        self._revision = revision
        try:
            self._persist()
        except Exception:
            pass
        try:
            self.sync_runtime(force_overlay=True)
        except Exception:
            pass

    def apply_series_control_patch(
        self, patch: dict[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        result = self._validate(patch, expected_revision)
        if result.get("status") != "ok":
            return result
        before_overlay = dict(self._overlay)
        before_revision = self._revision
        self._overlay.update(result["patch"])
        self._mode = "managed"
        self._revision += 1
        try:
            self._persist()
            self.sync_runtime(force_overlay=True)
        except Exception:
            self._restore(before_overlay, before_revision)
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "APPLIED",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }

    def reset_series_control_override(
        self,
        fields: list[str] | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if expected_revision is not None and expected_revision != self._revision:
            return {
                "status": "error",
                "reason": "REVISION_CONFLICT",
                "revision": self._revision,
            }
        if fields is not None and not isinstance(fields, list):
            return {"status": "error", "reason": "PATCH_INVALID", "revision": self._revision}
        names = list(self._overlay) if fields is None else fields
        if any(name not in _FIELDS for name in names):
            return {"status": "error", "reason": "UNKNOWN_FIELD", "revision": self._revision}
        before_overlay = dict(self._overlay)
        before_revision = self._revision
        for name in names:
            self._overlay.pop(name, None)
        self._revision += 1
        try:
            self._persist()
            self.sync_runtime()
        except Exception:
            self._restore(before_overlay, before_revision)
            return {
                "status": "error",
                "reason": "APPLY_FAILED_ROLLED_BACK",
                "revision": self._revision,
            }
        return {
            "status": "ok",
            "reason": "RESET",
            "revision": self._revision,
            "fields": self.series_control_snapshot()["fields"],
        }
