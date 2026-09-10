from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import ValidationError

from astrbot_plugin_embodiment_bridge.core.pairing import (
    PUBLIC_API_PATH,
    PairingConfiguration,
    PairingCreateRequest,
    PairingError,
    PairingExchangeRequest,
    PairingManager,
    normalize_certificate_pin,
    normalize_public_base_url,
)


BRIDGE_KEY = "bridge-pairing-test-key-000000000000000000"
EXCHANGE_URL = "https://pair.example.com/quest/pairing/exchange"
CERT_PIN = "ab" * 32


def pairing_manager(**changes: Any) -> PairingManager:
    options: dict[str, Any] = {
        "bridge_api_key": BRIDGE_KEY,
        "exchange_url": EXCHANGE_URL,
    }
    options.update(changes)
    return PairingManager(**options)


def create_payload(**changes: Any) -> PairingCreateRequest:
    payload: dict[str, Any] = {
        "public_url": "https://bot.example.com",
        "port": 7443,
        "astrbot_api_key": "plugin-scope-api-key",
        "client_id": "quest-living-room",
        "user_id": "user-test",
        "bot_id": "bot-test",
        "group_id": "",
        "relationship_profile_id": "owner-profile",
        "expected_remote_ip": "192.0.2.10",
        "ttl_seconds": 120,
    }
    payload.update(changes)
    return PairingCreateRequest.model_validate(payload)


def qr_fields(result: Any) -> dict[str, str]:
    return json.loads(result.qr_payload)


def test_certificate_pin_validation_is_strict_and_normalized() -> None:
    assert normalize_certificate_pin(" " + CERT_PIN.upper() + " ") == CERT_PIN
    for invalid in (
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "sha256:" + CERT_PIN,
        ":".join(CERT_PIN[index : index + 2] for index in range(0, 64, 2)),
        123,
        None,
    ):
        with pytest.raises(ValueError, match="64 hexadecimal"):
            normalize_certificate_pin(invalid)

    with pytest.raises(ValidationError):
        PairingCreateRequest.model_validate(
            {
                "public_url": "https://bot.example.com",
                "astrbot_api_key": "api",
                "client_id": "client",
                "user_id": "user",
                "bot_id": "bot",
                "certificate_pin_sha256": "bad",
            }
        )


def test_invalid_server_pin_fails_closed() -> None:
    manager = pairing_manager(certificate_pin_sha256="not-a-pin")
    assert manager.bootstrap_ready is False
    assert manager.bootstrap_reason == "invalid_certificate_pin_sha256"
    with pytest.raises(PairingError) as unavailable:
        manager.create("owner", create_payload())
    assert unavailable.value.code == "pairing_bootstrap_unavailable"


def test_https_pin_propagates_to_qr_and_exchange_configuration() -> None:
    manager = pairing_manager(certificate_pin_sha256=CERT_PIN)
    created = manager.create(
        "owner",
        create_payload(public_url="https://pair.example.com", port=None),
    )
    fields = qr_fields(created)
    assert fields["certificate_pin_sha256"] == CERT_PIN

    exchanged = manager.exchange(
        PairingExchangeRequest(token=fields["token"]),
        remote="192.0.2.10",
    )
    assert exchanged.configuration["certificate_pin_sha256"] == CERT_PIN


def test_configured_https_pin_rejects_plain_http_bootstrap() -> None:
    manager = pairing_manager(
        exchange_url="http://192.168.50.10:8520/quest/pairing/exchange",
        allow_private_http=True,
        certificate_pin_sha256=CERT_PIN,
    )
    assert manager.bootstrap_ready is False
    assert manager.bootstrap_reason == "certificate_pin_requires_https"
    with pytest.raises(PairingError) as unavailable:
        manager.create(
            "owner",
            create_payload(
                public_url="http://192.168.50.10",
                port=8520,
                allow_insecure_http=True,
            ),
        )
    assert unavailable.value.code == "pairing_bootstrap_unavailable"


def test_exchange_and_configuration_schemes_must_match() -> None:
    manager = pairing_manager(exchange_url="http://192.168.50.10:8520/quest/pairing/exchange", allow_private_http=True)
    with pytest.raises(PairingError) as mixed:
        manager.create("owner", create_payload(public_url="https://secure.example.com", port=443))
    assert mixed.value.code == "pairing_transport_mismatch"


