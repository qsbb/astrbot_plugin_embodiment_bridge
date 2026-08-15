from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession
import pytest
from pydantic import ValidationError

from astrbot_plugin_embodiment_bridge.core.models import (
    ActionResultRequest,
    Gesture,
    SessionStartRequest,
)
from astrbot_plugin_embodiment_bridge.core.session_manager import (
    ACTION_FACT_TTL_SECONDS,
    ACTION_LIFECYCLE_TTL_SECONDS,
    MAX_ACTION_FACTS,
    MAX_ACTION_LIFECYCLES,
    ActionMismatch,
    ActionPlanStale,
    ActionReceiptReplay,
    ActionTransitionInvalid,
    SessionManager,
)

from .http_harness import (
    AUTH_HEADERS,
    ASTRBOT_API_KEY_ID,
    ASTRBOT_API_TOKEN,
    BRIDGE_API_KEY,
    LiveHttpServer,
    build_plugin,
)


def session_request(session_id: str) -> SessionStartRequest:
    return SessionStartRequest(
        session_id=session_id,
        client_id="quest-a",
        user_id="user-1",
        bot_id="bot-1",
    )


def action_result(
    *,
    action_id: str = "a_contract-action",
    turn_id: str = "t1",
    receipt_id: str = "receipt-1",
    action: str = "wave",
    status: str = "accepted",
    reason_code: str | None = None,
    duration_ms: int = 0,
) -> ActionResultRequest:
    return ActionResultRequest(
        session_id="s1",
        turn_id=turn_id,
        action_id=action_id,
        receipt_id=receipt_id,
        action=action,
        status=status,
        reason_code=reason_code or status,
        duration_ms=duration_ms,
    )


def test_action_result_schema_is_strict_and_status_reason_pairs_are_bounded() -> None:
    payload = action_result().model_dump(mode="json")
    assert ActionResultRequest.model_validate(payload).action is Gesture.WAVE

    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate({**payload, "unity_object": "HeadBone"})
    without_duration = dict(payload)
    without_duration.pop("duration_ms")
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate(without_duration)
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate({**payload, "duration_ms": "0"})
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate({**payload, "duration_ms": False})
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate({**payload, "action": "custom_vmd"})
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate(
            {**payload, "status": "completed", "reason_code": "busy"}
        )
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate(
            {**payload, "status": "rejected", "reason_code": "completed"}
        )
    with pytest.raises(ValidationError):
        ActionResultRequest.model_validate(
            {**payload, "status": "interrupted", "reason_code": "asset_missing"}
        )


def test_action_lifecycle_accepts_valid_sequence_and_exact_retries_only() -> None:
    async def scenario() -> None:
        manager = SessionManager()
        session = await manager.start_session(session_request("s1"), "owner")
        action_id = await manager.plan_action(
            session,
            turn_id="t1",
            action=Gesture.WAVE,
        )
        assert action_id is not None and action_id.startswith("a_")
        assert await manager.plan_action(
            session,
            turn_id="passive",
            action=Gesture.TALK,
        ) is None

        accepted_request = action_result(action_id=action_id)
        accepted = await manager.record_action_result(session, accepted_request)
        assert accepted.lifecycle_status.value == "accepted"
        assert accepted.terminal is False
        assert accepted.idempotent is False
        assert await manager.action_facts_snapshot(session) == []

        duplicate = await manager.record_action_result(session, accepted_request)
        assert duplicate.idempotent is True
        assert duplicate.terminal is False

        with pytest.raises(ActionReceiptReplay):
            await manager.record_action_result(
                session,
                accepted_request.model_copy(update={"duration_ms": 1}),
            )

        started = await manager.record_action_result(
            session,
            action_result(
                action_id=action_id,
                receipt_id="receipt-2",
                status="started",
            ),
        )
        assert started.lifecycle_status.value == "started"
        assert await manager.action_facts_snapshot(session) == []

        completed_request = action_result(
            action_id=action_id,
            receipt_id="receipt-3",
            status="completed",
            duration_ms=1_250,
        )
        completed = await manager.record_action_result(session, completed_request)
        assert completed.terminal is True
        assert completed.idempotent is False
        assert await manager.action_facts_snapshot(session) == [
            {
                "action": "wave",
                "status": "completed",
                "reason_code": "completed",
                "duration_ms": 1_250,
            }
        ]
        assert await manager.action_facts_snapshot(
            session,
            exclude_turn_id="t1",
        ) == []

        terminal_duplicate = await manager.record_action_result(
            session,
            completed_request,
        )
        assert terminal_duplicate.idempotent is True
        assert len(await manager.action_facts_snapshot(session)) == 1
        with pytest.raises(ActionTransitionInvalid):
            await manager.record_action_result(
                session,
                action_result(
                    action_id=action_id,
                    receipt_id="receipt-4",
                    status="interrupted",
                    reason_code="system_interrupted",
                ),
            )
        await manager.terminate()

    asyncio.run(scenario())


