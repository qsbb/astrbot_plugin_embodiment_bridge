from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ssl
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from yarl import URL

import astrbot_plugin_embodiment_bridge.transport.builtin_listener as builtin_listener_module
from astrbot_plugin_embodiment_bridge.core.pairing import (
    PUBLIC_API_PATH,
    PairingCreateRequest,
    PairingExchangeService,
    PairingManager,
)
from astrbot_plugin_embodiment_bridge.core.plugin_identity import (
    LEGACY_PUBLIC_API_PREFIX,
)
from astrbot_plugin_embodiment_bridge.transport.builtin_listener import (
    EXCHANGE_PATH,
    _FIXED_PROXY_ROUTES,
    _LEGACY_FIXED_PROXY_ROUTES,
    BuiltinListenerConfig,
    BuiltinQuestListener,
    certificate_pin_from_pem,
    normalize_listener_public_url,
    normalize_loopback_upstream,
)


BRIDGE_KEY = "listener-bridge-key-000000000000000000000"
EXTERNAL_EXCHANGE_URL = f"https://pair.example.com{EXCHANGE_PATH}"
Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class LogCapture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        del kwargs
        self.lines.append(message % args if args else message)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        del kwargs
        self.lines.append(message % args if args else message)


def make_manager(**changes: Any) -> PairingManager:
    options: dict[str, Any] = {
        "bridge_api_key": BRIDGE_KEY,
        "exchange_url": EXTERNAL_EXCHANGE_URL,
    }
    options.update(changes)
    return PairingManager(**options)


def test_tls_load_failure_never_starts_plaintext_listener(tmp_path: Path) -> None:
    async def scenario() -> None:
        listener = BuiltinQuestListener(
            config=BuiltinListenerConfig(
                enabled=True,
                bind_host="127.0.0.1",
                port=0,
                upstream_base_url="http://127.0.0.1:9",
                public_exchange_url=EXTERNAL_EXCHANGE_URL,
                tls_enabled=True,
                tls_cert_path=str(tmp_path / "missing.crt"),
                tls_key_path=str(tmp_path / "missing.key"),
            ),
            exchange_service=PairingExchangeService(make_manager()),
            logger=LogCapture(),
        )
        try:
            # Retry must remain fail-closed, not reuse a plaintext context.
            for _ in range(2):
                await listener.start()
                assert not listener.ready
                assert listener.status_snapshot()["reason"] == "tls_certificate_invalid"
                assert listener._site is None
                assert listener._runner is None
                assert listener._client is None
        finally:
            await listener.close()

    asyncio.run(scenario())


def _pem_block(der: bytes, *, line_width: int = 64) -> bytes:
    encoded = base64.b64encode(der).decode("ascii")
    lines = [encoded[index : index + line_width] for index in range(0, len(encoded), line_width)]
    return (
        b"-----BEGIN CERTIFICATE-----\n"
        + "\n".join(lines).encode("ascii")
        + b"\n-----END CERTIFICATE-----\n"
    )


