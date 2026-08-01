from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

from astrbot_plugin_quest_avatar_bridge.core.models import SessionStartRequest


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
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    sys.modules.pop("astrbot_plugin_quest_avatar_bridge.transport.http_sse", None)
    sys.modules.pop("astrbot_plugin_quest_avatar_bridge.main", None)
    return request_stub


def test_plugin_registers_only_public_http_sse_routes_and_terminates(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_quest_avatar_bridge.main")
        context = ContextStub()
        plugin = module.QuestAvatarBridgePlugin(
            context,
            {
                "bridge_api_key": "x" * 32,
                "chat_provider_id": "provider-1",
            },
        )
        registered = {
            (route, tuple(methods)) for route, _, methods, _ in context.routes
        }
        assert len(registered) == 9
        assert (
            "/astrbot_plugin_quest_avatar_bridge/events/<session_id>",
            ("GET",),
        ) in registered
        assert (
            "/astrbot_plugin_quest_avatar_bridge/interaction",
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

    asyncio.run(scenario())


def test_http_layer_requires_both_astrbot_and_bridge_auth(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_stub = install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_quest_avatar_bridge.main")
        plugin = module.QuestAvatarBridgePlugin(
            ContextStub(),
            {
                "bridge_api_key": "s" * 32,
                "chat_provider_id": "provider-1",
            },
        )

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
        await plugin.terminate()

    asyncio.run(scenario())


def test_http_session_schema_and_owner_are_enforced(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_stub = install_astrbot_stubs(monkeypatch, tmp_path)
        module = importlib.import_module("astrbot_plugin_quest_avatar_bridge.main")
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
