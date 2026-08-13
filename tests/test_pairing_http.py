from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from aiohttp import ClientSession

from .http_harness import (
    AUTH_HEADERS,
    ASTRBOT_API_TOKEN,
    BRIDGE_API_KEY,
    LiveHttpServer,
    build_plugin,
)


PAGE_AUTH = {"Authorization": f"Bearer {ASTRBOT_API_TOKEN}"}


def pairing_create_body() -> dict[str, Any]:
    return {
        "protocol_version": "1.0",
        "public_url": "https://bot.example.com",
        "port": 7443,
        "astrbot_api_key": "quest-plugin-scope-key",
        "client_id": "quest-living-room",
        "user_id": "user-test",
        "bot_id": "bot-test",
        "group_id": "",
        "relationship_profile_id": "owner-profile",
        "expected_remote_ip": "127.0.0.1",
        "ttl_seconds": 120,
    }


def test_real_http_pairing_create_exchange_status_and_replay(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                overview = await client.get(
                    server.url("/pairing/overview"),
                    headers=PAGE_AUTH,
                )
                assert overview.status == 200
                overview_body = await overview.json()
                assert overview_body["success"] is True
                assert overview_body["bridge_key_configured"] is True
                assert overview_body["bootstrap_ready"] is True
                assert "bridge_api_key" not in overview_body

                missing_auth = await client.post(
                    server.url("/pairing/create"),
                    json=pairing_create_body(),
                )
                assert missing_auth.status == 401
                assert (await missing_auth.json())["data"]["code"] == (
                    "astrbot_auth_required"
                )

                created = await client.post(
                    server.url("/pairing/create"),
                    headers=PAGE_AUTH,
                    json=pairing_create_body(),
                )
                assert created.status == 201
                assert created.headers["Cache-Control"].startswith("no-store")
                created_body = await created.json()
                serialized = str(created_body)
                assert "quest-plugin-scope-key" not in serialized
                assert BRIDGE_API_KEY not in serialized
                result = created_body["pairing"]
                assert result["qr_svg_data_uri"].startswith(
                    "data:image/svg+xml;base64,"
                )
                assert result["short_code"].isdigit()
                assert len(result["short_code"]) == 6

                exchanged = await client.post(
                    server.url("/pairing/exchange"),
                    headers=PAGE_AUTH,
                    json={
                        "protocol_version": "1.0",
                        "code": result["short_code"],
                    },
                )
                assert exchanged.status == 200
                assert exchanged.headers["Cache-Control"].startswith("no-store")
                exchange_body = await exchanged.json()
                configuration = exchange_body["data"]["configuration"]
                assert configuration["astrbot_api_key"] == "quest-plugin-scope-key"
                assert configuration["bridge_api_key"] == BRIDGE_API_KEY
                assert configuration["base_url"].endswith(
                    "/api/v1/plugins/extensions/astrbot_plugin_embodiment_bridge"
                )
                assert configuration["allow_insecure_http"] is False
                assert configuration["user_id"] == "server-managed-user"
                assert configuration["bot_id"] == "server-managed-bot"
                assert "user-test" not in repr(configuration)
                assert "bot-test" not in repr(configuration)

                replay = await client.post(
                    server.url("/pairing/exchange"),
                    headers=PAGE_AUTH,
                    json={
                        "protocol_version": "1.0",
                        "code": result["short_code"],
                    },
                )
                assert replay.status == 401
                assert (await replay.json())["data"]["code"] == (
                    "pairing_not_available"
                )

                status = await client.post(
                    server.url("/pairing/status"),
                    headers=PAGE_AUTH,
                    json={"pairing_id": result["pairing_id"]},
                )
                assert status.status == 200
                assert (await status.json())["pairing"]["state"] == "consumed"

    asyncio.run(scenario())


def test_quick_pairing_page_request_uses_server_only_defaults(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={
                "trusted_client_id": "quest-living-room",
                "pairing_group_id": "",
                "pairing_relationship_profile_id": "",
                "pairing_ttl_seconds": 120,
            },
        )
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                overview = await client.get(
                    server.url("/pairing/overview"),
                    headers=PAGE_AUTH,
                )
                overview_body = await overview.json()
                assert overview_body["quick_pairing_ready"] is True
                serialized_overview = str(overview_body)
                assert "quick-pair-plugin-scope-key" not in serialized_overview
                assert "trusted_client_id" not in overview_body
                assert "trusted_platform_id" not in overview_body

                created = await client.post(
                    server.url("/pairing/create"),
                    headers=PAGE_AUTH,
                    json={"protocol_version": "1.0"},
                )
                assert created.status == 201
                result = (await created.json())["pairing"]

                exchanged = await client.post(
                    server.url("/pairing/exchange"),
                    headers=PAGE_AUTH,
                    json={
                        "protocol_version": "1.0",
                        "code": result["short_code"],
                    },
                )
                assert exchanged.status == 200
                configuration = (await exchanged.json())["data"]["configuration"]
                assert configuration["astrbot_api_key"] == (
                    "quick-pair-plugin-scope-key"
                )
                assert configuration["client_id"] == "quest-living-room"
                assert configuration["user_id"] == "server-managed-user"
                assert configuration["bot_id"] == "server-managed-bot"

    asyncio.run(scenario())


def test_pairing_exchange_rejects_malformed_or_dual_credentials(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                for body in (
                    {"protocol_version": "1.0"},
                    {"protocol_version": "1.0", "code": "123"},
                    {
                        "protocol_version": "1.0",
                        "code": "123456",
                        "token": "x" * 43,
                    },
                ):
                    response = await client.post(
                        server.url("/pairing/exchange"),
                        headers=PAGE_AUTH,
                        json=body,
                    )
                    assert response.status == 422
                    assert response.headers["Cache-Control"].startswith("no-store")
                    assert (await response.json())["data"]["code"] == (
                        "schema_validation_failed"
                    )

    asyncio.run(scenario())


def test_operator_model_settings_and_identity_catalog_are_dashboard_protected(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/operator-settings"))
                assert denied.status == 401

                settings_response = await client.get(
                    server.url("/pairing/operator-settings"),
                    headers=PAGE_AUTH,
                )
                assert settings_response.status == 200
                settings = (await settings_response.json())["settings"]
                assert settings["selected_id"] == "fake-provider"
                assert settings["selected_available"] is True
                assert settings["providers"] == [
                    {
                        "id": "fake-provider",
                        "model": "contract-model",
                        "adapter_type": "openai",
                        "provider_type": "chat_completion",
                    }
                ]

                platform_denied = await client.get(
                    server.url("/pairing/platform-settings")
                )
                assert platform_denied.status == 401

                platform_response = await client.get(
                    server.url("/pairing/platform-settings"),
                    headers=PAGE_AUTH,
                )
                assert platform_response.status == 200
                platform = (await platform_response.json())["platform"]
                assert platform["configured"] is False
                assert platform["available"] is False
                assert platform["availability_reason"] == (
                    "trusted_platform_not_configured"
                )
                assert platform["platforms_status"] == "ok"
                assert platform["platforms"] == [
                    {
                        "id": "contract-platform",
                        "adapter_type": "aiocqhttp",
                        "display_name": "OneBot 11",
                    }
                ]

                unknown_platform = await client.post(
                    server.url("/pairing/platform-settings"),
                    headers=PAGE_AUTH,
                    json={"trusted_platform_id": "missing"},
                )
                assert unknown_platform.status == 422
                assert (await unknown_platform.json())["data"]["code"] == (
                    "trusted_platform_not_available"
                )

                saved_platform = await client.post(
                    server.url("/pairing/platform-settings"),
                    headers=PAGE_AUTH,
                    json={"trusted_platform_id": "contract-platform"},
                )
                assert saved_platform.status == 200
                saved_platform_body = (await saved_platform.json())["platform"]
                assert saved_platform_body["available"] is True
                assert saved_platform_body["availability_reason"] == "ready"
                assert bundle.plugin.identity.trusted_platform_id == (
                    "contract-platform"
                )
                assert bundle.plugin.message_pipeline.platform_id == (
                    "contract-platform"
                )

                identity_denied = await client.get(
                    server.url("/pairing/quest-identity-settings")
                )
                assert identity_denied.status == 401

                identity_response = await client.get(
                    server.url("/pairing/quest-identity-settings"),
                    headers=PAGE_AUTH,
                )
                assert identity_response.status == 200
                identity = (await identity_response.json())["identity"]
                assert identity["control_plane"]["source"] == "bridge_local"
                assert identity["control_plane"]["authoritative"] is False
                assert "quick-pair-plugin-scope-key" not in repr(identity)

                dashboard_proof = await client.get(
                    server.url("/pairing/api-principal-proof"),
                    headers=PAGE_AUTH,
                )
                assert dashboard_proof.status == 401
                api_key_proof = await client.get(
                    server.url("/pairing/api-principal-proof"),
                    headers=AUTH_HEADERS,
                )
                assert api_key_proof.status == 200
                assert await api_key_proof.json() == {
                    "success": True,
                    "api_principal_digest": "sha256:"
                    + hashlib.sha256(
                        b"api_key:11111111-2222-3333-4444-555555555555"
                    ).hexdigest(),
                }

                saved_identity = await client.post(
                    server.url("/pairing/quest-identity-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "client_id": "quest-room",
                        "platform_id": "contract-platform",
                        "bot_id": "bot-test",
                        "user_id": "user-test",
                        "api_key": ASTRBOT_API_TOKEN,
                    },
                )
                assert saved_identity.status == 200
                saved_identity_body = (await saved_identity.json())["identity"]
                assert saved_identity_body["status"] == "ready"
                assert saved_identity_body["local_fallback_configured"] is True
                assert "contract-plugin-token" not in repr(saved_identity_body)
                assert saved_identity_body["bot_id"] == ""
                assert saved_identity_body["user_id"] == ""
                assert saved_identity_body["bot_id_configured"] is True
                assert saved_identity_body["user_id_configured"] is True
                assert "bot-test" not in repr(saved_identity_body)
                assert "user-test" not in repr(saved_identity_body)
                assert bundle.plugin.pairing_api.pairing_defaults["client_id"] == (
                    "quest-room"
                )
                assert bundle.plugin.pairing_api.pairing_defaults["user_id"] == (
                    "server-managed-user"
                )

                assert bundle.plugin.pairing_api.pairing_defaults["bot_id"] == (
                    "server-managed-bot"
                )
                assert bundle.plugin.config["pairing_api_principal_digest"].startswith(
                    "sha256:"
                )
                assert (
                    "contract-plugin-token"
                    not in bundle.plugin.config["pairing_api_principal_digest"]
                )

                api_key_cannot_call_management = await client.get(
                    server.url("/pairing/operator-settings"),
                    headers=AUTH_HEADERS,
                )
                assert api_key_cannot_call_management.status == 401
                assert (await api_key_cannot_call_management.json())["data"][
                    "code"
                ] == "astrbot_dashboard_auth_required"

                local_session = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.start",
                        "protocol_version": "1.0",
                        "session_id": "local-fallback-session",
                        "client_id": "quest-room",
                        "user_id": "user-test",
                        "bot_id": "bot-test",
                        "group_id": "",
                        "relationship_profile_id": "",
                    },
                )
                assert local_session.status == 201
                protected = (await local_session.json())["data"]["protected_context"]
                assert protected == {
                    "authorized": True,
                    "reason": "authorized_local_owner_identity",
                }

                saved = await client.post(
                    server.url("/pairing/operator-settings"),
                    headers=PAGE_AUTH,
                    json={"chat_provider_id": "fake-provider"},
                )
                assert saved.status == 200

                unknown = await client.post(
                    server.url("/pairing/operator-settings"),
                    headers=PAGE_AUTH,
                    json={"chat_provider_id": "missing"},
                )
                assert unknown.status == 422
                assert (await unknown.json())["data"]["code"] == (
                    "chat_provider_not_available"
                )

                identities = await client.get(
                    server.url("/pairing/identity-candidates"),
                    headers=PAGE_AUTH,
                )
                assert identities.status == 200
                catalog = (await identities.json())["identity_catalog"]
                assert catalog["status"] == "provider_unavailable"
                assert catalog["candidates"] == []

                blocked = await client.post(
                    server.url("/pairing/identity-selection"),
                    headers=PAGE_AUTH,
                    json={"person_id": "person-a"},
                )
                assert blocked.status == 503
                assert (await blocked.json())["data"]["code"] == (
                    "relationship_identity_contract_unavailable"
                )

                cleared = await client.post(
                    server.url("/pairing/identity-selection"),
                    headers=PAGE_AUTH,
                    json={"person_id": ""},
                )
                assert cleared.status == 200

    asyncio.run(scenario())


def test_stt_provider_settings_are_dashboard_protected_strict_and_redacted(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={
                "enable_plugin_mimo_stt": True,
                "plugin_mimo_stt_api_base": "https://legacy.invalid/v1",
                "plugin_mimo_stt_api_key": "legacy-private-value",
                "plugin_mimo_stt_model": "legacy-private-model",
            },
        )
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/stt-settings"))
                assert denied.status == 401
                assert (await denied.json())["data"]["code"] == (
                    "astrbot_auth_required"
                )

                api_key_denied = await client.get(
                    server.url("/pairing/stt-settings"),
                    headers=AUTH_HEADERS,
                )
                assert api_key_denied.status == 401
                assert (await api_key_denied.json())["data"]["code"] == (
                    "astrbot_dashboard_auth_required"
                )

                overview = await client.get(
                    server.url("/pairing/stt-settings"),
                    headers=PAGE_AUTH,
                )
                assert overview.status == 200
                overview_body = await overview.json()
                assert overview_body["success"] is True
                assert overview_body["stt"] == {
                    "source": "astrbot_stt_provider",
                    "available": False,
                    "status": "legacy_private_mimo_disabled",
                    "selected": False,
                    "selected_id": "",
                    "legacy_default": False,
                    "external_contract_status": "no_standard_contract",
                    "providers": [
                        {
                            "id": "fake-stt-provider",
                            "model": "contract-stt-model",
                            "adapter_type": "contract-stt-adapter",
                            "provider_type": "speech_to_text",
                        }
                    ],
                    "config_writable": True,
                }
                serialized = json.dumps(overview_body, ensure_ascii=False)
                for forbidden in (
                    "plugin_mimo_stt_api_base",
                    "plugin_mimo_stt_api_key",
                    "plugin_mimo_stt_model",
                    "legacy-private-value",
                    "legacy.invalid",
                ):
                    assert forbidden not in serialized

                extra = await client.post(
                    server.url("/pairing/stt-settings"),
                    headers=PAGE_AUTH,
                    json={"provider_id": "fake-stt-provider", "unexpected": True},
                )
                assert extra.status == 422
                assert (await extra.json())["data"]["code"] == (
                    "schema_validation_failed"
                )

                missing = await client.post(
                    server.url("/pairing/stt-settings"),
                    headers=PAGE_AUTH,
                    json={"provider_id": "missing-stt-provider"},
                )
                assert missing.status == 422
                assert (await missing.json())["data"]["code"] == (
                    "stt_provider_not_available"
                )

                selected = await client.post(
                    server.url("/pairing/stt-settings"),
                    headers=PAGE_AUTH,
                    json={"provider_id": "fake-stt-provider"},
                )
                assert selected.status == 200
                selected_body = await selected.json()
                assert selected_body["stt"]["status"] == "ready"
                assert selected_body["stt"]["available"] is True
                assert selected_body["stt"]["selected"] is True
                assert selected_body["stt"]["selected_id"] == "fake-stt-provider"
                assert bundle.plugin.config["astrbot_stt_provider_id"] == (
                    "fake-stt-provider"
                )
                assert bundle.plugin.config["enable_astrbot_stt"] is False
                assert bundle.plugin.config["enable_plugin_mimo_stt"] is False
                assert bundle.plugin.config["plugin_mimo_stt_api_base"] == ""
                assert bundle.plugin.config["plugin_mimo_stt_api_key"] == ""
                assert bundle.plugin.config["plugin_mimo_stt_model"] == ""

                disabled = await client.post(
                    server.url("/pairing/stt-settings"),
                    headers=PAGE_AUTH,
                    json={"provider_id": ""},
                )
                assert disabled.status == 200
                disabled_stt = (await disabled.json())["stt"]
                assert disabled_stt["status"] == "disabled"
                assert disabled_stt["available"] is False
                assert disabled_stt["selected"] is False

    asyncio.run(scenario())


def test_session_start_uses_server_canonical_identity_not_device_claims(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        principal = "api_key:11111111-2222-3333-4444-555555555555"
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={
                "trusted_client_id": "quest-room",
                "trusted_platform_id": "contract-platform",
                "pairing_bot_id": "server-real-bot",
                "pairing_user_id": "server-real-user",
                "pairing_api_principal_digest": "sha256:"
                + hashlib.sha256(principal.encode("utf-8")).hexdigest(),
            },
        )
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                response = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.start",
                        "protocol_version": "1.0",
                        "session_id": "canonical-session",
                        "client_id": "quest-room",
                        "user_id": "device-placeholder-user",
                        "bot_id": "device-placeholder-bot",
                        "group_id": "",
                    },
                )

                assert response.status == 201
                body = await response.json()
                assert body["data"]["protected_context"]["authorized"] is True
                session = await bundle.plugin.sessions.get_owned(
                    "canonical-session", principal
                )
                assert session.user_id == "server-real-user"
                assert session.bot_id == "server-real-bot"

    asyncio.run(scenario())


