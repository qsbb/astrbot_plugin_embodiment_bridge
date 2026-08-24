from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.adapters import quest_enriched_pipeline as qep
from astrbot_plugin_embodiment_bridge.adapters.astrbot_pipeline import (
    MessagePipelineEmpty,
    MessagePipelineUnavailable,
)


# --------------------------------------------------------------------------- #
# 测试桩
# --------------------------------------------------------------------------- #


class PlatformStub:
    def meta(self) -> Any:
        return SimpleNamespace(id="qq", name="aiocqhttp")

    def create_event(self, message: Any) -> Any:
        return message


class ContextStub:
    def __init__(self, llm_text: str = "临独立链路的回复。") -> None:
        self.llm_text = llm_text
        self.last_llm_kwargs: dict[str, Any] | None = None

    def get_platform_inst(self, platform_id: str) -> Any | None:
        return PlatformStub() if platform_id == "qq" else None

    async def llm_generate(self, **kwargs: Any) -> Any:
        self.last_llm_kwargs = kwargs
        return SimpleNamespace(completion_text=self.llm_text, result_chain=None)


class CaptureEventStub:
    def __init__(self, result_text: str = "") -> None:
        self._stopped = False
        self.unified_msg_origin = "qq:user"
        self.plugins_name = None
        self._result_text = result_text

    def is_stopped(self) -> bool:
        return self._stopped

    def stop_event(self) -> None:
        self._stopped = True

    def get_result(self) -> Any:
        return SimpleNamespace(get_plain_text=lambda: self._result_text)


class HandlerStub:
    """模拟一个 OnLLMRequestEvent 钩子。"""

    def __init__(
        self,
        plugin: str,
        *,
        append: str = "",
        delay: float = 0.0,
        raise_exc: bool = False,
    ) -> None:
        self.handler_name = "on_llm_request"
        self.handler_module_path = f"plugins.{plugin}"
        self._plugin = plugin
        self._append = append
        self._delay = delay
        self._raise = raise_exc

    async def handler(self, event: Any, req: Any) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise RuntimeError("boom")
        if self._append:
            req.system_prompt = (req.system_prompt or "") + self._append


def make_session(*, authorized: bool = True) -> Any:
    return SimpleNamespace(
        protected_context_authorized=authorized,
        user_id="user",
        bot_id="bot",
        group_id="",
        session_id="qq:user",
        history=[
            {"role": "user", "text": "之前的问题"},
            {"role": "assistant", "text": "之前的回答"},
            {"role": "tool", "text": "应被过滤"},
        ],
        current_turn=None,
    )


