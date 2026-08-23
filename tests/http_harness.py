from __future__ import annotations

import asyncio
import contextvars
import hashlib
import importlib
import json
import sys
import types
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from aiohttp import web

from astrbot_plugin_embodiment_bridge.adapters.api_principal import (
    ApiPrincipalVerificationError,
)
from astrbot_plugin_embodiment_bridge.core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
)


API_MOUNT = "/api/v1/plugins/extensions"
ASTRBOT_API_TOKEN = "contract-plugin-token"
ASTRBOT_API_KEY_ID = "11111111-2222-3333-4444-555555555555"
BRIDGE_API_KEY = "contract-bridge-key-0000000000000000"
AUTH_HEADERS = {
    "Authorization": f"ApiKey {ASTRBOT_API_TOKEN}",
    "X-Quest-Avatar-Key": BRIDGE_API_KEY,
}


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


class ApiPrincipalVerifierStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve_digest(self, api_key: object) -> str:
        credential = str(api_key or "")
        self.calls.append(credential)
        if not credential:
            raise ApiPrincipalVerificationError(
                "pairing_astrbot_api_key_missing",
                422,
                "A valid Quest AstrBot API Key is required",
            )
        if credential != ASTRBOT_API_TOKEN:
            raise ValueError("unexpected test API key")
        principal = f"api_key:{ASTRBOT_API_KEY_ID}"
        return "sha256:" + hashlib.sha256(principal.encode("utf-8")).hexdigest()


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
        self.body = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


class StreamingResponseStub:
    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str = "text/event-stream",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.content_type = content_type


class BoundRequest:
    def __init__(self, value: web.Request) -> None:
        self.value = value
        authorization = value.headers.get("Authorization", "")
        if authorization == f"ApiKey {ASTRBOT_API_TOKEN}":
            self.username = f"api_key:{ASTRBOT_API_KEY_ID}"
        elif authorization == f"Bearer {ASTRBOT_API_TOKEN}":
            self.username = "dashboard-admin"
        else:
            self.username = ""

    @property
    def content_type(self) -> str:
        return self.value.content_type

    @property
    def headers(self) -> Any:
        return self.value.headers

    @property
    def client_host(self) -> str:
        return str(self.value.remote or "")

    async def body(self) -> bytes:
        return await self.value.read()


class RequestProxy:
    def __init__(self) -> None:
        self._current: contextvars.ContextVar[BoundRequest | None] = (
            contextvars.ContextVar("quest_avatar_http_request", default=None)
        )

    def bind(self, value: BoundRequest) -> contextvars.Token[BoundRequest | None]:
        return self._current.set(value)

    def reset(self, token: contextvars.Token[BoundRequest | None]) -> None:
        self._current.reset(token)

    def _bound(self) -> BoundRequest:
        value = self._current.get()
        if value is None:
            raise RuntimeError("no HTTP request is bound to this task")
        return value

    @property
    def username(self) -> str:
        return self._bound().username

    @property
    def content_type(self) -> str:
        return self._bound().content_type

    @property
    def headers(self) -> Any:
        return self._bound().headers

    @property
    def client_host(self) -> str:
        return self._bound().client_host

    async def body(self) -> bytes:
        return await self._bound().body()


class ContextStub:
    def __init__(self) -> None:
        self.routes: list[tuple[str, Any, list[str], str]] = []
        self.providers = [ChatProviderStub()]
        self.stt_providers = [SpeechToTextProviderStub()]
        self.persona_manager = PersonaManagerStub()
        self.stars: list[Any] = []
        self.contract_platform = types.SimpleNamespace(
            meta=lambda: types.SimpleNamespace(
                id="contract-platform",
                name="aiocqhttp",
                adapter_display_name="OneBot 11",
            ),
            create_event=lambda message: message,
        )
        self.platform_manager = types.SimpleNamespace(
            get_insts=lambda: [self.contract_platform]
        )

    def register_web_api(
        self,
        route: str,
        handler: Any,
        methods: list[str],
        description: str,
    ) -> None:
        self.routes.append((route, handler, methods, description))

    def get_all_stars(self) -> list[Any]:
        return self.stars

    def get_all_providers(self) -> list[Any]:
        return self.providers

    def get_all_stt_providers(self) -> list[Any]:
        return self.stt_providers

    def get_using_stt_provider(self, umo: Any = None) -> Any | None:
        del umo
        return self.stt_providers[0] if self.stt_providers else None

    def get_platform_inst(self, platform_id: str) -> Any | None:
        if platform_id != "contract-platform":
            return None
        return self.contract_platform

    def get_event_queue(self) -> asyncio.Queue[Any]:
        return asyncio.Queue()


class ChatProviderStub:
    def meta(self) -> Any:
        return types.SimpleNamespace(
            id="fake-provider",
            model="contract-model",
            type="openai",
            provider_type="chat_completion",
        )


