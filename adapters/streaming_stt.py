"""Provider-neutral streaming speech-to-text (STT) session abstraction.

The orchestrator already owns the bounded input queue, the optional streaming
worker and the whole-PCM file fallback.  This module fills the missing middle:
a session-shaped contract that a cloud WebSocket recogniser can implement once,
so ``TurnOrchestrator`` never has to know about Tencent, Alibaba or Volcengine
wire protocols.

Design rules enforced here:

* ``feed`` applies backpressure by awaiting the provider send path.  The
  orchestrator's bounded ``stt_stream_queue`` (maxsize=8, 0.25s put timeout)
  turns a slow provider into a ``queue_backpressure`` stream failure instead of
  unbounded buffering.  Nothing in this module grows without bound.
* Partial events are advisory only.  The orchestrator forwards them to SSE and
  diagnostics; a partial can never start the formal LLM turn.  Only a single
  non-empty ``final`` event is surfaced.
* A provider ``error`` event is raised as :class:`StreamingSTTError` so the
  orchestrator's existing exception path marks ``stt_stream_failure`` and falls
  back to the durable whole-PCM transcription exactly once.
* ``close``/``end_of_input``/``cancel`` are idempotent; a late event after
  close must not surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Protocol
from uuid import uuid4

from .stt import AdapterUnavailable, INPUT_SAMPLE_RATE, INPUT_CHANNELS


class StreamingSTTError(RuntimeError):
    """A streaming recogniser failed; the caller should fall back, not retry."""

    def __init__(self, code: str = "provider_error", message: str = "") -> None:
        super().__init__(message or code)
        self.code = code or "provider_error"


@dataclass(frozen=True, slots=True)
class StreamingSTTCapabilities:
    streaming: bool = True
    partial: bool = True
    server_vad: bool = False
    word_timestamps: bool = False
    language: str = "zh"
    max_audio_seconds: int = 60


@dataclass(frozen=True, slots=True)
class STTEvent:
    """One recogniser event, provider-neutral and monotonic per session."""

    kind: Literal["partial", "final", "error"]
    text: str = ""
    sequence: int = 0
    is_final: bool = False
    stable_prefix: str = ""
    utterance_end: bool = False
    confidence: float | None = None
    provider_time_ms: int | None = None
    error_code: str = ""

    def to_contract(self) -> dict[str, Any]:
        """The minimal shape the orchestrator's streaming consumer reads.

        Extra fields are deliberately omitted: the SSE/diagnostic layers only
        need ``kind`` and ``text``, and keeping the payload small avoids leaking
        provider internals to the client.
        """
        return {"kind": self.kind, "text": self.text}


class StreamingSTTSession(Protocol):
    """One open recogniser session bound to a single audio turn."""

    async def feed(self, pcm16: bytes, *, sequence: int, byte_offset: int) -> None:
        """Send one complete PCM16 chunk; may block to apply backpressure."""
        ...

    async def end_of_input(self) -> None:
        """Mark the upload complete and request a final result (idempotent)."""
        ...

    def events(self) -> AsyncIterator[STTEvent]:
        """Yield partial/final/error events until a terminal event or close."""
        ...

    async def cancel(self, reason: str) -> None:
        """Abort the session; no further events may be produced."""
        ...

    async def close(self) -> None:
        """Release resources; idempotent."""
        ...


class StreamingSTTProvider(Protocol):
    """Factory for recogniser sessions (cloud WebSocket, self-hosted, ...)."""

    @property
    def capabilities(self) -> StreamingSTTCapabilities: ...

    async def open(
        self,
        *,
        voice_id: str,
        sample_rate: int,
        channels: int,
        language: str,
    ) -> StreamingSTTSession: ...

    async def close(self) -> None:
        """Release provider-scoped resources (HTTP session, pools); idempotent."""
        ...


class StreamingSTTAdapter:
    """Bridge the orchestrator's ``AsyncIterator[bytes]`` contract to a session.

    ``transcribe_stream`` matches ``STTAdapter.transcribe_stream``: it consumes
    raw PCM16 chunks as they arrive and yields ``{"kind", "text"}`` dicts.
    """

    def __init__(
        self,
        provider: StreamingSTTProvider | None,
        *,
        sample_rate: int = INPUT_SAMPLE_RATE,
        channels: int = INPUT_CHANNELS,
        language: str = "zh",
    ) -> None:
        self._provider = provider
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.language = str(language or "zh")
        self._closed = False

    @property
    def available(self) -> bool:
        return not self._closed and self._provider is not None

    @property
    def streaming_available(self) -> bool:
        return self.available

    async def transcribe_stream(
        self, chunks: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[dict[str, Any]]:
        if self._closed:
            raise AdapterUnavailable("streaming STT adapter is disabled")
        if sample_rate != self.sample_rate:
            raise ValueError(
                f"streaming STT input sample rate must be {self.sample_rate} Hz"
            )
        provider = self._provider
        if provider is None:
            raise AdapterUnavailable("streaming STT provider is not configured")

        session = await provider.open(
            voice_id=uuid4().hex,
            sample_rate=self.sample_rate,
            channels=self.channels,
            language=self.language,
        )
        feeder: asyncio.Task[None] | None = None

        async def feed_all() -> None:
            sequence = 0
            byte_offset = 0
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    await session.feed(
                        chunk, sequence=sequence, byte_offset=byte_offset
                    )
                    sequence += 1
                    byte_offset += len(chunk)
            finally:
                await session.end_of_input()

        try:
            feeder = asyncio.create_task(
                feed_all(), name="embodiment-bridge:stt-feed"
            )
            final_yielded = False
            async for event in session.events():
                if event.kind == "error":
                    raise StreamingSTTError(
                        event.error_code or "provider_error", event.text
                    )
                if event.kind == "final":
                    text = event.text.strip()
                    if text and not final_yielded:
                        final_yielded = True
                        yield {"kind": "final", "text": text}
                    break
                if event.kind == "partial" and event.text.strip():
                    yield {"kind": "partial", "text": event.text.strip()}
        finally:
            if feeder is not None and not feeder.done():
                feeder.cancel()
                try:
                    await feeder
                except (asyncio.CancelledError, Exception):
                    pass
            await session.close()

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        del pcm16, sample_rate
        raise AdapterUnavailable(
            "streaming STT adapter has no file transcription path"
        )

    async def close(self) -> None:
        self._closed = True
        provider = self._provider
        close_provider = getattr(provider, "close", None) if provider else None
        if callable(close_provider):
            try:
                await close_provider()
            except Exception:
                pass


class CompositeSTTAdapter:
    """Streaming recogniser with a durable whole-PCM fallback provider.

    ``transcribe_stream`` goes to the streaming adapter; ``transcribe`` (the
    orchestrator's fallback) goes to the file adapter, preserving the existing
    MiMo/AstrBot provider path when the realtime recogniser yields no final.
    """

    def __init__(
        self,
        *,
        streaming: StreamingSTTAdapter | None,
        file: Any | None,
    ) -> None:
        self._streaming = streaming
        self._file = file

    @property
    def available(self) -> bool:
        return bool(
            (self._file is not None and getattr(self._file, "available", False))
            or (
                self._streaming is not None
                and getattr(self._streaming, "available", False)
            )
        )

    @property
    def streaming_available(self) -> bool:
        return bool(
            self._streaming is not None
            and getattr(self._streaming, "streaming_available", False)
        )

    async def transcribe_stream(
        self, chunks: AsyncIterator[bytes], *, sample_rate: int
    ) -> AsyncIterator[dict[str, Any]]:
        if self._streaming is None:
            raise AdapterUnavailable("streaming STT is not configured")
        async for item in self._streaming.transcribe_stream(
            chunks, sample_rate=sample_rate
        ):
            yield item

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        if self._file is None:
            raise AdapterUnavailable("STT fallback provider is not configured")
        return await self._file.transcribe(pcm16, sample_rate=sample_rate)

    async def close(self) -> None:
        for adapter in (self._streaming, self._file):
            if adapter is None:
                continue
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:
                    pass
