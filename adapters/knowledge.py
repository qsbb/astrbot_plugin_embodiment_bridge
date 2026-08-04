from __future__ import annotations

import asyncio
import math
from typing import Any

from .provider_utils import contract_matches, find_active_provider


KNOWLEDGE_PLUGIN_NAME = "astrbot_plugin_active_learner"
KNOWLEDGE_CONTRACT_NAME = "active_learner.knowledge"
KNOWLEDGE_CONTRACT_MAJOR = "1"
KNOWLEDGE_CAPABILITY = "recall"


class GlobalKnowledgeAdapter:
    """Read only the collision-safe global knowledge scope."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        top_k: int = 5,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = enabled
        self.top_k = min(max(int(top_k), 1), 10)
        self.timeout_seconds = min(max(float(timeout_seconds), 0.1), 5.0)
        self.status = "enabled" if enabled else "disabled"
        self._missing_logged = False
        self._incompatible_logged = False

    async def recall(self, query: str) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not self.enabled or not text:
            return []
        provider = find_active_provider(self.context, KNOWLEDGE_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] active learner not installed; continuing without global knowledge"
                )
                self._missing_logged = True
            return []
        try:
            contract = provider.knowledge_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            contract = None
        if not contract_matches(
            contract,
            name=KNOWLEDGE_CONTRACT_NAME,
            major=KNOWLEDGE_CONTRACT_MAJOR,
            capability=KNOWLEDGE_CAPABILITY,
        ):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] active_learner.knowledge contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            return []
        try:
            raw = await asyncio.wait_for(
                provider.recall(text, scope="global", top_k=self.top_k),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.status = "timeout"
            return []
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] global knowledge recall failed: error_type=%s",
                type(exc).__name__,
            )
            self.status = "error"
            return []
        if not isinstance(raw, list):
            self.status = "invalid_response"
            return []

        evidence: list[dict[str, Any]] = []
        for item in raw[: self.top_k]:
            normalized = self._normalize_item(item)
            if normalized is not None:
                evidence.append(normalized)
        self.status = "ok"
        return evidence

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "contract": f"{KNOWLEDGE_CONTRACT_NAME}@1.0",
            "enabled": self.enabled,
            "status": self.status,
            "scope": "global",
            "private_scope_enabled": False,
        }

    @staticmethod
    def _normalize_item(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        required = {"content", "source", "score", "topic", "verified", "confidence"}
        if not required.issubset(item):
            return None
        if (
            not isinstance(item.get("content"), str)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("topic"), str)
            or not isinstance(item.get("verified"), bool)
            or isinstance(item.get("score"), bool)
            or isinstance(item.get("confidence"), bool)
        ):
            return None
        content = item["content"].strip()
        if not content:
            return None
        try:
            score = float(item.get("score"))
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(score) or not math.isfinite(confidence):
            return None
        return {
            "content": content[:2000],
            "source": item["source"][:256],
            "score": max(0.0, min(1.0, score)),
            "topic": item["topic"][:128],
            "verified": item["verified"],
            "confidence": max(0.0, min(1.0, confidence)),
        }

    async def close(self) -> None:
        return None
