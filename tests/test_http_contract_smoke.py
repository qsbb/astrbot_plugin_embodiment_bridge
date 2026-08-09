from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout
import pytest

from .http_harness import (
    AUTH_HEADERS,
    ASTRBOT_API_KEY_ID,
    ASTRBOT_API_TOKEN,
    BRIDGE_API_KEY,
    LiveHttpServer,
    build_plugin,
    read_sse_frame,
)


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN_ROOT / "fixtures" / "protocol_v1"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_real_http_sse_contract_smoke(monkeypatch: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        registered_routes = {
            (method, route.removeprefix("/astrbot_plugin_quest_avatar_bridge"))
            for route, _handler, methods, _description in bundle.context.routes
            for method in methods
        }
        fixture_routes = {
            (item["method"], item["path"])
            for item in load_json("manifest.json")["routes"]
        }
        assert registered_routes == fixture_routes
        async with LiveHttpServer(bundle) as server:
            timeout = ClientTimeout(total=None, connect=2)
            async with ClientSession(timeout=timeout) as client:
                health = await client.get(server.url("/health"), headers=AUTH_HEADERS)
                assert health.status == 200
                health_body = await health.json()
                assert health_body["data"]["protocol_version"] == "1.0"
                assert health_body["data"]["input_audio"]["stt_available"] is True
                assert health_body["data"]["output_audio"]["tts_available"] is True
                assert health_body["data"]["diagnostic_log"] == {
                    "enabled": False,
                    "status": "disabled",
                    "write_failures": 0,
                }
                integrations = health_body["data"]["series_integrations"]
                assert integrations["identity"]["configured"] is False
                assert integrations["identity"]["status"] == (
                    "trusted_client_id_missing"
                )
                assert integrations["identity"]["default_access"] == "denied"
                assert integrations["identity"]["unity_trusted_source_fields"] is False
                assert integrations["knowledge"]["scope"] == "global"
                assert integrations["knowledge"]["private_scope_enabled"] is False
                assert integrations["environment"]["mode"] == "cached_only"
                assert integrations["voice_audio_output"]["status"] == (
                    "provider_unavailable"
                )
                assert integrations["runtime"]["status"] == "unavailable"
                assert integrations["runtime"]["reason"] == "provider_unavailable"

                session_request = load_json("session_start.request.json")
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201
                assert await created.json() == load_json("session_start.response.json")

                duplicate = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert duplicate.status == 201
                assert await duplicate.json() == load_json(
                    "session_start.response.json"
                )

                events = await client.get(
                    server.url("/events/smoke-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert events.status == 200
                assert events.headers["Content-Type"].startswith("text/event-stream")
                assert (await read_sse_frame(events)).comment == "connected"

                bundle.tts.block_next = True
                interaction = await client.post(
                    server.url("/interaction"),
                    headers=AUTH_HEADERS,
                    json=load_json("interaction.request.json"),
                )
                assert interaction.status == 202
                assert await interaction.json() == load_json(
                    "interaction.response.json"
                )

                intent = await read_sse_frame(events)
                assert intent.event == "avatar.intent"
                assert intent.data == load_json("avatar_intent.event.json")
                await asyncio.wait_for(bundle.tts.started.wait(), timeout=1)

                interrupted = await client.post(
                    server.url("/interrupt"),
                    headers=AUTH_HEADERS,
                    json=load_json("interrupt.request.json"),
                )
                assert interrupted.status == 200
                assert await interrupted.json() == load_json("interrupt.response.json")
                await asyncio.wait_for(bundle.tts.cancelled.wait(), timeout=1)

                observed_after_interrupt: list[str] = []
                while True:
                    try:
                        frame = await read_sse_frame(events, timeout=0.1)
                    except TimeoutError:
                        break
                    if frame.event is not None:
                        observed_after_interrupt.append(frame.event)
                assert set(observed_after_interrupt) <= {"reply.text.delta"}

                closed = await client.post(
                    server.url("/session/close"),
                    headers=AUTH_HEADERS,
                    json=load_json("session_close.request.json"),
                )
                assert closed.status == 200
                assert await closed.json() == load_json("session_close.response.json")
                await asyncio.wait_for(events.content.read(), timeout=1)
                events.close()

        assert bundle.llm.closed is True
        assert bundle.stt.closed is True
        assert bundle.tts.closed is True

    asyncio.run(scenario())


def test_real_http_audio_validation_matches_error_fixtures(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        cases = {
            item["id"]: item
            for item in load_json("audio_flow_cases.json")["request_cases"]
        }
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                session_request = load_json("session_start.request.json")
                session_request["session_id"] = "audio-session"
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201
                started = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json=load_json("audio_turn_start.request.json"),
                )
                assert started.status == 202

                for case_id in (
                    "invalid_base64",
                    "odd_pcm16_byte_count",
                    "non_contiguous_sequence",
                    "wrong_sample_rate",
                    "wrong_channels",
                    "wrong_format",
                    "audio_end_without_data",
                ):
                    case = cases[case_id]
                    endpoint = (
                        "/audio/end"
                        if case_id == "audio_end_without_data"
                        else "/audio/chunk"
                    )
                    response = await client.post(
                        server.url(endpoint),
                        headers=AUTH_HEADERS,
                        json=load_json(case["request_fixture"]),
                    )
                    assert response.status == case["expected_status"], case_id
                    assert await response.json() == case["expected_body"], case_id

                overflow = load_json("audio_chunk.request.json")
                overflow["data"] = base64.b64encode(b"\x00\x00" * 8_001).decode("ascii")
                overflow_response = await client.post(
                    server.url("/audio/chunk"),
                    headers=AUTH_HEADERS,
                    json=overflow,
                )
                overflow_case = cases["chunk_decoded_size_overflow"]
                assert overflow_response.status == overflow_case["expected_status"]
                assert await overflow_response.json() == overflow_case["expected_body"]

                bundle.plugin.sessions.max_audio_bytes = 32_000
                for sequence in range(3):
                    request_body = load_json("audio_chunk.request.json")
                    request_body["sequence"] = sequence
                    request_body["data"] = base64.b64encode(b"\x00\x00" * 8_000).decode(
                        "ascii"
                    )
                    response = await client.post(
                        server.url("/audio/chunk"),
                        headers=AUTH_HEADERS,
                        json=request_body,
                    )
                    if sequence < 2:
                        assert response.status == 202
                    else:
                        total_case = cases["turn_total_size_overflow"]
                        assert response.status == total_case["expected_status"]
                        assert await response.json() == total_case["expected_body"]

                new_turn = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "turn.start",
                        "protocol_version": "1.0",
                        "session_id": "audio-session",
                        "turn_id": "new-audio-turn",
                        "cancel_previous": True,
                    },
                )
                assert new_turn.status == 202
                stale_response = await client.post(
                    server.url("/audio/chunk"),
                    headers=AUTH_HEADERS,
                    json=load_json("audio_chunk.request.json"),
                )
                stale_case = cases["stale_turn_audio"]
                assert stale_response.status == stale_case["expected_status"]
                assert await stale_response.json() == stale_case["expected_body"]

                closed = await client.post(
                    server.url("/session/close"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.close",
                        "protocol_version": "1.0",
                        "session_id": "audio-session",
                    },
                )
                assert closed.status == 200

    asyncio.run(scenario())


def test_unity_conversation_controller_realtime_chain(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    """Exercise the exact HTTP/SSE order used by ConversationController.

    The server is a real TCP aiohttp listener and invokes production handlers;
    only the LLM/STT/TTS boundaries are fake. The voice turn is intentionally
    interrupted while TTS is blocked so the no-late-events guarantee is tested
    on the same path as the Unity client.
    """

    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                session_request = load_json("session_start.request.json")
                session_request["session_id"] = "unity-session"
                health = await client.get(server.url("/health"), headers=AUTH_HEADERS)
                assert health.status == 200
                assert (await health.json())["data"]["protocol_version"] == "1.0"

                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201
                created_body = await created.json()
                assert created_body["data"]["session_id"] == "unity-session"

                events = await client.get(
                    server.url("/events/unity-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert events.status == 200
                assert (await read_sse_frame(events)).comment == "connected"

                text_start = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "turn.start",
                        "protocol_version": "1.0",
                        "session_id": "unity-session",
                        "turn_id": "unity-text-turn",
                        "text": "你好",
                        "cancel_previous": True,
                    },
                )
                assert text_start.status == 202
                assert (await text_start.json())["data"]["state"] == "processing"

                text_frames = [await read_sse_frame(events) for _ in range(4)]
                assert [frame.event for frame in text_frames] == load_json(
                    "manifest.json"
                )["event_order"]["text_success"]
                assert all(
                    frame.data is not None
                    and frame.data["protocol_version"] == "1.0"
                    and frame.data["session_id"] == "unity-session"
                    and frame.data["turn_id"] == "unity-text-turn"
                    for frame in text_frames
                )
                audio_frame = text_frames[2].data
                assert audio_frame is not None
                assert audio_frame["format"] == "pcm16"
                assert audio_frame["sample_rate"] == 24_000
                assert audio_frame["channels"] == 1

                # Unity sends null (or, depending on JsonUtility/runtime, an
                # omitted/empty string) for the voice turn's text field.
                audio_start = load_json("unity_audio_turn_start.request.json")
                started = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json=audio_start,
                )
                assert started.status == 202
                assert (await started.json())["data"]["state"] == "awaiting_audio"

                pcm16 = b"\x00\x00" * 1_280  # Unity's 80 ms @ 16 kHz chunk.
                bundle.stt.expected_pcm16 = pcm16
                bundle.tts.block_next = True
                chunk = await client.post(
                    server.url("/audio/chunk"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "audio.chunk",
                        "protocol_version": "1.0",
                        "session_id": "unity-session",
                        "turn_id": "unity-audio-turn",
                        "sequence": 0,
                        "format": "pcm16",
                        "sample_rate": 16_000,
                        "channels": 1,
                        "data": base64.b64encode(pcm16).decode("ascii"),
                    },
                )
                assert chunk.status == 202
                assert (await chunk.json())["data"]["buffered_bytes"] == len(pcm16)

                ended = await client.post(
                    server.url("/audio/end"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "audio.end",
                        "protocol_version": "1.0",
                        "session_id": "unity-session",
                        "turn_id": "unity-audio-turn",
                    },
                )
                assert ended.status == 202

                asr_final = await read_sse_frame(events)
                avatar_intent = await read_sse_frame(events)
                reply_delta = await read_sse_frame(events)
                assert [
                    asr_final.event,
                    avatar_intent.event,
                    reply_delta.event,
                ] == ["asr.final", "avatar.intent", "reply.text.delta"]
                assert asr_final.data is not None
                assert asr_final.data["text"] == "你好"
                assert all(
                    frame.data is not None
                    and frame.data["session_id"] == "unity-session"
                    and frame.data["turn_id"] == "unity-audio-turn"
                    for frame in (asr_final, avatar_intent, reply_delta)
                )
                await asyncio.wait_for(bundle.tts.started.wait(), timeout=1)

                interrupted = await client.post(
                    server.url("/interrupt"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "interrupt",
                        "protocol_version": "1.0",
                        "session_id": "unity-session",
                        "turn_id": "unity-audio-turn",
                        "reason": "unity_interrupt",
                    },
                )
                assert interrupted.status == 200
                assert (await interrupted.json())["data"]["cancelled"] is True
                await asyncio.wait_for(bundle.tts.cancelled.wait(), timeout=1)

                with pytest.raises(TimeoutError):
                    await read_sse_frame(events, timeout=0.15)

                closed = await client.post(
                    server.url("/session/close"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.close",
                        "protocol_version": "1.0",
                        "session_id": "unity-session",
                    },
                )
                assert closed.status == 200
                events.close()

    asyncio.run(scenario())


def test_real_http_stt_unavailable_and_tts_failure_orders(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    from astrbot_plugin_quest_avatar_bridge.adapters.stt import DisabledSTTAdapter

    async def stt_scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path / "stt")
        bundle.plugin.orchestrator.stt = DisabledSTTAdapter()
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                request_body = load_json("session_start.request.json")
                request_body["session_id"] = "audio-session"
                assert (
                    await client.post(
                        server.url("/session/start"),
                        headers=AUTH_HEADERS,
                        json=request_body,
                    )
                ).status == 201
                events = await client.get(
                    server.url("/events/audio-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert (await read_sse_frame(events)).comment == "connected"
                for endpoint, fixture_name in (
                    ("/turn/start", "audio_turn_start.request.json"),
                    ("/audio/chunk", "audio_chunk.request.json"),
                    ("/audio/end", "audio_end.request.json"),
                ):
                    response = await client.post(
                        server.url(endpoint),
                        headers=AUTH_HEADERS,
                        json=load_json(fixture_name),
                    )
                    assert response.status == 202
                error = await read_sse_frame(events)
                assert error.event == "error"
                assert error.data == load_json("stt_unavailable.event.json")
                terminal = await read_sse_frame(events)
                assert terminal.event == "reply.end"
                assert terminal.data == load_json("reply_end.failed.event.json")
                events.close()

    async def tts_scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path / "tts")
        bundle.tts.fail_next = True
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                request_body = load_json("session_start.request.json")
                request_body["session_id"] = "tts-failure-session"
                assert (
                    await client.post(
                        server.url("/session/start"),
                        headers=AUTH_HEADERS,
                        json=request_body,
                    )
                ).status == 201
                events = await client.get(
                    server.url("/events/tts-failure-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert (await read_sse_frame(events)).comment == "connected"
                started = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "turn.start",
                        "protocol_version": "1.0",
                        "session_id": "tts-failure-session",
                        "turn_id": "tts-failure-turn",
                        "text": "hello",
                        "cancel_previous": True,
                    },
                )
                assert started.status == 202
                frames = [await read_sse_frame(events) for _ in range(4)]
                assert "".join(frame.raw for frame in frames) == (
                    FIXTURES / "tts_failure.events.sse"
                ).read_text(encoding="utf-8")
                assert [frame.event for frame in frames] == load_json(
                    "audio_flow_cases.json"
                )["sse_event_order"]["tts_failed_after_text"]
                events.close()

    async def stt_failure_scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path / "stt-failure")
        bundle.stt.fail_next = True
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                request_body = load_json("session_start.request.json")
                request_body["session_id"] = "audio-session"
                assert (
                    await client.post(
                        server.url("/session/start"),
                        headers=AUTH_HEADERS,
                        json=request_body,
                    )
                ).status == 201
                events = await client.get(
                    server.url("/events/audio-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert (await read_sse_frame(events)).comment == "connected"
                for endpoint, fixture_name in (
                    ("/turn/start", "audio_turn_start.request.json"),
                    ("/audio/chunk", "audio_chunk.request.json"),
                    ("/audio/end", "audio_end.request.json"),
                ):
                    response = await client.post(
                        server.url(endpoint),
                        headers=AUTH_HEADERS,
                        json=load_json(fixture_name),
                    )
                    assert response.status == 202
                error = await read_sse_frame(events)
                assert error.event == "error"
                assert error.data == load_json("stt_failed.event.json")
                terminal = await read_sse_frame(events)
                assert terminal.event == "reply.end"
                assert terminal.data == load_json("reply_end.failed.event.json")
                events.close()

    asyncio.run(stt_scenario())
    asyncio.run(stt_failure_scenario())
    asyncio.run(tts_scenario())


def test_fake_stt_tts_audio_path_matches_sse_fixture(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                session_request = load_json("session_start.request.json")
                session_request["session_id"] = "audio-session"
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201

                events = await client.get(
                    server.url("/events/audio-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert (await read_sse_frame(events)).comment == "connected"

                for endpoint, fixture_name in (
                    ("/turn/start", "audio_turn_start.request.json"),
                    ("/audio/chunk", "audio_chunk.request.json"),
                    ("/audio/end", "audio_end.request.json"),
                ):
                    response = await client.post(
                        server.url(endpoint),
                        headers=AUTH_HEADERS,
                        json=load_json(fixture_name),
                    )
                    assert response.status == 202
                    await response.read()

                frames = [await read_sse_frame(events) for _ in range(5)]
                raw_stream = "".join(frame.raw for frame in frames)
                expected_stream = (FIXTURES / "audio_turn.events.sse").read_text(
                    encoding="utf-8"
                )
                assert raw_stream == expected_stream
                assert [frame.event for frame in frames] == load_json("manifest.json")[
                    "event_order"
                ]["speech_success"]

                close_request = load_json("session_close.request.json")
                close_request["session_id"] = "audio-session"
                closed = await client.post(
                    server.url("/session/close"),
                    headers=AUTH_HEADERS,
                    json=close_request,
                )
                assert closed.status == 200
                events.close()

    asyncio.run(scenario())


def test_sse_reconnect_and_late_old_turn_do_not_leak(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession(
                timeout=ClientTimeout(total=None, connect=2)
            ) as client:
                session_request = load_json("session_start.request.json")
                session_request["session_id"] = "reconnect-session"
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json=session_request,
                )
                assert created.status == 201

                first = await client.get(
                    server.url("/events/reconnect-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert (await read_sse_frame(first)).comment == "connected"
                first.close()

                session = await bundle.plugin.sessions.get_owned(
                    "reconnect-session", f"api_key:{ASTRBOT_API_KEY_ID}"
                )
                for _ in range(100):
                    if not session.stream_attached:
                        break
                    await asyncio.sleep(0.01)
                assert session.stream_attached is False

                second = await client.get(
                    server.url("/events/reconnect-session"),
                    headers={**AUTH_HEADERS, "Accept": "text/event-stream"},
                )
                assert second.status == 200
                assert (await read_sse_frame(second)).comment == "connected"

                old_turn = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "turn.start",
                        "protocol_version": "1.0",
                        "session_id": "reconnect-session",
                        "turn_id": "old-turn",
                        "text": "hold-old-turn",
                        "cancel_previous": True,
                    },
                )
                assert old_turn.status == 202
                await asyncio.wait_for(bundle.llm.late_started.wait(), timeout=1)

                new_turn = await client.post(
                    server.url("/turn/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "turn.start",
                        "protocol_version": "1.0",
                        "session_id": "reconnect-session",
                        "turn_id": "new-turn",
                        "cancel_previous": True,
                    },
                )
                assert new_turn.status == 202
                await asyncio.wait_for(bundle.llm.late_cancelled.wait(), timeout=1)
                bundle.llm.late_release.set()
                await asyncio.sleep(0.05)

                try:
                    leaked = await read_sse_frame(second, timeout=0.15)
                except TimeoutError:
                    leaked = None
                assert leaked is None

                interrupted = await client.post(
                    server.url("/interrupt"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "interrupt",
                        "protocol_version": "1.0",
                        "session_id": "reconnect-session",
                        "turn_id": "new-turn",
                        "reason": "test_cleanup",
                    },
                )
                assert interrupted.status == 200

                closed = await client.post(
                    server.url("/session/close"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.close",
                        "protocol_version": "1.0",
                        "session_id": "reconnect-session",
                    },
                )
                assert closed.status == 200
                second.close()

    asyncio.run(scenario())


def test_network_harness_rejects_missing_auth(monkeypatch: Any, tmp_path: Path) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                missing_astrbot = await client.get(
                    server.url("/health"),
                    headers={"X-Quest-Avatar-Key": BRIDGE_API_KEY},
                )
                assert missing_astrbot.status == 401
                assert (await missing_astrbot.json())["data"]["code"] == (
                    "astrbot_auth_required"
                )

                missing_bridge = await client.get(
                    server.url("/health"),
                    headers={"Authorization": f"Bearer {ASTRBOT_API_TOKEN}"},
                )
                assert missing_bridge.status == 401
                assert (await missing_bridge.json())["data"]["code"] == (
                    "bridge_auth_failed"
                )

    asyncio.run(scenario())
