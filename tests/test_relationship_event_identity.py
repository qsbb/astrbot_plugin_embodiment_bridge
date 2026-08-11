from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_embodiment_bridge.adapters.relationship_event_identity import (
    RelationshipQuestEventIdentityAdapter,
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
    def quest_event_identity_contract(self) -> dict[str, Any]:
        return {
            "name": "relationship.quest_event_identity",
            "version": "1.0",
            "plugin": "astrbot_plugin_relationship",
            "capabilities": ("resolve_private_event_identity",),
            "method": "resolve_quest_event_identity",
            "privacy": "server_only_raw_account",
            "browser_exposed": False,
            "exposes_raw_account_ids": True,
            "grants_permission": False,
            "active_platform_match_required": True,
            "private_session_required": True,
        }

    async def resolve_quest_event_identity(self, **request: Any) -> dict[str, Any]:
        assert request == {
            "person_id": "person-a",
            "platform_candidates": ["platform-a"],
        }
        return {
            "contract_version": "1.0",
            "status": "ok",
            "reason": "resolved_unique_active_private_account",
            "identity": {
                "platform_id": "platform-a",
                "bot_id": "private-bot",
                "user_id": "private-user",
                "session_id": "platform-a:FriendMessage:private-user",
            },
        }


def test_resolves_strict_server_only_private_identity() -> None:
    async def scenario() -> None:
        adapter = RelationshipQuestEventIdentityAdapter(
            ContextStub(CompatibleProvider()),
            LoggerStub(),
        )

        resolution = await adapter.resolve(
            person_id="person-a",
            platform_candidates=("platform-a",),
        )

        identity = resolution.identity
        assert identity is not None
        assert identity.platform_id == "platform-a"
        assert identity.bot_id == "private-bot"
        assert identity.user_id == "private-user"
        assert identity.session_id == "platform-a:FriendMessage:private-user"
        assert resolution.status == "ok"
        assert "private-user" not in repr(identity)

    asyncio.run(scenario())


def test_missing_or_incompatible_provider_fails_closed() -> None:
    async def scenario() -> None:
        missing = RelationshipQuestEventIdentityAdapter(ContextStub(None), LoggerStub())
        missing_resolution = await missing.resolve(
            person_id="person-a", platform_candidates=("platform-a",)
        )
        assert missing_resolution.identity is None
        assert missing_resolution.status == "provider_unavailable"

        incompatible_provider = SimpleNamespace(
            quest_event_identity_contract=lambda: {
                **CompatibleProvider().quest_event_identity_contract(),
                "browser_exposed": True,
            }
        )
        incompatible = RelationshipQuestEventIdentityAdapter(
            ContextStub(incompatible_provider), LoggerStub()
        )
        incompatible_resolution = await incompatible.resolve(
            person_id="person-a", platform_candidates=("platform-a",)
        )
        assert incompatible_resolution.identity is None
        assert incompatible_resolution.status == "contract_unavailable"

    asyncio.run(scenario())


def test_rejects_leaky_mismatched_or_non_private_payloads() -> None:
    variants = (
        {
            "platform_id": "platform-a",
            "bot_id": "private-bot",
            "user_id": "private-user",
            "session_id": "platform-a:FriendMessage:private-user",
            "display_name": "must-not-cross-contract",
        },
        {
            "platform_id": "platform-b",
            "bot_id": "private-bot",
            "user_id": "private-user",
            "session_id": "platform-b:FriendMessage:private-user",
        },
        {
            "platform_id": "platform-a",
            "bot_id": "private-bot",
            "user_id": "private-user",
            "session_id": "platform-a:GroupMessage:group-a",
        },
    )

    for identity in variants:

        class InvalidProvider(CompatibleProvider):
            async def resolve_quest_event_identity(
                self, **request: Any
            ) -> dict[str, Any]:
                del request
                return {
                    "contract_version": "1.0",
                    "status": "ok",
                    "reason": "resolved_unique_active_private_account",
                    "identity": identity,
                }

        async def scenario() -> None:
            adapter = RelationshipQuestEventIdentityAdapter(
                ContextStub(InvalidProvider()), LoggerStub()
            )
            resolution = await adapter.resolve(
                person_id="person-a",
                platform_candidates=("platform-a",),
            )
            assert resolution.identity is None
            assert resolution.status == "invalid_response"

        asyncio.run(scenario())


def test_accepts_only_declared_unavailable_reasons() -> None:
    class UnavailableProvider(CompatibleProvider):
        def __init__(self, reason: str) -> None:
            self.reason = reason

        async def resolve_quest_event_identity(self, **request: Any) -> dict[str, Any]:
            del request
            return {
                "contract_version": "1.0",
                "status": "unavailable",
                "reason": self.reason,
                "identity": None,
            }

    async def scenario(reason: str, expected_status: str) -> None:
        adapter = RelationshipQuestEventIdentityAdapter(
            ContextStub(UnavailableProvider(reason)), LoggerStub()
        )
        resolution = await adapter.resolve(
            person_id="person-a", platform_candidates=("platform-a",)
        )
        assert resolution.identity is None
        assert resolution.status == expected_status

    asyncio.run(scenario("private_account_ambiguous", "unavailable"))
    asyncio.run(scenario("raw-secret-in-error", "invalid_response"))