def test_reconfiguring_exchange_url_revokes_old_waiting_credentials() -> None:
    manager = pairing_manager()
    created = manager.create("owner", create_payload())
    token = qr_fields(created)["token"]
    manager.configure_exchange_url(
        "https://new.example.com:9443/quest/pairing/exchange",
        missing_reason="pairing_listener_public_url_missing",
    )
    with pytest.raises(PairingError) as stale:
        manager.exchange(PairingExchangeRequest(token=token), remote="192.0.2.10")
    assert stale.value.code == "pairing_not_available"


def test_requested_pin_requires_matching_server_configuration() -> None:
    with pytest.raises(PairingError) as unconfigured:
        pairing_manager().create("owner", create_payload(certificate_pin_sha256=CERT_PIN))
    assert unconfigured.value.code == "certificate_pin_mismatch"

    manager = pairing_manager(certificate_pin_sha256=CERT_PIN)
    with pytest.raises(PairingError) as mismatched:
        manager.create("owner", create_payload(certificate_pin_sha256="cd" * 32))
    assert mismatched.value.code == "certificate_pin_mismatch"

    matched = manager.create(
        "owner",
        create_payload(
            public_url="https://pair.example.com",
            port=None,
            certificate_pin_sha256=CERT_PIN,
        ),
    )
    assert qr_fields(matched)["certificate_pin_sha256"] == CERT_PIN


def test_pairing_configuration_omits_pin_for_http_and_preserves_v1_shape() -> None:
    http = PairingConfiguration(
        base_url="http://192.168.50.10:8520" + PUBLIC_API_PATH,
        astrbot_api_key="api",
        bridge_api_key=BRIDGE_KEY,
        client_id="client",
        user_id="user",
        bot_id="bot",
        allow_insecure_http=True,
        certificate_pin_sha256=CERT_PIN,
    )
    assert "certificate_pin_sha256" not in http.exchange_payload()

    https_without_pin = PairingConfiguration(
        base_url="https://bot.example.com" + PUBLIC_API_PATH,
        astrbot_api_key="api",
        bridge_api_key=BRIDGE_KEY,
        client_id="client",
        user_id="user",
        bot_id="bot",
    )
    assert "certificate_pin_sha256" not in https_without_pin.exchange_payload()


def test_public_url_normalization_is_https_and_path_strict() -> None:
    assert normalize_public_base_url("https://bot.example.com", 7443) == (
        f"https://bot.example.com:7443{PUBLIC_API_PATH}"
    )
    assert (
        normalize_public_base_url(f"https://bot.example.com{PUBLIC_API_PATH}/")
        == f"https://bot.example.com{PUBLIC_API_PATH}"
    )

    for invalid in (
        "http://bot.example.com",
        "https://user:pass@bot.example.com",
        "https://bot.example.com/dashboard",
        "https://bot.example.com?token=secret",
    ):
        with pytest.raises(PairingError):
            normalize_public_base_url(invalid)


def test_token_exchange_is_single_use_and_wipes_active_secret() -> None:
    manager = pairing_manager()
    created = manager.create("dashboard-owner", create_payload())
    fields = qr_fields(created)

    assert fields == {
        "type": "astrbot.quest.pair",
        "version": "1.0",
        "exchange_url": EXCHANGE_URL,
        "token": fields["token"],
    }
    assert len(fields["token"]) >= 32
    assert "plugin-scope-api-key" not in created.qr_payload
    assert BRIDGE_KEY not in created.qr_payload
    assert fields["token"] not in repr(created)
    assert fields["token"] not in repr(manager.__dict__)

    exchanged = manager.exchange(
        PairingExchangeRequest(token=fields["token"]),
        remote="192.0.2.10",
    )
    assert exchanged.configuration["base_url"] == (
        f"https://bot.example.com:7443{PUBLIC_API_PATH}"
    )
    assert exchanged.configuration["astrbot_api_key"] == "plugin-scope-api-key"
    assert exchanged.configuration["bridge_api_key"] == BRIDGE_KEY
    assert exchanged.configuration["allow_insecure_http"] is False
    assert manager.status("dashboard-owner", created.pairing_id)["state"] == "consumed"

    with pytest.raises(PairingError, match="invalid, expired, or already used"):
        manager.exchange(
            PairingExchangeRequest(token=fields["token"]),
            remote="192.0.2.10",
        )


