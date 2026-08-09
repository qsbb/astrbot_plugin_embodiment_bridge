from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_quest_avatar_bridge.adapters.identity_control_plane import (
    IdentityControlPlaneAdapter,
    IdentityControlPlaneError,
    authenticated_principal_digest,
)


class LoggerStub:
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
                name="astrbot_plugin_identity_guardian",
                activated=True,
                star_cls=self.provider,
            )
        ]


class ProviderStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def identity_control_plane_contract(self) -> dict[str, Any]:
        return {
            "name": "identity.control_plane",
            "version": "1.0",
            "plugin": "astrbot_plugin_identity_guardian",
            "capabilities": (
                "read_status",
                "upsert_quest_owner_binding",
                "authorize_quest_session",
            ),
            "methods": (
                "get_identity_control_plane",
                "upsert_quest_owner_binding",
                "authorize_quest_session",
            ),
            "privacy": "counts_only",
            "principal_storage": "sha256_digest_only",
            "natural_person_grants_permission": False,
            "provider_present_fallback": "deny_without_local_merge",
        }

    def get_identity_control_plane(self) -> dict[str, Any]:
        return self._result(status="ready", reason="ready", updated=False)

    async def upsert_quest_owner_binding(
        self, request: dict[str, str]
    ) -> dict[str, Any]:
        self.requests.append(dict(request))
        return self._result(status="saved", reason="saved", updated=True)

    @staticmethod
    def _result(*, status: str, reason: str, updated: bool) -> dict[str, Any]:
        return {
            "contract_version": "1.0",
            "status": status,
            "reason": reason,
            "updated": updated,
            "authorized": status == "saved",
            "config_writable": True,
            "owner_count": 1,
            "quest_binding_count": 1,
            "grants_platform_action": False,
        }


def test_missing_provider_reports_local_fallback_without_identifiers() -> None:
    async def scenario() -> None:
        adapter = IdentityControlPlaneAdapter(ContextStub(None), LoggerStub())
        snapshot = await adapter.snapshot()
        assert snapshot == {
            "source": "bridge_local",
            "authoritative": False,
            "status": "ready",
            "reason": "identity_guardian_not_installed",
            "config_writable": True,
            "owner_count": 0,
            "quest_binding_count": 0,
        }

    asyncio.run(scenario())


def test_upsert_hashes_principal_and_returns_only_redacted_counts() -> None:
    async def scenario() -> None:
        provider = ProviderStub()
        adapter = IdentityControlPlaneAdapter(ContextStub(provider), LoggerStub())

        snapshot = await adapter.snapshot()
        assert snapshot["source"] == "identity_guardian"
        assert snapshot["owner_count"] == 1

        result = await adapter.upsert_quest_owner_binding(
            api_principal_digest=authenticated_principal_digest(
                "api_key:11111111-2222-3333-4444-555555555555"
            ),
            client_id="quest-room",
            platform_id="platform-test",
            bot_id="bot-test",
            user_id="user-test",
        )
        assert result["status"] == "saved"
        assert result["authoritative"] is True
        assert provider.requests[0].keys() == {
            "api_principal_digest",
            "client_id",
            "platform_id",
            "bot_id",
            "user_id",
        }
        assert provider.requests[0]["api_principal_digest"].startswith("sha256:")
        assert provider.requests[0]["api_principal_digest"] == (
            authenticated_principal_digest(
                "api_key:11111111-2222-3333-4444-555555555555"
            )
        )
        assert "11111111-2222-3333-4444-555555555555" not in repr(
            provider.requests
        )
        assert "11111111-2222-3333-4444-555555555555" not in repr(result)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "principal",
    (
        "",
        "dashboard-admin",
        "api_key:",
        "api_key:contains space",
        "api_key:contains|separator",
        "api_key:" + "x" * 129,
    ),
)
def test_only_authenticated_api_key_principals_can_be_hashed(principal: str) -> None:
    with pytest.raises(IdentityControlPlaneError) as error:
        authenticated_principal_digest(principal)
    assert error.value.code == "invalid_authenticated_api_principal"


def test_present_incompatible_provider_fails_closed() -> None:
    class IncompatibleProvider(ProviderStub):
        def identity_control_plane_contract(self) -> dict[str, Any]:
            value = super().identity_control_plane_contract()
            value["provider_present_fallback"] = "merge_local"
            return value

    async def scenario() -> None:
        adapter = IdentityControlPlaneAdapter(
            ContextStub(IncompatibleProvider()),
            LoggerStub(),
        )
        with pytest.raises(IdentityControlPlaneError) as error:
            await adapter.snapshot()
        assert error.value.code == "identity_control_plane_incompatible"

    asyncio.run(scenario())
