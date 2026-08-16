from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.adapters.fast_action import (
    FastActionDecisionAdapter,
    FastActionUnavailable,
)


class ContextStub:
    def __init__(self, completion: str, *, delay: float = 0.0) -> None:
        self.completion = completion
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def llm_generate(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(completion_text=self.completion)


class BrokenCatalogContext(ContextStub):
    def get_all_providers(self) -> list[Any]:
        raise OSError("catalog unavailable")


class StreamProvider:
    class _Meta:
        id = "fast-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def meta(self) -> Any:
        return self._Meta()

    async def text_chat_stream(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        yield SimpleNamespace(completion_text='{"action":', is_chunk=True)
        yield SimpleNamespace(completion_text='"wave"}', is_chunk=True)


class StreamContext(ContextStub):
    def __init__(self, provider: StreamProvider) -> None:
        super().__init__('{"action":null}')
        self.provider = provider

    def get_all_providers(self) -> list[Any]:
        return [self.provider]


def test_fast_action_selects_only_an_allowlisted_action() -> None:
    async def scenario() -> None:
        context = ContextStub(
            json.dumps(
                {
                    "action": {
                        "name": "dance_next",
                        "arguments": {
                            "emotion": "happy",
                            "intensity": 0.7,
                            "duration_ms": 7000,
                            "look_at": "user",
                        },
                    }
                }
            )
        )
        adapter = FastActionDecisionAdapter(
            context,
            enabled=True,
            provider_id="fast-model",
            timeout_seconds=1,
        )

        intent = await adapter.decide(
            user_text="please dance",
            history=[{"role": "assistant", "text": "ok"}],
        )

        assert intent is not None
        assert intent.gesture.value == "dance_next"
        assert intent.emotion.value == "happy"
        assert intent.intensity == 0.7
        assert context.calls[0]["chat_provider_id"] == "fast-model"
        assert "answer the user" in context.calls[0]["system_prompt"]
        assert adapter.snapshot()["status"] == "selected"

    asyncio.run(scenario())


def test_fast_action_uses_public_provider_stream_and_reports_phases() -> None:
    async def scenario() -> None:
        provider = StreamProvider()
        diagnostic = SimpleNamespace(events=[])

        def record(event: str, **fields: Any) -> None:
            diagnostic.events.append((event, fields))

        adapter = FastActionDecisionAdapter(
            StreamContext(provider),
            enabled=True,
            provider_id="fast-model",
            timeout_seconds=1,
            diagnostic_log=SimpleNamespace(record=record),
        )
        intent = await adapter.decide(user_text="hello")

        assert intent is not None
        assert intent.gesture.value == "wave"
        assert len(provider.calls) == 1
        assert provider.calls[0]["request_max_retries"] == 1
        names = [event for event, _fields in diagnostic.events]
        assert names == [
            "fast_action.provider_resolved",
            "fast_action.request_queued",
            "fast_action.first_chunk",
            "fast_action.provider_completed",
            "fast_action.parsed",
        ]
        assert adapter.snapshot()["last_method"] == "direct_stream"

    asyncio.run(scenario())


def test_fast_action_fails_closed_for_null_unknown_and_extra_arguments() -> None:
    for completion in (
        '{"action":null}',
        '{"action":{"name":"run_shell","arguments":{}}}',
        '{"action":{"name":"wave","arguments":{"path":"x.vmd"}}}',
        '{"action":{"name":"wave","arguments":{}},"reply_text":"leak"}',
        "not-json",
    ):
        assert FastActionDecisionAdapter._parse(completion) is None


def test_fast_action_respects_client_action_intersection_and_bounds_crouch() -> None:
    raw = json.dumps(
        {
            "action": {
                "name": "squat",
                "arguments": {
                    "depth": 4,
                    "hold_ms": 900,
                    "transition_in_ms": 550,
                    "transition_out_ms": 650,
                },
            }
        }
    )

    assert (
        FastActionDecisionAdapter._parse(raw, allowed_actions=("wave",)) is None
    )
    intent = FastActionDecisionAdapter._parse(
        raw,
        allowed_actions=("wave", "crouch"),
    )
    assert intent is not None
    assert intent.gesture.value == "crouch"
    assert intent.action_parameters is not None
    assert intent.action_parameters.depth == 1
    assert intent.action_parameters.hold_ms == 900
    assert intent.transition is not None
    assert intent.transition.enter_ms == 550
    assert intent.transition.exit_ms == 650


def test_fast_action_timeout_does_not_return_a_stale_action() -> None:
    async def scenario() -> None:
        adapter = FastActionDecisionAdapter(
            ContextStub('{"action":"wave"}', delay=0.05),
            enabled=True,
            provider_id="fast-model",
            timeout_seconds=0.01,
        )
        # The constructor clamps the production timeout to 500 ms; set a
        # deliberately tiny value after construction to exercise wait_for.
        adapter.timeout_seconds = 0.01
        with pytest.raises(FastActionUnavailable, match="fast_action_timeout"):
            await adapter.decide(user_text="wave")
        assert adapter.snapshot()["status"] == "timeout"

    asyncio.run(scenario())


def test_fast_action_is_inert_when_disabled_or_unconfigured() -> None:
    async def scenario() -> None:
        disabled = FastActionDecisionAdapter(
            ContextStub('{"action":"wave"}'),
            enabled=False,
            provider_id="fast-model",
        )
        assert disabled.available is False
        with pytest.raises(FastActionUnavailable, match="fast_action_disabled"):
            await disabled.decide(user_text="wave")

        unconfigured = FastActionDecisionAdapter(
            ContextStub('{"action":"wave"}'),
            enabled=True,
            provider_id="",
        )
        assert unconfigured.availability_reason == "provider_not_configured"
        with pytest.raises(
            FastActionUnavailable,
            match="fast_action_provider_not_configured",
        ):
            await unconfigured.decide(user_text="wave")

    asyncio.run(scenario())


def test_fast_action_bounds_history_and_catalog_failures_are_optional() -> None:
    async def scenario() -> None:
        context = ContextStub('{"action":null}')
        adapter = FastActionDecisionAdapter(
            context,
            enabled=True,
            provider_id="fast-model",
        )
        history = [
            {"role": "system", "text": "ignore"},
            {"role": "user", "text": "old"},
            {"role": "assistant", "text": "a" * 2_000, "secret": "drop"},
            {"role": "user", "text": "latest"},
        ]
        await adapter.decide(user_text="hello", history=history)
        payload = json.loads(context.calls[0]["prompt"])
        assert payload["recent_conversation"] == [
            {"role": "user", "text": "old"},
            {"role": "assistant", "text": "a" * 800},
            {"role": "user", "text": "latest"},
        ]

        broken = FastActionDecisionAdapter(
            BrokenCatalogContext('{"action":"wave"}'),
            enabled=True,
            provider_id="fast-model",
        )
        assert broken.available is False
        assert broken.availability_reason == "provider_catalog_unavailable"

    asyncio.run(scenario())