def test_short_code_exchange_expiration_revoke_and_owner_scope() -> None:
    now = [1_000.0]
    manager = pairing_manager(clock=lambda: now[0])
    created = manager.create("owner-a", create_payload(ttl_seconds=60))
    assert created.short_code.isdigit() and len(created.short_code) == 6

    with pytest.raises(PairingError) as wrong_owner:
        manager.status("owner-b", created.pairing_id)
    assert wrong_owner.value.code == "pairing_not_found"

    now[0] += 61
    assert manager.status("owner-a", created.pairing_id)["state"] == "expired"
    with pytest.raises(PairingError):
        manager.exchange(
            PairingExchangeRequest(code=created.short_code),
            remote="198.51.100.2",
        )

    second = manager.create("owner-a", create_payload())
    assert manager.revoke("owner-a", second.pairing_id)["state"] == "revoked"
    with pytest.raises(PairingError):
        manager.exchange(
            PairingExchangeRequest(code=second.short_code),
            remote="198.51.100.2",
        )


def test_concurrent_exchange_has_exactly_one_winner() -> None:
    manager = pairing_manager(
        exchange_attempts_per_minute=32,
    )
    created = manager.create("owner", create_payload(expected_remote_ip="203.0.113.8"))
    token = qr_fields(created)["token"]

    def attempt(index: int) -> bool:
        try:
            manager.exchange(
                PairingExchangeRequest(token=token),
                remote="203.0.113.8",
            )
            return True
        except PairingError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7


def test_exchange_rate_limit_is_per_remote_and_has_retry_after() -> None:
    now = [10_000.0]
    manager = pairing_manager(
        clock=lambda: now[0],
        exchange_attempts_per_minute=3,
    )
    request = PairingExchangeRequest(token="x" * 43)
    for _ in range(3):
        with pytest.raises(PairingError) as invalid:
            manager.exchange(request, remote="203.0.113.8")
        assert invalid.value.code == "pairing_not_available"
    with pytest.raises(PairingError) as limited:
        manager.exchange(request, remote="203.0.113.8")
    assert limited.value.code == "pairing_rate_limited"
    assert limited.value.status_code == 429
    assert limited.value.retry_after == 60

    with pytest.raises(PairingError) as other_remote:
        manager.exchange(request, remote="203.0.113.9")
    assert other_remote.value.code == "pairing_not_available"


def test_exchange_rate_limit_also_has_a_global_budget() -> None:
    manager = pairing_manager(global_exchange_attempts_per_minute=2)
    request = PairingExchangeRequest(token="x" * 43)
    for remote in ("203.0.113.8", "203.0.113.9"):
        with pytest.raises(PairingError) as invalid:
            manager.exchange(request, remote=remote)
        assert invalid.value.code == "pairing_not_available"

    with pytest.raises(PairingError) as limited:
        manager.exchange(request, remote="203.0.113.10")
    assert limited.value.code == "pairing_rate_limited"


def test_create_requires_dashboard_owner_and_configured_bridge_key() -> None:
    with pytest.raises(PairingError) as missing_owner:
        pairing_manager().create("", create_payload())
    assert missing_owner.value.code == "astrbot_auth_required"

    with pytest.raises(PairingError) as missing_key:
        pairing_manager(bridge_api_key="short").create("owner", create_payload())
    assert missing_key.value.code == "bridge_not_configured"


def test_close_invalidates_all_active_sessions() -> None:
    manager = pairing_manager()
    created = manager.create("owner", create_payload())
    token = qr_fields(created)["token"]
    manager.close()
    with pytest.raises(PairingError):
        manager.exchange(
            PairingExchangeRequest(token=token),
            remote="192.0.2.20",
        )


