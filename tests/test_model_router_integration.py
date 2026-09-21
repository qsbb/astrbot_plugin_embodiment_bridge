from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import wave

import pytest

from astrbot_plugin_embodiment_bridge.adapters.astrbot_llm import AstrBotLLMAdapter
from astrbot_plugin_embodiment_bridge.adapters.stt import (
    AdapterUnavailable,
    AstrBotSTTAdapter,
)
from astrbot_plugin_embodiment_bridge.adapters.tts import AstrBotTTSAdapter


CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.1",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}


def route(kind: str, **overrides: Any) -> dict[str, Any]:
    value = {
        "kind": kind,
        "source": "core",
        "available": True,
        "provider_id": f"core-{kind}",
        "model": "",
        "voice": "",
    }
    value.update(overrides)
    return value


class Router:
    def __init__(
        self,
        routes: dict[str, dict[str, Any]] | None = None,
        *,
        contract: Any = CONTRACT,
        error: Exception | None = None,
    ) -> None:
        self.routes = routes or {}
        self.contract = contract
        self.error = error
        self.calls: list[str] = []

    def series_model_router_contract(self) -> Any:
        return self.contract

    def resolve_model_route(self, kind: str, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.calls.append(kind)
        if self.error is not None:
            raise self.error
        return dict(self.routes.get(kind, {}))


class Context:
    def __init__(
        self,
        *,
        router: Router | None = None,
        providers: dict[str, Any] | None = None,
        stt_providers: list[Any] | None = None,
        default_stt: Any = None,
        default_tts: Any = None,
    ) -> None:
        self.router = router
        self.providers = providers or {}
        self.stt_providers = list(stt_providers or ())
        self.default_stt = default_stt
        self.default_tts = default_tts

    def get_star_instance(self, plugin_name: str) -> Any:
        if plugin_name != "astrbot_plugin_update_manager":
            return None
        return self.router

    def get_provider_by_id(self, provider_id: str) -> Any:
        return self.providers.get(provider_id)

    def get_all_stt_providers(self) -> list[Any]:
        return list(self.stt_providers)

    def get_using_stt_provider(self) -> Any:
        return self.default_stt

    def get_using_tts_provider(self) -> Any:
        return self.default_tts


class LLMContext(Context):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.llm_calls: list[dict[str, Any]] = []

    async def llm_generate(self, **kwargs: Any) -> Any:
        self.llm_calls.append(dict(kwargs))
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "should_reply": False,
                    "reply_text": "",
                    "intent": {
                        "emotion": "neutral",
                        "gesture": "idle",
                        "look_at": "none",
                        "intensity": 0,
                        "duration_ms": 0,
                        "reason_code": "no_reply",
                    },
                }
            )
        )


class LegacyLLMContext(LLMContext):
    async def llm_generate(
        self,
        *,
        chat_provider_id: str,
        prompt: str,
        system_prompt: str,
    ) -> Any:
        del prompt, system_prompt
        self.llm_calls.append({"chat_provider_id": chat_provider_id})
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "should_reply": False,
                    "reply_text": "",
                    "intent": {
                        "emotion": "neutral",
                        "gesture": "idle",
                        "look_at": "none",
                        "intensity": 0,
                        "duration_ms": 0,
                        "reason_code": "no_reply",
                    },
                }
            )
        )


class STTProvider:
    def __init__(self, provider_id: str, text: str) -> None:
        self.provider_id = provider_id
        self.text = text
        self.calls = 0

    def meta(self) -> Any:
        return SimpleNamespace(
            id=self.provider_id,
            model=self.provider_id,
            type="test",
            provider_type="speech_to_text",
        )

    async def get_text(self, audio_url: str) -> str:
        assert Path(audio_url).is_file()
        self.calls += 1
        return self.text


class TTSProvider:
    def __init__(self, provider_id: str, audio_path: Path) -> None:
        self.provider_id = provider_id
        self.audio_path = audio_path
        self.calls: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        return str(self.audio_path)


def write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * 1_200)


def run_dialogue(adapter: AstrBotLLMAdapter) -> None:
    asyncio.run(
        adapter.generate(
            user_text="hello",
            history=[],
            interaction=None,
            relationship=None,
        )
    )


def test_conversation_uses_core_provider_and_model() -> None:
    context = LLMContext(
        router=Router(
            {
                "conversation": route(
                    "conversation",
                    provider_id="core-chat",
                    model="chat-model",
                )
            }
        ),
        providers={"core-chat": object()},
    )
    adapter = AstrBotLLMAdapter(context, chat_provider_id="", persona_prompt="")

    assert adapter.available is True
    run_dialogue(adapter)

    assert context.llm_calls[0]["chat_provider_id"] == "core-chat"
    assert context.llm_calls[0]["model"] == "chat-model"