def make_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    context: ContextStub | None = None,
    request_handlers: list[Any] | None = None,
    response_handlers: list[Any] | None = None,
    enabled: bool = True,
    per_hook_budget: float = 6.0,
    platform_id: str = "qq",
    **kwargs: Any,
) -> qep.QuestEnrichedPipelineAdapter:
    ctx = context or ContextStub()
    monkeypatch.setattr(
        qep, "_build_capture_event", lambda **_: CaptureEventStub()
    )
    monkeypatch.setattr(qep, "_session_spatial_context", lambda session: None)
    monkeypatch.setattr(
        qep,
        "_provider_request_type",
        lambda: type(
            "Req",
            (),
            {
                "__init__": lambda self: None,
            },
        ),
    )
    monkeypatch.setattr(
        qep, "_request_handlers", lambda event: list(request_handlers or [])
    )
    monkeypatch.setattr(
        qep, "_response_handlers", lambda event: list(response_handlers or [])
    )
    monkeypatch.setattr(
        qep, "_handler_plugin_name", lambda h: getattr(h, "_plugin", "unknown")
    )
    monkeypatch.setattr(qep, "_abort_synthetic_event", lambda *a, **k: None)
    return qep.QuestEnrichedPipelineAdapter(
        ctx,
        SimpleNamespace(),
        enabled=enabled,
        platform_id=platform_id,
        chat_provider_id="deepseek/test",
        per_hook_budget_seconds=per_hook_budget,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# availability_reason
# --------------------------------------------------------------------------- #


def test_availability_reason_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter(monkeypatch, enabled=False)
    assert adapter.available is False
    assert adapter.availability_reason == "disabled"


def test_availability_reason_missing_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(monkeypatch, platform_id="")
    # platform_id 为空：不可用
    assert adapter.available is False
    assert adapter.availability_reason == "trusted_platform_not_configured"


def test_availability_reason_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter(monkeypatch)
    assert adapter.available is True
    assert adapter.availability_reason == "ready"


# --------------------------------------------------------------------------- #
# _extract_fragment / _extract_llm_text / 历史投影（纯逻辑）
# --------------------------------------------------------------------------- #


def test_extract_fragment_append() -> None:
    fragment, position = qep._extract_fragment("人设。", "人设。[记忆] 用户喜欢猫")
    assert fragment == "[记忆] 用户喜欢猫"
    assert position == "append"


def test_extract_fragment_prepend() -> None:
    fragment, position = qep._extract_fragment("人设。", "[前缀] 人设。")
    assert fragment == "[前缀] "
    assert position == "prepend"


def test_extract_fragment_from_empty() -> None:
    fragment, position = qep._extract_fragment("", "全部人设")
    assert fragment == "全部人设"
    assert position == "append"


def test_extract_fragment_wholesale_rewrite_uncacheable() -> None:
    fragment, _ = qep._extract_fragment("旧人设A", "全新人设B")
    assert fragment == ""


def test_extract_llm_text_prefers_completion_text() -> None:
    resp = SimpleNamespace(
        completion_text="直接文本",
        result_chain=SimpleNamespace(get_plain_text=lambda: "链文本"),
    )
    assert qep._extract_llm_text(resp) == "直接文本"


def test_extract_llm_text_falls_back_to_chain() -> None:
    resp = SimpleNamespace(
        completion_text="  ",
        result_chain=SimpleNamespace(get_plain_text=lambda: "链文本"),
    )
    assert qep._extract_llm_text(resp) == "链文本"


def test_extract_llm_text_none() -> None:
    assert qep._extract_llm_text(None) == ""
    assert qep._extract_llm_text(SimpleNamespace(completion_text=None)) == ""


def test_session_history_snapshot_filters_roles() -> None:
    session = make_session()
    contexts = qep._session_history_snapshot(session)
    assert contexts == [
        {"role": "user", "content": "之前的问题"},
        {"role": "assistant", "content": "之前的回答"},
    ]


# --------------------------------------------------------------------------- #
# 贡献缓存（超时熔断兜底）
# --------------------------------------------------------------------------- #


def test_contribution_cache_record_and_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = make_adapter(monkeypatch)
    req = SimpleNamespace(system_prompt="人设。")
    adapter._record_contribution("s1", "memory", "人设。", "人设。[记忆]X")
    # 模拟熔断时回滚到 base，再应用缓存
    req.system_prompt = "人设。"
    applied = adapter._apply_cached_contribution("s1", "memory", req)
    assert applied is True
    assert req.system_prompt == "人设。[记忆]X"


def test_contribution_cache_ttl_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter(monkeypatch, memory_cache_ttl_seconds=0.0)
    req = SimpleNamespace(system_prompt="人设。")
    adapter._record_contribution("s1", "memory", "人设。", "人设。[记忆]X")
    req.system_prompt = "人设。"
    # ttl=0 表示禁用缓存
    assert adapter._apply_cached_contribution("s1", "memory", req) is False


def test_contribution_cache_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = make_adapter(monkeypatch)
    req = SimpleNamespace(system_prompt="人设。")
    assert adapter._apply_cached_contribution("s1", "nonexistent", req) is False


# --------------------------------------------------------------------------- #
# generate() 端到端（桩）
# --------------------------------------------------------------------------- #


def test_generate_happy_path_calls_llm_with_enriched_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub(llm_text="这是临独立链路的回答。")
        hooks = [
            HandlerStub("memory", append="[记忆] 用户叫小夏"),
            HandlerStub("persona", append="[人设] 伴夏"),
        ]
        adapter = make_adapter(monkeypatch, context=context, request_handlers=hooks)
        decision = await adapter.generate(session=make_session(), user_text="你好")
        assert decision.should_reply is True
        assert decision.reply_text == "这是临独立链路的回答。"
        assert decision.intent.reason_code == "quest_enriched_pipeline"
        # 钩子的富化（记忆+人设）应进入发给 LLM 的 system_prompt
        sent = context.last_llm_kwargs
        assert sent is not None
        assert "[记忆] 用户叫小夏" in sent["system_prompt"]
        assert "[人设] 伴夏" in sent["system_prompt"]
        # 对话历史投影为 OpenAI contexts
        assert any(c["role"] == "user" for c in sent["contexts"])

    asyncio.run(scenario())


def test_generate_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        adapter = make_adapter(monkeypatch, enabled=False)
        with pytest.raises(MessagePipelineUnavailable):
            await adapter.generate(session=make_session(), user_text="你好")

    asyncio.run(scenario())


def test_generate_raises_when_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        adapter = make_adapter(monkeypatch)
        with pytest.raises(MessagePipelineUnavailable):
            await adapter.generate(
                session=make_session(authorized=False), user_text="你好"
            )

    asyncio.run(scenario())


def test_slow_hook_circuit_breaks_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        hooks = [
            HandlerStub("fast", append="[快钩子]"),
            # 慢钩子：2 秒，超过 0.3s 的单钩子预算 → 熔断
            HandlerStub("memory", append="[不应出现]", delay=2.0),
        ]
        adapter = make_adapter(
            monkeypatch,
            context=context,
            request_handlers=hooks,
            per_hook_budget=0.3,
        )
        started = time.monotonic()
        decision = await adapter.generate(session=make_session(), user_text="你好")
        elapsed = time.monotonic() - started
        assert decision.should_reply is True
        # 慢钩子被熔断：其追加内容不应进入 prompt，且整轮远快于 2 秒
        sent = context.last_llm_kwargs
        assert "[不应出现]" not in sent["system_prompt"]
        assert "[快钩子]" in sent["system_prompt"]
        assert elapsed < 1.5

    asyncio.run(scenario())


def test_hook_exception_does_not_break_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub()
        hooks = [
            HandlerStub("bad", raise_exc=True),
            HandlerStub("good", append="[好钩子]"),
        ]
        adapter = make_adapter(monkeypatch, context=context, request_handlers=hooks)
        decision = await adapter.generate(session=make_session(), user_text="你好")
        assert decision.should_reply is True
        assert "[好钩子]" in context.last_llm_kwargs["system_prompt"]

    asyncio.run(scenario())


def test_empty_reply_raises_when_text_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        context = ContextStub(llm_text="")
        adapter = make_adapter(monkeypatch, context=context)
        # 显式要求回复的句子（"回答我"）需要文本回复；空回复应抛 MessagePipelineEmpty
        with pytest.raises(MessagePipelineEmpty):
            await adapter.generate(
                session=make_session(), user_text="现在几点了，回答我"
            )

    asyncio.run(scenario())


def test_excluded_plugin_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        context = ContextStub()
        hooks = [
            HandlerStub("memory", append="[应被跳过]"),
            HandlerStub("persona", append="[保留]"),
        ]
        adapter = make_adapter(
            monkeypatch,
            context=context,
            request_handlers=hooks,
            excluded_plugins=("memory",),
        )
        decision = await adapter.generate(session=make_session(), user_text="你好")
        assert decision.should_reply is True
        sent = context.last_llm_kwargs
        assert "[应被跳过]" not in sent["system_prompt"]
        assert "[保留]" in sent["system_prompt"]

    asyncio.run(scenario())
