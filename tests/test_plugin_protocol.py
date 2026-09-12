from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from astrbot_plugin_embodiment_bridge.core.models import SessionStartRequest


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


class ResponseStub:
    def __init__(
        self,
        data: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.body = json.dumps(data, ensure_ascii=False).encode("utf-8")


class StreamingResponseStub:
    def __init__(self, content: Any, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.headers = headers or {}
        self.status_code = 200


class RequestStub:
    username = "api_key:test"
    content_type = "application/json"
    headers: dict[str, str] = {}
    body_bytes = b""
    client_host = "127.0.0.1"

    async def body(self) -> bytes:
        return self.body_bytes


class ContextStub:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any, list[str], str]] = []

    def register_web_api(
        self,
        route: str,
        handler: Any,
        methods: list[str],
        description: str,
    ) -> None:
        self.routes.append((route, handler, methods, description))

    def get_all_stars(self) -> list[Any]:
        return []

    async def llm_generate(self, **kwargs: Any) -> Any:
        del kwargs
        return types.SimpleNamespace(
            completion_text=(
                '{"should_reply":false,"reply_text":"","intent":'
                '{"emotion":"neutral","gesture":"idle","look_at":"none",'
                '"intensity":0,"duration_ms":0,"reason_code":"test"}}'
            )
        )


def install_astrbot_stubs(monkeypatch: Any, tmp_path: Path) -> RequestStub:
    request_stub = RequestStub()
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = LoggerStub()
    event = types.ModuleType("astrbot.api.event")
    event.filter = types.SimpleNamespace(
        on_llm_request=lambda **_kwargs: lambda handler: handler
    )
    api.event = event

    agent_tool = types.ModuleType("astrbot.core.agent.tool")

    class FunctionTool:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class ToolSet:
        def __init__(self) -> None:
            self.tools: list[Any] = []

        def add_tool(self, tool: Any) -> None:
            self.remove_tool(tool.name)
            self.tools.append(tool)

        def remove_tool(self, name: str) -> None:
            self.tools = [tool for tool in self.tools if tool.name != name]

        def get_tool(self, name: str) -> Any | None:
            return next((tool for tool in self.tools if tool.name == name), None)

    agent_tool.FunctionTool = FunctionTool
    agent_tool.ToolSet = ToolSet

    star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context: Any) -> None:
            self.context = context

    class StarTools:
        @staticmethod
        def get_data_dir(name: str) -> str:
            return str(tmp_path / "data" / "plugin_data" / name)

    star.Context = ContextStub
    star.Star = Star
    star.StarTools = StarTools

    web = types.ModuleType("astrbot.api.web")
    web.request = request_stub
    web.json_response = lambda data=None, status_code=200, headers=None: ResponseStub(
        data or {}, status_code=status_code, headers=headers
    )
    web.error_response = lambda message, status_code=400, data=None, headers=None: (
        ResponseStub(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )
    )
    web.stream_response = lambda content, headers=None, **kwargs: StreamingResponseStub(
        content, headers=headers
    )

    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    monkeypatch.setitem(sys.modules, "astrbot.core", types.ModuleType("astrbot.core"))
    monkeypatch.setitem(
        sys.modules, "astrbot.core.agent", types.ModuleType("astrbot.core.agent")
    )
    monkeypatch.setitem(sys.modules, "astrbot.core.agent.tool", agent_tool)
    sys.modules.pop("astrbot_plugin_embodiment_bridge.transport.http_sse", None)
    sys.modules.pop("astrbot_plugin_embodiment_bridge.main", None)
    return request_stub