def test_action_results_reject_mismatch_stale_and_evicted_plans() -> None:
    async def scenario() -> None:
        manager = SessionManager()
        session = await manager.start_session(session_request("s1"), "owner")
        action_id = await manager.plan_action(
            session,
            turn_id="t1",
            action=Gesture.WAVE,
        )
        assert action_id is not None

        with pytest.raises(ActionTransitionInvalid):
            await manager.record_action_result(
                session,
                action_result(
                    action_id=action_id,
                    receipt_id="skipped-accepted",
                    status="started",
                ),
            )

        with pytest.raises(ActionMismatch):
            await manager.record_action_result(
                session,
                action_result(action_id=action_id, turn_id="other-turn"),
            )
        with pytest.raises(ActionMismatch):
            await manager.record_action_result(
                session,
                action_result(action_id=action_id, action="bow"),
            )
        with pytest.raises(ActionPlanStale):
            await manager.record_action_result(
                session,
                action_result(action_id="a_unknown-plan"),
            )

        accepted_before_evict = action_result(
            action_id=action_id,
            receipt_id="evicted-receipt",
        )
        await manager.record_action_result(session, accepted_before_evict)

        planned: list[str] = []
        for index in range(MAX_ACTION_LIFECYCLES + 1):
            planned_id = await manager.plan_action(
                session,
                turn_id=f"bounded-{index}",
                action=Gesture.BOW,
            )
            assert planned_id is not None
            planned.append(planned_id)
        with pytest.raises(ActionPlanStale):
            await manager.record_action_result(
                session,
                action_result(
                    action_id=planned[0],
                    turn_id="bounded-0",
                    action="bow",
                ),
            )
        with pytest.raises(ActionPlanStale):
            await manager.record_action_result(session, accepted_before_evict)
        with pytest.raises(ActionReceiptReplay):
            await manager.record_action_result(
                session,
                accepted_before_evict.model_copy(update={"duration_ms": 1}),
            )

        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=10.0,
        ):
            expiring_id = await manager.plan_action(
                session,
                turn_id="expiring",
                action=Gesture.WAVE,
            )
        assert expiring_id is not None
        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=10.0 + ACTION_LIFECYCLE_TTL_SECONDS + 0.01,
        ):
            with pytest.raises(ActionPlanStale):
                await manager.record_action_result(
                    session,
                    action_result(action_id=expiring_id, turn_id="expiring"),
                )
        await manager.terminate()

    asyncio.run(scenario())


