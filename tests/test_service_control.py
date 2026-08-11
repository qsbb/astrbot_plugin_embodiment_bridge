from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.core.models import SessionStartRequest
from astrbot_plugin_embodiment_bridge.core.service_control import (
    BridgeServiceControl,
    BridgeServiceControlError,
    BridgeServiceUnavailable,
)
from astrbot_plugin_embodiment_bridge.core.session_manager import SessionManager


class ConfigStub(dict[str, Any]):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.saves: list[dict[str, Any]] = []

    async def save_config_async(self, changes: dict[str, Any]) -> bool:
        if self.fail:
            raise OSError("save failed")
        self.update(changes)
        self.saves.append(dict(changes))
        return True


class LegacySyncConfigStub(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.saves: list[dict[str, Any]] = []

    def save_config(self, changes: dict[str, Any]) -> None:
        self.update(changes)
        self.saves.append(dict(changes))


class ListenerStub:
    def __init__(self) -> None:
        self.config = SimpleNamespace(port=8520)
        self.configured = True
        self.ready = False
        self.reason = "not_started"
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.fail_start_ports: set[int] = set()

    async def start(self) -> None:
        self.starts += 1
        if self.config.port in self.fail_start_ports:
            self.ready = False
            self.reason = "start_failed"
            raise OSError("listener start failed")
        self.ready = True
        self.reason = "ready"

    async def stop(self, *, reason: str) -> None:
        self.stops += 1
        self.ready = False
        self.reason = reason

    async def close(self) -> None:
        self.closes += 1
        self.ready = False
        self.reason = "closed"

    def configure_port(self, port: int) -> None:
        assert self.ready is False
        self.config.port = port
        self.reason = "not_started"

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.configured,
            "ready": self.ready,
            "reason": self.reason,
            "bind_host": "0.0.0.0",
            "port": self.config.port,
            "secret": "not-projected",
        }


class OrchestratorStub:
    def __init__(self) -> None:
        self.llm = SimpleNamespace(available=True)
        self.stt = SimpleNamespace(available=True)
        self.tts = SimpleNamespace(available=True)

    def integration_status(self) -> dict[str, Any]:
        return {
            "identity": {"configured": True},
            "astrbot_message_pipeline": {"available": True},
        }


class LoggerStub:
    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


def build_control(*, fail_save: bool = False) -> tuple[Any, ...]:
    config = ConfigStub(fail=fail_save)
    listener = ListenerStub()
    sessions = SessionManager()
    control = BridgeServiceControl(
        config=config,
        listener=listener,
        sessions=sessions,
        orchestrator=OrchestratorStub(),
        logger=LoggerStub(),
        enabled=True,
    )
    return control, config, listener, sessions


def test_service_can_stop_close_sessions_and_start_again() -> None:
    async def scenario() -> None:
        control, config, listener, sessions = build_control()
        await control.initialize()
        await sessions.start_session(
            SessionStartRequest(
                session_id="s1",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "owner",
        )

        running = await control.status_snapshot()
        assert running["status"] == "running"
        assert running["ready"] is True
        assert running["sessions"]["active_sessions"] == 1
        assert running["listener"] == {
            "configured": True,
            "ready": True,
            "reason": "ready",
            "bind_host": "0.0.0.0",
            "port": 8520,
        }
        assert running["capabilities"] == {
            "dialogue": True,
            "eventbus": True,
            "identity_configured": True,
            "stt": True,
            "tts": True,
            "avatar_actions": True,
        }
        assert "not-projected" not in repr(running)

        stopped = await control.set_enabled(False)
        assert stopped["status"] == "stopped"
        assert stopped["sessions"]["active_sessions"] == 0
        assert listener.stops == 1
        assert config.saves == [{"bridge_service_enabled": False}]
        with pytest.raises(BridgeServiceUnavailable):
            control.require_enabled()

        restarted = await control.set_enabled(True)
        assert restarted["status"] == "running"
        assert listener.starts == 2
        assert config.saves[-1] == {"bridge_service_enabled": True}
        await control.close()
        assert listener.closes == 1

    asyncio.run(scenario())


def test_stop_closes_session_creation_gate_before_cleanup_snapshot() -> None:
    async def scenario() -> None:
        control, _, _, sessions = build_control()
        await control.initialize()
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        original_close_all = sessions.close_all_sessions

        async def delayed_close_all() -> None:
            cleanup_entered.set()
            await release_cleanup.wait()
            await original_close_all()

        sessions.close_all_sessions = delayed_close_all  # type: ignore[method-assign]
        stopping = asyncio.create_task(control.set_enabled(False))
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)

        with pytest.raises(BridgeServiceUnavailable):
            control.require_enabled()
        with pytest.raises(Exception, match="not accepting new sessions"):
            await sessions.start_session(
                SessionStartRequest(
                    session_id="late-session",
                    client_id="quest",
                    user_id="user",
                    bot_id="bot",
                ),
                "owner",
            )

        release_cleanup.set()
        stopped = await asyncio.wait_for(stopping, timeout=1)
        assert stopped["sessions"]["active_sessions"] == 0

    asyncio.run(scenario())