def test_plugin_registers_public_http_sse_and_pairing_routes_and_terminates(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
        context = ContextStub()
        plugin = module.QuestAvatarBridgePlugin(
            context,
            {
                "bridge_api_key": "x" * 32,
                "chat_provider_id": "provider-1",
                "pairing_exchange_proxy_url": (
                    "https://pair.example.com/quest/pairing/exchange"
                ),
            },
        )
        await plugin.initialize()
        health = plugin.plugin_health()
        assert health["version"] == module.__version__
        assert health["checks"]["pairing_bootstrap_ready"] is True
        assert health["checks"]["quest_chain_dialogue_available"] is False
        assert "DIALOGUE_PATH_AVAILABLE" in health["reasons"]
        assert plugin.diagnostic_log_contract() == {
            "name": "series.diagnostics",
            "version": "1.0",
            "series_id": "ningxin_suxi",
            "plugin_id": "astrbot_plugin_embodiment_bridge",
            "plugin_name": "临",
            "capabilities": ("read", "clear", "read_events", "clear_events"),
            "storage": "memory_only",
            "astrbot_log_propagation": False,
        }
        series_diagnostics = plugin.diagnostic_events()
        assert series_diagnostics["contract"] == "series.diagnostics@1.0"
        assert series_diagnostics["status"] == "ready"
        assert series_diagnostics["reason"] == "READY"
        bridge_diagnostics = plugin.diagnostic_log.diagnostic_events()
        assert bridge_diagnostics["contract"] == ("embodiment_bridge.diagnostics@1.0")
        assert bridge_diagnostics["status"] == "memory_only"
        assert bridge_diagnostics["reason"] == "FILE_LOG_DISABLED"
        registered = {
            (route, tuple(methods)) for route, _, methods, _ in context.routes
        }
        assert len(registered) == 70
        assert (
            "/astrbot_plugin_embodiment_bridge/playback/receipt",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/diagnostics/report",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_quest_avatar_bridge/diagnostics/report",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_quest_avatar_bridge/playback/receipt",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/service-status",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/service-control",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/listener-port",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/events/<session_id>",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/interaction",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/action/result",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_quest_avatar_bridge/action/result",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/spatial/context",
            ("POST",),
        ) in registered
        assert not any(
            route == "/astrbot_plugin_quest_avatar_bridge/spatial/context"
            for route, _methods in registered
        )
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/create",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/exchange",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/operator-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/operator-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/fast-action-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/fast-action-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/stt-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/stt-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/platform-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/platform-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/persona-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/persona-settings",
            ("POST",),
        ) in registered
        for suffix, methods in {
            "persona-library": ("GET",),
            "persona-converter-settings": ("POST",),
            "persona-conversion-start": ("POST",),
            "persona-conversion-status": ("POST",),
            "persona-conversion-cancel": ("POST",),
            "persona-profile-open": ("POST",),
            "persona-profile-save": ("POST",),
            "persona-profile-activate": ("POST",),
            "persona-profile-delete": ("POST",),
        }.items():
            assert (
                f"/astrbot_plugin_embodiment_bridge/pairing/{suffix}",
                methods,
            ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/quest-identity-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/quest-identity-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/api-principal-proof",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/diagnostics",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/diagnostics-settings",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/diagnostics-settings",
            ("POST",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/identity-candidates",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_embodiment_bridge/pairing/identity-selection",
            ("POST",),
        ) in registered

        session = await plugin.sessions.start_session(
            SessionStartRequest(
                session_id="s1",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "api_key:test",
        )
        await plugin.terminate()
        assert session.closed is True
        assert (await plugin.sessions.stats())["active_sessions"] == 0
        await plugin.terminate()

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "register_websocket" not in source
        assert "@register" not in source

        pairing_source = (
            Path(module.__file__).parent / "transport" / "pairing.py"
        ).read_text(encoding="utf-8")
        assert "request.client_host" in pairing_source
        assert "request.remote_addr" not in pairing_source

    asyncio.run(scenario())


def test_component_construction_failure_does_not_leave_registered_routes(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    install_astrbot_stubs(monkeypatch, tmp_path)
    module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
    context = ContextStub()

    class FailingPairingHttpApi:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("pairing API construction failed")

    monkeypatch.setattr(module, "PairingHttpApi", FailingPairingHttpApi)
    with pytest.raises(RuntimeError, match="pairing API construction failed"):
        module.QuestAvatarBridgePlugin(context, {"bridge_api_key": "x" * 32})

    assert context.routes == []


def test_persona_api_schema_rejects_extra_or_secret_fields(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    install_astrbot_stubs(monkeypatch, tmp_path)
    pairing = importlib.import_module(
        "astrbot_plugin_embodiment_bridge.transport.pairing"
    )

    with pytest.raises(ValidationError):
        pairing.CharacterPersonaSettingsRequest.model_validate(
            {
                "character_name": "name",
                "character_self_reference": "I",
                "character_self_description": "description",
                "character_user_relationship": "friend",
                "bridge_api_key": "secret",
            }
        )

    with pytest.raises(ValidationError):
        pairing.CharacterPersonaSettingsRequest.model_validate(
            {
                "persona_source_mode": "astrbot",
                "astrbot_persona_id": "persona-a",
                "system_prompt": "client supplied persona",
            }
        )


def test_http_layer_requires_both_astrbot_and_bridge_auth(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_stub = install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
        plugin = module.QuestAvatarBridgePlugin(
            ContextStub(),
            {
                "bridge_api_key": "s" * 32,
                "chat_provider_id": "provider-1",
                "pairing_exchange_proxy_url": "http://public.example/pairing/exchange",
            },
        )
        await plugin.initialize()
        health = plugin.plugin_health()
        assert health["checks"]["pairing_bootstrap_ready"] is False
        assert "PAIRING_BOOTSTRAP_READY" in health["reasons"]

        request_stub.headers = {}
        denied = await plugin.transport.health()
        assert denied.status_code == 401

        request_stub.headers = {"x-quest-avatar-key": "s" * 32}
        request_stub.username = ""
        denied = await plugin.transport.health()
        assert denied.status_code == 401

        request_stub.username = "api_key:test"
        allowed = await plugin.transport.health()
        assert allowed.status_code == 200
        request_stub.headers = {"x-embodiment-bridge-key": "s" * 32}
        preferred = await plugin.transport.health()
        assert preferred.status_code == 200
        await plugin.terminate()

    asyncio.run(scenario())


def test_http_session_schema_and_owner_are_enforced(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_stub = install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
        plugin = module.QuestAvatarBridgePlugin(
            ContextStub(),
            {
                "bridge_api_key": "s" * 32,
                "chat_provider_id": "provider-1",
            },
        )
        request_stub.username = "api_key:owner-a"
        request_stub.headers = {
            "x-quest-avatar-key": "s" * 32,
            "content-type": "application/json",
        }
        request_stub.content_type = "application/json"
        request_stub.body_bytes = json.dumps(
            {
                "type": "session.start",
                "protocol_version": "1.0",
                "session_id": "s1",
                "client_id": "quest",
                "user_id": "user",
                "bot_id": "bot",
            }
        ).encode()
        created = await plugin.transport.session_start()
        assert created.status_code == 201

        request_stub.username = "api_key:owner-b"
        request_stub.body_bytes = json.dumps(
            {
                "type": "turn.start",
                "protocol_version": "1.0",
                "session_id": "s1",
                "turn_id": "t1",
            }
        ).encode()
        denied = await plugin.transport.turn_start()
        assert denied.status_code == 403

        request_stub.username = "api_key:owner-a"
        request_stub.body_bytes = json.dumps(
            {
                "type": "turn.start",
                "protocol_version": "1.0",
                "session_id": "s1",
                "turn_id": "t1",
                "unexpected": True,
            }
        ).encode()
        invalid = await plugin.transport.turn_start()
        assert invalid.status_code == 422
        await plugin.terminate()

    asyncio.run(scenario())


def test_plugin_listener_binds_only_during_initialize_and_terminate_releases_port(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")

        probe = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        port = int(probe.sockets[0].getsockname()[1])
        probe.close()
        await probe.wait_closed()

        context = ContextStub()
        plugin = module.QuestAvatarBridgePlugin(
            context,
            {
                "bridge_api_key": "x" * 32,
                "chat_provider_id": "provider-1",
                "pairing_listener_enabled": True,
                "pairing_listener_host": "127.0.0.1",
                "pairing_listener_port": port,
                "pairing_listener_upstream_url": "http://127.0.0.1:9",
                "pairing_listener_public_url": (
                    "https://pair.example.com"
                    "/api/v1/plugins/extensions/"
                    "astrbot_plugin_embodiment_bridge/pairing/exchange"
                ),
            },
        )
        assert plugin.pairing_listener.ready is False
        assert len(context.routes) == 70

        constructor_probe = await asyncio.start_server(
            lambda _r, _w: None,
            "127.0.0.1",
            port,
        )
        constructor_probe.close()
        await constructor_probe.wait_closed()

        await plugin.initialize()
        assert plugin.pairing_listener.ready is True
        assert plugin.pairing.bootstrap_ready is True
        await plugin.terminate()
        assert plugin.pairing_listener.ready is False

        rebound = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", port)
        rebound.close()
        await rebound.wait_closed()

    asyncio.run(scenario())


def test_webui_contract_declares_readonly_bounded_sse(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    install_astrbot_stubs(monkeypatch, tmp_path)
    module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
    plugin = object.__new__(module.EmbodimentBridgePlugin)

    assert plugin.series_module_contract()["standalone"]["available"] is True

    contract = plugin.webui_panels_contract()

    assert contract["name"] == "series.webui@2.0"
    assert contract["version"] == "2.0"
    assert contract["state_owner"] == "plugin"
    assert contract["managed"] == {"supported": True, "level": "read"}
    assert {"generic_table", "sse"} <= set(contract["capabilities"])
    assert contract["panels"] == [
        {
            "id": "service_status",
            "title": "临服务状态",
            "description": "只读查看配对监听、Bootstrap 与具身运行状态",
            "read_only": True,
        }
    ]
    assert module.WEBUI_SERVICE_STATUS_STREAM_INTERVAL_SECONDS == 1.0
    assert 5 <= module.WEBUI_SERVICE_STATUS_STREAM_MAX_EVENTS <= 10


def test_webui_service_status_stream_is_bounded_and_redacted(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    install_astrbot_stubs(monkeypatch, tmp_path)
    module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")

    class ListenerStub:
        def __init__(self) -> None:
            self.calls = 0

        def status_snapshot(self) -> dict[str, Any]:
            self.calls += 1
            return {
                "status": "running",
                "ready": True,
                "reason": "ready",
                "bind_host": "10.20.30.40",
                "port": 8520,
                "device_secret": "device-secret-must-not-leak",
            }

    listener = ListenerStub()
    plugin = object.__new__(module.EmbodimentBridgePlugin)
    plugin.pairing_listener = listener
    plugin.pairing = types.SimpleNamespace(
        bootstrap_ready=True,
        pairing_code="pairing-code-must-not-leak",
    )
    plugin.config = {
        "persona_mode": "astrbot",
        "bridge_api_key": "api-key-must-not-leak",
        "pairing_listener_tls_key_path": "/private/key.pem",
    }

    async def scenario() -> None:
        snapshot = plugin.webui_panel_data("service_status")
        assert snapshot["success"] is True
        assert snapshot["rows"] == [
            {"item": "配对监听", "value": "running"},
            {"item": "Bootstrap", "value": "就绪"},
            {"item": "人格模式", "value": "astrbot"},
        ]
        serialized_snapshot = json.dumps(snapshot, ensure_ascii=False)
        for secret in (
            "10.20.30.40",
            "device-secret-must-not-leak",
            "pairing-code-must-not-leak",
            "api-key-must-not-leak",
            "/private/key.pem",
        ):
            assert secret not in serialized_snapshot

        monkeypatch.setattr(
            module, "WEBUI_SERVICE_STATUS_STREAM_INTERVAL_SECONDS", 0.0
        )
        stream = plugin.webui_panel_stream("service_status")
        assert inspect.isasyncgen(stream)
        events = [event async for event in stream]

        assert len(events) == module.WEBUI_SERVICE_STATUS_STREAM_MAX_EVENTS == 5
        assert listener.calls == 6
        assert all(
            event["timestamp"]
            and event["success"] is True
            and event["status"] == "running"
            and event["bootstrap_ready"] is True
            and event["persona_mode"] == "astrbot"
            for event in events
        )
        serialized_events = json.dumps(events, ensure_ascii=False)
        for secret in (
            "10.20.30.40",
            "device-secret-must-not-leak",
            "pairing-code-must-not-leak",
            "api-key-must-not-leak",
            "/private/key.pem",
        ):
            assert secret not in serialized_events

        cancellable = plugin.webui_panel_stream("service_status")
        await anext(cancellable)
        await cancellable.aclose()

    asyncio.run(scenario())


def test_webui_panel_failures_use_stable_error_codes(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    install_astrbot_stubs(monkeypatch, tmp_path)
    module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")

    class BrokenListener:
        def status_snapshot(self) -> dict[str, Any]:
            raise OSError("private listener path /tmp/device.key")

    plugin = object.__new__(module.EmbodimentBridgePlugin)
    plugin.pairing_listener = BrokenListener()
    plugin.pairing = types.SimpleNamespace(bootstrap_ready=True)
    plugin.config = {"persona_mode": "astrbot"}

    assert plugin.webui_panel_data("unknown") == {
        "success": False,
        "error": "UNKNOWN_PANEL",
    }
    assert plugin.webui_panel_data("service_status") == {
        "success": False,
        "error": "SERVICE_STATUS_UNAVAILABLE",
    }

    async def scenario() -> None:
        unknown_stream = plugin.webui_panel_stream("unknown")
        assert inspect.isasyncgen(unknown_stream)
        unknown_event = await anext(unknown_stream)
        assert unknown_event["success"] is False
        assert unknown_event["status"] == "error"
        assert unknown_event["error"] == "UNKNOWN_PANEL"
        assert "/tmp/device.key" not in json.dumps(unknown_event)
        with pytest.raises(StopAsyncIteration):
            await anext(unknown_stream)

        failed_stream = plugin.webui_panel_stream("service_status")
        failed_event = await anext(failed_stream)
        assert failed_event["success"] is False
        assert failed_event["status"] == "error"
        assert failed_event["error"] == "SERVICE_STATUS_UNAVAILABLE"
        assert "/tmp/device.key" not in json.dumps(failed_event)
        with pytest.raises(StopAsyncIteration):
            await anext(failed_stream)

    asyncio.run(scenario())