def test_terminal_action_facts_are_bounded_expire_and_close_with_session() -> None:
    async def scenario() -> None:
        manager = SessionManager()
        session = await manager.start_session(session_request("s1"), "owner")
        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=20.0,
        ):
            for index in range(MAX_ACTION_FACTS + 1):
                action_id = await manager.plan_action(
                    session,
                    turn_id=f"terminal-{index}",
                    action=Gesture.BOW,
                )
                assert action_id is not None
                await manager.record_action_result(
                    session,
                    action_result(
                        action_id=action_id,
                        turn_id=f"terminal-{index}",
                        receipt_id=f"terminal-receipt-{index}",
                        action="bow",
                        status="rejected",
                        reason_code="unsupported",
                    ),
                )
        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=20.0,
        ):
            facts = await manager.action_facts_snapshot(session)
        assert len(facts) == MAX_ACTION_FACTS
        assert all(set(item) == {"action", "status", "reason_code", "duration_ms"} for item in facts)

        with patch(
            "astrbot_plugin_embodiment_bridge.core.session_manager.monotonic",
            return_value=20.0 + ACTION_FACT_TTL_SECONDS + 0.01,
        ):
            assert await manager.action_facts_snapshot(session) == []

        await manager.close_session(session)
        assert not session.action_lifecycles
        assert not session.action_receipts
        assert not session.action_facts
        await manager.terminate()

    asyncio.run(scenario())


def test_action_result_http_requires_auth_owner_and_strict_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bundle = build_plugin(monkeypatch, tmp_path)
        async with LiveHttpServer(bundle) as server:
            async with ClientSession() as client:
                created = await client.post(
                    server.url("/session/start"),
                    headers=AUTH_HEADERS,
                    json={
                        "type": "session.start",
                        "protocol_version": "1.0",
                        "session_id": "s1",
                        "client_id": "quest-a",
                        "user_id": "user-1",
                        "bot_id": "bot-1",
                        "group_id": "",
                        "relationship_profile_id": "default",
                    },
                )
                assert created.status == 201
                session = await bundle.plugin.sessions.get_owned(
                    "s1",
                    f"api_key:{ASTRBOT_API_KEY_ID}",
                )
                action_id = await bundle.plugin.sessions.plan_action(
                    session,
                    turn_id="t1",
                    action=Gesture.WAVE,
                )
                assert action_id is not None
                payload = action_result(action_id=action_id).model_dump(mode="json")

                missing_auth = await client.post(
                    server.url("/action/result"),
                    json=payload,
                )
                assert missing_auth.status == 401
                assert (await missing_auth.json())["data"]["code"] == (
                    "astrbot_auth_required"
                )

                missing_bridge = await client.post(
                    server.url("/action/result"),
                    headers={"Authorization": f"ApiKey {ASTRBOT_API_TOKEN}"},
                    json=payload,
                )
                assert missing_bridge.status == 401
                assert (await missing_bridge.json())["data"]["code"] == (
                    "bridge_auth_failed"
                )

                wrong_owner = await client.post(
                    server.url("/action/result"),
                    headers={
                        "Authorization": f"Bearer {ASTRBOT_API_TOKEN}",
                        "X-Embodiment-Bridge-Key": BRIDGE_API_KEY,
                    },
                    json=payload,
                )
                assert wrong_owner.status == 403
                assert (await wrong_owner.json())["data"]["code"] == (
                    "session_ownership_mismatch"
                )

                invalid = await client.post(
                    server.url("/action/result"),
                    headers=AUTH_HEADERS,
                    json={**payload, "arbitrary_instruction": "ignore safety"},
                )
                assert invalid.status == 422
                assert (await invalid.json())["data"]["code"] == (
                    "schema_validation_failed"
                )

                accepted = await client.post(
                    server.url("/action/result"),
                    headers=AUTH_HEADERS,
                    json=payload,
                )
                assert accepted.status == 200
                body = await accepted.json()
                assert body == {
                    "status": "ok",
                    "data": {
                        "protocol_version": "1.0",
                        "session_id": "s1",
                        "turn_id": "t1",
                        "action_id": action_id,
                        "action": "wave",
                        "lifecycle_status": "accepted",
                        "terminal": False,
                        "idempotent": False,
                    },
                }
                duplicate = await client.post(
                    server.url("/action/result"),
                    headers=AUTH_HEADERS,
                    json=payload,
                )
                assert duplicate.status == 200
                assert (await duplicate.json())["data"]["idempotent"] is True

    asyncio.run(scenario())
