from __future__ import annotations

import asyncio
import functools
import inspect
import time
from typing import Any, Callable

from .plugin_identity import BRIDGE_EVENT_MARKER, LEGACY_BRIDGE_EVENT_MARKER
from .timing_trace import TimingTrace


_PROFILED_EVENT_NAMES = frozenset(
    {
        "OnWaitingLLMRequestEvent",
        "OnLLMRequestEvent",
        "OnLLMResponseEvent",
        "OnAgentBeginEvent",
        "OnAgentDoneEvent",
        "OnDecoratingResultEvent",
        "OnCallingFuncToolEvent",
        "OnUsingLLMToolEvent",
        "OnLLMToolRespondEvent",
        "OnAfterMessageSentEvent",
    }
)


class PluginHookProfiler:
    """Measure registered AstrBot coroutine handlers without changing Core source.

    The wrapper is intentionally installed only when the administrator enables
    the diagnostic switch. It records wall-clock time for the handler boundary,
    so provider, database and network waits are included. It does not attempt
    to profile arbitrary helper functions, which would add substantial overhead
    and would report CPU slices rather than the actual awaited method duration.
    """

    _WRAPPER_ATTR = "__embodiment_bridge_hook_profiler__"
    _WRAPPER_OWNER_ATTR = "__embodiment_bridge_hook_profiler_owner__"
    _WRAPPER_ORIGINAL_ATTR = "__embodiment_bridge_hook_profiler_original__"

    def __init__(self, diagnostic_log: Any, *, enabled: bool = False) -> None:
        self.diagnostic_log = diagnostic_log
        self.enabled = bool(enabled)
        self._installed = False
        self._wrapped_count = 0

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def wrapped_count(self) -> int:
        return self._wrapped_count

    def configure(self, *, enabled: bool) -> None:
        requested = bool(enabled)
        if self.enabled and not requested:
            self.uninstall()
        self.enabled = requested

    def uninstall(self) -> int:
        """Restore handlers wrapped by this profiler instance."""
        restored = 0
        try:
            registry_module = __import__(
                "astrbot.core.star.star_handler",
                fromlist=["star_handlers_registry"],
            )
            registry = getattr(registry_module, "star_handlers_registry")
        except (ImportError, ModuleNotFoundError, AttributeError):
            try:
                registry_module = __import__(
                    "astrbot.core.star.star_handlers_registry",
                    fromlist=["star_handlers_registry"],
                )
                registry = getattr(registry_module, "star_handlers_registry")
            except (ImportError, ModuleNotFoundError, AttributeError):
                self._installed = False
                self._wrapped_count = 0
                return 0
        for metadata in self._registry_handlers(registry):
            registered = getattr(metadata, "handler", None)
            bound_args: tuple[Any, ...] = ()
            bound_kwargs: dict[str, Any] = {}
            original = registered
            if isinstance(registered, functools.partial):
                bound_args = registered.args
                bound_kwargs = dict(registered.keywords or {})
                original = registered.func
            original, owner = self._unwrap_wrapper(original)
            if owner is not self:
                continue
            restored_handler = original
            if bound_args or bound_kwargs:
                restored_handler = functools.partial(
                    restored_handler, *bound_args, **bound_kwargs
                )
            setattr(metadata, "handler", restored_handler)
            restored += 1
        self._installed = False
        self._wrapped_count = 0
        return restored

    def install(self) -> int:
        """Wrap currently registered coroutine handlers, if AstrBot exposes them."""
        if not self.enabled:
            self._installed = False
            return 0
        try:
            from astrbot.core.star.star import star_map
            try:
                from astrbot.core.star.star_handler import star_handlers_registry
            except (ImportError, ModuleNotFoundError):
                # AstrBot 4.27 deployments may retain the compatibility module
                # name used by older plugin test/runtime bundles.
                from astrbot.core.star.star_handlers_registry import (
                    star_handlers_registry,
                )
        except (ImportError, ModuleNotFoundError, AttributeError):
            return 0

        wrapped = 0
        active = 0
        handlers = self._registry_handlers(star_handlers_registry)
        for metadata in handlers:
            event_type = self._event_type_name(metadata)
            if event_type not in _PROFILED_EVENT_NAMES:
                continue
            registered = getattr(metadata, "handler", None)
            bound_args: tuple[Any, ...] = ()
            bound_kwargs: dict[str, Any] = {}
            original = registered
            if isinstance(registered, functools.partial):
                bound_args = registered.args
                bound_kwargs = dict(registered.keywords or {})
                original = registered.func
            original, owner = self._unwrap_wrapper(original)
            if owner is self:
                active += 1
                continue
            # A reload can leave metadata pointing at a wrapper owned by the
            # previous plugin instance. Unwrap that stale closure before
            # attaching this instance's diagnostic sink.
            if not inspect.iscoroutinefunction(original):
                continue
            module_path = str(getattr(metadata, "handler_module_path", "") or "")
            plugin_metadata = (
                star_map.get(module_path)
                if hasattr(star_map, "get")
                else None
            )
            plugin_name = str(
                getattr(plugin_metadata, "display_name", None)
                or getattr(plugin_metadata, "name", None)
                or module_path
                or "unknown"
            )
            method_name = str(
                getattr(metadata, "handler_name", None)
                or getattr(original, "__name__", None)
                or "unknown"
            )
            extras = getattr(metadata, "extras_configs", {}) or {}
            priority_value = (
                extras.get("priority", 0)
                if hasattr(extras, "get")
                else getattr(metadata, "priority", 0)
            )
            wrapper = self._make_wrapper(
                original,
                plugin_name=plugin_name,
                module_path=module_path,
                method_name=method_name,
                hook=event_type,
                priority=priority_value,
            )
            replacement: Callable[..., Any] = wrapper
            if bound_args or bound_kwargs:
                replacement = functools.partial(
                    wrapper, *bound_args, **bound_kwargs
                )
            setattr(metadata, "handler", replacement)
            wrapped += 1
            active += 1
        self._wrapped_count = active
        self._installed = True
        self._record(
            "plugin_hook_profiler.scan",
            component="diagnostics",
            status="ready",
            event_count=active,
        )
        return wrapped

    @classmethod
    def _unwrap_wrapper(cls, function: Any) -> tuple[Any, Any]:
        """Return the raw coroutine and the profiler instance that owns it."""
        current = function
        owner = None
        seen: set[int] = set()
        while id(current) not in seen:
            seen.add(id(current))
            candidate_owner = getattr(current, cls._WRAPPER_OWNER_ATTR, None)
            if candidate_owner is None:
                break
            owner = candidate_owner
            current = getattr(current, cls._WRAPPER_ORIGINAL_ATTR, current)
        return current, owner

    @staticmethod
    def _registry_handlers(registry: Any) -> list[Any]:
        """Read both iterable and ``.handlers`` AstrBot registry variants."""
        try:
            handlers = getattr(registry, "handlers", None)
            if handlers is not None:
                return list(handlers)
            return list(registry)
        except (TypeError, AttributeError):
            return []

    @staticmethod
    def _event_type_name(metadata: Any) -> str:
        value = getattr(metadata, "event_type", "")
        name = getattr(value, "name", None)
        return str(name if name is not None else value or "")

    def _make_wrapper(
        self,
        original: Callable[..., Any],
        *,
        plugin_name: str,
        module_path: str,
        method_name: str,
        hook: str,
        priority: Any,
    ) -> Callable[..., Any]:
        @functools.wraps(original)
        async def timed_handler(*args: Any, **kwargs: Any) -> Any:
            event = self._find_event(args, kwargs)
            if not self.enabled or not self._is_bridge_event(event):
                return await original(*args, **kwargs)
            started = time.perf_counter()
            status = "ok"
            error_type = ""
            bridge_trace = getattr(event, "_quest_bridge_trace", None)
            span_id = ""
            parent_span_id = ""
            if isinstance(bridge_trace, TimingTrace):
                parent_span_id = str(
                    getattr(event, "_quest_bridge_processing_span_id", "") or ""
                )
                if not parent_span_id:
                    parent_span_id = bridge_trace.mark_event_consumed()
                    try:
                        setattr(event, "_quest_bridge_processing_span_id", parent_span_id)
                    except Exception:
                        pass
                span_id = bridge_trace.start_span(
                    f"plugin.{method_name}",
                    kind="plugin_hook",
                    parent_id=parent_span_id,
                    category="await",
                )
            try:
                return await original(*args, **kwargs)
            except asyncio.CancelledError:
                status = "cancelled"
                error_type = "CancelledError"
                raise
            except BaseException as exc:
                status = "error"
                error_type = type(exc).__name__
                raise
            finally:
                if isinstance(bridge_trace, TimingTrace) and span_id:
                    bridge_trace.finish_span(
                        span_id,
                        status=status,
                        plugin_name=plugin_name,
                        plugin_module=module_path,
                        hook=hook,
                        method=method_name,
                        priority=priority,
                        stopped=self._event_stopped(event),
                    )
                fields: dict[str, Any] = {
                    "component": "plugin_hook",
                    "status": status,
                    "plugin_name": plugin_name,
                    "plugin_module": module_path,
                    "hook": hook,
                    "method": method_name,
                    "priority": priority,
                    "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
                    "stopped": self._event_stopped(event),
                }
                # Keep the legacy hook event useful to existing dashboard
                # consumers while exposing the same fixed timing vocabulary as
                # ``timing.span.completed``.  The method-boundary event has no
                # independent queue/lock/provider markers, so those remain
                # conservative zero/default values; the nested span carries
                # the full parent relationship and loop-lag data.
                duration_ms = fields["duration_ms"]
                fields.update(
                    {
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "span_name": f"plugin.{method_name}",
                        "span_kind": "plugin_hook",
                        "wall_ms": duration_ms,
                        "active_ms": duration_ms,
                        "queue_wait_ms": 0,
                        "lock_wait_ms": 0,
                        "provider_wait_ms": 0,
                        "cache_hit": False,
                        "retry_count": 0,
                        "timeout": False,
                        "fallback": False,
                        "active_ms_estimated": True,
                    }
                )
                if error_type:
                    fields["error_type"] = error_type
                self._record("plugin_hook.completed", **fields)

        setattr(timed_handler, self._WRAPPER_ATTR, True)
        setattr(timed_handler, self._WRAPPER_OWNER_ATTR, self)
        setattr(timed_handler, self._WRAPPER_ORIGINAL_ATTR, original)
        return timed_handler

    @staticmethod
    def _find_event(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """Find the Event object after optional plugin-instance binding."""
        for candidate in args:
            if callable(getattr(candidate, "get_extra", None)):
                return candidate
        for candidate in kwargs.values():
            if callable(getattr(candidate, "get_extra", None)):
                return candidate
        return None

    @staticmethod
    def _is_bridge_event(event: Any) -> bool:
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return False
        try:
            return bool(
                getter(BRIDGE_EVENT_MARKER) is True
                or getter(LEGACY_BRIDGE_EVENT_MARKER) is True
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    @staticmethod
    def _event_stopped(event: Any) -> bool:
        getter = getattr(event, "is_stopped", None)
        if not callable(getter):
            return False
        try:
            return bool(getter())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

    def _record(self, event: str, **fields: Any) -> None:
        recorder = getattr(self.diagnostic_log, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(event, **fields)
        except Exception:
            # Profiling is diagnostic-only and must never affect a request.
            return
