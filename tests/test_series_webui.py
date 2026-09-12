from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

from astrbot_plugin_embodiment_bridge.core.pairing import (
    PairingCreateRequest,
    PairingError,
)
from astrbot_plugin_embodiment_bridge.series_webui import SeriesWebUIPanels


class PairingManagerStub:
    def __init__(self) -> None:
        self.created: tuple[str, PairingCreateRequest] | None = None
        self.status_calls: list[tuple[str, str]] = []
        self.revoke_calls: list[tuple[str, str]] = []
        self.revoke_all_calls = 0

    def create(self, owner: str, request: PairingCreateRequest) -> Any:
        self.created = (owner, request)
        return SimpleNamespace(
            pairing_id="a" * 32,
            short_code="123456",
            created_at=time.time(),
            expires_at=time.time() + 120,
            exchange_url="https://bind.example/pairing/exchange",
            qr_payload="pairing-token-release-secret",
        )

    def status(self, owner: str, pairing_id: str) -> dict[str, Any]:
        self.status_calls.append((owner, pairing_id))
        return {
            "pairing_id": pairing_id,
            "state": "waiting",
            "expires_at": time.time() + 60,
            "remaining_seconds": 60,
            "consumed_at": None,
            "device_secret": "device-secret-must-not-leak",
            "bind_host": "10.20.30.40",
        }

    def revoke(self, owner: str, pairing_id: str) -> dict[str, Any]:
        self.revoke_calls.append((owner, pairing_id))
        return {
            "pairing_id": pairing_id,
            "state": "revoked",
            "expires_at": time.time() + 60,
            "remaining_seconds": 60,
            "consumed_at": None,
            "pairing_token": "token-must-not-leak",
        }

    def revoke_waiting(self) -> int:
        self.revoke_all_calls += 1
        return 2


class PairingApiStub:
    def __init__(self, manager: PairingManagerStub) -> None:
        self.manager = manager

    def _complete_create_request(
        self, payload: PairingCreateRequest
    ) -> PairingCreateRequest:
        return payload


class ServiceStub:
    def __init__(self) -> None:
        self.enabled = True
        self.calls: list[bool] = []

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        self.enabled = enabled
        self.calls.append(enabled)
        return self._snapshot()

    async def status_snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ready": self.enabled,
            "status": "running" if self.enabled else "stopped",
            "reason": "ready" if self.enabled else "service_disabled",
            "listener": {
                "bind_host": "10.20.30.40",
                "port": 8520,
                "device_secret": "listener-secret",
            },
            "sessions": {
                "active_sessions": 3,
                "attached_streams": 2,
                "queued_events": 4,
            },
            "capabilities": {
                "bridge": self.enabled,
                "direct_provider_fallback": False,
                "eventbus": False,
                "device_secret": "capability-secret",
            },
            "device_secret": "service-secret",
        }


class SessionsStub:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close_all_sessions(self) -> None:
        self.close_calls += 1


class OperatorSettingsStub:
    def __init__(self) -> None:
        self.fallback_calls: list[bool] = []
        self.persona_mode_calls: list[dict[str, Any]] = []

    async def save_quest_chain_settings(self, **kwargs: Any) -> dict[str, Any]:
        self.fallback_calls.append(bool(kwargs["allow_direct_provider_fallback"]))
        return {
            "mode": "bridge",
            "bridge_available": True,
            "allow_direct_provider_fallback": bool(
                kwargs["allow_direct_provider_fallback"]
            ),
            "device_secret": "dialogue-secret",
        }

    async def persona_overview(self) -> dict[str, Any]:
        return {
            "source_mode": "astrbot",
            "astrbot_persona_id": "persona-a",
            "character_name": "凌溪",
            "character_self_reference": "我",
            "character_self_description": "温和",
            "character_user_relationship": "伙伴",
            "quest_persona_prompt": "prompt-must-not-leak",
        }

    async def save_character_persona(self, **kwargs: Any) -> dict[str, Any]:
        self.persona_mode_calls.append(dict(kwargs))
        return {
            **await self.persona_overview(),
            "source_mode": kwargs["persona_source_mode"],
        }


class PersonaServiceStub:
    def __init__(self) -> None:
        self.activations: list[str] = []

    async def library_snapshot(self) -> dict[str, Any]:
        return {
            "active_quest_persona_id": "profile-a",
            "active_profile_name": "温柔临",
            "active_available": True,
            "active_status": "ready",
            "profiles": [
                {
                    "profile_id": "profile-a",
                    "display_name": "温柔临",
                    "quest_persona_prompt": "profile-prompt-must-not-leak",
                }
            ],
            "providers": [{"id": "provider-a", "key": "provider-secret"}],
        }

    async def activate(self, profile_id: str) -> dict[str, Any]:
        self.activations.append(profile_id)
        snapshot = await self.library_snapshot()
        snapshot["active_quest_persona_id"] = profile_id
        snapshot["active_profile_name"] = "温柔临" if profile_id else ""
        return snapshot


