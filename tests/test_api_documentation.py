from __future__ import annotations

from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_frontend_api_document_covers_public_protocol() -> None:
    document = (PLUGIN_ROOT / "docs" / "API_CN.md").read_text(encoding="utf-8")

    for route in (
        "/session/start",
        "/events/<session_id>",
        "/turn/start",
        "/audio/chunk",
        "/audio/end",
        "/interaction",
        "/interrupt",
        "/session/close",
        "/health",
    ):
        assert route in document

    assert "Authorization: Bearer <ASTRBOT_API_KEY_WITH_PLUGIN_SCOPE>" in document
    assert "X-Quest-Avatar-Key: <bridge_api_key>" in document

    for event_type in (
        "asr.partial",
        "asr.final",
        "reply.text.delta",
        "reply.audio.chunk",
        "avatar.intent",
        "reply.end",
        "error",
    ):
        assert f"`{event_type}`" in document

    for allowed_value in (
        "neutral | happy | shy | surprised | concerned | uncomfortable",
        "idle | talk | wave | bow | handshake | head_pat | cheek_pinch | refuse | step_back",
        "user | hand | away | none",
    ):
        assert allowed_value in document
    for error_code in (
        "stt_empty",
        "stt_unavailable",
        "stt_failed",
        "turn_failed",
        "interaction_failed",
        "tts_failed",
    ):
        assert f"`{error_code}`" in document


def test_local_integration_document_keeps_security_and_fixture_contract() -> None:
    document = (PLUGIN_ROOT / "docs" / "LOCAL_INTEGRATION_CN.md").read_text(
        encoding="utf-8"
    )
    for required_text in (
        "127.0.0.1",
        "Quest 中的 `127.0.0.1` 指向头显自身",
        "Authorization: Bearer",
        "X-Quest-Avatar-Key",
        "plugin` scope",
        "fixtures/protocol_v1/manifest.json",
        "不启用宽泛 CORS",
        "fake 只替代外部 LLM/STT/TTS",
    ):
        assert required_text in document
