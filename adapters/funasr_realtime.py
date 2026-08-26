"""Fun-ASR-Realtime (Alibaba Cloud DashScope) streaming recogniser.

Implements the ``StreamingSTTSession``/``StreamingSTTProvider`` contract from
``adapters/streaming_stt.py`` against the DashScope ``/api-ws/v1/inference``
WebSocket protocol:

* Header ``Authorization: Bearer <DASHSCOPE_API_KEY>``.
* ``run-task`` (JSON) -> ``task-started`` -> binary PCM16 frames -> ``finish-task``.
* Server events use ``header.event``: ``task-started`` / ``result-generated`` /
  ``task-finished`` / ``task-failed``.
* Recognised text lives in ``payload.output.sentence.text``; ``sentence_end``
  distinguishes an in-progress sentence (``false``, advisory partial) from a
  finalised sentence (``true``).

Audio is sent as raw binary frames (no base64), which keeps the realtime path
cheap.  The session buffers a bounded amount of PCM until ``task-started`` so
that the orchestrator's bounded input queue is drained from the first chunk,
without waiting for the provider handshake.

Spec source: ``/data/dsh/home/dsh/asr-realtime-protocol-spec.md``.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import AsyncIterator
from uuid import uuid4

import aiohttp

from .streaming_stt import (
    STTEvent,
    StreamingSTTCapabilities,
    StreamingSTTError,
    StreamingSTTProvider,
    StreamingSTTSession,
)

DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
DEFAULT_MODEL = "fun-asr-realtime"
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CONNECT_TIMEOUT = 8.0
MAX_PENDING_BYTES = 65_536  # ~2s of 16kHz mono PCM16 before task-started


class FunASRRealtimeSession:
    def __init__(
        self,
        *,
        client: aiohttp.ClientSession,
        api_key: str,
        model: str,
        sample_rate: int,
        task_id: str,
        connect_timeout: float,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._model = model
        self._sample_rate = int(sample_rate)
        self._task_id = task_id or uuid4().hex
        self._connect_timeout = max(1.0, connect_timeout)

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._started_event = asyncio.Event()
        self._terminal_event = asyncio.Event()

        # Bounded pre-start buffer. ``feed`` returns immediately before
        # ``task-started``; the audio is flushed in order on the next write.
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0

        self._input_ended = False
        self._closed = False
        self._terminal = False
        self._final_parts: list[str] = []
        self._out_queue: asyncio.Queue[STTEvent] = asyncio.Queue(maxsize=64)

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            self._ws = await self._client.ws_connect(
                DASHSCOPE_WS_URL,
                headers=headers,
                timeout=aiohttp.ClientWSTimeout(ws_close=self._connect_timeout),
            )
        except Exception as exc:
            raise StreamingSTTError("connect_error", str(exc)) from exc
        self._reader_task = asyncio.create_task(
            self._reader(), name="embodiment-bridge:funasr-reader"
        )
        await self._send_text_locked(
            {
                "header": {
                    "action": "run-task",
                    "task_id": self._task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self._model,
                    "parameters": {
                        "format": "pcm",
                        "sample_rate": self._sample_rate,
                    },
                    "input": {},
                },
            }
        )

    async def feed(self, pcm16: bytes, *, sequence: int, byte_offset: int) -> None:
        del sequence, byte_offset
        if self._closed:
            raise StreamingSTTError("session_closed")
        if self._terminal:
            raise StreamingSTTError("session_ended")
        async with self._write_lock:
            if self._started_event.is_set():
                await self._flush_pending_locked()
                await self._send_bytes_locked(pcm16)
                return
            if self._pending_bytes + len(pcm16) > MAX_PENDING_BYTES:
                raise StreamingSTTError(
                    "task_start_slow",
                    "recogniser did not start before the input buffer filled",
                )
            self._pending.append(pcm16)
            self._pending_bytes += len(pcm16)

    async def end_of_input(self) -> None:
        if self._input_ended or self._closed:
            return
        self._input_ended = True
        async with self._write_lock:
            if not self._started_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._started_event.wait(), timeout=self._connect_timeout
                    )
                except TimeoutError:
                    self._push_terminal(
                        STTEvent(
                            "error",
                            text="recogniser did not start",
                            error_code="task_start_timeout",
                        )
                    )
                    return
            await self._flush_pending_locked()
            await self._send_text_locked(
                {
                    "header": {
                        "action": "finish-task",
                        "task_id": self._task_id,
                        "streaming": "duplex",
                    },
                    "payload": {"input": {}},
                }
            )

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._out_queue.get()
            yield event
            if event.kind in {"final", "error"}:
                return

    async def cancel(self, reason: str) -> None:
        if not self._terminal:
            self._push_terminal(
                STTEvent("error", text=reason, error_code="cancelled")
            )
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._terminal:
            self._push_terminal(
                STTEvent("error", text="session closed", error_code="closed")
            )
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None and not self._ws.closed:
            try:
                async with self._write_lock:
                    await self._ws.close()
            except Exception:
                pass

    # -- wire helpers ---------------------------------------------------

    async def _send_text_locked(self, payload: dict[str, object]) -> None:
        if self._ws is None or self._ws.closed:
            raise StreamingSTTError("ws_closed")
        await self._ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def _send_bytes_locked(self, data: bytes) -> None:
        if self._ws is None or self._ws.closed:
            raise StreamingSTTError("ws_closed")
        await self._ws.send_bytes(data)

    async def _flush_pending_locked(self) -> None:
        while self._pending:
            chunk = self._pending.popleft()
            self._pending_bytes -= len(chunk)
            await self._send_bytes_locked(chunk)

    async def _reader(self) -> None:
        ws = self._ws
        try:
            if ws is None:
                self._push_terminal(
                    STTEvent("error", text="no websocket", error_code="ws_closed")
                )
                return
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self._handle_text(message.data)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
                # BINARY and CLOSING frames are ignored for ASR results.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._push_terminal(
                STTEvent("error", text=str(exc), error_code="ws_error")
            )
        finally:
            self._handle_ws_closed()

    def _handle_text(self, data: str) -> None:
        try:
            message = json.loads(data)
        except ValueError:
            return
        header = message.get("header") or {}
        event = header.get("event")
        if event == "task-started":
            self._started_event.set()
            return
        if event == "result-generated":
            self._handle_result(message)
            return
        if event == "task-finished":
            self._handle_task_finished()
            return
        if event == "task-failed":
            self._push_terminal(
                STTEvent(
                    "error",
                    text=str(header.get("error_message") or ""),
                    error_code=str(header.get("error_code") or "task_failed"),
                )
            )
            return

    def _handle_result(self, message: dict[str, object]) -> None:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        output = payload.get("output")
        if not isinstance(output, dict):
            return
        sentence = output.get("sentence")
        if not isinstance(sentence, dict):
            return
        if sentence.get("heartbeat"):
            return
        text = str(sentence.get("text") or "").strip()
        if not text:
            return
        if sentence.get("sentence_end"):
            self._final_parts.append(text)
        else:
            self._push_partial(
                STTEvent(
                    "partial",
                    text=text,
                    stable_prefix="".join(self._final_parts),
                )
            )

    def _handle_task_finished(self) -> None:
        if self._terminal:
            return
        if self._final_parts:
            self._push_terminal(
                STTEvent("final", text="".join(self._final_parts), is_final=True)
            )
        else:
            self._push_terminal(
                STTEvent(
                    "error",
                    text="recogniser produced no transcript",
                    error_code="no_transcript",
                )
            )

    def _handle_ws_closed(self) -> None:
        if self._terminal:
            return
        if self._final_parts:
            self._push_terminal(
                STTEvent("final", text="".join(self._final_parts), is_final=True)
            )
        else:
            self._push_terminal(
                STTEvent(
                    "error",
                    text="recogniser closed before final",
                    error_code="connection_closed",
                )
            )

    def _push_partial(self, event: STTEvent) -> None:
        if self._terminal:
            return
        try:
            self._out_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def _push_terminal(self, event: STTEvent) -> None:
        if self._terminal:
            return
        self._terminal = True
        while True:
            try:
                self._out_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self._out_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
        self._terminal_event.set()


class FunASRRealtimeProvider:
    """DashScope Fun-ASR-Realtime factory."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        language: str = "zh",
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._api_key = str(api_key or "")
        self._model = str(model or DEFAULT_MODEL)
        self._sample_rate = int(sample_rate)
        self._language = str(language or "zh")
        self._connect_timeout = max(1.0, float(connect_timeout))
        self._client: aiohttp.ClientSession | None = None
        self._closed = False

    @property
    def capabilities(self) -> StreamingSTTCapabilities:
        return StreamingSTTCapabilities(
            streaming=True,
            partial=True,
            server_vad=False,
            word_timestamps=True,
            language=self._language,
            max_audio_seconds=60,
        )

    async def open(
        self,
        *,
        voice_id: str,
        sample_rate: int,
        channels: int,
        language: str,
    ) -> FunASRRealtimeSession:
        del channels, language  # FunASR is mono-only; language is model-driven.
        if self._closed:
            raise StreamingSTTError("provider_closed")
        session = FunASRRealtimeSession(
            client=await self._client_session(),
            api_key=self._api_key,
            model=self._model,
            sample_rate=sample_rate,
            task_id=voice_id or uuid4().hex,
            connect_timeout=self._connect_timeout,
        )
        await session.start()
        return session

    async def _client_session(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._connect_timeout * 2)
            )
        return self._client

    async def close(self) -> None:
        self._closed = True
        if self._client is not None and not self._client.closed:
            await self._client.close()
            self._client = None
