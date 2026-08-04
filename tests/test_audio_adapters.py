from __future__ import annotations

import asyncio
from pathlib import Path
import struct
from typing import Any
import wave

import pytest

from astrbot_plugin_quest_avatar_bridge.adapters.stt import (
    AdapterUnavailable,
    AstrBotSTTAdapter,
    ConfiguredMiMoSTTAdapter,
    select_stt_adapter,
)
from astrbot_plugin_quest_avatar_bridge.adapters.tts import AstrBotTTSAdapter


class ProviderContext:
    def __init__(self, *, stt: Any = None, tts: Any = None) -> None:
        self.stt = stt
        self.tts = tts

    def get_using_stt_provider(self) -> Any:
        return self.stt

    def get_using_tts_provider(self) -> Any:
        return self.tts


class InspectingSTTProvider:
    def __init__(self, expected_pcm: bytes) -> None:
        self.expected_pcm = expected_pcm
        self.path: Path | None = None

    async def get_text(self, audio_url: str) -> str:
        self.path = Path(audio_url)
        with wave.open(audio_url, "rb") as source:
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            assert source.getframerate() == 16_000
            assert source.readframes(source.getnframes()) == self.expected_pcm
        return "  recognized text  "


class BlockingSTTProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def get_text(self, audio_url: str) -> str:
        assert Path(audio_url).is_file()
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"


class FileTTSProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.inputs: list[str] = []

    async def get_audio(self, text: str) -> str:
        self.inputs.append(text)
        return str(self.path)


def write_wav(
    path: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
    frames: bytes,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def test_astrbot_stt_adapter_wraps_pcm16_and_cleans_temp_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        pcm16 = struct.pack("<hhhh", -1000, 0, 1000, 2000)
        provider = InspectingSTTProvider(pcm16)
        adapter = AstrBotSTTAdapter(
            ProviderContext(stt=provider),
            data_dir=tmp_path / "plugin_data" / "stt_input",
            enabled=True,
        )

        assert adapter.available is True
        assert await adapter.transcribe(pcm16, sample_rate=16_000) == (
            "recognized text"
        )
        assert provider.path is not None
        assert provider.path.parent == tmp_path / "plugin_data" / "stt_input"
        assert provider.path.exists() is False
        await adapter.close()
        assert adapter.available is False

    asyncio.run(scenario())


def test_astrbot_stt_adapter_unavailable_and_cancel_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        unavailable = AstrBotSTTAdapter(
            ProviderContext(),
            data_dir=tmp_path / "unavailable",
            enabled=True,
        )
        assert unavailable.available is False
        with pytest.raises(AdapterUnavailable):
            await unavailable.transcribe(b"\x00\x00", sample_rate=16_000)

        provider = BlockingSTTProvider()
        work_dir = tmp_path / "cancelled"
        adapter = AstrBotSTTAdapter(
            ProviderContext(stt=provider),
            data_dir=work_dir,
            enabled=True,
        )
        task = asyncio.create_task(adapter.transcribe(b"\x00\x00", sample_rate=16_000))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(work_dir.glob("quest-stt-*.wav")) == []
        await adapter.close()

    asyncio.run(scenario())


def test_plugin_mimo_stt_wraps_pcm_without_global_provider(monkeypatch) -> None:
    async def scenario() -> None:
        captured = {}

        async def fake_post(url, *, api_key, payload, timeout_seconds):
            captured.update(
                url=url,
                api_key=api_key,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
            return {"choices": [{"message": {"content": "  plugin transcript  "}}]}

        monkeypatch.setattr(
            "astrbot_plugin_quest_avatar_bridge.adapters.stt._post_mimo_json",
            fake_post,
        )
        adapter = ConfiguredMiMoSTTAdapter(
            enabled=True,
            api_base="https://api.xiaomimimo.com/v1",
            api_key="plugin-only-key",
            model="mimo-v2.5-asr",
            timeout_seconds=12,
        )
        pcm16 = struct.pack("<hhhh", -1000, 0, 1000, 2000)

        assert adapter.available is True
        assert await adapter.transcribe(pcm16, sample_rate=16_000) == (
            "plugin transcript"
        )
        assert captured["url"] == "https://api.xiaomimimo.com/v1/chat/completions"
        assert captured["api_key"] == "plugin-only-key"
        assert "max_completion_tokens" not in captured["payload"]
        audio_url = captured["payload"]["messages"][0]["content"][0]["input_audio"][
            "data"
        ]
        header = __import__("base64").b64decode(audio_url.split(",", 1)[1])[:12]
        assert header[:4] == b"RIFF" and header[8:12] == b"WAVE"
        await adapter.close()
        assert adapter.available is False

    asyncio.run(scenario())


def test_plugin_mimo_stt_requires_https_and_has_explicit_fallback() -> None:
    primary = ConfiguredMiMoSTTAdapter(
        enabled=True,
        api_base="http://api.example.test/v1",
        api_key="key",
    )
    fallback = ProviderContext(stt=InspectingSTTProvider(b"\x00\x00"))
    fallback_adapter = AstrBotSTTAdapter(
        fallback,
        data_dir=Path.cwd() / ".unused-stt-test",
        enabled=True,
    )
    try:
        assert primary.available is False
        assert select_stt_adapter(primary, fallback_adapter) is fallback_adapter
    finally:
        asyncio.run(fallback_adapter.close())
        try:
            (Path.cwd() / ".unused-stt-test").rmdir()
        except OSError:
            pass


def test_astrbot_tts_adapter_normalizes_stereo_48k_to_mono_24k(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source_path = tmp_path / "provider.wav"
        stereo_frames = b"".join(
            struct.pack("<hh", sample, -sample) for sample in range(-1000, 1000)
        )
        write_wav(
            source_path,
            sample_rate=48_000,
            channels=2,
            sample_width=2,
            frames=stereo_frames,
        )
        provider = FileTTSProvider(source_path)
        adapter = AstrBotTTSAdapter(
            ProviderContext(tts=provider),
            enabled=True,
            max_audio_seconds=2,
        )

        chunks = [chunk async for chunk in adapter.synthesize("hello", emotion="shy")]
        normalized = b"".join(chunks)
        assert provider.inputs == ["hello"]
        assert normalized
        assert len(normalized) % 2 == 0
        assert 1_990 <= len(normalized) <= 2_010
        assert set(normalized) == {0}
        assert source_path.is_file()
        await adapter.close()
        assert adapter.available is False

    asyncio.run(scenario())


def test_astrbot_tts_adapter_rejects_invalid_or_oversized_wav(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        invalid_path = tmp_path / "invalid.wav"
        invalid_path.write_bytes(b"not-a-wav")
        invalid = AstrBotTTSAdapter(
            ProviderContext(tts=FileTTSProvider(invalid_path)),
            enabled=True,
        )
        with pytest.raises(ValueError, match="invalid WAV"):
            _ = [
                chunk async for chunk in invalid.synthesize("hello", emotion="neutral")
            ]

        long_path = tmp_path / "too-long.wav"
        write_wav(
            long_path,
            sample_rate=24_000,
            channels=1,
            sample_width=2,
            frames=b"\x00\x00" * 48_000,
        )
        limited = AstrBotTTSAdapter(
            ProviderContext(tts=FileTTSProvider(long_path)),
            enabled=True,
            max_audio_seconds=1,
        )
        with pytest.raises(ValueError, match="duration limit"):
            _ = [
                chunk async for chunk in limited.synthesize("hello", emotion="neutral")
            ]

    asyncio.run(scenario())