class OrchestratorStub:
    def __init__(self) -> None:
        self.allow_direct_provider_fallback = False
        self.quest_enriched_pipeline = SimpleNamespace(available=True)


class PluginStub:
    def __init__(self) -> None:
        self.config = {
            "bridge_service_enabled": True,
            "persona_source_mode": "astrbot",
            "active_quest_persona_id": "profile-a",
            "max_sessions": 8,
            "bridge_api_key": "bridge-key-must-not-leak",
            "pairing_token": "config-token-must-not-leak",
        }
        self.pairing_manager = PairingManagerStub()
        self.pairing = self.pairing_manager
        self.pairing_api = PairingApiStub(self.pairing_manager)
        self.pairing_listener = SimpleNamespace(
            status_snapshot=lambda: {
                "enabled": True,
                "ready": True,
                "reason": "ready",
                "bind_host": "10.20.30.40",
                "device_secret": "listener-secret",
            }
        )
        self.service = ServiceStub()
        self.sessions = SessionsStub()
        self.operator_settings = OperatorSettingsStub()
        self.persona_service = PersonaServiceStub()
        self.orchestrator = OrchestratorStub()

    def _webui_service_status_snapshot(self) -> dict[str, Any]:
        return {
            "status": "running",
            "bootstrap_ready": True,
            "persona_mode": "astrbot",
        }


def test_contract_declares_operator_actions_and_risk_metadata() -> None:
    adapter = SeriesWebUIPanels(PluginStub())

    contract = adapter.contract()
    panels = {panel["id"]: panel for panel in contract["panels"]}

    assert set(panels) == {"service_status", "operator"}
    assert panels["service_status"]["read_only"] is True
    assert panels["service_status"]["actions"] == []
    actions = {item["id"]: item for item in panels["operator"]["actions"]}
    assert {
        "create_pairing_session",
        "refresh_pairing_session",
        "revoke_pairing_session",
        "set_bridge_service",
        "disconnect_all_sessions",
        "set_dialogue_fallback",
        "set_persona_source_mode",
        "activate_persona_profile",
    } <= set(actions)
    assert actions["create_pairing_session"]["min_role"] == "admin"
    assert actions["create_pairing_session"]["effect"] == "non_idempotent"
    assert actions["create_pairing_session"]["idempotency_required"] is True
    assert actions["revoke_pairing_session"]["confirm"]
    assert actions["set_bridge_service"]["confirm"]
    assert actions["disconnect_all_sessions"]["confirm"]
    assert actions["set_persona_source_mode"]["min_role"] == "owner"
    assert actions["activate_persona_profile"]["min_role"] == "owner"
    assert all(
        "revision_required" in item and "effect" in item for item in actions.values()
    )


def test_pairing_lifecycle_reuses_manager_and_drops_credentials() -> None:
    async def scenario() -> None:
        plugin = PluginStub()
        adapter = SeriesWebUIPanels(plugin)

        created = await adapter.panel_action(
            "operator",
            "create_pairing_session",
            {},
            {"role": "admin"},
        )
        assert created["success"] is True
        assert created["pairing"]["pairing_id"] == "a" * 32
        assert created["pairing"]["state"] == "waiting"
        assert plugin.pairing_manager.created is not None
        assert plugin.pairing_manager.created[0] == "series.webui"

        refreshed = await adapter.panel_action(
            "operator",
            "refresh_pairing_session",
            {"pairing_id": "a" * 32},
            {"role": "admin"},
        )
        assert refreshed["success"] is True
        assert refreshed["pairing"]["remaining_seconds"] == 60

        revoked = await adapter.panel_action(
            "operator",
            "revoke_pairing_session",
            {"pairing_id": "a" * 32},
            {"role": "admin"},
        )
        assert revoked["success"] is True
        assert revoked["pairing"]["state"] == "revoked"

        revoked_all = await adapter.panel_action(
            "operator",
            "revoke_all_pairing_sessions",
            {},
            {"role": "admin"},
        )
        assert revoked_all == {
            "success": True,
            "message": "全部待配对凭据已撤销",
            "revoked_count": 2,
        }
        assert plugin.pairing_manager.revoke_all_calls == 1

        serialized = json.dumps(
            [created, refreshed, revoked, revoked_all], ensure_ascii=False
        )
        for secret in (
            "123456",
            "pairing-token-release-secret",
            "device-secret-must-not-leak",
            "token-must-not-leak",
            "10.20.30.40",
        ):
            assert secret not in serialized

    asyncio.run(scenario())


