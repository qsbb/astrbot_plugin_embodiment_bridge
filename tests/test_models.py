from __future__ import annotations

import pytest
from pydantic import ValidationError

from astrbot_plugin_embodiment_bridge.core.intent_parser import IntentParser
from astrbot_plugin_embodiment_bridge.core.models import (
    AudioChunkRequest,
    Emotion,
    Gesture,
    InteractionEvent,
    LookAt,
    SessionStartRequest,
    TurnStartRequest,
)


def test_interaction_schema_rejects_unknown_fields_and_names() -> None:
    payload = {
        "type": "interaction",
        "protocol_version": "1.0",
        "session_id": "s1",
        "event_id": "e9",
        "name": "head_pat",
        "phase": "start",
        "strength": 0.7,
        "duration_ms": 0,
        "hand": "right",
    }
    parsed = InteractionEvent.model_validate(payload)
    assert parsed.name.value == "head_pat"

    with pytest.raises(ValidationError):
        InteractionEvent.model_validate({**payload, "name": "custom_morph"})
    with pytest.raises(ValidationError):
        InteractionEvent.model_validate({**payload, "unity_object": "HeadBone"})


def test_audio_schema_enforces_pcm_contract() -> None:
    payload = {
        "type": "audio.chunk",
        "protocol_version": "1.0",
        "session_id": "s1",
        "turn_id": "t1",
        "sequence": 0,
        "format": "pcm16",
        "sample_rate": 16000,
        "channels": 1,
        "data": "AAAA",
    }
    assert AudioChunkRequest.model_validate(payload).sample_rate == 16000
    with pytest.raises(ValidationError):
        AudioChunkRequest.model_validate({**payload, "sample_rate": 48000})
    with pytest.raises(ValidationError):
        AudioChunkRequest.model_validate({**payload, "channels": 2})


def test_session_schema_rejects_whitespace_only_group_scope() -> None:
    payload = {
        "type": "session.start",
        "protocol_version": "1.0",
        "session_id": "s1",
        "client_id": "quest",
        "user_id": "user",
        "bot_id": "bot",
        "group_id": "",
    }
    assert SessionStartRequest.model_validate(payload).group_id == ""
    with pytest.raises(ValidationError):
        SessionStartRequest.model_validate({**payload, "group_id": "   "})
    with pytest.raises(ValidationError):
        SessionStartRequest.model_validate(
            {**payload, "persona_id": "client-selected-persona"}
        )
    with pytest.raises(ValidationError):
        SessionStartRequest.model_validate(
            {**payload, "persona": {"system_prompt": "client content"}}
        )


def test_turn_start_accepts_unity_text_and_audio_shapes() -> None:
    base = {
        "type": "turn.start",
        "protocol_version": "1.0",
        "session_id": "s1",
        "turn_id": "t1",
        "cancel_previous": True,
    }
    assert TurnStartRequest.model_validate(base).text is None
    assert TurnStartRequest.model_validate({**base, "text": None}).text is None
    assert TurnStartRequest.model_validate({**base, "text": ""}).text is None
    assert TurnStartRequest.model_validate({**base, "text": "x" * 8192}).text == (
        "x" * 8192
    )

    with pytest.raises(ValidationError):
        TurnStartRequest.model_validate({**base, "text": "   "})
    with pytest.raises(ValidationError):
        TurnStartRequest.model_validate({**base, "text": "x" * 8193})


def test_intent_parser_accepts_whitelist_and_rejects_drift() -> None:
    parser = IntentParser()
    valid = parser.parse(
        '{"should_reply":true,"reply_text":"你好",'
        '"intent":{"emotion":"shy","gesture":"step_back",'
        '"look_at":"away","intensity":0.65,"duration_ms":1800,'
        '"reason_code":"boundary_soft_refusal"}}'
    )
    assert valid.intent.emotion is Emotion.SHY
    assert valid.intent.gesture is Gesture.STEP_BACK
    assert valid.intent.look_at is LookAt.AWAY

    unknown = parser.parse(
        '{"should_reply":false,"reply_text":"",'
        '"intent":{"emotion":"furious","gesture":"pmx_bone",'
        '"look_at":"camera","intensity":1,"duration_ms":100,'
        '"reason_code":"bad"}}'
    )
    assert unknown.intent.emotion is Emotion.NEUTRAL
    assert unknown.intent.gesture is Gesture.IDLE
    assert unknown.intent.look_at is LookAt.NONE
    assert unknown.intent.reason_code == "invalid_model_output"


def test_intent_parser_rejects_markdown_wrapped_json() -> None:
    decision = IntentParser().parse(
        '```json\n{"should_reply":false,"reply_text":"","intent":{}}\n```'
    )
    assert decision.intent.reason_code == "invalid_model_output"
