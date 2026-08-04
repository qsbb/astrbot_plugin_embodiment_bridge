from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

from .provider_utils import contract_matches, find_active_provider


ENVIRONMENT_PLUGIN_NAME = "astrbot_plugin_environment_awareness"
ENVIRONMENT_CONTRACT_NAME = "environment.opportunity"
ENVIRONMENT_CONTRACT_MAJOR = "1"
ENVIRONMENT_CAPABILITY = "cached_read"
_SEVERITIES = {"low", "medium", "high", "critical"}


class CachedEnvironmentAdapter:
    """Consume only the provider-managed, cache-only environment snapshot."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        timeout_seconds: float = 0.05,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = enabled
        self.timeout_seconds = min(max(float(timeout_seconds), 0.01), 0.5)
        self.status = "enabled" if enabled else "disabled"
        self._missing_logged = False
        self._incompatible_logged = False

    async def read(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        provider = find_active_provider(self.context, ENVIRONMENT_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] environment awareness not installed; continuing without cached context"
                )
                self._missing_logged = True
            return None
        try:
            contract = provider.environment_opportunity_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not contract_matches(
            contract,
            name=ENVIRONMENT_CONTRACT_NAME,
            major=ENVIRONMENT_CONTRACT_MAJOR,
            capability=ENVIRONMENT_CAPABILITY,
        ):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] environment.opportunity contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            return None
        if contract.get("request_hook_network") is not False:
            self.status = "network_safety_mismatch"
            self.logger.warning(
                "[quest-avatar] environment cache contract no longer guarantees a network-free request hook"
            )
            return None
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(provider.get_cached_opportunity, allow_stale=True),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "timeout"
            return None
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] cached environment read failed: error_type=%s",
                type(exc).__name__,
            )
            self.status = "error"
            return None
        try:
            normalized = self._normalize(raw)
        except (TypeError, ValueError):
            normalized = None
        self.status = "ok" if normalized is not None else "empty_or_invalid"
        return normalized

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "contract": f"{ENVIRONMENT_CONTRACT_NAME}@1.0",
            "enabled": self.enabled,
            "status": self.status,
            "mode": "cached_only",
            "request_hook_network": False,
            "realtime_private_methods_enabled": False,
        }

    @classmethod
    def _normalize(cls, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        version = payload.get("version")
        severity = payload.get("severity")
        if (
            payload.get("contract") != ENVIRONMENT_CONTRACT_NAME
            or not isinstance(version, str)
            or version.split(".", 1)[0] != ENVIRONMENT_CONTRACT_MAJOR
            or not isinstance(severity, str)
            or severity not in _SEVERITIES
            or not isinstance(payload.get("event_key"), str)
            or not isinstance(payload.get("revision"), str)
            or not isinstance(payload.get("kind"), str)
            or cls._expired(payload.get("valid_until"))
        ):
            return None
        location = payload.get("location")
        provenance = payload.get("provenance")
        facts = payload.get("facts")
        if (
            not isinstance(location, dict)
            or set(location) != {"key", "name", "timezone"}
            or not all(isinstance(value, str) for value in location.values())
            or not isinstance(provenance, dict)
            or set(provenance) != {"authority", "provider", "local_assessment"}
            or not all(isinstance(value, str) for value in provenance.values())
        ):
            return None
        if not isinstance(facts, dict):
            return None
        try:
            encoded_facts = json.dumps(
                facts,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            encoded_facts = "{}"
            facts = {}
        if len(encoded_facts.encode("utf-8")) > 4096:
            facts = {}
        basis = payload.get("severity_basis")
        observed_at = payload.get("observed_at")
        fetched_at = payload.get("fetched_at")
        if (
            not isinstance(basis, (list, tuple))
            or not all(isinstance(item, str) for item in basis)
            or not isinstance(payload.get("stale"), bool)
            or (observed_at is not None and not isinstance(observed_at, str))
            or (fetched_at is not None and not isinstance(fetched_at, str))
            or cls._parse_datetime(payload.get("valid_from")) is None
        ):
            return None
        severity_rank_raw = payload.get("severity_rank")
        if isinstance(severity_rank_raw, bool) or not isinstance(
            severity_rank_raw, int
        ):
            return None
        if severity_rank_raw < 0 or severity_rank_raw > 3:
            return None
        return {
            "contract": ENVIRONMENT_CONTRACT_NAME,
            "version": version,
            "event_key": payload["event_key"][:128],
            "revision": payload["revision"][:128],
            "kind": payload["kind"][:128],
            "severity": severity,
            "severity_rank": severity_rank_raw,
            "severity_basis": [item[:256] for item in basis[:16]],
            "facts": facts,
            "location": {
                "key": location["key"][:128],
                "name": location["name"][:128],
                "timezone": location["timezone"][:64],
            },
            "observed_at": observed_at[:64] if observed_at is not None else None,
            "fetched_at": fetched_at[:64] if fetched_at is not None else None,
            "stale": payload.get("stale") is True,
            "provenance": {
                "authority": provenance["authority"][:256],
                "provider": provenance["provider"][:128],
                "local_assessment": provenance["local_assessment"][:256],
            },
            "valid_from": str(payload.get("valid_from") or "")[:64],
            "valid_until": str(payload.get("valid_until") or "")[:64],
        }

    @staticmethod
    def _expired(value: Any) -> bool:
        parsed = CachedEnvironmentAdapter._parse_datetime(value)
        return parsed is None or parsed <= datetime.now(timezone.utc)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    async def close(self) -> None:
        return None
