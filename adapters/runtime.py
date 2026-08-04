from __future__ import annotations

import asyncio
from typing import Any

from .provider_utils import contract_matches, find_active_provider


RUNTIME_PLUGIN_NAME = "astrbot_plugin_update_manager"
RUNTIME_CONTRACT_NAME = "update_manager.series_runtime"
RUNTIME_CONTRACT_MAJOR = "1"
RUNTIME_CAPABILITY = "read_runtime_snapshot"
RUNTIME_METHOD = "get_series_runtime_snapshot"
_RUNTIME_STATUSES = {"ok", "degraded", "unavailable", "error"}
_MEMBER_STATUSES = {"ok", "compatible", "degraded", "unhealthy", "missing"}
_RUNTIME_REASONS = {
    "HEALTHY",
    "MEMBERS_DEGRADED",
    "ADAPTER_UNAVAILABLE",
    "DIAGNOSTIC_TIMEOUT",
    "INVALID_REQUEST",
    "DIAGNOSTIC_FAILED",
    "DIAGNOSTIC_INVALID",
}
_STATUS_REASONS = {
    "ok": {"HEALTHY"},
    "degraded": {"MEMBERS_DEGRADED"},
    "unavailable": {"ADAPTER_UNAVAILABLE", "DIAGNOSTIC_TIMEOUT"},
    "error": {"INVALID_REQUEST", "DIAGNOSTIC_FAILED", "DIAGNOSTIC_INVALID"},
}


class SeriesRuntimeAdapter:
    """Read and cache diagnostics without invoking update operations."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = enabled
        self.timeout_seconds = min(max(float(timeout_seconds), 0.05), 5.0)
        self.snapshot: dict[str, Any] = {
            "contract": f"{RUNTIME_CONTRACT_NAME}@1.0",
            "status": "disabled" if not enabled else "not_checked",
            "reason": "",
            "members": [],
            "healthy": 0,
            "total": 0,
        }
        self._missing_logged = False
        self._incompatible_logged = False
        self._refresh_lock = asyncio.Lock()

    async def refresh(self) -> dict[str, Any]:
        async with self._refresh_lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> dict[str, Any]:
        if not self.enabled:
            return self.snapshot
        provider = find_active_provider(self.context, RUNTIME_PLUGIN_NAME)
        if provider is None:
            self.snapshot = self._empty("unavailable", "provider_unavailable")
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] update manager not installed; runtime diagnostics skipped"
                )
                self._missing_logged = True
            return self.snapshot
        try:
            contract = provider.series_runtime_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if (
            not contract_matches(
                contract,
                name=RUNTIME_CONTRACT_NAME,
                major=RUNTIME_CONTRACT_MAJOR,
                capability=RUNTIME_CAPABILITY,
                method=RUNTIME_METHOD,
            )
            or contract.get("network_access") is not False
            or contract.get("update_side_effects") is not False
        ):
            self.snapshot = self._empty("unavailable", "contract_incompatible")
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] update_manager.series_runtime contract is incompatible; diagnostics disabled"
                )
                self._incompatible_logged = True
            return self.snapshot
        try:
            raw = await asyncio.wait_for(
                provider.get_series_runtime_snapshot(
                    timeout_seconds=self.timeout_seconds
                ),
                timeout=self.timeout_seconds + 0.5,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.snapshot = self._empty("unavailable", "DIAGNOSTIC_TIMEOUT")
            return self.snapshot
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] series runtime diagnostics failed: error_type=%s",
                type(exc).__name__,
            )
            self.snapshot = self._empty("error", "DIAGNOSTIC_FAILED")
            return self.snapshot
        try:
            normalized = self._normalize(raw)
        except (TypeError, ValueError):
            normalized = None
        self.snapshot = normalized or self._empty("error", "DIAGNOSTIC_INVALID")
        return self.snapshot

    @classmethod
    def _normalize(cls, payload: Any) -> dict[str, Any] | None:
        required = {
            "contract_name",
            "contract_version",
            "capability",
            "status",
            "reason",
            "members",
            "healthy",
            "total",
        }
        status = payload.get("status") if isinstance(payload, dict) else None
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload.get("contract_name") != RUNTIME_CONTRACT_NAME
            or str(payload.get("contract_version") or "").split(".", 1)[0]
            != RUNTIME_CONTRACT_MAJOR
            or payload.get("capability") != RUNTIME_CAPABILITY
            or not isinstance(status, str)
            or status not in _RUNTIME_STATUSES
            or not isinstance(reason, str)
            or reason not in _RUNTIME_REASONS
            or reason not in _STATUS_REASONS.get(status, set())
            or not isinstance(payload.get("members"), list)
        ):
            return None
        members: list[dict[str, Any]] = []
        member_fields = {
            "plugin_id",
            "label",
            "installed",
            "loaded",
            "activated",
            "version",
            "health_status",
            "reason",
        }
        for item in payload["members"][:32]:
            if (
                not isinstance(item, dict)
                or set(item) != member_fields
                or not isinstance(item.get("health_status"), str)
                or item.get("health_status") not in _MEMBER_STATUSES
                or not isinstance(item.get("plugin_id"), str)
                or not isinstance(item.get("label"), str)
                or not isinstance(item.get("installed"), bool)
                or not isinstance(item.get("loaded"), bool)
                or not isinstance(item.get("activated"), bool)
                or not isinstance(item.get("version"), str)
                or not isinstance(item.get("reason"), str)
            ):
                return None
            members.append(
                {
                    "plugin_id": str(item.get("plugin_id") or "")[:128],
                    "label": str(item.get("label") or "")[:128],
                    "installed": item.get("installed") is True,
                    "loaded": item.get("loaded") is True,
                    "activated": item.get("activated") is True,
                    "version": str(item.get("version") or "")[:32],
                    "health_status": str(item.get("health_status")),
                    "reason": str(item.get("reason") or "")[:128],
                }
            )
        if (
            isinstance(payload.get("healthy"), bool)
            or not isinstance(payload.get("healthy"), int)
            or isinstance(payload.get("total"), bool)
            or not isinstance(payload.get("total"), int)
        ):
            return None
        try:
            healthy = int(payload.get("healthy") or 0)
            total = int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return None
        if healthy < 0 or total < 0 or healthy > total:
            return None
        return {
            "contract": f"{RUNTIME_CONTRACT_NAME}@1.0",
            "status": str(payload.get("status")),
            "reason": str(payload.get("reason") or "")[:128],
            "members": members,
            "healthy": healthy,
            "total": total,
        }

    @staticmethod
    def _empty(status: str, reason: str) -> dict[str, Any]:
        return {
            "contract": f"{RUNTIME_CONTRACT_NAME}@1.0",
            "status": status,
            "reason": reason,
            "members": [],
            "healthy": 0,
            "total": 0,
        }

    async def close(self) -> None:
        return None
