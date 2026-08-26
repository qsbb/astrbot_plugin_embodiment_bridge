from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_embodiment_bridge.adapters.funasr_realtime import (
    DASHSCOPE_WS_URL,
    FunASRRealtimeProvider,
    FunASRRealtimeSession,
)
from astrbot_plugin_embodiment_bridge.adapters.streaming_stt import STTEvent


class BlockingWS:
    """Minimal fake WebSocket whose reader blocks until cancelled."""

    def __init__(self) -> None:
        self.sent_bytes: list[bytes] = []
        self.sent_str: list[str] = []
        self.closed = False

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_str(self, text: str) -> None:
        self.sent_str.append(text)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> "BlockingWS":
        return self

    async def __anext__(self) -> Any:
        await asyncio.Event().wait()
        raise StopAsyncIteration


def make_session(**overrides: Any) -> FunASRRealtimeSession:
    kwargs: dict[str, Any] = {
        "client": AsyncMock(),
        "api_key": "test-key",
        "model": "fun-asr-realtime",
        "sample_rate": 16_000,
        "task_id": "task-1",
        "connect_timeout": 8.0,
    }
    kwargs.update(overrides)
    return FunASRRealtimeSession(**kwargs)


async def drain(session: FunASRRealtimeSession) -> list[STTEvent]:
    events: list[STTEvent] = []
    async for event in session.events():
        events.append(event)
        if event.kind in {"final", "error"}:
            break
    return events


def test_handle_partial_final_and_finished() -> None:
    async def scenario() -> None:
        session = make_session()

        # In-progress sentence -> advisory partial event.
        session._handle_text(
            json.dumps(
                {
                    "header": {"task_id": "task-1", "event": "result-generated"},
                    "payload": {
                        "output": {
                            "sentence": {
                                "text": "你好",
                                "sentence_end": False,
                                "sentence_id": 1,
                            }
                        }
                    },
                }
            )
        )
        assert session._out_queue.qsize() == 1
        partial = session._out_queue.get_nowait()
        assert partial.kind == "partial"
        assert partial.text == "你好"

        # Finalised sentence -> accumulated, not emitted yet.
        session._handle_text(
            json.dumps(
                {
                    "header": {"task_id": "task-1", "event": "result-generated"},
                    "payload": {
                        "output": {
                            "sentence": {
                                "text": "你好世界。",
                                "sentence_end": True,
                                "sentence_id": 1,
                            }
                        }
                    },
                }
            )
        )
        assert session._final_parts == ["你好世界。"]

        # Task finished -> one joined final.
        session._handle_text(
            json.dumps(
                {
                    "header": {"task_id": "task-1", "event": "task-finished"},
                    "payload": {},
                }
            )
        )
        events = await asyncio.wait_for(drain(session), timeout=1)
        assert [event.kind for event in events] == ["final"]
        assert events[0].text == "你好世界。"
        assert events[0].is_final is True

    asyncio.run(scenario())


def test_handle_task_failed_emits_error() -> None:
    async def scenario() -> None:
        session = make_session()
        session._handle_text(
            json.dumps(
                {
                    "header": {
                        "task_id": "task-1",
                        "event": "task-failed",
                        "error_code": "CLIENT_ERROR",
                        "error_message": "bad request",
                    },
                    "payload": {},
                }
            )
        )
        events = await asyncio.wait_for(drain(session), timeout=1)
        assert len(events) == 1
        assert events[0].kind == "error"
        assert events[0].error_code == "CLIENT_ERROR"

    asyncio.run(scenario())


def test_handle_heartbeat_ignored() -> None:
    async def scenario() -> None:
        session = make_session()
        session._handle_text(
            json.dumps(
                {
                    "header": {"task_id": "task-1", "event": "result-generated"},
                    "payload": {
                        "output": {
                            "sentence": {
                                "text": "",
                                "sentence_end": False,
                                "sentence_id": 0,
                                "heartbeat": True,
                            }
                        }
                    },
                }
            )
        )
        assert session._out_queue.empty()
        assert session._terminal is False

    asyncio.run(scenario())


def test_feed_buffers_before_started_and_flushes_after() -> None:
    async def scenario() -> None:
        session = make_session()
        ws = BlockingWS()
        session._ws = ws  # type: ignore[assignment]

        await session.feed(b"\x00\x00", sequence=0, byte_offset=0)
        await session.feed(b"\x01\x01", sequence=1, byte_offset=2)
        assert ws.sent_bytes == []  # buffered, not sent yet

        session._started_event.set()
        await session.feed(b"\x02\x02", sequence=2, byte_offset=4)
        assert ws.sent_bytes == [b"\x00\x00", b"\x01\x01", b"\x02\x02"]
        assert session._pending_bytes == 0

    asyncio.run(scenario())


def test_end_of_input_flushes_and_sends_finish_task() -> None:
    async def scenario() -> None:
        session = make_session()
        ws = BlockingWS()
        session._ws = ws  # type: ignore[assignment]
        session._started_event.set()

        await session.feed(b"\x00\x00", sequence=0, byte_offset=0)
        await session.end_of_input()
        assert ws.sent_bytes == [b"\x00\x00"]
        assert len(ws.sent_str) == 1
        finish = json.loads(ws.sent_str[0])
        assert finish["header"]["action"] == "finish-task"
        assert finish["header"]["task_id"] == "task-1"

    asyncio.run(scenario())


def test_start_sends_run_task_with_model_and_format() -> None:
    async def scenario() -> None:
        client = AsyncMock()
        ws = BlockingWS()
        client.ws_connect = AsyncMock(return_value=ws)
        session = make_session(client=client)
        await session.start()

        client.ws_connect.assert_awaited_once()
        args, kwargs = client.ws_connect.call_args
        assert args[0] == DASHSCOPE_WS_URL
        assert kwargs["headers"] == {"Authorization": "Bearer test-key"}
        assert len(ws.sent_str) == 1
        run = json.loads(ws.sent_str[0])
        assert run["header"]["action"] == "run-task"
        assert run["header"]["task_id"] == "task-1"
        assert run["payload"]["model"] == "fun-asr-realtime"
        assert run["payload"]["parameters"] == {
            "format": "pcm",
            "sample_rate": 16_000,
        }
        await session.close()

    asyncio.run(scenario())


def test_provider_capabilities_and_open() -> None:
    async def scenario() -> None:
        provider = FunASRRealtimeProvider(api_key="k", model="fun-asr-realtime")
        capabilities = provider.capabilities
        assert capabilities.streaming is True
        assert capabilities.partial is True
        assert capabilities.language == "zh"

        client = AsyncMock()
        client.closed = False
        ws = BlockingWS()
        client.ws_connect = AsyncMock(return_value=ws)
        provider._client = client
        session = await provider.open(
            voice_id="voice-1", sample_rate=16_000, channels=1, language="zh"
        )
        assert isinstance(session, FunASRRealtimeSession)
        assert client.ws_connect.await_count == 1
        await session.close()
        await provider.close()
        assert client.close.await_count == 1

    asyncio.run(scenario())


def test_provider_without_api_key_still_constructs() -> None:
    provider = FunASRRealtimeProvider(api_key="")
    assert provider.capabilities.streaming is True