def test_first_session_retries_relationship_refresh_after_provider_load(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    class RelationshipProvider:
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
            assert request["person_id"] == "person-a"
            return {
                "contract_version": "1.0",
                "status": "ok",
                "reason": "resolved_unique_active_private_account",
                "identity": {
                    "platform_id": "contract-platform",
                    "bot_id": "provider-bot",
                    "user_id": "provider-user",
                    "session_id": "contract-platform:FriendMessage:provider-user",
                },
            }

    async def scenario() -> None:
        principal = "api_key:11111111-2222-3333-4444-555555555555"
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={
                "trusted_client_id": "quest-room",
                "trusted_platform_id": "contract-platform",
                "relationship_person_id": "person-a",
                "pairing_identity_source": "relationship",
                "pairing_api_principal_digest": "sha256:"
                + hashlib.sha256(principal.encode("utf-8")).hexdigest(),
                "pairing_bot_id": "old-placeholder-bot",
                "pairing_user_id": "old-placeholder-user",
            },
        )
        assert bundle.plugin.pairing_api.relationship_refresh_ready is False
        bundle.context.stars = [
            SimpleNamespace(
                name="astrbot_plugin_relationship",
                activated=True,
                star_cls=RelationshipProvider(),
            )
        ]

        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                response = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.start",
                        "protocol_version": "1.0",
                        "session_id": "late-provider-session",
                        "client_id": "quest-room",
                        "user_id": "server-managed-user",
                        "bot_id": "server-managed-bot",
                        "group_id": "",
                    },
                )

                assert response.status == 201
                protected = (await response.json())["data"]["protected_context"]
                assert protected["authorized"] is True
                assert bundle.plugin.server_identity_store.identity.user_id == (
                    "provider-user"
                )
                assert bundle.plugin.server_identity_store.identity.bot_id == (
                    "provider-bot"
                )
                assert bundle.plugin.pairing_api.relationship_refresh_ready is True

    asyncio.run(scenario())


