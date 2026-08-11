from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_embodiment_bridge.adapters.relationship_candidates import (
    RelationshipIdentityCandidatesAdapter,
)


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


class ContextStub:
    def __init__(self, provider: Any | None) -> None:
        self.provider = provider

    def get_all_stars(self) -> list[Any]:
        if self.provider is None:
            return []
        return [
            SimpleNamespace(
                name="astrbot_plugin_relationship",
                activated=True,
                star_cls=self.provider,
            )
        ]


class CompatibleProvider:
    def identity_candidates_contract(self) -> dict[str, Any]:
        return {
            "name": "relationship.identity_candidates",
            "version": "1.0",
            "capabilities": ("list_candidates",),
            "method": "list_identity_candidates",
            "privacy": "admin_labels_only",
            "exposes_raw_account_ids": False,
            "grants_permission": False,
        }

    async def list_identity_candidates(self) -> dict[str, Any]:
        return {
            "contract_version": "1.0",
            "status": "ok",
            "candidates": [
                {
                    "person_id": "person-b",
                    "display_name": "乙",
                    "account_count": 2,
                },
                {
                    "person_id": "person-a",
                    "display_name": "阿甲",
                    "account_count": 1,
                },
            ],
        }


def test_missing_or_current_relationship_plugin_fails_closed_without_private_fallback() -> (
    None
):
    async def scenario() -> None:
        missing = RelationshipIdentityCandidatesAdapter(
            ContextStub(None),
            LoggerStub(),
        )
        assert (await missing.list_candidates())["status"] == "provider_unavailable"

        current_provider = SimpleNamespace(identity_registry=object())
        adapter = RelationshipIdentityCandidatesAdapter(
            ContextStub(current_provider),
            LoggerStub(),
        )
        result = await adapter.list_candidates()
        assert result["status"] == "contract_unavailable"
        assert result["candidates"] == []

    asyncio.run(scenario())


def test_compatible_contract_returns_only_sorted_admin_labels() -> None:
    async def scenario() -> None:
        adapter = RelationshipIdentityCandidatesAdapter(
            ContextStub(CompatibleProvider()),
            LoggerStub(),
        )

        result = await adapter.list_candidates()

        assert result == {
            "contract": "relationship.identity_candidates@1.0",
            "status": "ok",
            "candidates": [
                {
                    "person_id": "person-a",
                    "display_name": "阿甲",
                    "account_count": 1,
                },
                {
                    "person_id": "person-b",
                    "display_name": "乙",
                    "account_count": 2,
                },
            ],
            "privacy": "admin_labels_only",
            "grants_permission": False,
        }

    asyncio.run(scenario())


def test_candidate_payload_with_raw_account_fields_is_rejected() -> None:
    class LeakyProvider(CompatibleProvider):
        async def list_identity_candidates(self) -> dict[str, Any]:
            payload = await super().list_identity_candidates()
            payload["candidates"][0]["user_id"] = "should-not-leak"
            return payload

    async def scenario() -> None:
        adapter = RelationshipIdentityCandidatesAdapter(
            ContextStub(LeakyProvider()),
            LoggerStub(),
        )

        result = await adapter.list_candidates()

        assert result["status"] == "invalid_response"
        assert result["candidates"] == []

    asyncio.run(scenario())
