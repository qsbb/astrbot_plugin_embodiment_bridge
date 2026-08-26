from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from astrbot_plugin_embodiment_bridge.adapters.stt import AdapterUnavailable
from astrbot_plugin_embodiment_bridge.adapters.streaming_stt import (
    CompositeSTTAdapter,
    STTEvent,
    StreamingSTTAdapter,
    StreamingSTTCapabilities,
    StreamingSTTError,
)


class FakeSession:
    def __init__(
        self,
        *,
        final_text: str = "你好世界",
        partials: tuple[str, ...] = ("你好",),
        emit_final_on_end: bool = True,
        immediate_final: bool = False,
        error_code: str = "",
    ) -> None:
        self.fed: list[tuple[bytes, int, int]] = []
        self.ended = False
        self.closed = False
        self.cancelled_reason: str | None = None
        self._queue: asyncio.Queue[STTEvent] = asyncio.Queue()
        self._final_text = final_text
        self._partials = partials
        self._emit_final_on_end = emit_final_on_end
        self._immediate_final = immediate_final
        self._error_code = error_code

    async def feed(self, pcm16: bytes, *, sequence: int, byte_offset: int) -> None:
        self.fed.append((pcm16, sequence, byte_offset))

    async def end_of_input(self) -> None:
        if self.ended:
            return
        self.ended = True
        if not self._emit_final_on_end:
            return
        for partial in self._partials:
            await self._queue.put(STTEvent("partial", partial))
        if self._error_code:
            await self._queue.put(
                STTEvent("error", text="provider failed", error_code=self._error_code)
            )
        else:
            await self._queue.put(
                STTEvent("final", self._final_text, is_final=True)
            )

    async def events(self) -> AsyncIterator[STTEvent]:
        if self._immediate_final:
            yield STTEvent("final", self._final_text, is_final=True)
            return
        while True:
            event = await self._queue.get()
            yield event
            if event.kind in {"final", "error"}:
                return

    async def cancel(self, reason: str) -> None:
        self.cancelled_reason = reason

    async def close(self) -> None:
        self.closed = True


class FakeProvider:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.opened_with: dict[str, Any] | None = None

    @property
    def capabilities(self) -> StreamingSTTCapabilities:
        return StreamingSTTCapabilities()

    async def open(
        self, *, voice_id: str, sample_rate: int, channels: int, language: str
    ) -> FakeSession:
        self.opened_with = {
            "voice_id": voice_id,
            "sample_rate": sample_rate,
            "channels": channels,
            "language": language,
        }
        return self.session


async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def collect(agen: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    async for item in agen:
        result.append(item)
    return result


def test_yields_partial_then_final() -> None:
    async def scenario() -> None:
        provider = FakeProvider()
        adapter = StreamingSTTAdapter(provider)
        items = await collect(
            adapter.transcribe_stream(
                chunks_of(b"\x00\x00", b"\x01\x01"), sample_rate=16_000
            )
        )
        assert items == [
            {"kind": "partial", "text": "你好"},
            {"kind": "final", "text": "你好世界"},
        ]
        assert provider.session.fed == [
            (b"\x00\x00", 0, 0),
            (b"\x01\x01", 1, 2),
        ]
        assert provider.session.ended is True
        assert provider.session.closed is True

    asyncio.run(scenario())


def test_skips_empty_partials_and_final_once() -> None:
    async def scenario() -> None:
        session = FakeSession(partials=("", "你好"), final_text="")
        adapter = StreamingSTTAdapter(FakeProvider(session))
        items = await collect(
            adapter.transcribe_stream(chunks_of(b"\x00\x00"), sample_rate=16_000)
        )
        # Empty partial is skipped; empty final yields nothing.
        assert items == [{"kind": "partial", "text": "你好"}]

    asyncio.run(scenario())


def test_error_event_raises_streaming_error() -> None:
    async def scenario() -> None:
        session = FakeSession(error_code="quota_exceeded")
        adapter = StreamingSTTAdapter(FakeProvider(session))
        with pytest.raises(StreamingSTTError) as exc_info:
            await collect(
                adapter.transcribe_stream(chunks_of(b"\x00\x00"), sample_rate=16_000)
            )
        assert exc_info.value.code == "quota_exceeded"
        assert session.closed is True

    asyncio.run(scenario())


def test_early_final_cancels_feeder_and_closes() -> None:
    async def scenario() -> None:
        released = asyncio.Event()

        async def blocking_chunks() -> AsyncIterator[bytes]:
            yield b"\x00\x00"
            try:
                await released.wait()
            finally:
                released.set()

        session = FakeSession(immediate_final=True)
        adapter = StreamingSTTAdapter(FakeProvider(session))
        items = await collect(
            adapter.transcribe_stream(blocking_chunks(), sample_rate=16_000)
        )
        assert items == [{"kind": "final", "text": "你好世界"}]
        assert session.closed is True

    asyncio.run(scenario())


def test_unavailable_when_provider_missing() -> None:
    async def scenario() -> None:
        adapter = StreamingSTTAdapter(None)
        assert adapter.available is False
        assert adapter.streaming_available is False
        with pytest.raises(AdapterUnavailable):
            await collect(
                adapter.transcribe_stream(chunks_of(b"\x00\x00"), sample_rate=16_000)
            )

    asyncio.run(scenario())


def test_rejects_wrong_sample_rate() -> None:
    async def scenario() -> None:
        adapter = StreamingSTTAdapter(FakeProvider())
        with pytest.raises(ValueError):
            await collect(
                adapter.transcribe_stream(chunks_of(b"\x00\x00"), sample_rate=8_000)
            )

    asyncio.run(scenario())


class FakeFileAdapter:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.transcribed: bytes | None = None
        self.closed = False

    async def transcribe(self, pcm16: bytes, *, sample_rate: int) -> str:
        del sample_rate
        self.transcribed = pcm16
        return "file result"

    async def close(self) -> None:
        self.closed = True


def test_composite_delegates_stream_and_file() -> None:
    async def scenario() -> None:
        streaming = StreamingSTTAdapter(FakeProvider())
        file = FakeFileAdapter()
        composite = CompositeSTTAdapter(streaming=streaming, file=file)
        assert composite.available is True
        assert composite.streaming_available is True

        items = await collect(
            composite.transcribe_stream(
                chunks_of(b"\x00\x00"), sample_rate=16_000
            )
        )
        assert items[-1] == {"kind": "final", "text": "你好世界"}

        text = await composite.transcribe(b"\x02\x02", sample_rate=16_000)
        assert text == "file result"
        assert file.transcribed == b"\x02\x02"

        await composite.close()
        assert file.closed is True

    asyncio.run(scenario())


def test_composite_without_streaming_still_falls_back() -> None:
    async def scenario() -> None:
        file = FakeFileAdapter()
        composite = CompositeSTTAdapter(streaming=None, file=file)
        assert composite.available is True
        assert composite.streaming_available is False
        with pytest.raises(AdapterUnavailable):
            await collect(
                composite.transcribe_stream(chunks_of(b"\x00\x00"), sample_rate=16_000)
            )
        assert await composite.transcribe(b"\x00\x00", sample_rate=16_000) == (
            "file result"
        )

    asyncio.run(scenario())