def test_certificate_pin_hashes_first_pem_certificate_der_and_normalizes_pin(
    tmp_path: Path,
) -> None:
    first_der = b"first-leaf-der"
    second_der = b"second-chain-der"
    certificate_path = tmp_path / "chain.pem"
    certificate_path.write_bytes(
        b"  preamble\n"
        + _pem_block(first_der)
        + b"\n"
        + _pem_block(second_der)
    )

    expected = hashlib.sha256(first_der).hexdigest()
    assert certificate_pin_from_pem(str(certificate_path)) == expected
    assert certificate_pin_from_pem(str(certificate_path)).islower()

    config = BuiltinListenerConfig.from_mapping(
        {
            "pairing_listener_enabled": True,
            "pairing_listener_host": "127.0.0.1",
            "pairing_listener_port": 18443,
            "pairing_listener_upstream_url": "http://127.0.0.1:6185",
            "pairing_listener_tls_enabled": True,
            "pairing_listener_tls_cert_path": str(certificate_path),
            "pairing_listener_tls_key_path": str(tmp_path / "bridge.key"),
            "pairing_certificate_pin_sha256": f"  {expected.upper()}  ",
            "pairing_listener_public_url": "https://bot.example.test:18443",
        },
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert config.certificate_pin_sha256 == expected
    assert config.validation_reason == ""
    assert config.public_exchange_url == f"https://bot.example.test:18443{EXCHANGE_PATH}"


def test_invalid_configured_pin_and_explicit_mismatch_fail_closed(tmp_path: Path) -> None:
    certificate_path = tmp_path / "bridge.crt"
    certificate_path.write_bytes(_pem_block(b"leaf-der"))
    base = {
        "pairing_listener_enabled": True,
        "pairing_listener_host": "127.0.0.1",
        "pairing_listener_port": 18443,
        "pairing_listener_upstream_url": "http://127.0.0.1:6185",
        "pairing_listener_tls_enabled": True,
        "pairing_listener_tls_cert_path": str(certificate_path),
        "pairing_listener_tls_key_path": str(tmp_path / "bridge.key"),
        "pairing_listener_public_url": "https://bot.example.test:18443",
    }
    invalid = BuiltinListenerConfig.from_mapping(
        {**base, "pairing_certificate_pin_sha256": "not-a-sha256-pin"},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert invalid.validation_reason == "invalid_certificate_pin_sha256"
    assert invalid.certificate_pin_sha256 == ""

    actual = hashlib.sha256(b"leaf-der").hexdigest()
    mismatch = BuiltinListenerConfig.from_mapping(
        {**base, "pairing_certificate_pin_sha256": "cd" * 32},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert mismatch.validation_reason == "tls_certificate_pin_mismatch"
    assert mismatch.certificate_pin_sha256 == ""

    matching = BuiltinListenerConfig.from_mapping(
        {**base, "pairing_certificate_pin_sha256": actual.upper()},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert matching.validation_reason == ""
    assert matching.certificate_pin_sha256 == actual


def test_missing_or_invalid_tls_certificate_paths_fail_closed(tmp_path: Path) -> None:
    common = {
        "pairing_listener_enabled": True,
        "pairing_listener_host": "127.0.0.1",
        "pairing_listener_port": 18443,
        "pairing_listener_upstream_url": "http://127.0.0.1:6185",
        "pairing_listener_tls_enabled": True,
        "pairing_listener_tls_key_path": str(tmp_path / "bridge.key"),
        "pairing_listener_public_url": "https://bot.example.test:18443",
    }
    missing = BuiltinListenerConfig.from_mapping(
        {**common, "pairing_listener_tls_cert_path": str(tmp_path / "missing.crt")},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert missing.validation_reason == "tls_certificate_invalid"
    assert missing.certificate_pin_sha256 == ""

    malformed_path = tmp_path / "malformed.crt"
    malformed_path.write_text("this is not PEM", encoding="ascii")
    malformed = BuiltinListenerConfig.from_mapping(
        {**common, "pairing_listener_tls_cert_path": str(malformed_path)},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert malformed.validation_reason == "tls_certificate_invalid"
    assert malformed.certificate_pin_sha256 == ""

    missing_paths = BuiltinListenerConfig.from_mapping(
        {**common, "pairing_listener_tls_cert_path": ""},
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert missing_paths.validation_reason == "tls_certificate_paths_missing"


def test_tls_public_url_must_be_https_and_never_retains_http(tmp_path: Path) -> None:
    certificate_path = tmp_path / "bridge.crt"
    certificate_path.write_bytes(_pem_block(b"leaf-der"))
    values = {
        "pairing_listener_enabled": True,
        "pairing_listener_host": "127.0.0.1",
        "pairing_listener_port": 18443,
        "pairing_listener_upstream_url": "http://127.0.0.1:6185",
        "pairing_listener_tls_enabled": True,
        "pairing_listener_tls_cert_path": str(certificate_path),
        "pairing_listener_tls_key_path": str(tmp_path / "bridge.key"),
        "pairing_listener_public_url": "http://192.168.50.10:18443",
    }
    config = BuiltinListenerConfig.from_mapping(
        values,
        allow_private_http=True,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert config.public_exchange_url == ""
    assert config.public_url_reason == "tls_public_url_must_be_https"
    assert "http://" not in config.public_exchange_url


def test_tls_status_is_redacted_and_start_passes_tls12_context_to_site(
    tmp_path: Path,
) -> None:
    pin = hashlib.sha256(b"leaf-der").hexdigest()
    config = BuiltinListenerConfig(
        enabled=True,
        bind_host="127.0.0.1",
        port=18443,
        upstream_base_url="http://127.0.0.1:6185",
        public_exchange_url=f"https://bot.example.test:18443{EXCHANGE_PATH}",
        certificate_pin_sha256=pin,
        tls_enabled=True,
        tls_cert_path=str(tmp_path / "bridge.crt"),
        tls_key_path=str(tmp_path / "bridge.key"),
    )
    observed: dict[str, Any] = {}

    class FakeContext:
        def __init__(self, protocol: object) -> None:
            observed["protocol"] = protocol
            self.minimum_version: object = None

        def load_cert_chain(self, certfile: str, keyfile: str, password: str) -> None:
            observed["certfile"] = certfile
            observed["keyfile"] = keyfile
            observed["password"] = password

    class FakeRunner:
        async def setup(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    class FakeServer:
        sockets = [SimpleNamespace(getsockname=lambda: ("127.0.0.1", 18443))]

    class FakeSite:
        def __init__(self, runner: object, host: str, port: int, *, ssl_context: object) -> None:
            del runner
            observed["host"] = host
            observed["port"] = port
            observed["ssl_context"] = ssl_context
            self._server = FakeServer()

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    class FakeClient:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeRouter:
        def add_route(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    class FakeApplication:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.router = FakeRouter()

    def fake_client_session(*, timeout: object, connector: object) -> FakeClient:
        del timeout, connector
        return FakeClient()

    listener = BuiltinQuestListener(
        config=config,
        exchange_service=PairingExchangeService(make_manager()),
        logger=LogCapture(),
    )
    with (
        patch.object(builtin_listener_module.ssl, "SSLContext", FakeContext),
        patch.object(builtin_listener_module.aiohttp, "ClientSession", fake_client_session),
        patch.object(builtin_listener_module.web, "Application", FakeApplication),
        patch.object(builtin_listener_module.web, "AppRunner", lambda *args, **kwargs: FakeRunner()),
        patch.object(builtin_listener_module.web, "TCPSite", FakeSite),
    ):
        asyncio.run(listener.start())

    assert listener.ready is True
    assert observed["protocol"] is ssl.PROTOCOL_TLS_SERVER
    assert observed["certfile"] == config.tls_cert_path
    assert observed["keyfile"] == config.tls_key_path
    assert observed["password"] == ""
    assert observed["ssl_context"].minimum_version is ssl.TLSVersion.TLSv1_2
    snapshot = listener.status_snapshot()
    assert snapshot["tls_enabled"] is True
    assert snapshot["public_scheme"] == "https"
    assert snapshot["certificate_pin_configured"] is True
    assert pin not in snapshot.values()
    assert config.tls_cert_path not in snapshot.values()
    asyncio.run(listener.close())


def test_http_listener_remains_compatible_on_explicit_high_port() -> None:
    async def scenario() -> None:
        listener = await start_listener(make_manager(), bind_port=18444)
        try:
            assert listener.ready is True
            snapshot = listener.status_snapshot()
            assert snapshot["port"] == 18444
            assert snapshot["tls_enabled"] is False
            assert snapshot["public_scheme"] == "http"
            assert listener.public_exchange_url == EXTERNAL_EXCHANGE_URL
            async with aiohttp.ClientSession() as client:
                response = await client.get(listener_base(listener) + f"{PUBLIC_API_PATH}/health")
                assert response.status == 503
                await response.read()
        finally:
            await listener.close()

    asyncio.run(scenario())


def create_pair(
    manager: PairingManager,
    *,
    expected_remote_ip: str = "127.0.0.1",
) -> Any:
    return manager.create(
        "dashboard-owner",
        PairingCreateRequest.model_validate(
            {
                "protocol_version": "1.0",
                "public_url": "https://bot.example.com",
                "astrbot_api_key": "plugin-scope-api-key",
                "client_id": "quest-living-room",
                "user_id": "user-1",
                "bot_id": "bot-1",
                "expected_remote_ip": expected_remote_ip,
                "ttl_seconds": 60,
            }
        ),
    )


def token_from(result: Any) -> str:
    return str(json.loads(result.qr_payload)["token"])


def listener_config(
    *,
    upstream_port: int,
    bind_port: int = 0,
    enabled: bool = True,
    **changes: Any,
) -> BuiltinListenerConfig:
    config = BuiltinListenerConfig(
        enabled=enabled,
        bind_host="127.0.0.1",
        port=bind_port,
        upstream_base_url=f"http://127.0.0.1:{upstream_port}",
        public_exchange_url=EXTERNAL_EXCHANGE_URL,
        max_json_body_bytes=4096,
        max_audio_request_bytes=8192,
        exchange_body_bytes=16_384,
        max_connections=8,
        body_timeout_seconds=1.0,
        upstream_connect_timeout_seconds=0.5,
        upstream_response_timeout_seconds=1.0,
    )
    return replace(config, **changes)


def listener_base(listener: BuiltinQuestListener) -> str:
    return f"http://127.0.0.1:{listener.status_snapshot()['port']}"


async def start_listener(
    manager: PairingManager,
    *,
    upstream_port: int = 9,
    bind_port: int = 0,
    logger: LogCapture | None = None,
    **changes: Any,
) -> BuiltinQuestListener:
    listener = BuiltinQuestListener(
        config=listener_config(
            upstream_port=upstream_port,
            bind_port=bind_port,
            **changes,
        ),
        exchange_service=PairingExchangeService(manager),
        logger=logger or LogCapture(),
    )
    await listener.start()
    return listener


async def start_upstream(handler: Handler) -> tuple[web.AppRunner, int]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    server = site._server
    assert server is not None and server.sockets
    return runner, int(server.sockets[0].getsockname()[1])


async def raw_status(port: int, request_bytes: bytes) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request_bytes)
        await writer.drain()
        first_line = await asyncio.wait_for(reader.readline(), timeout=1.0)
        return int(first_line.split()[1])
    finally:
        writer.close()
        await writer.wait_closed()


def test_listener_config_is_strict_and_private_http_is_explicit() -> None:
    valid = BuiltinListenerConfig.from_mapping(
        {
            "pairing_listener_enabled": True,
            "pairing_listener_host": "0.0.0.0",
            "pairing_listener_port": 8520,
            "pairing_listener_upstream_url": "http://127.0.0.1:6185",
            "pairing_listener_public_url": "http://192.168.50.10:8520",
        },
        allow_private_http=True,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert valid.validation_reason == ""
    assert valid.public_url_reason == ""
    assert valid.public_exchange_url == (f"http://192.168.50.10:8520{EXCHANGE_PATH}")

    invalid = BuiltinListenerConfig.from_mapping(
        {
            "pairing_listener_enabled": "yes",
            "pairing_listener_host": "localhost",
            "pairing_listener_port": 80,
            "pairing_listener_upstream_url": "http://example.com:6185",
            "pairing_listener_public_url": "http://192.168.50.10:8520",
        },
        allow_private_http=False,
        max_json_body_bytes=65_536,
        max_audio_request_bytes=32_768,
    )
    assert invalid.enabled is False
    assert invalid.validation_reason == "invalid_enabled"
    assert invalid.public_exchange_url == ""
    assert invalid.public_url_reason == "https_required"

    for upstream in (
        "https://127.0.0.1:6185",
        "http://localhost:6185",
        "http://192.168.50.10:6185",
        "http://user@127.0.0.1:6185",
        "http://127.0.0.1:6185/path",
        "http://127.0.0.1:6185?target=x",
    ):
        try:
            normalize_loopback_upstream(upstream)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe upstream accepted: {upstream}")

    assert (
        normalize_listener_public_url(
            "https://pair.example.com",
            allow_private_http=False,
        )
        == f"https://pair.example.com{EXCHANGE_PATH}"
    )


def test_stopped_listener_port_can_be_reconfigured_without_changing_default() -> None:
    listener = BuiltinQuestListener(
        config=listener_config(
            upstream_port=9,
            bind_port=8520,
            enabled=False,
            public_exchange_url=f"http://192.168.50.10:8520{EXCHANGE_PATH}",
        ),
        exchange_service=PairingExchangeService(make_manager()),
        logger=LogCapture(),
    )

    assert listener.status_snapshot()["port"] == 8520
    listener.configure_port(9020)
    assert listener.status_snapshot()["port"] == 9020
    assert listener.config.public_exchange_url == (
        f"http://192.168.50.10:9020{EXCHANGE_PATH}"
    )


def test_listener_lifecycle_bind_degrades_and_port_is_reusable() -> None:
    async def scenario() -> None:
        manager = make_manager()
        disabled = await start_listener(manager, enabled=False)
        assert disabled.status_snapshot() == {
            "enabled": False,
            "ready": False,
            "bind_host": "127.0.0.1",
            "port": 0,
            "upstream_kind": "loopback_http",
            "tls_enabled": False,
            "public_scheme": "unavailable",
            "certificate_pin_configured": False,
            "reason": "disabled",
        }
        await disabled.close()

        occupied = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        occupied_port = int(occupied.sockets[0].getsockname()[1])
        logger = LogCapture()
        failed = await start_listener(
            manager,
            bind_port=occupied_port,
            logger=logger,
        )
        assert failed.ready is False
        assert failed.status_snapshot()["reason"] == "bind_failed"
        assert all(BRIDGE_KEY not in line for line in logger.lines)
        await failed.close()
        occupied.close()
        await occupied.wait_closed()

        first = await start_listener(manager)
        assert first.ready is True
        rebound_port = int(first.status_snapshot()["port"])

        probe = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        changed_port = int(probe.sockets[0].getsockname()[1])
        probe.close()
        await probe.wait_closed()
        await first.stop(reason="port_reconfiguring")
        first.configure_port(changed_port)
        await first.start()
        assert first.ready is True
        assert first.status_snapshot()["port"] == changed_port
        await first.close()

        second = await start_listener(manager, bind_port=rebound_port)
        assert second.ready is True
        assert second.status_snapshot()["port"] == rebound_port
        await second.close()
        await second.close()

    asyncio.run(scenario())


def test_anonymous_exchange_reuses_manager_and_never_trusts_forwarded_ip() -> None:
    async def scenario() -> None:
        manager = make_manager(exchange_attempts_per_minute=20)
        listener = await start_listener(manager)
        base = listener_base(listener)
        created = create_pair(manager)
        token = token_from(created)

        async with aiohttp.ClientSession() as client:
            response = await client.post(
                base + EXCHANGE_PATH,
                json={"protocol_version": "1.0", "token": token},
            )
            assert response.status == 200
            assert "Authorization" not in response.request_info.headers
            assert response.headers["Cache-Control"].startswith("no-store")
            assert response.headers["Pragma"] == "no-cache"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            payload = await response.json()
            assert payload["data"]["pairing_protocol_version"] == "1.0"
            assert payload["data"]["pairing_id"] == created.pairing_id
            assert payload["data"]["configuration"]["bridge_api_key"] == BRIDGE_KEY

            replay = await client.post(
                base + EXCHANGE_PATH,
                json={"protocol_version": "1.0", "token": token},
            )
            replay_text = await replay.text()
            assert replay.status == 401
            assert token not in replay_text
            assert "plugin-scope-api-key" not in replay_text

            code_pair = create_pair(manager)
            by_code = await client.post(
                base + EXCHANGE_PATH,
                json={
                    "protocol_version": "1.0",
                    "code": code_pair.short_code,
                },
            )
            assert by_code.status == 200

            wrong_ip = create_pair(manager, expected_remote_ip="127.0.0.2")
            forged = await client.post(
                base + EXCHANGE_PATH,
                json={
                    "protocol_version": "1.0",
                    "token": token_from(wrong_ip),
                },
                headers={
                    "Forwarded": "for=127.0.0.2",
                    "X-Forwarded-For": "127.0.0.2",
                    "X-Real-IP": "127.0.0.2",
                    "X-Quest-Pairing-Source": "127.0.0.2",
                },
            )
            assert forged.status == 401

        await listener.close()

    asyncio.run(scenario())


def test_exchange_expiry_revoke_concurrency_and_both_rate_limits() -> None:
    async def scenario() -> None:
        now = [1000.0]
        manager = make_manager(
            clock=lambda: now[0],
            exchange_attempts_per_minute=20,
            global_exchange_attempts_per_minute=40,
        )
        listener = await start_listener(manager)
        url = listener_base(listener) + EXCHANGE_PATH

        async with aiohttp.ClientSession() as client:
            expired = create_pair(manager)
            now[0] += 61
            expired_response = await client.post(
                url,
                json={"protocol_version": "1.0", "token": token_from(expired)},
            )
            assert expired_response.status == 401

            revoked = create_pair(manager)
            manager.revoke("dashboard-owner", revoked.pairing_id)
            revoked_response = await client.post(
                url,
                json={"protocol_version": "1.0", "token": token_from(revoked)},
            )
            assert revoked_response.status == 401

            concurrent = create_pair(manager)
            concurrent_token = token_from(concurrent)

            async def attempt() -> int:
                response = await client.post(
                    url,
                    json={
                        "protocol_version": "1.0",
                        "token": concurrent_token,
                    },
                )
                await response.read()
                return response.status

            statuses = await asyncio.gather(attempt(), attempt())
            assert sorted(statuses) == [200, 401]

        await listener.close()

        per_remote = make_manager(
            exchange_attempts_per_minute=1,
            global_exchange_attempts_per_minute=10,
        )
        per_listener = await start_listener(per_remote)
        per_url = listener_base(per_listener) + EXCHANGE_PATH
        async with aiohttp.ClientSession() as client:
            for expected in (401, 429):
                response = await client.post(
                    per_url,
                    json={"protocol_version": "1.0", "token": "x" * 43},
                )
                assert response.status == expected
        await per_listener.close()

        global_limit = make_manager(
            exchange_attempts_per_minute=10,
            global_exchange_attempts_per_minute=1,
        )
        global_listener = await start_listener(global_limit)
        global_url = listener_base(global_listener) + EXCHANGE_PATH
        async with aiohttp.ClientSession() as client:
            for expected in (401, 429):
                response = await client.post(
                    global_url,
                    json={"protocol_version": "1.0", "token": "y" * 43},
                )
                assert response.status == expected
        await global_listener.close()

    asyncio.run(scenario())


def test_exchange_request_validation_and_path_fail_closed() -> None:
    async def scenario() -> None:
        manager = make_manager(exchange_attempts_per_minute=50)
        listener = await start_listener(manager)
        base = listener_base(listener)
        url = base + EXCHANGE_PATH

        async with aiohttp.ClientSession() as client:
            assert (await client.get(url)).status == 405
            assert (
                await client.post(
                    url, data=b"{}", headers={"Content-Type": "text/plain"}
                )
            ).status == 415
            assert (
                await client.post(
                    url, data=b"", headers={"Content-Type": "application/json"}
                )
            ).status == 400
            extra = await client.post(
                url,
                json={
                    "protocol_version": "1.0",
                    "token": "x" * 43,
                    "unexpected": True,
                },
            )
            assert extra.status == 422
            oversized = await client.post(
                url,
                data=b"x" * 16_385,
                headers={"Content-Type": "application/json"},
            )
            assert oversized.status == 413

            async def small_chunks() -> Any:
                yield b"{}"

            chunked = await client.post(
                url,
                data=small_chunks(),
                headers={"Content-Type": "application/json"},
            )
            assert chunked.status == 411

            async def large_chunks() -> Any:
                yield b"x" * 16_384
                yield b"x"

            chunked_large = await client.post(
                url,
                data=large_chunks(),
                headers={"Content-Type": "application/json"},
            )
            assert chunked_large.status == 413

            upstream_forbidden = (
                "/api/v1/plugins/extensions/other_plugin/health",
                "/",
                "/api/v1/plugins",
                f"{PUBLIC_API_PATH}/pairing/create",
                f"{PUBLIC_API_PATH}/pairing/status",
                f"{PUBLIC_API_PATH}/pairing/revoke",
                f"{PUBLIC_API_PATH}/pairing/overview",
                f"{PUBLIC_API_PATH}/pairing/service-status",
                f"{PUBLIC_API_PATH}/pairing/service-control",
                f"{PUBLIC_API_PATH}/pairing/operator-settings",
                f"{PUBLIC_API_PATH}/pairing/platform-settings",
                f"{PUBLIC_API_PATH}/pairing/identity-candidates",
                f"{PUBLIC_API_PATH}/pairing/identity-selection",
                f"{PUBLIC_API_PATH}/pairing/api-principal-proof",
            )
            for path in upstream_forbidden:
                assert (await client.get(base + path)).status == 404

            assert (
                await client.get(base + f"{PUBLIC_API_PATH}/health?target=http://evil")
            ).status == 400
            encoded = URL(
                base + f"{PUBLIC_API_PATH}/events/%2e%2e%2fhealth",
                encoded=True,
            )
            assert (await client.get(encoded)).status == 400
            backslash = URL(
                base + f"{PUBLIC_API_PATH}/events/%5cs1",
                encoded=True,
            )
            assert (await client.get(backslash)).status == 400

        port = int(listener.status_snapshot()["port"])
        missing_length = (
            f"POST {EXCHANGE_PATH} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        assert await raw_status(port, missing_length) == 411
        await listener.close()

    asyncio.run(scenario())


def test_proxy_allowlist_body_limits_and_header_sanitization() -> None:
    async def scenario() -> None:
        received: list[dict[str, Any]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            body = await request.read()
            received.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "headers": dict(request.headers),
                    "body": body,
                }
            )
            return web.json_response({"path": request.path, "size": len(body)})

        runner, upstream_port = await start_upstream(upstream_handler)
        manager = make_manager()
        listener = await start_listener(manager, upstream_port=upstream_port)
        base = listener_base(listener)
        fixed_routes = (
            ("GET", f"{PUBLIC_API_PATH}/health"),
            ("POST", f"{PUBLIC_API_PATH}/session/start"),
            ("POST", f"{PUBLIC_API_PATH}/turn/start"),
            ("POST", f"{PUBLIC_API_PATH}/audio/chunk"),
            ("POST", f"{PUBLIC_API_PATH}/audio/end"),
            ("POST", f"{PUBLIC_API_PATH}/interaction"),
            ("POST", f"{PUBLIC_API_PATH}/interrupt"),
            ("POST", f"{PUBLIC_API_PATH}/session/close"),
            ("POST", f"{PUBLIC_API_PATH}/spatial/context"),
            ("GET", f"{PUBLIC_API_PATH}/events/session-1"),
        )

        async with aiohttp.ClientSession() as client:
            for method, path in fixed_routes:
                kwargs: dict[str, Any] = {}
                if method == "POST":
                    kwargs = {
                        "data": b"{}",
                        "headers": {"Content-Type": "application/json"},
                    }
                response = await client.request(method, base + path, **kwargs)
                assert response.status == 200
                await response.read()

            headers = {
                "Authorization": "Bearer plugin-scope-key",
                "X-Embodiment-Bridge-Key": "bridge-secret",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Last-Event-ID": "event-7",
                "Host": "evil.example",
                "Connection": "close",
                "Proxy-Connection": "keep-alive",
                "Forwarded": "for=203.0.113.8",
                "X-Forwarded-For": "203.0.113.8",
                "X-Real-IP": "203.0.113.8",
            }
            response = await client.get(
                base + f"{PUBLIC_API_PATH}/events/session-headers",
                headers=headers,
            )
            assert response.status == 200
            await response.read()
            observed = received[-1]["headers"]
            assert observed["Authorization"] == "Bearer plugin-scope-key"
            assert observed["X-Embodiment-Bridge-Key"] == "bridge-secret"
            assert observed["Content-Type"] == "application/json"
            assert observed["Accept"] == "text/event-stream"
            assert observed["Last-Event-ID"] == "event-7"
            assert observed["Host"] == f"127.0.0.1:{upstream_port}"
            assert "Proxy-Connection" not in observed
            assert "Forwarded" not in observed
            assert "X-Forwarded-For" not in observed
            assert "X-Real-IP" not in observed
            assert observed.get("Connection", "").lower() != "close"

            too_large = await client.post(
                base + f"{PUBLIC_API_PATH}/turn/start",
                data=b"x" * 4097,
                headers={"Content-Type": "application/json"},
            )
            assert too_large.status == 413

            legacy_health = await client.get(
                base + f"{LEGACY_PUBLIC_API_PREFIX}/health",
                headers={
                    "Authorization": "Bearer plugin-scope-key",
                    "X-Quest-Avatar-Key": "legacy-bridge-secret",
                },
            )
            assert legacy_health.status == 200
            await legacy_health.read()
            assert received[-1]["path"] == f"{LEGACY_PUBLIC_API_PREFIX}/health"
            assert received[-1]["headers"]["X-Quest-Avatar-Key"] == (
                "legacy-bridge-secret"
            )

            legacy_exchange = await client.post(
                base + f"{LEGACY_PUBLIC_API_PREFIX}/pairing/exchange",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            assert legacy_exchange.status == 404

        before = len(received)
        async with aiohttp.ClientSession() as client:
            for path in (
                "/dashboard",
                "/api/v1/config",
                "/api/v1/plugins/extensions/other/health",
                f"{PUBLIC_API_PATH}/pairing/create",
                f"{PUBLIC_API_PATH}/pairing/overview",
                f"{PUBLIC_API_PATH}/pairing/service-status",
                f"{PUBLIC_API_PATH}/pairing/service-control",
                f"{PUBLIC_API_PATH}/pairing/operator-settings",
                f"{PUBLIC_API_PATH}/pairing/platform-settings",
                f"{PUBLIC_API_PATH}/pairing/identity-candidates",
                f"{PUBLIC_API_PATH}/pairing/api-principal-proof",
            ):
                assert (await client.get(base + path)).status == 404
        assert len(received) == before

        await listener.close()
        await runner.cleanup()

    asyncio.run(scenario())


def test_spatial_context_is_not_added_to_legacy_listener_routes() -> None:
    assert (
        "POST",
        f"{PUBLIC_API_PATH}/spatial/context",
    ) in _FIXED_PROXY_ROUTES
    assert not any(
        path.endswith("/spatial/context")
        for _method, path in _LEGACY_FIXED_PROXY_ROUTES
    )
    assert (
        "POST",
        f"{PUBLIC_API_PATH}/action/result",
    ) in _FIXED_PROXY_ROUTES
    assert any(
        method == "POST" and path.endswith("/action/result")
        for method, path in _LEGACY_FIXED_PROXY_ROUTES
    )


def test_sse_is_streamed_before_upstream_completion() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        upstream_finished = asyncio.Event()

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            assert request.path.endswith("/events/session-sse")
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"event: first\ndata: one\n\n")
            await release.wait()
            await response.write(b"event: second\ndata: two\n\n")
            await response.write_eof()
            upstream_finished.set()
            return response

        runner, upstream_port = await start_upstream(upstream_handler)
        listener = await start_listener(make_manager(), upstream_port=upstream_port)
        url = listener_base(listener) + f"{PUBLIC_API_PATH}/events/session-sse"

        async with aiohttp.ClientSession() as client:
            response = await client.get(url, headers={"Accept": "text/event-stream"})
            first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 0.5)
            assert first == b"event: first\ndata: one\n\n"
            assert upstream_finished.is_set() is False
            assert response.headers["X-Accel-Buffering"] == "no"
            release.set()
            remainder = await asyncio.wait_for(response.read(), 0.5)
            assert remainder == b"event: second\ndata: two\n\n"
            assert upstream_finished.is_set() is True

        await listener.close()
        await runner.cleanup()

    asyncio.run(scenario())


def test_sse_client_disconnect_releases_upstream() -> None:
    async def scenario() -> None:
        continue_writing = asyncio.Event()
        upstream_released = asyncio.Event()

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await continue_writing.wait()
            try:
                chunk = b"data: " + (b"x" * 65_536) + b"\n\n"
                for _ in range(4096):
                    await response.write(chunk)
            except (ConnectionResetError, asyncio.CancelledError):
                upstream_released.set()
                raise
            return response

        runner, upstream_port = await start_upstream(upstream_handler)
        listener = await start_listener(make_manager(), upstream_port=upstream_port)
        url = listener_base(listener) + f"{PUBLIC_API_PATH}/events/session-drop"
        client = aiohttp.ClientSession()
        response = await client.get(url)
        assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 0.5)
        response.close()
        continue_writing.set()
        await asyncio.wait_for(upstream_released.wait(), 2.0)
        await client.close()
        await listener.close()
        await runner.cleanup()

    asyncio.run(scenario())


def test_unreachable_upstream_returns_stable_no_store_error_without_secrets() -> None:
    async def scenario() -> None:
        probe = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        unavailable_port = int(probe.sockets[0].getsockname()[1])
        probe.close()
        await probe.wait_closed()

        logger = LogCapture()
        listener = await start_listener(
            make_manager(),
            upstream_port=unavailable_port,
            logger=logger,
        )
        secret = "do-not-echo-api-key"
        async with aiohttp.ClientSession() as client:
            response = await client.get(
                listener_base(listener) + f"{PUBLIC_API_PATH}/health",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "X-Quest-Avatar-Key": "do-not-echo-bridge-key",
                },
            )
            text = await response.text()
            assert response.status == 503
            assert response.headers["Cache-Control"].startswith("no-store")
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert secret not in text
            assert "do-not-echo-bridge-key" not in text
            assert (await response.json())["data"]["code"] == (
                "listener_upstream_unavailable"
            )
        assert all(secret not in line for line in logger.lines)
        await listener.close()

    asyncio.run(scenario())


def test_listener_can_stop_release_port_and_start_again() -> None:
    async def scenario() -> None:
        listener = await start_listener(make_manager())
        first_port = int(listener.status_snapshot()["port"])
        assert listener.ready is True

        await listener.stop(reason="service_disabled")
        assert listener.ready is False
        assert listener.status_snapshot()["reason"] == "service_disabled"

        probe = await asyncio.start_server(
            lambda _reader, _writer: None,
            "127.0.0.1",
            first_port,
        )
        probe.close()
        await probe.wait_closed()

        await listener.start()
        assert listener.ready is True
        assert listener.status_snapshot()["reason"] == "ready"
        await listener.close()

    asyncio.run(scenario())


def test_schema_and_requirements_declare_safe_listener_defaults() -> None:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema["server_timing_enabled"]["default"] is False
    assert schema["pairing_listener_enabled"]["default"] is False
    assert schema["pairing_listener_host"]["default"] == "0.0.0.0"
    port = schema["pairing_listener_port"]
    assert port["default"] == 8520
    assert port["minimum"] == 1024
    assert port["maximum"] == 65535
    assert schema["pairing_listener_upstream_url"]["default"] == (
        "http://127.0.0.1:6185"
    )
    assert schema["pairing_listener_public_url"]["default"] == ""
    assert schema["astrbot_stt_provider_id"] == {
        "description": schema["astrbot_stt_provider_id"]["description"],
        "type": "string",
        "_special": "select_provider_stt",
        "hint": schema["astrbot_stt_provider_id"]["hint"],
        "default": "",
    }
    for removed in (
        "enable_astrbot_stt",
        "enable_plugin_mimo_stt",
        "plugin_mimo_stt_api_base",
        "plugin_mimo_stt_api_key",
        "plugin_mimo_stt_model",
    ):
        assert removed not in schema
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "aiohttp>=3.11.18,<4" in requirements.splitlines()