def test_astrbot_4265_sync_config_enables_service_control() -> None:
    async def scenario() -> None:
        config = LegacySyncConfigStub()
        listener = ListenerStub()
        sessions = SessionManager()
        control = BridgeServiceControl(
            config=config,
            listener=listener,
            sessions=sessions,
            orchestrator=OrchestratorStub(),
            logger=LoggerStub(),
            enabled=True,
        )

        await control.initialize()
        assert (await control.status_snapshot())["config_writable"] is True
        stopped = await control.set_enabled(False)
        assert stopped["status"] == "stopped"
        assert config.saves == [{"bridge_service_enabled": False}]

    asyncio.run(scenario())


def test_service_save_failure_does_not_change_runtime() -> None:
    async def scenario() -> None:
        control, _, listener, _ = build_control(fail_save=True)
        await control.initialize()

        with pytest.raises(BridgeServiceControlError) as failed:
            await control.set_enabled(False)

        assert failed.value.code == "config_save_failed"
        assert control.enabled is True
        assert listener.ready is True

    asyncio.run(scenario())


def test_listener_port_persists_rewrites_urls_and_restarts() -> None:
    async def scenario() -> None:
        control, config, listener, sessions = build_control()
        config.update(
            pairing_listener_public_url="http://192.168.50.10:8520",
            pairing_public_url="http://192.168.50.10:8520",
        )
        await control.initialize()
        await sessions.start_session(
            SessionStartRequest(
                session_id="port-session",
                client_id="quest",
                user_id="user-test",
                bot_id="bot-test",
            ),
            "owner",
        )

        updated = await control.set_listener_port(9020)

        assert updated["status"] == "running"
        assert updated["listener"]["port"] == 9020
        assert updated["sessions"]["active_sessions"] == 0
        assert listener.config.port == 9020
        assert listener.stops == 1
        assert listener.starts == 2
        assert config.saves[-1] == {
            "pairing_listener_port": 9020,
            "pairing_listener_public_url": "http://192.168.50.10:9020",
            "pairing_public_url": "http://192.168.50.10:9020",
        }

        with pytest.raises(BridgeServiceControlError) as invalid:
            await control.set_listener_port(80)
        assert invalid.value.code == "invalid_listener_port"

    asyncio.run(scenario())


def test_service_start_failure_rolls_back_switch_and_keeps_gate_closed() -> None:
    async def scenario() -> None:
        control, config, listener, sessions = build_control()
        control.enabled = False
        await control.initialize()
        listener.fail_start_ports.add(8520)

        with pytest.raises(BridgeServiceControlError) as failed:
            await control.set_enabled(True)

        assert failed.value.code == "service_start_failed"
        assert control.enabled is False
        assert listener.ready is False
        assert config.saves == [
            {"bridge_service_enabled": True},
            {"bridge_service_enabled": False},
        ]
        with pytest.raises(Exception, match="not accepting new sessions"):
            await sessions.start_session(
                SessionStartRequest(
                    session_id="must-not-start",
                    client_id="quest",
                    user_id="user",
                    bot_id="bot",
                ),
                "owner",
            )

    asyncio.run(scenario())


def test_initialize_start_failure_closes_session_gate() -> None:
    async def scenario() -> None:
        control, _, listener, sessions = build_control()
        listener.fail_start_ports.add(8520)

        with pytest.raises(OSError, match="listener start failed"):
            await control.initialize()

        with pytest.raises(Exception, match="not accepting new sessions"):
            await sessions.start_session(
                SessionStartRequest(
                    session_id="initialize-failed",
                    client_id="quest",
                    user_id="user",
                    bot_id="bot",
                ),
                "owner",
            )

    asyncio.run(scenario())


def test_disabled_standalone_listener_keeps_astrbot_routes_accepting() -> None:
    async def scenario() -> None:
        control, _, listener, sessions = build_control()
        listener.configured = False
        await control.initialize()

        session = await sessions.start_session(
            SessionStartRequest(
                session_id="astrbot-routed",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "owner",
        )
        assert session.session_id == "astrbot-routed"

        updated = await control.set_listener_port(9020)
        assert updated["listener"]["configured"] is False
        another = await sessions.start_session(
            SessionStartRequest(
                session_id="astrbot-routed-after-port",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "owner",
        )
        assert another.session_id == "astrbot-routed-after-port"

    asyncio.run(scenario())


def test_port_start_failure_restores_previous_listener_and_config() -> None:
    async def scenario() -> None:
        control, config, listener, sessions = build_control()
        config.update(
            pairing_listener_public_url="http://192.168.50.10:8520",
            pairing_public_url="http://192.168.50.10:8520",
        )
        await control.initialize()
        listener.fail_start_ports.add(9020)

        with pytest.raises(BridgeServiceControlError) as failed:
            await control.set_listener_port(9020)

        assert failed.value.code == "listener_port_update_failed"
        assert listener.config.port == 8520
        assert listener.ready is True
        assert config["pairing_listener_port"] == 8520
        assert config["pairing_listener_public_url"] == "http://192.168.50.10:8520"
        assert config["pairing_public_url"] == "http://192.168.50.10:8520"
        session = await sessions.start_session(
            SessionStartRequest(
                session_id="restored-listener",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "owner",
        )
        assert session.session_id == "restored-listener"

    asyncio.run(scenario())
