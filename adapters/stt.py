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
    """Adapt one explicitly selected AstrBot STT provider to Quest PCM16."""

    def __init__(
        self,
        context: Any,
        *,
        data_dir: Path,
        provider_id: str = "",
        legacy_default_enabled: bool = False,
        legacy_private_mimo_enabled: bool = False,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.context = context
        self.data_dir = data_dir
        self.provider_id = str(provider_id or "").strip()
        self.legacy_default_enabled = bool(legacy_default_enabled)
        self.legacy_private_mimo_enabled = bool(legacy_private_mimo_enabled)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._active_files: set[Path] = set()
        self._closed = False
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return not self._closed and self._provider() is not None

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        if self._closed:
            raise AdapterUnavailable("AstrBot STT adapter is disabled")
        provider = self._provider()
        if provider is None:
            raise AdapterUnavailable(f"AstrBot STT unavailable: {self.status_reason}")
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

    def configure_provider(self, provider_id: str) -> None:
        self.provider_id = str(provider_id or "").strip()
        self.legacy_default_enabled = False
        self.legacy_private_mimo_enabled = False

    @property
    def status_reason(self) -> str:
        if self._closed:
            return "closed"
        if self.provider_id:
            return (
                "ready" if self._selected_provider() is not None else "selected_missing"
            )
        if self.legacy_default_enabled:
            return (
                "legacy_default_ready"
                if self._default_provider() is not None
                else "legacy_default_missing"
            )
        if self.legacy_private_mimo_enabled:
            return "legacy_private_mimo_disabled"
        return "disabled"

    def status_snapshot(self) -> dict[str, Any]:
        catalog = self.provider_catalog()
        return {
            "source": "astrbot_stt_provider",
            "available": self.available,
            "status": self.status_reason,
            "selected": bool(self.provider_id),
            "selected_id": self.provider_id,
            "legacy_default": bool(
                not self.provider_id and self.legacy_default_enabled
            ),
            "external_contract_status": "no_standard_contract",
            "providers": catalog,
        }

    def provider_catalog(self) -> list[dict[str, str]]:
        providers = self._all_providers()
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in providers:
            try:
                meta = provider.meta()
                provider_id = str(meta.id or "").strip()
                model = str(meta.model or "").strip()
                adapter_type = str(meta.type or "").strip()
                raw_provider_type = meta.provider_type
                provider_type = (
                    raw_provider_type.strip()
                    if isinstance(raw_provider_type, str)
                    else str(raw_provider_type.value or "").strip()
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if (
                not provider_id
                or len(provider_id) > 256
                or provider_id in seen
                or any(ord(char) < 33 for char in provider_id)
                or provider_type != "speech_to_text"
            ):
                continue
            seen.add(provider_id)
            result.append(
                {
                    "id": provider_id,
                    "model": model[:256],
                    "adapter_type": adapter_type[:128],
                    "provider_type": provider_type,
                }
            )
        result.sort(key=lambda item: (item["model"].casefold(), item["id"]))
        return result

    def _provider(self) -> Any | None:
        if self.provider_id:
            return self._selected_provider()
        if self.legacy_default_enabled:
            return self._default_provider()
        return None

    def _selected_provider(self) -> Any | None:
        for provider in self._all_providers():
            try:
                if str(provider.meta().id or "").strip() == self.provider_id:
                    return provider
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        return None

    def _default_provider(self) -> Any | None:
        try:
            return self.context.get_using_stt_provider()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None

    def _all_providers(self) -> tuple[Any, ...]:
        try:
            providers = self.context.get_all_stt_providers()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return ()
        return tuple(providers) if isinstance(providers, (list, tuple)) else ()


def _write_pcm16_wav(path: Path, pcm16: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(INPUT_CHANNELS)
        output.setsampwidth(INPUT_SAMPLE_WIDTH)
        output.setframerate(INPUT_SAMPLE_RATE)
        output.writeframes(pcm16)
