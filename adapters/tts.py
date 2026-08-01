from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol


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
