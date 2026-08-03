from __future__ import annotations

import asyncio
from typing import Any

from astrbot_plugin_quest_avatar_bridge.adapters.stt import DisabledSTTAdapter
from astrbot_plugin_quest_avatar_bridge.adapters.tts import DisabledTTSAdapter
from astrbot_plugin_quest_avatar_bridge.core.interaction_policy import InteractionPolicy
from astrbot_plugin_quest_avatar_bridge.core.models import (
    SessionStartRequest,
    TurnStartRequest,
    safe_neutral_decision,
)
from astrbot_plugin_quest_avatar_bridge.core.session_manager import SessionManager
from astrbot_plugin_quest_avatar_bridge.core.turn_orchestrator import TurnOrchestrator


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


class CapturingLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(self, **kwargs: Any):
        self.calls.append(kwargs)
        return safe_neutral_decision("context_test")

    async def close(self) -> None:
        pass


class RelationshipStub:
    def __init__(self) -> None:
        self.calls = 0

    async def read(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"version": "1.0", "relationship_tier": "close"}

    async def close(self) -> None:
        pass


class KnowledgeStub:
    def __init__(self) -> None:
        self.calls = 0
        self.status = "ok"

    async def recall(self, query: str) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"content": query}]

    async def close(self) -> None:
        pass


class EnvironmentStub:
    def __init__(self) -> None:
        self.calls = 0
        self.status = "ok"

    async def read(self) -> dict[str, Any]:
        self.calls += 1
        return {"kind": "cached"}

    async def close(self) -> None:
        pass


async def wait_for_end(session: Any) -> None:
    while True:
        event = await asyncio.wait_for(session.queue.get(), timeout=1)
        if event.event_type == "reply.end":
            return


def test_relationship_is_authorization_gated_but_public_context_still_flows() -> None:
    async def scenario(authorized: bool) -> tuple[Any, ...]:
        sessions = SessionManager(interaction_debounce_ms=0)
        session = await sessions.start_session(
            SessionStartRequest(
                session_id="s1",
                client_id="quest",
                user_id="user",
                bot_id="bot",
            ),
            "astrbot-api",
            protected_context_authorized=authorized,
            context_authorization_reason=(
                "authorized_private_owner_identity" if authorized else "denied"
            ),
        )
        llm = CapturingLLM()
        relationship = RelationshipStub()
        knowledge = KnowledgeStub()
        environment = EnvironmentStub()
        orchestrator = TurnOrchestrator(
            sessions=sessions,
            llm=llm,
            stt=DisabledSTTAdapter(),
            tts=DisabledTTSAdapter(),
            relationship=relationship,
            policy=InteractionPolicy(gesture_cooldown_seconds=0),
            logger=LoggerStub(),
            knowledge=knowledge,
            environment=environment,
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t1", text="hello"),
        )
        await wait_for_end(session)
        captured = llm.calls[0]
        await orchestrator.close()
        return relationship, knowledge, environment, captured

    async def run() -> None:
        relationship, knowledge, environment, captured = await scenario(False)
        assert relationship.calls == 0
        assert captured["relationship"] is None
        assert knowledge.calls == environment.calls == 1
        assert captured["knowledge"] == [{"content": "hello"}]
        assert captured["environment"] == {"kind": "cached"}

        relationship, knowledge, environment, captured = await scenario(True)
        assert relationship.calls == 1
        assert captured["relationship"]["relationship_tier"] == "close"
        assert knowledge.calls == environment.calls == 1

    asyncio.run(run())
