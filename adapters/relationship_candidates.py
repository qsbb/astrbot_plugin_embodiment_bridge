from __future__ import annotations

import asyncio
import re
from typing import Any

from .provider_utils import contract_matches, find_active_provider


RELATIONSHIP_PLUGIN_NAME = "astrbot_plugin_relationship"
IDENTITY_CANDIDATES_CONTRACT_NAME = "relationship.identity_candidates"
IDENTITY_CANDIDATES_CONTRACT_MAJOR = "1"
IDENTITY_CANDIDATES_METHOD = "list_identity_candidates"
_PERSON_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RelationshipIdentityCandidatesAdapter:
    """Consume only a minimal, versioned natural-person label catalog."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.timeout_seconds = min(max(float(timeout_seconds), 0.05), 5.0)
        self.status = "not_checked"
        self._missing_logged = False
        self._incompatible_logged = False

    async def list_candidates(self) -> dict[str, Any]:
        provider = find_active_provider(self.context, RELATIONSHIP_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] relationship plugin not installed; identity candidates unavailable"
                )
                self._missing_logged = True
            return self._result()

        try:
            contract = provider.identity_candidates_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not self._compatible_contract(contract):
            self.status = "contract_unavailable"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] relationship.identity_candidates contract unavailable; private registry fallback is forbidden"
                )
                self._incompatible_logged = True
            return self._result()

        try:
            payload = await asyncio.wait_for(
                provider.list_identity_candidates(),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "timeout"
            return self._result()
        except Exception as exc:
            self.status = "error"
            self.logger.warning(
                "[quest-avatar] relationship identity candidates failed: error_type=%s",
                type(exc).__name__,
            )
            return self._result()

        normalized = self._normalize(payload)
        if normalized is None:
            self.status = "invalid_response"
            self.logger.warning(
                "[quest-avatar] relationship.identity_candidates returned an invalid payload"
            )
            return self._result()
        self.status = "ok"
        return self._result(normalized)

    @staticmethod
    def _compatible_contract(contract: Any) -> bool:
        return bool(
            contract_matches(
                contract,
                name=IDENTITY_CANDIDATES_CONTRACT_NAME,
                major=IDENTITY_CANDIDATES_CONTRACT_MAJOR,
                capability="list_candidates",
                method=IDENTITY_CANDIDATES_METHOD,
            )
            and contract.get("privacy") == "admin_labels_only"
            and contract.get("exposes_raw_account_ids") is False
            and contract.get("grants_permission") is False
        )

    @staticmethod
    def _normalize(payload: Any) -> list[dict[str, Any]] | None:
        if not isinstance(payload, dict) or set(payload) != {
            "contract_version",
            "status",
            "candidates",
        }:
            return None
        if payload.get("contract_version") != "1.0" or payload.get("status") != "ok":
            return None
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 1000:
            return None

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
            if not isinstance(item, dict) or set(item) != {
                "person_id",
                "display_name",
                "account_count",
            }:
                return None
            person_id = str(item.get("person_id") or "").strip()
            display_name = str(item.get("display_name") or "").strip()
            account_count = item.get("account_count")
            if (
                not _PERSON_ID_RE.fullmatch(person_id)
                or not display_name
                or len(display_name) > 80
                or isinstance(account_count, bool)
                or not isinstance(account_count, int)
                or not 0 <= account_count <= 20
                or person_id in seen
            ):
                return None
            seen.add(person_id)
            normalized.append(
                {
                    "person_id": person_id,
                    "display_name": display_name,
                    "account_count": account_count,
                }
            )
        normalized.sort(key=lambda item: item["person_id"])
        return normalized

    def _result(self, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "contract": f"{IDENTITY_CANDIDATES_CONTRACT_NAME}@1.0",
            "status": self.status,
            "candidates": candidates or [],
            "privacy": "admin_labels_only",
            "grants_permission": False,
        }

    async def close(self) -> None:
        return None