def test_quest_identity_save_reuses_configured_api_key_and_fails_when_missing(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        configured = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={"pairing_astrbot_api_key": ASTRBOT_API_TOKEN},
        )
        async with LiveHttpServer(configured) as server:
            async with ClientSession() as client:
                platform = await client.post(
                    server.url("/pairing/platform-settings"),
                    headers=PAGE_AUTH,
                    json={"trusted_platform_id": "contract-platform"},
                )
                assert platform.status == 200
                saved = await client.post(
                    server.url("/pairing/quest-identity-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "client_id": "quest-room",
                        "platform_id": "contract-platform",
                        "bot_id": "bot-test",
                        "user_id": "user-test",
                        "api_key": "",
                    },
                )
                assert saved.status == 200
                assert configured.plugin.pairing_api.api_principal_verifier.calls == [
                    ASTRBOT_API_TOKEN
                ]
                assert ASTRBOT_API_TOKEN not in repr(await saved.json())

        missing = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={"pairing_astrbot_api_key": ""},
        )
        async with LiveHttpServer(missing) as server:
            async with ClientSession() as client:
                response = await client.post(
                    server.url("/pairing/quest-identity-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "client_id": "quest-room",
                        "platform_id": "contract-platform",
                        "bot_id": "bot-test",
                        "user_id": "user-test",
                        "api_key": "",
                    },
                )
                assert response.status == 422
                assert (await response.json())["data"]["code"] == (
                    "pairing_astrbot_api_key_missing"
                )

    asyncio.run(scenario())