def test_service_session_and_dialogue_actions_reuse_existing_services() -> None:
    async def scenario() -> None:
        plugin = PluginStub()
        adapter = SeriesWebUIPanels(plugin)

        stopped = await adapter.panel_action(
            "operator", "set_bridge_service", {"enabled": False}, {"role": "admin"}
        )
        assert stopped["success"] is True
        assert plugin.service.calls == [False]
        assert stopped["service"]["sessions"]["active_sessions"] == 3

        disconnected = await adapter.panel_action(
            "operator", "disconnect_all_sessions", {}, {"role": "admin"}
        )
        assert disconnected["success"] is True
        assert plugin.sessions.close_calls == 1

        dialogue = await adapter.panel_action(
            "operator",
            "set_dialogue_fallback",
            {"enabled": True},
            {"role": "admin"},
        )
        assert dialogue["success"] is True
        assert plugin.operator_settings.fallback_calls == [True]
        assert dialogue["dialogue"] == {
            "mode": "bridge",
            "bridge_available": True,
            "allow_direct_provider_fallback": True,
        }

        status = await adapter.panel_action(
            "operator", "refresh_status", {}, {"role": "viewer"}
        )
        assert status["success"] is True
        serialized = json.dumps([stopped, disconnected, dialogue, status], ensure_ascii=False)
        for secret in (
            "listener-secret",
            "capability-secret",
            "service-secret",
            "dialogue-secret",
            "10.20.30.40",
        ):
            assert secret not in serialized

    asyncio.run(scenario())


def test_persona_actions_require_owner_and_only_return_public_summary() -> None:
    async def scenario() -> None:
        plugin = PluginStub()
        adapter = SeriesWebUIPanels(plugin)

        forbidden = await adapter.panel_action(
            "operator",
            "set_persona_source_mode",
            {"mode": "manual_override"},
            {"role": "admin"},
        )
        assert forbidden["error"] == "ROLE_FORBIDDEN"
        assert plugin.operator_settings.persona_mode_calls == []

        switched = await adapter.panel_action(
            "operator",
            "set_persona_source_mode",
            {"mode": "manual_override"},
            {"role": "owner"},
        )
        assert switched["success"] is True
        assert plugin.operator_settings.persona_mode_calls[0]["persona_source_mode"] == (
            "manual_override"
        )

        activated = await adapter.panel_action(
            "operator",
            "activate_persona_profile",
            {"profile_id": "profile-a"},
            {"role": "owner"},
        )
        assert activated["success"] is True
        assert plugin.persona_service.activations == ["profile-a"]

        missing = await adapter.panel_action(
            "operator",
            "activate_persona_profile",
            {"profile_id": "missing"},
            {"role": "owner"},
        )
        assert missing["error"] == "PERSONA_PROFILE_NOT_FOUND"

        serialized = json.dumps([switched, activated, missing], ensure_ascii=False)
        for secret in (
            "prompt-must-not-leak",
            "profile-prompt-must-not-leak",
            "provider-secret",
        ):
            assert secret not in serialized

    asyncio.run(scenario())


def test_action_validation_and_failures_are_fail_closed_and_redacted() -> None:
    class BrokenPairingManager(PairingManagerStub):
        def status(self, owner: str, pairing_id: str) -> dict[str, Any]:
            raise PairingError(
                "pairing_not_owned",
                403,
                "device-secret /private/device.key must not leak",
            )

    async def scenario() -> None:
        plugin = PluginStub()
        plugin.pairing_manager = BrokenPairingManager()
        plugin.pairing = plugin.pairing_manager
        plugin.pairing_api = PairingApiStub(plugin.pairing_manager)
        adapter = SeriesWebUIPanels(plugin)

        invalid_bool = await adapter.panel_action(
            "operator", "set_bridge_service", {"enabled": "true"}, {"role": "admin"}
        )
        assert invalid_bool["error"] == "INVALID_PAYLOAD"

        invalid_pairing = await adapter.panel_action(
            "operator",
            "refresh_pairing_session",
            {"pairing_id": "not-a-pairing-id"},
            {"role": "admin"},
        )
        assert invalid_pairing["error"] == "INVALID_PAIRING_ID"

        failed = await adapter.panel_action(
            "operator",
            "refresh_pairing_session",
            {"pairing_id": "b" * 32},
            {"role": "admin"},
        )
        assert failed["error"] == "PAIRING_NOT_OWNED"
        assert "must not leak" not in json.dumps(failed, ensure_ascii=False)

        unknown_action = await adapter.panel_action(
            "operator", "set_eventbus_dialogue", {}, {"role": "owner"}
        )
        assert unknown_action["error"] == "UNKNOWN_ACTION"
        assert (
            await adapter.panel_action("unknown", "refresh_status", {})
        )["error"] == "UNKNOWN_PANEL"

    asyncio.run(scenario())


def test_operator_panel_redacts_binding_addresses_and_credentials() -> None:
    plugin = PluginStub()
    adapter = SeriesWebUIPanels(plugin)

    panel = adapter.panel_data("operator")

    assert panel["success"] is True
    assert panel["title"] == "临日常管理"
    rows = {row["item"]: row["value"] for row in panel["rows"]}
    assert rows["EventBus 对话"] == "已退役（固定关闭）"
    assert rows["人格来源"] == "astrbot"
    serialized = json.dumps(panel, ensure_ascii=False)
    for secret in (
        "bridge-key-must-not-leak",
        "config-token-must-not-leak",
        "listener-secret",
        "10.20.30.40",
    ):
        assert secret not in serialized
