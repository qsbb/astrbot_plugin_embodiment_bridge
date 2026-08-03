from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .provider_utils import contract_matches, find_active_provider
from .stt import AdapterUnavailable
from .tts import (
    OUTPUT_CHANNELS,
    OUTPUT_SAMPLE_RATE,
    TTSAdapter,
    _read_and_normalize_wav,
)


VOICE_PLUGIN_NAME = "astrbot_plugin_voice_hub"
VOICE_CONTRACT_NAME = "voice.audio_output"
VOICE_CONTRACT_MAJOR = "1"
VOICE_CAPABILITY = "render_pcm_wav"
VOICE_METHOD = "render_pcm_wav"


class VoiceHubTTSAdapter:
    """Consume voice.audio_output@1 without event or message side effects."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        timeout_seconds: float = 65.0,
        max_audio_seconds: int = 120,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = enabled
        self.timeout_seconds = min(max(float(timeout_seconds), 1.0), 180.0)
        self.max_output_bytes = (
            OUTPUT_SAMPLE_RATE * OUTPUT_CHANNELS * 2 * max(1, int(max_audio_seconds))
        )
        self._closed = False
        self.status = "enabled" if enabled else "disabled"
        self._missing_logged = False
        self._incompatible_logged = False

    @property
    def available(self) -> bool:
        if self._closed or not self.enabled:
            return False
        provider = find_active_provider(self.context, VOICE_PLUGIN_NAME)
        return provider is not None and self._contract_compatible(provider)

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        if self._closed or not self.enabled:
            raise AdapterUnavailable("voice.audio_output adapter is disabled")
        provider = find_active_provider(self.context, VOICE_PLUGIN_NAME)
        if provider is None:
            self.status = "provider_unavailable"
            if not self._missing_logged:
                self.logger.info(
                    "[quest-avatar] voice hub not installed; voice.audio_output unavailable"
                )
                self._missing_logged = True
            raise AdapterUnavailable("voice hub is not installed")
        if not self._contract_compatible(provider):
            self.status = "contract_incompatible"
            if not self._incompatible_logged:
                self.logger.warning(
                    "[quest-avatar] voice.audio_output contract is incompatible; integration disabled"
                )
                self._incompatible_logged = True
            raise AdapterUnavailable("voice.audio_output contract is incompatible")
        clean_text = str(text or "").strip()
        if not clean_text:
            return

        try:
            response = await asyncio.wait_for(
                provider.render_pcm_wav(
                    clean_text,
                    emotion=str(emotion or ""),
                    voice="",
                    context="",
                    session_id="",
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self.status = "timeout"
            raise AdapterUnavailable("voice.audio_output timed out") from exc
        except Exception as exc:
            self.status = "synthesis_failed"
            raise RuntimeError("voice.audio_output failed") from exc

        path = self._validated_path(response)
        pcm16 = await asyncio.to_thread(
            _read_and_normalize_wav,
            path,
            self.max_output_bytes,
        )
        self.status = "ok"
        for offset in range(0, len(pcm16), 64 * 1024):
            yield pcm16[offset : offset + 64 * 1024]

    def _contract_compatible(self, provider: Any) -> bool:
        try:
            contract = provider.voice_audio_output_contract()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
        return bool(
            contract_matches(
                contract,
                name=VOICE_CONTRACT_NAME,
                major=VOICE_CONTRACT_MAJOR,
                capability=VOICE_CAPABILITY,
                method=VOICE_METHOD,
            )
            and contract.get("sends_message") is False
        )

    def _validated_path(self, response: Any) -> Path:
        required = {
            "contract_name",
            "contract_version",
            "capability",
            "status",
            "error_code",
            "path",
            "container",
            "encoding",
            "sample_rate",
            "channels",
            "sample_width",
            "frame_count",
            "duration_ms",
            "ownership",
            "consumer_may_delete",
        }
        if not isinstance(response, dict) or set(response) != required:
            self.status = "invalid_response"
            raise RuntimeError("voice.audio_output returned an invalid response")
        if (
            response.get("contract_name") != VOICE_CONTRACT_NAME
            or str(response.get("contract_version") or "").split(".", 1)[0]
            != VOICE_CONTRACT_MAJOR
            or response.get("capability") != VOICE_CAPABILITY
        ):
            self.status = "invalid_response"
            raise RuntimeError("voice.audio_output response contract is incompatible")
        status = response.get("status")
        error_code = str(response.get("error_code") or "")
        if status == "unavailable":
            self.status = error_code or "unavailable"
            raise AdapterUnavailable(f"voice.audio_output unavailable: {self.status}")
        if status != "ok" or error_code:
            self.status = error_code or "error"
            raise RuntimeError(f"voice.audio_output error: {self.status}")
        if (
            response.get("container") != "wav"
            or response.get("encoding") != "pcm_s16le"
            or response.get("sample_width") != 2
            or response.get("ownership") != "provider_managed"
            or response.get("consumer_may_delete") is not False
        ):
            self.status = "unsupported_audio_format"
            raise RuntimeError("voice.audio_output returned unsafe audio metadata")
        try:
            sample_rate = int(response.get("sample_rate") or 0)
            channels = int(response.get("channels") or 0)
            frame_count = int(response.get("frame_count") or 0)
            duration_ms = int(response.get("duration_ms") or 0)
        except (TypeError, ValueError) as exc:
            self.status = "invalid_response"
            raise RuntimeError("voice.audio_output metadata is invalid") from exc
        if (
            sample_rate < 8_000
            or sample_rate > 192_000
            or channels not in {1, 2}
            or frame_count <= 0
            or duration_ms <= 0
        ):
            self.status = "unsupported_audio_format"
            raise RuntimeError("voice.audio_output metadata is unsupported")
        path = Path(str(response.get("path") or ""))
        if not path.is_absolute():
            self.status = "invalid_response"
            raise RuntimeError("voice.audio_output path must be absolute")
        return path

    async def close(self) -> None:
        self._closed = True


class FallbackTTSAdapter:
    """Prefer voice.audio_output while preserving the configured Core fallback."""

    def __init__(self, primary: TTSAdapter, fallback: TTSAdapter, logger: Any) -> None:
        self.primary = primary
        self.fallback = fallback
        self.logger = logger

    @property
    def available(self) -> bool:
        return self.primary.available or self.fallback.available

    async def synthesize(
        self,
        text: str,
        *,
        emotion: str,
    ) -> AsyncIterator[bytes]:
        if self.primary.available:
            yielded = False
            try:
                async for chunk in self.primary.synthesize(text, emotion=emotion):
                    yielded = True
                    yield chunk
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if yielded or not self.fallback.available:
                    raise
                self.logger.warning(
                    "[quest-avatar] voice hub TTS unavailable; using configured AstrBot Core fallback: error_type=%s",
                    type(exc).__name__,
                )
        if not self.fallback.available:
            raise AdapterUnavailable("no compatible TTS adapter is available")
        async for chunk in self.fallback.synthesize(text, emotion=emotion):
            yield chunk

    async def close(self) -> None:
        await asyncio.gather(
            self.primary.close(),
            self.fallback.close(),
            return_exceptions=True,
        )