def test_natural_person_selection_resolves_real_event_identity_without_exposing_it(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    class RelationshipProvider:
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
                        "person_id": "person-a",
                        "display_name": "已绑定用户",
                        "account_count": 1,
                    }
                ],
            }

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
                "platform_candidates": ["contract-platform"],
            }
            return {
                "contract_version": "1.0",
                "status": "ok",
                "reason": "resolved_unique_active_private_account",
                "identity": {
                    "platform_id": "contract-platform",
                    "bot_id": "real-private-bot",
                    "user_id": "real-private-user",
                    "session_id": ("contract-platform:FriendMessage:real-private-user"),
                },
            }

    async def scenario() -> None:
        digest = "sha256:" + "a" * 64
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={
                "trusted_client_id": "quest-room",
                "trusted_platform_id": "contract-platform",
                "pairing_api_principal_digest": digest,
            },
        )
        bundle.context.stars = [
            SimpleNamespace(
                name="astrbot_plugin_relationship",
                activated=True,
                star_cls=RelationshipProvider(),
            )
        ]
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                response = await client.post(
                    server.url("/pairing/identity-selection"),
                    headers=PAGE_AUTH,
                    json={"person_id": "person-a"},
                )

                assert response.status == 200
                body = await response.json()
                assert body["event_identity"] == {
                    "status": "resolved",
                    "source": "relationship.quest_event_identity@1.0",
                }
                serialized = json.dumps(body, ensure_ascii=False)
                assert "real-private-bot" not in serialized
                assert "real-private-user" not in serialized
                assert bundle.plugin.config["pairing_bot_id"] == ""
                assert bundle.plugin.config["pairing_user_id"] == ""
                assert bundle.plugin.server_identity_store.identity.bot_id == (
                    "real-private-bot"
                )
                assert bundle.plugin.server_identity_store.identity.user_id == (
                    "real-private-user"
                )
                assert bundle.plugin.config["relationship_person_id"] == "person-a"
                assert bundle.plugin.message_pipeline.platform_id == (
                    "contract-platform"
                )
                assert bundle.plugin.pairing_api.pairing_defaults["bot_id"] == (
                    "server-managed-bot"
                )
                assert bundle.plugin.pairing_api.pairing_defaults["user_id"] == (
                    "server-managed-user"
                )

                owner_escalation = await client.post(
                    server.url("/pairing/quest-identity-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "client_id": "quest-room",
                        "platform_id": "contract-platform",
                        "bot_id": "",
                        "user_id": "",
                        "api_key": ASTRBOT_API_TOKEN,
                    },
                )
                assert owner_escalation.status == 422
                assert (await owner_escalation.json())["data"]["code"] == (
                    "invalid_bot_id"
                )

                cleared = await client.post(
                    server.url("/pairing/identity-selection"),
                    headers=PAGE_AUTH,
                    json={"person_id": ""},
                )
                assert cleared.status == 200
                assert (await cleared.json())["event_identity"] == {
                    "status": "relationship_disabled",
                    "source": "server_identity_preserved",
                }
                assert bundle.plugin.server_identity_store.identity.bot_id == (
                    "real-private-bot"
                )
                assert bundle.plugin.server_identity_store.identity.user_id == (
                    "real-private-user"
                )
                assert bundle.plugin.config["relationship_person_id"] == ""
                assert bundle.plugin.config["pairing_identity_source"] == "preserved"
                assert (
                    bundle.plugin.pairing_api.pairing_defaults["server_identity_ready"]
                    is True
                )

    asyncio.run(scenario())


