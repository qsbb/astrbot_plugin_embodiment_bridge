from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web
import pytest

from astrbot_plugin_quest_avatar_bridge.adapters.api_principal import (
    PROOF_PATH,
    ApiPrincipalVerificationError,
    AstrBotApiPrincipalVerifier,
)
from astrbot_plugin_quest_avatar_bridge.adapters.identity_control_plane import (
    authenticated_principal_digest,
)


def test_verifier_uses_official_api_key_scheme_and_accepts_only_strict_proof() -> None:
    async def scenario() -> None:
        observed: dict[str, str] = {}

        async def proof(request: web.Request) -> web.Response:
            observed["authorization"] = request.headers.get("Authorization", "")
            return web.json_response(
                {
                    "success": True,
                    "api_principal_digest": authenticated_principal_digest(
                        "api_key:11111111-2222-3333-4444-555555555555"
                    ),
                }
            )

        app = web.Application()
        app.router.add_get(PROOF_PATH, proof)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        try:
            verifier = AstrBotApiPrincipalVerifier(f"http://127.0.0.1:{port}")
            digest = await verifier.resolve_digest("test-api-key-credential")
            assert digest == authenticated_principal_digest(
                "api_key:11111111-2222-3333-4444-555555555555"
            )
            assert observed == {"authorization": "ApiKey test-api-key-credential"}
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    (
        (
            {"success": True, "api_principal_digest": "bad"},
            200,
            "api_principal_proof_invalid",
        ),
        (
            {
                "success": True,
                "api_principal_digest": "sha256:" + "0" * 64,
                "extra": True,
            },
            200,
            "api_principal_proof_invalid",
        ),
        (
            {"success": False, "api_principal_digest": "sha256:" + "0" * 64},
            200,
            "api_principal_proof_invalid",
        ),
        ({"message": "denied"}, 401, "astrbot_api_key_auth_failed"),
    ),
)
def test_verifier_fails_closed_on_invalid_or_rejected_proof(
    payload: dict[str, Any],
    status: int,
    code: str,
) -> None:
    async def scenario() -> None:
        async def proof(_request: web.Request) -> web.Response:
            return web.json_response(payload, status=status)

        app = web.Application()
        app.router.add_get(PROOF_PATH, proof)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        try:
            verifier = AstrBotApiPrincipalVerifier(f"http://127.0.0.1:{port}")
            with pytest.raises(ApiPrincipalVerificationError) as error:
                await verifier.resolve_digest("test-api-key-credential")
            assert error.value.code == code
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "upstream",
    (
        "",
        "https://127.0.0.1:6185",
        "http://192.0.2.1:6185",
        "http://localhost:6185",
        "http://127.0.0.1:6185/path",
        "http://user@127.0.0.1:6185",
    ),
)
def test_verifier_refuses_non_loopback_or_ambiguous_upstreams(upstream: str) -> None:
    async def scenario() -> None:
        verifier = AstrBotApiPrincipalVerifier(upstream)
        with pytest.raises(ApiPrincipalVerificationError) as error:
            await verifier.resolve_digest("test-api-key-credential")
        assert error.value.code == "api_principal_verification_unavailable"

    asyncio.run(scenario())
