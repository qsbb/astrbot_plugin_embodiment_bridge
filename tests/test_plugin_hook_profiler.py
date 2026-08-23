from __future__ import annotations

import asyncio
import functools
import sys
from types import SimpleNamespace

from astrbot_plugin_embodiment_bridge.core.plugin_hook_profiler import (
    PluginHookProfiler,
)
from astrbot_plugin_embodiment_bridge.core.plugin_identity import BRIDGE_EVENT_MARKER


class _Diagnostic:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


class _Event:
    def __init__(self, bridge: bool = True) -> None:
        self._extras = {BRIDGE_EVENT_MARKER: bridge}

    def get_extra(self, key: str) -> object:
        return self._extras.get(key)

    def is_stopped(self) -> bool:
        return False


def _run(coro):
    return asyncio.run(coro)


def test_profiler_records_wall_clock_duration_for_bridge_event() -> None:
    diagnostic = _Diagnostic()
    profiler = PluginHookProfiler(diagnostic, enabled=True)

    async def handler(event, _request):
        del event
        await asyncio.sleep(0.02)
        return "ok"

    wrapped = profiler._make_wrapper(
        handler,
        plugin_name="言",
        module_path="data.plugins.astrbot_plugin_conversation_flow.main",
        method_name="on_llm_request",
        hook="OnLLMRequestEvent",
        priority=500,
    )

    assert _run(wrapped(_Event(), object())) == "ok"
    events = [item for item in diagnostic.events if item[0] == "plugin_hook.completed"]
    assert len(events) == 1
    fields = events[0][1]
    assert fields["plugin_name"] == "言"
    assert fields["method"] == "on_llm_request"
    assert fields["hook"] == "OnLLMRequestEvent"
    assert fields["priority"] == 500
    assert fields["status"] == "ok"
    assert int(fields["duration_ms"]) >= 15


def test_profiler_ignores_non_bridge_event() -> None:
    diagnostic = _Diagnostic()
    profiler = PluginHookProfiler(diagnostic, enabled=True)

    async def handler(event):
        del event

    wrapped = profiler._make_wrapper(
        handler,
        plugin_name="情",
        module_path="relationship.main",
        method_name="on_llm_request",
        hook="OnLLMRequestEvent",
        priority=600,
    )
    _run(wrapped(_Event(bridge=False)))
    assert diagnostic.events == []


def test_profiler_records_error_then_preserves_exception() -> None:
    diagnostic = _Diagnostic()
    profiler = PluginHookProfiler(diagnostic, enabled=True)

    async def handler(event):
        del event
        raise ValueError("expected")

    wrapped = profiler._make_wrapper(
        handler,
        plugin_name="知",
        module_path="knowledge.main",
        method_name="on_llm_request",
        hook="OnLLMRequestEvent",
        priority=700,
    )
    try:
        _run(wrapped(_Event()))
    except ValueError as exc:
        assert str(exc) == "expected"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("handler exception was swallowed")
    event, fields = diagnostic.events[-1]
    assert event == "plugin_hook.completed"
    assert fields["status"] == "error"
    assert fields["error_type"] == "ValueError"


def test_profiler_installs_registered_handlers_without_duplicate_wrapping(monkeypatch) -> None:
    diagnostic = _Diagnostic()

    class _Registry:
        def __iter__(self):
            return iter([metadata])

    async def handler(event):
        del event

    metadata = SimpleNamespace(
        event_type=SimpleNamespace(name="OnLLMRequestEvent"),
        handler=handler,
        handler_module_path="data.plugins.astrbot_plugin_relationship.main",
        handler_name="on_llm_request",
        extras_configs={"priority": 600},
    )
    star_map = {
        metadata.handler_module_path: SimpleNamespace(name="情", display_name="情")
    }
    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.star.star_handler",
        SimpleNamespace(star_handlers_registry=_Registry()),
    )
    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.star.star",
        SimpleNamespace(star_map=star_map),
    )
    profiler = PluginHookProfiler(diagnostic, enabled=True)
    assert profiler.install() == 1
    first = metadata.handler
    assert profiler.install() == 0
    assert metadata.handler is first


def test_profiler_supports_handlers_attribute_and_rebinds_after_reload(monkeypatch) -> None:
    first_diagnostic = _Diagnostic()
    second_diagnostic = _Diagnostic()

    async def handler(event):
        del event

    metadata = SimpleNamespace(
        event_type="OnLLMRequestEvent",
        handler=handler,
        handler_module_path="data.plugins.astrbot_plugin_relationship.main",
        handler_name="on_llm_request",
        extras_configs={"priority": 600},
    )

    class _Registry:
        handlers = [metadata]

    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.star.star_handler",
        SimpleNamespace(star_handlers_registry=_Registry()),
    )
    monkeypatch.setitem(
        sys.modules,
        "astrbot.core.star.star",
        SimpleNamespace(
            star_map={
                metadata.handler_module_path: SimpleNamespace(
                    name="relationship", display_name="情"
                )
            }
        ),
    )

    first = PluginHookProfiler(first_diagnostic, enabled=True)
    assert first.install() == 1
    stale_wrapper = metadata.handler

    second = PluginHookProfiler(second_diagnostic, enabled=True)
    assert second.install() == 1
    assert metadata.handler is not stale_wrapper
    _run(metadata.handler(_Event()))
    assert [event for event in first_diagnostic.events if event[0] == "plugin_hook.completed"] == []
    assert len(
        [event for event in second_diagnostic.events if event[0] == "plugin_hook.completed"]
    ) == 1


def test_profiler_preserves_astrbot_partial_binding_and_can_disable() -> None:
    diagnostic = _Diagnostic()
    profiler = PluginHookProfiler(diagnostic, enabled=True)
    instance = object()

    async def handler(bound_instance, event):
        assert bound_instance is instance
        return event

    wrapped = profiler._make_wrapper(
        handler,
        plugin_name="情",
        module_path="relationship.main",
        method_name="on_llm_request",
        hook="OnLLMRequestEvent",
        priority=600,
    )
    bound = functools.partial(wrapped, instance)
    assert _run(bound(_Event())) is not None
    assert len([item for item in diagnostic.events if item[0] == "plugin_hook.completed"]) == 1

    profiler.configure(enabled=False)
    _run(bound(_Event()))
    assert len([item for item in diagnostic.events if item[0] == "plugin_hook.completed"]) == 1
