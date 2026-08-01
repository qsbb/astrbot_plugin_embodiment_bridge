from __future__ import annotations

from typing import Protocol


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