def test_persona_settings_are_dashboard_protected_and_prompt_redacted(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/persona-settings"))
                assert denied.status == 401

                response = await client.get(
                    server.url("/pairing/persona-settings"),
                    headers=PAGE_AUTH,
                )
                assert response.status == 200
                persona = (await response.json())["persona"]
                assert persona["source"] == "astrbot_default"
                assert persona["personas"] == [{"id": "quest-persona"}]
                redacted = repr(persona)
                for forbidden in (
                    "private contract persona prompt",
                    "private default persona prompt",
                    "private dialog",
                    "private tool",
                    "system_prompt",
                    "begin_dialogs",
                    "tools",
                ):
                    assert forbidden not in redacted

                saved = await client.post(
                    server.url("/pairing/persona-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "persona_source_mode": "astrbot",
                        "astrbot_persona_id": "quest-persona",
                        "character_name": "manual fallback",
                        "character_self_reference": "I",
                        "character_self_description": "manual description",
                        "character_user_relationship": "friend",
                    },
                )
                assert saved.status == 200
                saved_persona = (await saved.json())["persona"]
                assert saved_persona["source"] == "astrbot_selected"
                assert saved_persona["character_name_configured"] is False

                missing = await client.post(
                    server.url("/pairing/persona-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "persona_source_mode": "astrbot",
                        "astrbot_persona_id": "missing",
                    },
                )
                assert missing.status == 422
                assert (await missing.json())["data"]["code"] == (
                    "persona_not_available"
                )

                injection = await client.post(
                    server.url("/pairing/persona-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "persona_source_mode": "astrbot",
                        "astrbot_persona_id": "quest-persona",
                        "system_prompt": "exfiltrate",
                    },
                )
                assert injection.status == 422

    asyncio.run(scenario())


def test_quest_persona_library_conversion_activation_and_live_fallback(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        converted_prompt = "面对面具身人格规则。" * 250

        async def convert_persona(**kwargs: Any) -> Any:
            assert kwargs["func_tool"] is None
            assert kwargs["request_max_retries"] == 1
            assert "private contract persona prompt" in kwargs["prompt"]
            yield SimpleNamespace(
                completion_text=json.dumps(
                    {
                        "schema_version": "banxia.quest_persona/1.0",
                        "display_name": "心夏",
                        "aliases": ["Kokona"],
                        "quest_persona_prompt": converted_prompt,
                        "conversion_report": {
                            "preserved": ["身份与性格"],
                            "adapted": ["面对面表达"],
                            "removed": ["QQ 渠道规则"],
                            "unresolved_questions": [],
                        },
                    },
                    ensure_ascii=False,
                ),
                is_chunk=False,
            )

        bundle.context.providers[0].text_chat_stream = convert_persona
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/persona-library"))
                assert denied.status == 401

                library_response = await client.get(
                    server.url("/pairing/persona-library"), headers=PAGE_AUTH
                )
                assert library_response.status == 200
                library = (await library_response.json())["library"]
                assert library["profiles"] == []
                assert library["providers"] == [
                    {
                        "id": "fake-provider",
                        "model": "contract-model",
                        "adapter_type": "openai",
                        "provider_type": "chat_completion",
                    }
                ]
                assert {item["id"] for item in library["source_personas"]} == {
                    "@astrbot-default",
                    "quest-persona",
                }
                assert "private contract persona prompt" not in repr(library)

                selected = await client.post(
                    server.url("/pairing/persona-converter-settings"),
                    headers=PAGE_AUTH,
                    json={"persona_converter_provider_id": "fake-provider"},
                )
                assert selected.status == 200

                denied_job = await client.post(
                    server.url("/pairing/persona-conversion-start"),
                    json={
                        "source_type": "astrbot",
                        "source_persona_id": "quest-persona",
                        "display_name": "心夏",
                    },
                )
                assert denied_job.status == 401

                job_response = await client.post(
                    server.url("/pairing/persona-conversion-start"),
                    headers=PAGE_AUTH,
                    json={
                        "source_type": "astrbot",
                        "source_persona_id": "quest-persona",
                        "display_name": "心夏",
                        "admin_requirements": "保持自然的面对面交流。",
                    },
                )
                assert job_response.status == 202
                job = (await job_response.json())["job"]
                assert job["status"] == "queued"
                for _attempt in range(50):
                    status_response = await client.post(
                        server.url("/pairing/persona-conversion-status"),
                        headers=PAGE_AUTH,
                        json={"job_id": job["job_id"]},
                    )
                    assert status_response.status == 200
                    job = (await status_response.json())["job"]
                    if job["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                assert job["status"] == "completed"
                assert job["stage"] == "preview_ready"
                assert (
                    job["result"]["conversion"]["quest_persona_prompt"]
                    == converted_prompt
                )
                assert "private contract persona prompt" not in repr(job)

                preview_response = await client.post(
                    server.url("/pairing/persona-convert"),
                    headers=PAGE_AUTH,
                    json={
                        "source_type": "astrbot",
                        "source_persona_id": "quest-persona",
                        "display_name": "心夏",
                        "admin_requirements": "保持自然的面对面交流。",
                    },
                )
                assert preview_response.status == 200
                preview = await preview_response.json()
                assert preview["draft_token"]
                assert preview["conversion"]["quest_persona_prompt"] == (
                    converted_prompt
                )
                assert "private contract persona prompt" not in repr(preview)
                assert await bundle.plugin.persona_profiles.list_profiles() == []

                save_payload = {
                    "profile_id": "",
                    "draft_token": preview["draft_token"],
                    "display_name": preview["conversion"]["display_name"],
                    "aliases": preview["conversion"]["aliases"],
                    "source_type": "astrbot",
                    "source_persona_id": "quest-persona",
                    "source_prompt": "",
                    "quest_persona_prompt": converted_prompt,
                    "conversion_report": preview["conversion"]["conversion_report"],
                }
                saved_response = await client.post(
                    server.url("/pairing/persona-profile-save"),
                    headers=PAGE_AUTH,
                    json=save_payload,
                )
                assert saved_response.status == 200
                profile = (await saved_response.json())["profile"]
                profile_id = profile["profile_id"]
                assert profile["status"] == "ready"
                assert profile["source_snapshot"] == ("private contract persona prompt")

                reused = await client.post(
                    server.url("/pairing/persona-profile-save"),
                    headers=PAGE_AUTH,
                    json=save_payload,
                )
                assert reused.status == 409
                assert (await reused.json())["data"]["code"] == (
                    "conversion_draft_expired"
                )

                summary = (
                    await (
                        await client.get(
                            server.url("/pairing/persona-library"),
                            headers=PAGE_AUTH,
                        )
                    ).json()
                )["library"]
                assert "quest_persona_prompt" not in repr(summary)
                assert "private contract persona prompt" not in repr(summary)

                opened = await client.post(
                    server.url("/pairing/persona-profile-open"),
                    headers=PAGE_AUTH,
                    json={"profile_id": profile_id},
                )
                assert opened.status == 200
                assert (await opened.json())["profile"][
                    "quest_persona_prompt"
                ] == converted_prompt

                activated = await client.post(
                    server.url("/pairing/persona-profile-activate"),
                    headers=PAGE_AUTH,
                    json={"profile_id": profile_id},
                )
                assert activated.status == 200
                assert bundle.plugin.config["active_quest_persona_id"] == profile_id
                assert bundle.plugin.persona_service.llm.quest_persona_prompt == (
                    converted_prompt
                )

                active_delete = await client.post(
                    server.url("/pairing/persona-profile-delete"),
                    headers=PAGE_AUTH,
                    json={"profile_id": profile_id},
                )
                assert active_delete.status == 409

                live = await client.post(
                    server.url("/pairing/persona-settings"),
                    headers=PAGE_AUTH,
                    json={
                        "persona_source_mode": "astrbot",
                        "astrbot_persona_id": "quest-persona",
                    },
                )
                assert live.status == 200
                assert bundle.plugin.config["active_quest_persona_id"] == ""
                assert bundle.plugin.persona_service.llm.quest_persona_prompt == ""
                assert (
                    bundle.context.persona_manager.personas[
                        "quest-persona"
                    ].system_prompt
                    == "private contract persona prompt"
                )

                deleted = await client.post(
                    server.url("/pairing/persona-profile-delete"),
                    headers=PAGE_AUTH,
                    json={"profile_id": profile_id},
                )
                assert deleted.status == 200
                assert await bundle.plugin.persona_profiles.list_profiles() == []

    asyncio.run(scenario())


def test_diagnostics_projection_is_dashboard_protected_and_redacted(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        bundle.plugin.diagnostic_log.enabled = True
        bundle.plugin.diagnostic_log.record(
            "llm.error",
            component="llm",
            code="llm_failed",
            error_type="ProviderNotFoundError",
            duration_ms=12.5,
            status="failed",
            user_id="identity-secret",
            reply_text="reply-secret",
        )
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/diagnostics"))
                assert denied.status == 401

                response = await client.get(
                    server.url("/pairing/diagnostics"), headers=PAGE_AUTH
                )
                assert response.status == 200
                body = await response.json()
                assert body["diagnostics"]["status"] == "ready"
                llm_error = next(
                    event
                    for event in body["diagnostics"]["events"]
                    if event["event"] == "llm.error"
                )
                assert llm_error["event"] == "llm.error"
                assert llm_error["component"] == "llm"
                assert llm_error["code"] == "llm_failed"
                assert llm_error["error_type"] == "ProviderNotFoundError"
                assert llm_error["duration_ms"] == 12.5
                assert llm_error["status"] == "failed"
                assert llm_error["timestamp"]
                assert body["diagnostics"]["root_cause"] == {
                    "stage": "llm",
                    "code": "llm_failed",
                }
                bundle.plugin.diagnostic_log.record(
                    "llm.completed",
                    component="llm",
                    status="ok",
                    duration_ms=8.0,
                )
                recovered = await client.get(
                    server.url("/pairing/diagnostics"), headers=PAGE_AUTH
                )
                assert recovered.status == 200
                recovered_body = await recovered.json()
                assert recovered_body["diagnostics"]["root_cause"] == {
                    "stage": "",
                    "code": "",
                }
                serialized = json.dumps(body, ensure_ascii=False)
                assert "identity-secret" not in serialized
                assert "reply-secret" not in serialized

    asyncio.run(scenario())


def test_pairing_exchange_requires_proxy_injected_astrbot_auth(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                response = await client.post(
                    server.url("/pairing/exchange"),
                    json={"protocol_version": "1.0", "code": "123456"},
                )
                assert response.status == 401
                assert (await response.json())["data"]["code"] == (
                    "astrbot_auth_required"
                )

    asyncio.run(scenario())


def test_service_control_is_dashboard_protected_and_gates_quest_sessions(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                denied = await client.get(server.url("/pairing/service-status"))
                assert denied.status == 401

                status_response = await client.get(
                    server.url("/pairing/service-status"),
                    headers=PAGE_AUTH,
                )
                assert status_response.status == 200
                service = (await status_response.json())["service"]
                assert service["enabled"] is True
                assert service["config_writable"] is True
                assert service["listener"] == {
                    "configured": False,
                    "ready": False,
                    "reason": "disabled",
                    "bind_host": "0.0.0.0",
                    "port": 8520,
                }
                assert service["capabilities"] == {
                    "dialogue": True,
                    "eventbus": False,
                    "eventbus_dialogue": False,
                    "interaction_decision": True,
                    "direct_provider_fallback": True,
                    "identity_configured": False,
                    "stt": True,
                    "tts": True,
                    "avatar_actions": True,
                }

                port_denied = await client.post(
                    server.url("/pairing/listener-port"),
                    json={"port": 9020},
                )
                assert port_denied.status == 401
                invalid_port = await client.post(
                    server.url("/pairing/listener-port"),
                    headers=PAGE_AUTH,
                    json={"port": 80},
                )
                assert invalid_port.status == 422
                saved_port = await client.post(
                    server.url("/pairing/listener-port"),
                    headers=PAGE_AUTH,
                    json={"port": 9020},
                )
                assert saved_port.status == 200
                assert (await saved_port.json())["service"]["listener"]["port"] == 9020
                assert bundle.plugin.config["pairing_listener_port"] == 9020

                session_request = {
                    "type": "session.start",
                    "protocol_version": "1.0",
                    "session_id": "service-control-session",
                    "client_id": "quest-client",
                    "user_id": "quest-user",
                    "bot_id": "quest-bot",
                }
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201

                unauthenticated = await client.post(
                    server.url("/pairing/service-control"),
                    json={"enabled": False},
                )
                assert unauthenticated.status == 401
                malformed = await client.post(
                    server.url("/pairing/service-control"),
                    headers=PAGE_AUTH,
                    json={"enabled": False, "unexpected": True},
                )
                assert malformed.status == 422

                stopped = await client.post(
                    server.url("/pairing/service-control"),
                    headers=PAGE_AUTH,
                    json={"enabled": False},
                )
                assert stopped.status == 200
                stopped_service = (await stopped.json())["service"]
                assert stopped_service["status"] == "stopped"
                assert stopped_service["sessions"]["active_sessions"] == 0

                rejected = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert rejected.status == 503
                assert (await rejected.json())["data"]["code"] == (
                    "bridge_service_disabled"
                )

                health = await client.get(
                    server.url("/health"),
                    headers=AUTH_HEADERS,
                )
                assert health.status == 200
                assert (await health.json())["data"]["service"]["status"] == ("stopped")

                restarted = await client.post(
                    server.url("/pairing/service-control"),
                    headers=PAGE_AUTH,
                    json={"enabled": True},
                )
                assert restarted.status == 200
                assert (await restarted.json())["service"]["enabled"] is True
                recreated = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert recreated.status == 201

    asyncio.run(scenario())


def test_trusted_proxy_source_header_binds_exchange_to_quest_ip(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(
            monkeypatch,
            tmp_path,
            config_overrides={"pairing_trusted_proxy_ip": "127.0.0.1"},
        )
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                body = pairing_create_body()
                body["expected_remote_ip"] = "192.168.50.20"
                created = await client.post(
                    server.url("/pairing/create"),
                    headers=PAGE_AUTH,
                    json=body,
                )
                assert created.status == 201
                code = (await created.json())["pairing"]["short_code"]

                mismatch = await client.post(
                    server.url("/pairing/exchange"),
                    headers={
                        **PAGE_AUTH,
                        "X-Quest-Pairing-Source": "192.168.50.21",
                    },
                    json={"protocol_version": "1.0", "code": code},
                )
                assert mismatch.status == 401
                assert (await mismatch.json())["data"]["code"] == (
                    "pairing_not_available"
                )

                exchanged = await client.post(
                    server.url("/pairing/exchange"),
                    headers={
                        **PAGE_AUTH,
                        "X-Quest-Pairing-Source": "192.168.50.20",
                    },
                    json={"protocol_version": "1.0", "code": code},
                )
                assert exchanged.status == 200

    asyncio.run(scenario())
