from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
import struct
from types import SimpleNamespace
from typing import Any
import wave

import pytest

from astrbot_plugin_embodiment_bridge.adapters.stt import (
    AdapterUnavailable,
    AstrBotSTTAdapter,
)
from astrbot_plugin_embodiment_bridge.adapters.tts import AstrBotTTSAdapter


class ProviderContext:
    def __init__(
        self,
        *,
        stt: Any = None,
        stt_providers: list[Any] | None = None,
        tts: Any = None,
    ) -> None:
        self.stt = stt
        self.stt_providers = list(stt_providers or ([] if stt is None else [stt]))
        self.tts = tts

    def get_using_stt_provider(self) -> Any:
        return self.stt

    def get_all_stt_providers(self) -> list[Any]:
        return list(self.stt_providers)

    def get_using_tts_provider(self) -> Any:
        return self.tts


class ProviderType(str, Enum):
    SPEECH_TO_TEXT = "speech_to_text"


class InspectingSTTProvider:
    def __init__(
        self,
        expected_pcm: bytes,
        *,
        provider_id: str = "stt-a",
        model: str = "speech-model",
        adapter_type: str = "official-stt",
    ) -> None:
        self.expected_pcm = expected_pcm
        self.path: Path | None = None
        self.provider_config = {
            "api_key": "must-never-be-enumerated",
            "api_base": "https://provider.invalid/v1",
        }
        self._meta = SimpleNamespace(
            id=provider_id,
            model=model,
            type=adapter_type,
            provider_type=ProviderType.SPEECH_TO_TEXT,
        )

    def meta(self) -> Any:
        return self._meta

    async def get_text(self, audio_url: str) -> str:
        self.path = Path(audio_url)
        with wave.open(audio_url, "rb") as source:
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            assert source.getframerate() == 16_000
            assert source.readframes(source.getnframes()) == self.expected_pcm
        return "  recognized text  "


class StreamingSTTProvider(InspectingSTTProvider):
    async def transcribe_stream(self, chunks: Any, *, sample_rate: int) -> Any:
        assert sample_rate == 16_000
        received = bytearray()
        async for chunk in chunks:
            received.extend(chunk)
            yield {"kind": "partial", "text": "你"}
        assert bytes(received) == b"\x01\x02\x03\x04"
        yield {"kind": "final", "text": "你好吗"}


class BlockingSTTProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def get_text(self, audio_url: str) -> str:
        assert Path(audio_url).is_file()
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"

    def meta(self) -> Any:
        return SimpleNamespace(
            id="blocking-stt",
            model="blocking",
            type="test",
            provider_type=ProviderType.SPEECH_TO_TEXT,
        )


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
            ProviderContext(stt_providers=[provider]),
            data_dir=tmp_path / "plugin_data" / "stt_input",
            provider_id="stt-a",
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


def test_streaming_stt_is_explicit_and_separates_partial_from_final(
    tmp_path: Path,
) -> None:
    async def chunks() -> Any:
        yield b"\x01\x02"
        yield b"\x03\x04"

    async def scenario() -> None:
        provider = StreamingSTTProvider(b"", provider_id="streaming")
        adapter = AstrBotSTTAdapter(
            ProviderContext(stt_providers=[provider]),
            data_dir=tmp_path / "streaming",
            provider_id="streaming",
        )
        assert adapter.streaming_available is True
        results = [item async for item in adapter.transcribe_stream(chunks(), sample_rate=16_000)]
        assert results == [
            {"kind": "partial", "text": "你"},
            {"kind": "partial", "text": "你"},
            {"kind": "final", "text": "你好吗"},
        ]
        await adapter.close()

    asyncio.run(scenario())


def test_astrbot_stt_adapter_unavailable_and_cancel_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        unavailable = AstrBotSTTAdapter(
            ProviderContext(),
            data_dir=tmp_path / "unavailable",
            provider_id="missing-stt",
        )
        assert unavailable.available is False
        assert unavailable.status_reason == "selected_missing"
        with pytest.raises(AdapterUnavailable):
            await unavailable.transcribe(b"\x00\x00", sample_rate=16_000)

        provider = BlockingSTTProvider()
        work_dir = tmp_path / "cancelled"
        adapter = AstrBotSTTAdapter(
            ProviderContext(stt_providers=[provider]),
            data_dir=work_dir,
            provider_id="blocking-stt",
        )
        task = asyncio.create_task(adapter.transcribe(b"\x00\x00", sample_rate=16_000))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(work_dir.glob("quest-stt-*.wav")) == []
        await adapter.close()

    asyncio.run(scenario())


def test_stt_provider_catalog_is_safe_and_supports_formal_third_party_provider(
    tmp_path: Path,
) -> None:
    official = InspectingSTTProvider(
        b"\x00\x00", provider_id="official", model="official-model"
    )
    third_party = InspectingSTTProvider(
        b"\x00\x00",
        provider_id="third-party",
        model="plugin-registered-model",
        adapter_type="third-party-adapter",
    )
    context = ProviderContext(stt_providers=[third_party, official])
    adapter = AstrBotSTTAdapter(
        context,
        data_dir=tmp_path / "catalog",
        provider_id="third-party",
    )

    catalog = adapter.provider_catalog()

    assert catalog == [
        {
            "id": "official",
            "model": "official-model",
            "adapter_type": "official-stt",
            "provider_type": "speech_to_text",
        },
        {
            "id": "third-party",
            "model": "plugin-registered-model",
            "adapter_type": "third-party-adapter",
            "provider_type": "speech_to_text",
        },
    ]
    assert adapter.available is True
    assert "api_key" not in repr(catalog)
    assert "api_base" not in repr(catalog)


def test_selected_missing_never_falls_back_and_legacy_default_is_explicit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = InspectingSTTProvider(b"\x00\x00", provider_id="legacy-default")
        context = ProviderContext(stt=provider, stt_providers=[provider])
        selected_missing = AstrBotSTTAdapter(
            context,
            data_dir=tmp_path / "selected-missing",
            provider_id="removed-provider",
            legacy_default_enabled=True,
        )
        assert selected_missing.available is False
        assert selected_missing.status_reason == "selected_missing"
        with pytest.raises(AdapterUnavailable):
            await selected_missing.transcribe(b"\x00\x00", sample_rate=16_000)

        legacy = AstrBotSTTAdapter(
            context,
            data_dir=tmp_path / "legacy-default",
            legacy_default_enabled=True,
        )
        assert legacy.status_reason == "legacy_default_ready"
        assert await legacy.transcribe(b"\x00\x00", sample_rate=16_000) == (
            "recognized text"
        )

    asyncio.run(scenario())


def test_legacy_private_mimo_is_fail_closed_without_network_path(
    tmp_path: Path,
) -> None:
    adapter = AstrBotSTTAdapter(
        ProviderContext(),
        data_dir=tmp_path / "legacy-private",
        legacy_private_mimo_enabled=True,
    )

    assert adapter.available is False
    assert adapter.status_reason == "legacy_private_mimo_disabled"
    assert adapter.status_snapshot()["external_contract_status"] == (
        "no_standard_contract"
    )
    with pytest.raises(AdapterUnavailable):
        asyncio.run(adapter.transcribe(b"\x00\x00", sample_rate=16_000))


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
