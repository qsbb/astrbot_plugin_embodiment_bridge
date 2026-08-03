from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from .http_harness import (
    AUTH_HEADERS,
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
                assert duplicate.status == 409
                assert await duplicate.json() == load_json(
                    "duplicate_session.error.json"
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
            item["id"]: item for item in load_json("audio_flow_cases.json")["request_cases"]
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
                overflow["data"] = base64.b64encode(b"\x00\x00" * 8_001).decode(
                    "ascii"
                )
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
                    request_body["data"] = base64.b64encode(
                        b"\x00\x00" * 8_000
                    ).decode("ascii")
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
                    "reconnect-session", f"api_key:{ASTRBOT_API_TOKEN}"
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