class SpeechToTextProviderStub:
    def meta(self) -> Any:
        return types.SimpleNamespace(
            id="fake-stt-provider",
            model="contract-stt-model",
            type="contract-stt-adapter",
            provider_type="speech_to_text",
        )

    async def get_text(self, audio_url: str) -> str:
        del audio_url
        return "contract transcription"


class PersonaManagerStub:
    def __init__(self) -> None:
        self.personas = {
            "quest-persona": types.SimpleNamespace(
                persona_id="quest-persona",
                system_prompt="private contract persona prompt",
                begin_dialogs=["private dialog"],
                tools=["private tool"],
            )
        }

    async def get_persona(self, persona_id: str) -> Any:
        if persona_id not in self.personas:
            raise ValueError("missing persona")
        return self.personas[persona_id]

    async def get_default_persona_v3(self, umo: Any = None) -> dict[str, str]:
        assert umo is None
        return {"name": "default", "prompt": "private default persona prompt"}

    async def get_all_personas(self) -> list[Any]:
        return list(self.personas.values())


class NativeConfigStub(dict[str, Any]):
    async def save_config_async(self, changes: dict[str, Any]) -> bool:
        self.update(changes)
        return True


class FakeLLMAdapter:
    def __init__(self) -> None:
        self.late_started = asyncio.Event()
        self.late_cancelled = asyncio.Event()
        self.late_release = asyncio.Event()
        self.closed = False

    @property
    def available(self) -> bool:
        return True

    async def generate(self, *, user_text: str, **kwargs: Any) -> ModelDecision:
        interaction = kwargs.get("interaction")
        del kwargs
        if user_text == "hold-old-turn":
            self.late_started.set()
            try:
                await self.late_release.wait()
            except asyncio.CancelledError:
                self.late_cancelled.set()
                await self.late_release.wait()
        if interaction is None:
            return ModelDecision(
                should_reply=True,
                reply_text="请轻一点。",
                intent=ProposedIntent(
                    emotion=Emotion.NEUTRAL,
                    gesture=Gesture.TALK,
                    look_at=LookAt.USER,
                    intensity=0.38,
                    duration_ms=1_200,
                    reason_code="dialogue_only",
                ),
            )
        return ModelDecision(
            should_reply=True,
            reply_text="请轻一点。",
            intent=ProposedIntent(
                emotion=Emotion.SHY,
                gesture=Gesture.STEP_BACK,
                look_at=LookAt.AWAY,
                intensity=0.65,
                duration_ms=1_800,
                reason_code="boundary_soft_refusal",
            ),
        )

    async def close(self) -> None:
        self.closed = True


class FakeSTTAdapter:
    def __init__(self) -> None:
        self.fail_next = False
        self.expected_pcm16 = b"\x00\x00\x01\x00"
        self.closed = False

    @property
    def available(self) -> bool:
        return True

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        assert pcm16 == self.expected_pcm16
        assert sample_rate == 16_000
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("contract STT failure")
        return "你好"

    async def close(self) -> None:
        self.closed = True


class FakeTTSAdapter:
    def __init__(self) -> None:
        self.block_next = False
        self.fail_next = False
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    @property
    def available(self) -> bool:
        return True

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        assert text == "请轻一点。"
        assert emotion in {"neutral", "shy"}
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("contract TTS failure")
        if self.block_next:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        yield b"\x00\x00\x01\x00"

    async def close(self) -> None:
        self.release.set()
        self.closed = True


@dataclass(slots=True)
class HarnessBundle:
    plugin: Any
    context: ContextStub
    request_proxy: RequestProxy
    llm: FakeLLMAdapter
    stt: FakeSTTAdapter
    tts: FakeTTSAdapter


