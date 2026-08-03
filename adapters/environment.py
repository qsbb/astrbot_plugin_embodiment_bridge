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
        normalized = self._normalize(raw)
        self.status = "ok" if normalized is not None else "empty_or_invalid"
        return normalized

    @classmethod
    def _normalize(cls, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        version = str(payload.get("version") or "")
        if (
            payload.get("contract") != ENVIRONMENT_CONTRACT_NAME
            or version.split(".", 1)[0] != ENVIRONMENT_CONTRACT_MAJOR
            or payload.get("severity") not in _SEVERITIES
            or cls._expired(payload.get("valid_until"))
        ):
            return None
        location = payload.get("location")
        provenance = payload.get("provenance")
        facts = payload.get("facts")
        if not isinstance(location, dict) or not isinstance(provenance, dict):
            return None
        if not isinstance(facts, dict):
            facts = {}
        try:
            encoded_facts = json.dumps(
                facts,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            encoded_facts = "{}"
        if len(encoded_facts.encode("utf-8")) > 4096:
            facts = {}
        basis = payload.get("severity_basis")
        try:
            severity_rank = max(0, min(3, int(payload.get("severity_rank") or 0)))
        except (TypeError, ValueError):
            return None
        return {
            "contract": ENVIRONMENT_CONTRACT_NAME,
            "version": version,
            "event_key": str(payload.get("event_key") or "")[:128],
            "revision": str(payload.get("revision") or "")[:128],
            "kind": str(payload.get("kind") or "")[:128],
            "severity": str(payload.get("severity")),
            "severity_rank": severity_rank,
            "severity_basis": [str(item)[:256] for item in basis[:16]]
            if isinstance(basis, list)
            else [],
            "facts": facts,
            "location": {
                "key": str(location.get("key") or "")[:128],
                "name": str(location.get("name") or "")[:128],
                "timezone": str(location.get("timezone") or "")[:64],
            },
            "observed_at": payload.get("observed_at"),
            "fetched_at": payload.get("fetched_at"),
            "stale": payload.get("stale") is True,
            "provenance": {
                "authority": str(provenance.get("authority") or "")[:256],
                "provider": str(provenance.get("provider") or "")[:128],
                "local_assessment": str(provenance.get("local_assessment") or "")[:256],
            },
            "valid_from": str(payload.get("valid_from") or "")[:64],
            "valid_until": str(payload.get("valid_until") or "")[:64],
        }

    @staticmethod
    def _expired(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return True
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)

    async def close(self) -> None:
        return None
