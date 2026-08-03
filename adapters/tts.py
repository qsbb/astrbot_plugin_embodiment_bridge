from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from typing import Protocol
import warnings
import wave

from .stt import AdapterUnavailable


# AstrBot 4.26.8 uses audioop-lts on Python 3.13+; 3.12 emits a removal notice.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated",
        category=DeprecationWarning,
    )
    import audioop


OUTPUT_SAMPLE_RATE = 24_000
OUTPUT_CHANNELS = 1
OUTPUT_FORMAT = "pcm16"


class TTSAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class DisabledTTSAdapter:
    @property
    def available(self) -> bool:
        return False

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        del text, emotion
        return
        yield b""  # pragma: no cover

    async def close(self) -> None:
        return None


class AstrBotTTSAdapter:
    """Normalize AstrBot's selected file-based TTS provider to Quest PCM16."""

    def __init__(
        self,
        context: Any,
        *,
        enabled: bool,
        timeout_seconds: float = 60.0,
        max_audio_seconds: int = 120,
    ) -> None:
        self.context = context
        self.enabled = enabled
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.max_output_bytes = (
            OUTPUT_SAMPLE_RATE * OUTPUT_CHANNELS * 2 * max(1, max_audio_seconds)
        )
        self._closed = False

    @property
    def available(self) -> bool:
        return not self._closed and self.enabled and self._provider() is not None

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        del emotion
        if self._closed or not self.enabled:
            raise AdapterUnavailable("AstrBot TTS adapter is disabled")
        provider = self._provider()
        if provider is None:
            raise AdapterUnavailable("AstrBot TTS provider is not configured")
        if not text.strip():
            return

        try:
            async with asyncio.timeout(self.timeout_seconds):
                audio_path = await provider.get_audio(text)
        except TimeoutError as exc:
            raise RuntimeError("AstrBot TTS provider timed out") from exc
        if not isinstance(audio_path, str) or not audio_path.strip():
            raise TypeError("AstrBot TTS provider returned an invalid audio path")

        pcm16 = await asyncio.to_thread(
            _read_and_normalize_wav,
            Path(audio_path),
            self.max_output_bytes,
        )
        for offset in range(0, len(pcm16), 64 * 1024):
            yield pcm16[offset : offset + 64 * 1024]

    async def close(self) -> None:
        self._closed = True

    def _provider(self) -> Any | None:
        if not self.enabled:
            return None
        try:
            return self.context.get_using_tts_provider()
        except (RuntimeError, ValueError):
            return None


def _read_and_normalize_wav(path: Path, max_output_bytes: int) -> bytes:
    if not path.is_file():
        raise ValueError("AstrBot TTS provider audio path does not exist")
    max_source_bytes = max_output_bytes * 16 + 65_536
    if path.stat().st_size > max_source_bytes:
        raise ValueError("AstrBot TTS provider audio file is too large")

    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            if source.getcomptype() != "NONE":
                raise ValueError("AstrBot TTS WAV must be uncompressed PCM")
            if channels not in {1, 2}:
                raise ValueError("AstrBot TTS WAV must be mono or stereo")
            if sample_width != 2:
                raise ValueError("AstrBot TTS WAV must use PCM16 samples")
            if sample_rate < 8_000 or sample_rate > 192_000:
                raise ValueError("AstrBot TTS WAV sample rate is unsupported")
            projected_bytes = (
                frame_count * OUTPUT_SAMPLE_RATE * 2 // max(1, sample_rate)
            )
            if projected_bytes > max_output_bytes:
                raise ValueError("AstrBot TTS output exceeds the duration limit")
            raw = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise ValueError("AstrBot TTS provider returned an invalid WAV file") from exc

    expected_bytes = frame_count * channels * sample_width
    if not raw or len(raw) != expected_bytes:
        raise ValueError("AstrBot TTS WAV is empty or truncated")
    if channels == 2:
        raw = audioop.tomono(raw, sample_width, 0.5, 0.5)
    if sample_rate != OUTPUT_SAMPLE_RATE:
        raw, _state = audioop.ratecv(
            raw,
            sample_width,
            OUTPUT_CHANNELS,
            sample_rate,
            OUTPUT_SAMPLE_RATE,
            None,
        )
    if not raw or len(raw) % 2 or len(raw) > max_output_bytes:
        raise ValueError("AstrBot TTS WAV could not be normalized safely")
    return raw
