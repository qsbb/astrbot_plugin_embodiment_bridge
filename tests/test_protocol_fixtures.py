from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot_plugin_embodiment_bridge.core.models import (
    ActionResultReason,
    ActionResultRequest,
    ActionResultStatus,
    Emotion,
    Gesture,
    Hand,
    InteractionName,
    InteractionPhase,
    LookAt,
    TurnStartRequest,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "protocol_v1"


def load_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_protocol_manifest_matches_production_enums_and_errors() -> None:
    manifest = load_json("manifest.json")
    assert manifest["protocol_version"] == "1.0"
    assert manifest["optional_extensions"] == {
        "server_timing@1.0": {
            "enabled_by": "server_timing_enabled",
            "location": "reply.end.server_timing",
            "default_present": False,
            "decision_path": ["astrbot_event_bus", "direct_provider"],
            "duration_fields": [
                "stt_ms",
                "decision_ms",
                "tts_first_chunk_ms",
                "tts_total_ms",
                "turn_total_ms",
            ],
            "duration_unit": "milliseconds",
            "duration_minimum": 0,
            "duration_maximum": 86_400_000,
        },
        "action_receipts@1.0": {
            "intent_field": "avatar.intent.action_id",
            "receipt_route": "/action/result",
            "passive_gestures_without_receipts": ["idle", "talk"],
            "terminal_facts": ["completed", "rejected", "interrupted"],
            "storage": "bounded_session_memory",
            "grants_permission": False,
        },
        "action_methods@1.0": {
            "session_capability_field": "session.start.supported_actions",
            "intent_fields": [
                "action_id",
                "method",
                "parameters",
                "transition",
                "source",
            ],
            "selection": "server_client_intersection",
            "legacy_client_excludes": ["crouch", "raise_leg"],
            "one_full_body_action_per_turn": True,
        },
    }
    assert manifest["enums"] == {
        "emotion": [item.value for item in Emotion],
        "gesture": [item.value for item in Gesture],
        "look_at": [item.value for item in LookAt],
        "interaction_name": [item.value for item in InteractionName],
        "interaction_phase": [item.value for item in InteractionPhase],
        "hand": [item.value for item in Hand],
        "action_result_status": [item.value for item in ActionResultStatus],
        "action_result_reason": [item.value for item in ActionResultReason],
    }

    manifest_errors = {
        (item["status"], item["code"]) for item in manifest["http_error_codes"]
    }
    response_errors = {
        (item["status_code"], item["body"]["data"]["code"])
        for item in load_json("errors.json")["responses"]
    }
    assert response_errors == manifest_errors
    assert manifest["sse_error_codes"] == [
        "stt_empty",
        "stt_unavailable",
        "stt_failed",
        "astrbot_pipeline_not_woken",
        "astrbot_pipeline_event_stopped",
        "astrbot_pipeline_reply_capture_empty",
        "astrbot_pipeline_no_response",
        "astrbot_pipeline_empty_reply",
        "turn_failed",
        "interaction_failed",
        "tts_failed",
        "owner_not_configured",
        "quest_identity_not_allowlisted",
        "trusted_platform_not_configured",
    ]
    assert manifest["interrupt_semantics"]["forbidden_after_interrupt_ack"] == [
        "asr.partial",
        "asr.final",
        "reply.text.delta",
        "reply.audio.chunk",
        "avatar.intent",
        "reply.end",
        "error",
    ]
    assert manifest["reconnect_semantics"] == {
        "max_streams_per_session": 1,
        "last_event_id_replay": False,
        "already_consumed_events_replayed": False,
        "queued_unconsumed_critical_events_retained": True,
    }
    assert manifest["session_start_semantics"] == {
        "same_owner_and_identity_is_idempotent_and_reauthorized": True,
        "identity_change_for_existing_session_is_conflict": True,
        "protected_context_status_in_response": True,
        "protected_context_default_access": "denied",
        "api_principal_source": "astrbot_authenticated_request",
        "trusted_client_id_source": "bridge_server_config",
        "trusted_platform_id_source": "bridge_server_config",
        "unity_trusted_source_fields": False,
        "supported_actions_optional": True,
        "omitted_supported_actions_mode": "legacy",
    }
    assert manifest["spatial_context_semantics"] == {
        "schema_version": 1,
        "storage": "session_memory_only",
        "revision": "strictly_increasing_or_identical_idempotent",
        "injection_scope": "authorized_embodiment_eventbus_turn_only",
        "contains_free_text": False,
        "contains_geometry_or_identifiers": False,
        "grants_permission": False,
    }
    assert manifest["action_result_semantics"] == {
        "server_generated_action_id": True,
        "transitions": {
            "planned": ["accepted", "rejected", "interrupted"],
            "accepted": ["started", "rejected", "interrupted"],
            "started": ["completed", "rejected", "interrupted"],
            "completed": [],
            "rejected": [],
            "interrupted": [],
        },
        "exact_duplicate_is_idempotent": True,
        "changed_receipt_replay_is_conflict": True,
        "terminal_fact_injection_scope": (
            "later_authorized_embodiment_eventbus_turns_only"
        ),
        "planned_is_fact": False,
        "grants_permission": False,
    }
    assert manifest["pairing_bootstrap_semantics"] == {
        "register_web_api_anonymous_supported": False,
        "page_create_authentication": "astrbot_dashboard",
        "builtin_listener_anonymous_exact_path_only": True,
        "astrbot_extensions_exchange_requires_outer_auth": True,
        "legacy_reverse_proxy_supported": True,
        "exchange_credential": "single_use_token_or_six_digit_code",
        "expected_remote_ip_bound_per_session": True,
        "per_source_rate_limit": True,
        "global_rate_limit": True,
        "private_http_requires_server_and_session_opt_in": True,
        "public_network_requires_https": True,
    }
    assert manifest["series_integration_semantics"] == {
        "knowledge_scope": "global_only",
        "relationship_requires_identity_authorization": True,
        "environment_mode": "cached_only",
        "voice_contract": "voice.audio_output@1.0",
        "runtime_refresh": "startup_and_explicit_health",
        "conversation_proactive_delivery_consumed": False,
        "orchestration_hub_resolver_consumed": False,
    }
    assert manifest["unity_conversation_controller"] == {
        "text_max_characters": 8192,
        "audio_turn_text_forms_accepted": ["omitted", "null", "empty_string"],
        "recommended_capture_chunk_ms": 80,
        "recommended_capture_chunk_bytes": 2560,
        "input_sequence_first": 0,
        "input_sequence_step": 1,
        "interrupt_before_new_turn": True,
        "stale_events_after_interrupt_ack": "discard_by_session_and_turn",
    }
    assert manifest["audio"] == {
        "input": {
            "format": "pcm16",
            "byte_order": "little_endian",
            "sample_rate": 16000,
            "channels": 1,
            "sample_width_bytes": 2,
            "recommended_chunk_ms": {"minimum": 40, "maximum": 100},
            "sequence_first": 0,
            "sequence_step": 1,
        },
        "output": {
            "format": "pcm16",
            "byte_order": "little_endian",
            "sample_rate": 24000,
            "channels": 1,
            "sample_width_bytes": 2,
            "encoding": "base64_in_sse_data",
        },
    }
    assert manifest["critical_events"] == [
        "asr.final",
        "avatar.intent",
        "reply.audio.chunk",
        "reply.end",
        "error",
    ]
    assert manifest["droppable_events"] == ["asr.partial", "reply.text.delta"]
    assert manifest["backpressure_semantics"] == {
        "bounded_per_session_queue": True,
        "asr_partial_coalesced_per_turn": True,
        "droppable_when_full": ["asr.partial", "reply.text.delta"],
        "never_dropped_to_admit_noncritical": [
            "asr.final",
            "avatar.intent",
            "reply.audio.chunk",
            "reply.end",
            "error",
        ],
        "critical_producer_waits_when_only_critical_events_remain": True,
    }


def test_json_request_and_event_fixtures_are_protocol_v1() -> None:
    for path in FIXTURES.glob("*.request.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["protocol_version"] == "1.0", path.name
        assert isinstance(payload["type"], str), path.name

    unity_audio_start = load_json("unity_audio_turn_start.request.json")
    assert TurnStartRequest.model_validate(unity_audio_start).text is None
    assert ActionResultRequest.model_validate(
        load_json("action_result.request.json")
    ).status is ActionResultStatus.ACCEPTED
    action_response = load_json("action_result.response.json")
    assert action_response == {
        "status": "ok",
        "data": {
            "protocol_version": "1.0",
            "session_id": "smoke-session",
            "turn_id": "i:e9",
            "action_id": "a_contract-action",
            "action": "step_back",
            "lifecycle_status": "accepted",
            "terminal": False,
            "idempotent": False,
        },
    }

    for path in FIXTURES.glob("*.event.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["protocol_version"] == "1.0", path.name
        assert isinstance(payload["type"], str), path.name


def test_sse_fixture_is_parseable_and_has_stable_event_order() -> None:
    assert (
        _sse_event_types("audio_turn.events.sse")
        == load_json("manifest.json")["event_order"]["speech_success"]
    )
    assert (
        _sse_event_types("tts_failure.events.sse")
        == load_json("manifest.json")["event_order"]["tts_failed_after_text"]
    )


def test_audio_flow_cases_reference_stable_fixtures_and_errors() -> None:
    cases = load_json("audio_flow_cases.json")
    manifest = load_json("manifest.json")
    assert cases["protocol_version"] == manifest["protocol_version"]
    assert cases["input"]["sample_rate"] == manifest["audio"]["input"]["sample_rate"]
    assert cases["output"]["sample_rate"] == manifest["audio"]["output"]["sample_rate"]
    ids = {item["id"] for item in cases["request_cases"]}
    assert ids == {
        "invalid_base64",
        "odd_pcm16_byte_count",
        "non_contiguous_sequence",
        "wrong_sample_rate",
        "wrong_channels",
        "wrong_format",
        "chunk_decoded_size_overflow",
        "turn_total_size_overflow",
        "audio_end_without_data",
        "stale_turn_audio",
    }
    for item in cases["request_cases"]:
        assert item["expected_body"]["status"] == "error"
        assert item["expected_body"]["data"]["code"] in {
            "invalid_audio",
            "payload_too_large",
            "schema_validation_failed",
            "session_conflict",
        }
        fixture_name = item.get("request_fixture")
        if fixture_name:
            assert (FIXTURES / fixture_name).is_file()
    assert cases["sse_event_order"] == {
        "stt_unavailable": ["error", "reply.end"],
        "stt_failed": ["error", "reply.end"],
        "tts_failed_after_text": [
            "avatar.intent",
            "reply.text.delta",
            "error",
            "reply.end",
        ],
        "interrupted_after_ack": [],
    }


def _sse_event_types(name: str) -> list[str]:
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    frames = [frame for frame in raw.split("\n\n") if frame]
    event_types: list[str] = []
    for frame in frames:
        lines = frame.splitlines()
        event_type = next(
            line.removeprefix("event:").strip()
            for line in lines
            if line.startswith("event:")
        )
        data = json.loads(
            next(
                line.removeprefix("data:").strip()
                for line in lines
                if line.startswith("data:")
            )
        )
        assert data["type"] == event_type
        assert data["protocol_version"] == "1.0"
        event_types.append(event_type)
    return event_types