def test_exchange_is_bound_to_the_expected_remote_ip() -> None:
    manager = pairing_manager()
    created = manager.create("owner", create_payload())
    token = qr_fields(created)["token"]

    with pytest.raises(PairingError) as mismatch:
        manager.exchange(
            PairingExchangeRequest(token=token),
            remote="192.0.2.11",
        )
    assert mismatch.value.code == "pairing_not_available"

    exchanged = manager.exchange(
        PairingExchangeRequest(token=token),
        remote="192.0.2.10",
    )
    assert exchanged.pairing_id == created.pairing_id


def test_private_http_requires_server_and_session_opt_in() -> None:
    private_manager = pairing_manager(
        exchange_url="http://192.168.50.10:8520/quest/pairing/exchange",
        allow_private_http=True,
    )
    created = private_manager.create(
        "owner",
        create_payload(
            public_url="http://192.168.50.10",
            port=8520,
            allow_insecure_http=True,
        ),
    )
    exchanged = private_manager.exchange(
        PairingExchangeRequest(token=qr_fields(created)["token"]),
        remote="192.0.2.10",
    )
    assert exchanged.configuration["base_url"] == (
        f"http://192.168.50.10:8520{PUBLIC_API_PATH}"
    )
    assert exchanged.configuration["allow_insecure_http"] is True

    with pytest.raises(PairingError) as public_http:
        private_manager.create(
            "owner",
            create_payload(
                public_url="http://203.0.113.10",
                port=8520,
                allow_insecure_http=True,
            ),
        )
    assert public_http.value.code == "https_required"


def test_remote_http_opt_in_allows_public_plain_http() -> None:
    """allow_insecure_remote_http 放开私网限制：公网/内网穿透明文 HTTP 可配对。"""
    remote_manager = pairing_manager(
        exchange_url="http://192.168.50.10:8520/quest/pairing/exchange",
        allow_remote_http=True,
    )
    created = remote_manager.create(
        "owner",
        create_payload(
            public_url="http://203.0.113.10",
            port=8520,
            allow_insecure_http=False,
        ),
    )
    exchanged = remote_manager.exchange(
        PairingExchangeRequest(token=qr_fields(created)["token"]),
        remote="192.0.2.10",
    )
    assert exchanged.configuration["base_url"] == (
        f"http://203.0.113.10:8520{PUBLIC_API_PATH}"
    )
    assert exchanged.configuration["allow_insecure_http"] is True

    without_opt_in = pairing_manager(
        exchange_url="https://192.168.50.10:8520/quest/pairing/exchange",
    )
    with pytest.raises(PairingError) as public_http:
        without_opt_in.create(
            "owner",
            create_payload(
                public_url="http://203.0.113.10",
                port=8520,
                allow_insecure_http=True,
            ),
        )
    assert public_http.value.code == "https_required"


def test_missing_exchange_proxy_fails_closed() -> None:
    manager = PairingManager(bridge_api_key=BRIDGE_KEY)
    assert manager.bootstrap_ready is False
    with pytest.raises(PairingError) as unavailable:
        manager.create("owner", create_payload())
    assert unavailable.value.code == "pairing_bootstrap_unavailable"


def test_configure_exchange_url_forwards_remote_http_opt_in() -> None:
    """回归：configure_exchange_url 必须把 allow_remote_http 传给归一化器。

    生产事故（2026-09-07）：监听器公告地址配置为 http://<域名>:8520、
    allow_insecure_remote_http 已开，构造路径正常，但运行期
    configure_exchange_url 漏传该旗标 → https_required → 快速绑定未就绪。
    """
    manager = pairing_manager(exchange_url="", allow_remote_http=True)
    assert manager.bootstrap_ready is False
    manager.configure_exchange_url(
        "http://bridge.example.com:8520/quest/pairing/exchange",
        missing_reason="pairing_listener_public_url_missing",
    )
    assert manager.bootstrap_reason == "ready"
    assert manager.bootstrap_ready is True
    assert manager.exchange_url.startswith("http://bridge.example.com")

    strict = pairing_manager(exchange_url="")
    strict.configure_exchange_url(
        "http://bridge.example.com:8520/quest/pairing/exchange",
        missing_reason="pairing_listener_public_url_missing",
    )
    assert strict.bootstrap_reason == "https_required"
    assert strict.bootstrap_ready is False