def build_plugin(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    config_overrides: dict[str, Any] | None = None,
) -> HarnessBundle:
    request_proxy = RequestProxy()
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = LoggerStub()
    event = types.ModuleType("astrbot.api.event")
    event.filter = types.SimpleNamespace(
        on_llm_request=lambda **_kwargs: lambda handler: handler
    )
    api.event = event

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

    web_module = types.ModuleType("astrbot.api.web")
    web_module.request = request_proxy
    web_module.json_response = lambda data=None, status_code=200, headers=None: (
        ResponseStub(data or {}, status_code=status_code, headers=headers)
    )
    web_module.error_response = (
        lambda message, status_code=400, data=None, headers=None: ResponseStub(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )
    )
    web_module.stream_response = (
        lambda content, status_code=200, headers=None, content_type="text/event-stream", **kwargs: (
            StreamingResponseStub(
                content,
                status_code=status_code,
                headers=headers,
                content_type=content_type,
            )
        )
    )

    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    monkeypatch.setitem(sys.modules, "astrbot", astrbot)
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.star", star)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web_module)
    for module_name in (
        "astrbot_plugin_embodiment_bridge.adapters.astrbot_llm",
        "astrbot_plugin_embodiment_bridge.main",
        "astrbot_plugin_embodiment_bridge.transport.http_sse",
        "astrbot_plugin_embodiment_bridge.transport.pairing",
    ):
        sys.modules.pop(module_name, None)

    main_module = importlib.import_module("astrbot_plugin_embodiment_bridge.main")
    context = ContextStub()
    config: NativeConfigStub = NativeConfigStub(
        {
            "bridge_api_key": BRIDGE_API_KEY,
            "chat_provider_id": "fake-provider",
            # The legacy HTTP harness exercises the compatibility provider;
            # production defaults to the strict AstrBot EventBus path.
            "allow_direct_provider_fallback": True,
            "gesture_cooldown_ms": 0,
            "pairing_exchange_proxy_url": (
                "https://pair.example.com/quest/pairing/exchange"
            ),
            "pairing_public_url": "https://bot.example.com",
            "pairing_astrbot_api_key": "quick-pair-plugin-scope-key",
            "pairing_user_id": "user-test",
            "pairing_bot_id": "bot-test",
        }
    )
    config.update(config_overrides or {})
    plugin = main_module.QuestAvatarBridgePlugin(context, config)
    plugin.pairing_api.api_principal_verifier = ApiPrincipalVerifierStub()
    llm = FakeLLMAdapter()
    stt = FakeSTTAdapter()
    tts = FakeTTSAdapter()
    plugin.llm = llm
    plugin.stt = stt
    plugin.tts = tts
    plugin.orchestrator.llm = llm
    plugin.orchestrator.stt = stt
    plugin.orchestrator.tts = tts
    return HarnessBundle(plugin, context, request_proxy, llm, stt, tts)


class LiveHttpServer:
    def __init__(self, bundle: HarnessBundle) -> None:
        self.bundle = bundle
        self.runner: web.AppRunner | None = None
        self.base_url = ""

    async def __aenter__(self) -> LiveHttpServer:
        app = web.Application()
        for route, handler, methods, _description in self.bundle.context.routes:
            path = API_MOUNT + route.replace("<session_id>", "{session_id}")
            for method in methods:
                app.router.add_route(method, path, self._endpoint(handler))
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None and server.sockets
        port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}{API_MOUNT}"
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
        await self.bundle.plugin.terminate()

    def url(self, path: str) -> str:
        return f"{self.base_url}/astrbot_plugin_embodiment_bridge{path}"

    def _endpoint(self, handler: Any) -> Any:
        async def endpoint(value: web.Request) -> web.StreamResponse:
            token = self.bundle.request_proxy.bind(BoundRequest(value))
            try:
                result = await handler(**dict(value.match_info))
                if isinstance(result, StreamingResponseStub):
                    return await self._stream(value, result)
                headers = dict(result.headers)
                headers.setdefault("Content-Type", "application/json; charset=utf-8")
                return web.Response(
                    body=result.body,
                    status=result.status_code,
                    headers=headers,
                )
            finally:
                self.bundle.request_proxy.reset(token)

        return endpoint

    async def _stream(
        self,
        request: web.Request,
        result: StreamingResponseStub,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=result.status_code,
            headers=result.headers,
        )
        response.content_type = result.content_type
        await response.prepare(request)
        iterator = result.content.__aiter__()
        next_item: asyncio.Task[str] | None = None
        try:
            while True:
                if next_item is None:
                    next_item = asyncio.create_task(anext(iterator))
                done, _pending = await asyncio.wait({next_item}, timeout=0.02)
                if not done:
                    transport = request.transport
                    if transport is None or transport.is_closing():
                        next_item.cancel()
                        await asyncio.gather(next_item, return_exceptions=True)
                        break
                    continue
                try:
                    chunk = next_item.result()
                except StopAsyncIteration:
                    break
                next_item = None
                await response.write(chunk.encode("utf-8"))
        except ConnectionResetError:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            if next_item is not None and not next_item.done():
                next_item.cancel()
                await asyncio.gather(next_item, return_exceptions=True)
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except (RuntimeError, asyncio.CancelledError):
                    pass
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response


@dataclass(frozen=True, slots=True)
class SseFrame:
    raw: str
    event: str | None
    data: dict[str, Any] | None
    comment: str | None


async def read_sse_frame(response: Any, *, timeout: float = 1.0) -> SseFrame:
    deadline = monotonic() + timeout
    lines: list[str] = []
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for SSE frame")
        raw_line = await asyncio.wait_for(response.content.readline(), remaining)
        if not raw_line:
            raise EOFError("SSE stream ended")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line:
            lines.append(line)
            continue
        if not lines:
            continue
        raw = "\n".join(lines) + "\n\n"
        comment = next(
            (line[1:].strip() for line in lines if line.startswith(":")), None
        )
        event = next(
            (
                line.removeprefix("event:").strip()
                for line in lines
                if line.startswith("event:")
            ),
            None,
        )
        data_line = next(
            (
                line.removeprefix("data:").strip()
                for line in lines
                if line.startswith("data:")
            ),
            None,
        )
        data = json.loads(data_line) if data_line is not None else None
        return SseFrame(raw=raw, event=event, data=data, comment=comment)
