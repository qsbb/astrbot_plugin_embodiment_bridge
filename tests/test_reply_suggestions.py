"""M5 reply.suggestions：后处理建议事件与 ReplySuggestionService 单测。

覆盖：
- 解析：纯 JSON 数组、带护栏文本的 JSON、垃圾输出 → 空、超量截断、超长截断
- 超时/失败/禁用 → 空列表且不抛
- orchestrator 集成：reply.end 之后异步下发 suggestions 事件（不阻塞回合）
- 空回复（无文本）不触发建议
- 失败静默：无事件、无 error 事件
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot_plugin_embodiment_bridge.adapters.stt import DisabledSTTAdapter
from astrbot_plugin_embodiment_bridge.core.interaction_policy import InteractionPolicy
from astrbot_plugin_embodiment_bridge.core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    SessionStartRequest,
    TurnStartRequest,
    safe_neutral_decision,
)
from astrbot_plugin_embodiment_bridge.core.reply_suggestions import (
    MAX_SUGGESTIONS,
    ReplySuggestionService,
)
from astrbot_plugin_embodiment_bridge.core.session_manager import SessionManager
from astrbot_plugin_embodiment_bridge.core.turn_orchestrator import TurnOrchestrator


class LoggerStub:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


class DiagnosticStub:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.records.append((event, fields))


class RelationshipStub:
    async def read(self, **kwargs: Any) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        pass


class TTSStub:
    available = False

    async def synthesize(self, text: str, *, emotion: str) -> Any:
        yield b"\x00\x00" * 100  # pragma: no cover - unused

    async def close(self) -> None:
        pass


class DecisionStub:
    def __init__(self, decision: ModelDecision) -> None:
        self.decision = decision

    async def generate(self, **kwargs: Any) -> ModelDecision:
        return self.decision

    async def close(self) -> None:
        pass


class SuggestionStub:
    """Orchestrator-facing ReplySuggestionService 替身。"""

    def __init__(self, suggestions: list[str]) -> None:
        self.suggestions = suggestions
        self.calls: int = 0
        self.last_history: list[dict[str, str]] | None = None

    async def generate(self, history: list[dict[str, str]]) -> list[str]:
        self.calls += 1
        self.last_history = history
        return self.suggestions


class FailingSuggestionStub(SuggestionStub):
    async def generate(self, history: list[dict[str, str]]) -> list[str]:
        self.calls += 1
        raise RuntimeError("provider offline")


class SlowSuggestionStub(SuggestionStub):
    def __init__(self) -> None:
        super().__init__([])
        self.cancelled = asyncio.Event()

    async def generate(self, history: list[dict[str, str]]) -> list[str]:
        self.calls += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return []


def _decision(reply_text: str) -> ModelDecision:
    return safe_neutral_decision("reason").model_copy(
        update={"should_reply": True, "reply_text": reply_text}
    )


async def build_orchestrator(
    llm: Any,
    reply_suggestions: Any | None = None,
):
    sessions = SessionManager(event_queue_size=64, interaction_debounce_ms=0)
    session = await sessions.start_session(
        SessionStartRequest(
            session_id="s1",
            client_id="phone",
            user_id="user",
            bot_id="bot",
            supported_actions=("wave",),
        ),
        "owner",
    )
    orchestrator = TurnOrchestrator(
        sessions=sessions,
        llm=llm,
        stt=DisabledSTTAdapter(),
        tts=TTSStub(),
        relationship=RelationshipStub(),
        policy=InteractionPolicy(gesture_cooldown_seconds=0),
        logger=LoggerStub(),
        diagnostic_log=DiagnosticStub(),
        reply_suggestions=reply_suggestions,
    )
    return sessions, session, orchestrator


async def collect_until_end(session: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        item = await asyncio.wait_for(session.queue.get(), timeout=1)
        events.append(item.payload)
        if item.event_type in {"reply.end", "error"}:
            return events


async def drain_all(session: Any, timeout: float = 1.0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        while True:
            item = await asyncio.wait_for(session.queue.get(), timeout=timeout)
            events.append(item.payload)
    except asyncio.TimeoutError:
        return events


# ───────────────────────── service unit tests ─────────────────────────


def test_parse_plain_json_array() -> None:
    assert ReplySuggestionService._parse('["好啊","然后呢","哈哈哈"]') == [
        "好啊",
        "然后呢",
        "哈哈哈",
    ]


def test_parse_json_inside_guard_text() -> None:
    raw = '好的，以下是建议：\n["好啊","然后呢","哈哈哈"]\n希望有帮助。'
    assert ReplySuggestionService._parse(raw) == ["好啊", "然后呢", "哈哈哈"]


def test_parse_garbage_returns_empty() -> None:
    assert ReplySuggestionService._parse("随便说点什么") == []
    assert ReplySuggestionService._parse("") == []
    assert ReplySuggestionService._parse("[1, 2, 3]") == []
    assert ReplySuggestionService._parse('[""]') == []


def test_parse_caps_at_three_and_trims() -> None:
    raw = '[" a ", "b", "c", "d"]'
    assert ReplySuggestionService._parse(raw) == ["a", "b", "c"]
    long = "字" * 500
    assert len(ReplySuggestionService._parse(f'["{long}"]')[0]) == 200


def test_generate_disabled_returns_empty() -> None:
    service = ReplySuggestionService(object(), enabled=False)
    result = asyncio.run(service.generate([{"role": "user", "text": "hi"}]))
    assert result == []


def test_generate_empty_history_returns_empty_without_llm() -> None:
    class ExplodingContext:
        def llm_generate(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("must not be called")

    service = ReplySuggestionService(ExplodingContext())
    result = asyncio.run(service.generate([]))
    assert result == []


def test_generate_timeout_returns_empty() -> None:
    class SlowContext:
        async def llm_generate(self, **kwargs: Any) -> str:
            await asyncio.sleep(30)
            return "[]"  # pragma: no cover

    service = ReplySuggestionService(SlowContext(), timeout_seconds=0.05)
    result = asyncio.run(service.generate([{"role": "user", "text": "hi"}]))
    assert result == []
    assert service.last_status == "timeout"


def test_generate_provider_failure_returns_empty() -> None:
    class BrokenContext:
        async def llm_generate(self, **kwargs: Any) -> str:
            raise RuntimeError("offline")

    service = ReplySuggestionService(BrokenContext())
    result = asyncio.run(service.generate([{"role": "user", "text": "hi"}]))
    assert result == []
    assert service.last_status == "failed"


def test_generate_success_via_context_llm() -> None:
    class OkContext:
        async def llm_generate(self, **kwargs: Any) -> str:
            assert kwargs.get("prompt")
            return '["好呀","嗯嗯","去哪玩"]'

    service = ReplySuggestionService(OkContext(), provider_id="p1")
    result = asyncio.run(
        service.generate(
            [
                {"role": "user", "text": "你好"},
                {"role": "assistant", "text": "我在呢"},
            ]
        )
    )
    assert result == ["好呀", "嗯嗯", "去哪玩"]
    assert service.last_status == "emitted"


def test_prompt_contains_recent_window_only() -> None:
    class CaptureContext:
        last_prompt: str = ""

        async def llm_generate(self, **kwargs: Any) -> str:
            CaptureContext.last_prompt = str(kwargs.get("prompt", ""))
            return "[]"

    history = (
        [{"role": "user", "text": f"第{i}条历史"} for i in range(1, 11)]
        + [{"role": "assistant", "text": "最后一行"}]
    )
    service = ReplySuggestionService(CaptureContext())
    asyncio.run(service.generate(history))
    assert "第1条历史" not in CaptureContext.last_prompt
    assert "第5条历史" not in CaptureContext.last_prompt
    assert "第6条历史" in CaptureContext.last_prompt
    assert "最后一行" in CaptureContext.last_prompt


# ───────────────────── orchestrator integration tests ─────────────────────


def test_suggestions_emitted_after_reply_end() -> None:
    async def scenario() -> None:
        stub = SuggestionStub(["好啊", "那然后呢", "哈哈哈"])
        _sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(_decision("普通回复")), reply_suggestions=stub
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-suggest", text="你好"),
        )
        events = await collect_until_end(session)
        assert events[-1]["type"] == "reply.end"
        # 建议事件在 reply.end 之后、作为独立事件到达。
        rest = await drain_all(session, timeout=0.5)
        types = [event["type"] for event in rest]
        assert "reply.suggestions" in types
        suggestions_event = next(
            event for event in rest if event["type"] == "reply.suggestions"
        )
        assert suggestions_event["suggestions"] == ["好啊", "那然后呢", "哈哈哈"]
        assert suggestions_event["turn_id"] == "t-suggest"
        assert stub.calls == 1
        await orchestrator.close()

    asyncio.run(scenario())


def test_suggestions_not_spawned_when_reply_has_no_text() -> None:
    async def scenario() -> None:
        stub = SuggestionStub(["不应该出现"])
        _sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(_decision("")), reply_suggestions=stub
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-silent", text="你好"),
        )
        await collect_until_end(session)
        rest = await drain_all(session, timeout=0.3)
        assert all(event["type"] != "reply.suggestions" for event in rest)
        assert stub.calls == 0
        await orchestrator.close()

    asyncio.run(scenario())


def test_suggestion_failure_is_silent() -> None:
    async def scenario() -> None:
        stub = FailingSuggestionStub(["x"])
        _sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(_decision("普通回复")), reply_suggestions=stub
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-fail", text="你好"),
        )
        events = await collect_until_end(session)
        rest = await drain_all(session, timeout=0.5)
        # 无建议事件、无 error 事件：失败只进诊断，不进协议流。
        assert all(
            event["type"] not in {"reply.suggestions", "error"} for event in rest
        )
        assert events[-1]["type"] == "reply.end"
        assert stub.calls == 1
        await orchestrator.close()

    asyncio.run(scenario())


def test_new_turn_cancels_pending_suggestion_task() -> None:
    async def scenario() -> None:
        stub = SlowSuggestionStub()
        _sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(_decision("普通回复")), reply_suggestions=stub
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-one", text="你好"),
        )
        await collect_until_end(session)
        # 让 reply.end 之后的 spawn 代码先执行（fire-and-forget 任务落地）。
        await asyncio.sleep(0.1)
        # 慢建议任务仍挂着；开新回合应将其取消。
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-two", text="再问一次"),
        )
        await collect_until_end(session)
        await asyncio.wait_for(stub.cancelled.wait(), timeout=1)
        await orchestrator.close()

    asyncio.run(scenario())


def test_suggestions_capped_by_service_constant() -> None:
    async def scenario() -> None:
        stub = SuggestionStub(["1", "2", "3", "4", "5"])
        _sessions, session, orchestrator = await build_orchestrator(
            DecisionStub(_decision("普通回复")), reply_suggestions=stub
        )
        await orchestrator.start_turn(
            session,
            TurnStartRequest(session_id="s1", turn_id="t-cap", text="你好"),
        )
        await collect_until_end(session)
        rest = await drain_all(session, timeout=0.5)
        event = next(
            (e for e in rest if e["type"] == "reply.suggestions"), None
        )
        assert event is not None
        assert len(event["suggestions"]) == MAX_SUGGESTIONS
        await orchestrator.close()

    asyncio.run(scenario())