def test_conversation_local_provider_precedes_core_and_omits_model() -> None:
    local = object()
    router = Router(
        {
            "conversation": route(
                "conversation",
                provider_id="core-chat",
                model="chat-model",
            )
        }
    )
    context = LLMContext(
        router=router,
        providers={"local-chat": local, "core-chat": local},
    )
    adapter = AstrBotLLMAdapter(
        context,
        chat_provider_id="local-chat",
        persona_prompt="",
    )

    run_dialogue(adapter)

    assert context.llm_calls[0]["chat_provider_id"] == "local-chat"
    assert "model" not in context.llm_calls[0]
    assert router.calls == []


def test_conversation_model_type_error_retries_without_override() -> None:
    context = LegacyLLMContext(
        router=Router(
            {
                "conversation": route(
                    "conversation",
                    provider_id="core-chat",
                    model="chat-model",
                )
            }
        ),
        providers={"core-chat": object()},
    )
    adapter = AstrBotLLMAdapter(context, chat_provider_id="", persona_prompt="")

    run_dialogue(adapter)

    assert context.llm_calls == [{"chat_provider_id": "core-chat"}]


def test_conversation_core_failure_preserves_existing_unconfigured_behavior() -> None:
    context = LLMContext(
        router=Router(error=RuntimeError("core failed")),
        providers={},
    )
    adapter = AstrBotLLMAdapter(context, chat_provider_id="", persona_prompt="")

    assert adapter.available is False
    with pytest.raises(RuntimeError, match="chat_provider_id is not configured"):
        run_dialogue(adapter)


def test_stt_uses_core_provider_and_sync_status(tmp_path: Path) -> None:
    core = STTProvider("core-stt", "core text")
    native = STTProvider("native-stt", "native text")
    context = Context(
        router=Router(
            {"stt": route("stt", provider_id="core-stt")}
        ),
        providers={"core-stt": core},
        stt_providers=[core, native],
        default_stt=native,
    )
    adapter = AstrBotSTTAdapter(context, data_dir=tmp_path, provider_id="")

    assert adapter.available is True
    assert asyncio.run(adapter.transcribe(b"\x00\x00", sample_rate=16_000)) == (
        "core text"
    )
    assert core.calls == 1
    assert native.calls == 0


def test_stt_local_provider_precedes_core(tmp_path: Path) -> None:
    local = STTProvider("local-stt", "local text")
    core = STTProvider("core-stt", "core text")
    router = Router({"stt": route("stt", provider_id="core-stt")})
    context = Context(
        router=router,
        providers={"local-stt": local, "core-stt": core},
        stt_providers=[local, core],
    )
    adapter = AstrBotSTTAdapter(
        context,
        data_dir=tmp_path,
        provider_id="local-stt",
    )

    assert asyncio.run(adapter.transcribe(b"\x00\x00", sample_rate=16_000)) == (
        "local text"
    )
    assert local.calls == 1
    assert core.calls == 0
    assert router.calls == []


def test_stt_core_failure_falls_back_to_legacy_default(tmp_path: Path) -> None:
    native = STTProvider("native-stt", "native text")
    context = Context(
        router=Router(error=RuntimeError("core failed")),
        providers={},
        default_stt=native,
    )
    adapter = AstrBotSTTAdapter(
        context,
        data_dir=tmp_path,
        provider_id="",
        legacy_default_enabled=True,
    )

    assert asyncio.run(adapter.transcribe(b"\x00\x00", sample_rate=16_000)) == (
        "native text"
    )
    assert native.calls == 1


def test_stt_missing_core_and_native_remains_unavailable(tmp_path: Path) -> None:
    adapter = AstrBotSTTAdapter(
        Context(router=None),
        data_dir=tmp_path,
        provider_id="",
    )
    assert adapter.available is False
    with pytest.raises(AdapterUnavailable):
        asyncio.run(adapter.transcribe(b"\x00\x00", sample_rate=16_000))


def test_tts_uses_core_provider_before_native(tmp_path: Path) -> None:
    path = tmp_path / "core.wav"
    write_wav(path)
    core = TTSProvider("core-tts", path)
    native = TTSProvider("native-tts", path)
    context = Context(
        router=Router({"tts": route("tts", provider_id="core-tts", voice="ignored")}),
        providers={"core-tts": core},
        default_tts=native,
    )
    adapter = AstrBotTTSAdapter(context, enabled=True)

    assert adapter.available is True
    chunks = asyncio.run(_collect_tts(adapter, "hello"))
    assert chunks
    assert core.calls == ["hello"]
    assert native.calls == []


def test_tts_core_failure_falls_back_to_native(tmp_path: Path) -> None:
    path = tmp_path / "native.wav"
    write_wav(path)
    native = TTSProvider("native-tts", path)
    context = Context(
        router=Router(error=RuntimeError("core failed")),
        providers={},
        default_tts=native,
    )
    adapter = AstrBotTTSAdapter(context, enabled=True)

    chunks = asyncio.run(_collect_tts(adapter, "hello"))
    assert chunks
    assert native.calls == ["hello"]


async def _collect_tts(adapter: AstrBotTTSAdapter, text: str) -> list[bytes]:
    return [chunk async for chunk in adapter.synthesize(text, emotion="neutral")]
