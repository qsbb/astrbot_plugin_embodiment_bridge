from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4
import wave


INPUT_SAMPLE_RATE = 16_000
INPUT_CHANNELS = 1
INPUT_SAMPLE_WIDTH = 2


class AdapterUnavailable(RuntimeError):
    pass


class STTAdapter(Protocol):
    @property
    def available(self) -> bool: ...

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str: ...

    async def close(self) -> None: ...


class DisabledSTTAdapter:
    @property
    def available(self) -> bool:
        return False

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        del pcm16, sample_rate
        raise AdapterUnavailable(
            "No stable PCM16 streaming STT adapter is configured for this release"
        )

    async def close(self) -> None:
        return None


class AstrBotSTTAdapter:
    """Adapt AstrBot's selected file-based STT provider to Quest PCM16 input."""

    def __init__(
        self,
        context: Any,
        *,
        data_dir: Path,
        enabled: bool,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.context = context
        self.data_dir = data_dir
        self.enabled = enabled
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._active_files: set[Path] = set()
        self._closed = False
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return not self._closed and self.enabled and self._provider() is not None

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        if self._closed or not self.enabled:
            raise AdapterUnavailable("AstrBot STT adapter is disabled")
        provider = self._provider()
        if provider is None:
            raise AdapterUnavailable("AstrBot STT provider is not configured")
        if sample_rate != INPUT_SAMPLE_RATE:
            raise ValueError("STT input sample rate must be 16000 Hz")
        if not pcm16 or len(pcm16) % INPUT_SAMPLE_WIDTH:
            raise ValueError("STT input must contain complete PCM16 samples")

        audio_path = self.data_dir / f"quest-stt-{uuid4().hex}.wav"
        self._active_files.add(audio_path)
        try:
            await asyncio.to_thread(_write_pcm16_wav, audio_path, pcm16)
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    text = await provider.get_text(str(audio_path))
            except TimeoutError as exc:
                raise RuntimeError("AstrBot STT provider timed out") from exc
            if not isinstance(text, str):
                raise TypeError("AstrBot STT provider returned a non-string result")
            return text.strip()
        finally:
            self._active_files.discard(audio_path)
            await asyncio.to_thread(audio_path.unlink, missing_ok=True)

    async def close(self) -> None:
        self._closed = True
        active = tuple(self._active_files)
        self._active_files.clear()
        if active:
            await asyncio.gather(
                *(asyncio.to_thread(path.unlink, missing_ok=True) for path in active),
                return_exceptions=True,
            )

    def _provider(self) -> Any | None:
        if not self.enabled:
            return None
        try:
            return self.context.get_using_stt_provider()
        except (RuntimeError, ValueError):
            return None


def _write_pcm16_wav(path: Path, pcm16: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(INPUT_CHANNELS)
        output.setsampwidth(INPUT_SAMPLE_WIDTH)
        output.setframerate(INPUT_SAMPLE_RATE)
        output.writeframes(pcm16)
