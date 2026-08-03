from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import ClientSession

from .http_harness import (
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
        "user_id": "1483904397",
        "bot_id": "2058141897",
        "group_id": "",
        "relationship_profile_id": "owner-profile",
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
                    "/api/v1/plugins/extensions/astrbot_plugin_quest_avatar_bridge"
                )
                assert configuration["allow_insecure_http"] is False

                replay = await client.post(
                    server.url("/pairing/exchange"),
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
                        json=body,
                    )
                    assert response.status == 422
                    assert response.headers["Cache-Control"].startswith("no-store")
                    assert (await response.json())["data"]["code"] == (
                        "schema_validation_failed"
                    )

    asyncio.run(scenario())
