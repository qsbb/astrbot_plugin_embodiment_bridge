from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import wave

import aiohttp


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


class ConfiguredMiMoSTTAdapter:
    """Plugin-owned MiMo STT client independent of AstrBot global providers."""

    def __init__(
        self,
        *,
        enabled: bool,
        api_base: str,
        api_key: str,
        model: str = "mimo-v2.5-asr",
        timeout_seconds: float = 45.0,
    ) -> None:
        self.enabled = enabled
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip() or "mimo-v2.5-asr"
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._closed = False
        try:
            self.api_url = _normalize_mimo_api_url(api_base)
        except ValueError:
            self.api_url = ""

    @property
    def available(self) -> bool:
        return bool(
            not self._closed
            and self.enabled
            and self.api_key
            and self.api_url
            and self.model
        )

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        if not self.available:
            raise AdapterUnavailable("Plugin MiMo STT is not configured")
        if sample_rate != INPUT_SAMPLE_RATE:
            raise ValueError("STT input sample rate must be 16000 Hz")
        if not pcm16 or len(pcm16) % INPUT_SAMPLE_WIDTH:
            raise ValueError("STT input must contain complete PCM16 samples")

        audio_data_url = "data:audio/wav;base64," + base64.b64encode(
            _pcm16_wav_bytes(pcm16)
        ).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_data_url},
                        }
                    ],
                }
            ],
        }
        data = await _post_mimo_json(
            self.api_url,
            api_key=self.api_key,
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        choices = data.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = (first_choice or {}).get("message") or {}
        content = message.get("content") or message.get("reasoning_content") or ""
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Plugin MiMo STT returned an empty transcription")
        return content.strip()

    async def close(self) -> None:
        self._closed = True


def select_stt_adapter(primary: STTAdapter, fallback: STTAdapter) -> STTAdapter:
    return primary if primary.available else fallback


def _normalize_mimo_api_url(api_base: str) -> str:
    raw = str(api_base or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise ValueError("Plugin STT API URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Plugin STT API URL must be an HTTPS URL without credentials")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit(("https", parsed.netloc, path, "", ""))


async def _post_mimo_json(
    url: str,
    *,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.post(url, headers=headers, json=payload) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(
                    f"Plugin MiMo STT request failed: HTTP {response.status}"
                )
            data = await response.json(content_type=None)
    if not isinstance(data, dict):
        raise TypeError("Plugin MiMo STT returned an invalid response")
    return data


def _pcm16_wav_bytes(pcm16: bytes) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(INPUT_CHANNELS)
        wav.setsampwidth(INPUT_SAMPLE_WIDTH)
        wav.setframerate(INPUT_SAMPLE_RATE)
        wav.writeframes(pcm16)
    return output.getvalue()


def _write_pcm16_wav(path: Path, pcm16: bytes) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(INPUT_CHANNELS)
        output.setsampwidth(INPUT_SAMPLE_WIDTH)
        output.setframerate(INPUT_SAMPLE_RATE)
        output.writeframes(pcm16)
