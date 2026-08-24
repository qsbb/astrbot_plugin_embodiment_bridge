"""Quest 专用富化直管链（临独立链路）。

复刻 AstrBot 主链路的插件钩子富化（记忆/知识/人设/关系/环境/情绪），但：

- 不进入共享事件总线，与 QQ 洪峰、``session_lock_manager`` 完全隔离；
- 每个 OnLLMRequestEvent / OnLLMResponseEvent 钩子包独立 ``asyncio.wait_for``
  超时熔断，慢钩子（如 memory_companion）超时即跳过并用该插件最近一次成功
  注入的缓存片段兜底，绝不让单个钩子拖死整轮；
- 直管调用 ``context.llm_generate(chat_provider_id=...)``，不进 AstrBot 的
  ProcessStage / agent_runner。

本模块只新增、不改写现有事件总线路径；QQ 链路不受影响。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
)
from ..core.explicit_action_parser import requires_text_reply
from ..core.session_manager import SessionState
from ..core.timing_trace import TimingTrace
from .astrbot_pipeline import (
    MessagePipelineEmpty,
    MessagePipelineUnavailable,
    _abort_synthetic_event,
    _build_capture_event,
    _session_spatial_context,
)

# 钩子贡献缓存片段的最大长度，避免异常插件撑爆内存。
_MAX_CONTRIBUTION_FRAGMENT = 12_000
# 缓存的最大插件条目数。
_MAX_CACHE_ENTRIES = 64


class QuestEnrichedPipelineAdapter:
    """Drive AstrBot's plugin hook chain in-process with per-hook timeouts."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = False,
        platform_id: str = "",
        chat_provider_id: str = "",
        per_hook_budget_seconds: float = 6.0,
        total_hook_budget_seconds: float = 10.0,
        llm_timeout_seconds: float = 30.0,
        memory_cache_ttl_seconds: float = 30.0,
        excluded_plugins: tuple[str, ...] | list[str] = (),
        diagnostic_log: Any | None = None,
    ) -> None:
        self.context = context
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self._current_event: Any | None = None
        # per-session -> plugin_name -> (position, fragment, monotonic_ts)
        self._contribution_cache: dict[str, dict[str, tuple[str, str, float]]] = {}
        self.configure(
            enabled=enabled,
            platform_id=platform_id,
            chat_provider_id=chat_provider_id,
            per_hook_budget_seconds=per_hook_budget_seconds,
            total_hook_budget_seconds=total_hook_budget_seconds,
            llm_timeout_seconds=llm_timeout_seconds,
            memory_cache_ttl_seconds=memory_cache_ttl_seconds,
            excluded_plugins=excluded_plugins,
        )

    # ------------------------------------------------------------------ config
    def configure(
        self,
        *,
        enabled: bool | None = None,
        platform_id: str | None = None,
        chat_provider_id: str | None = None,
        per_hook_budget_seconds: float | None = None,
        total_hook_budget_seconds: float | None = None,
        llm_timeout_seconds: float | None = None,
        memory_cache_ttl_seconds: float | None = None,
        excluded_plugins: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if platform_id is not None:
            self.platform_id = str(platform_id or "").strip()
        if chat_provider_id is not None:
            self.chat_provider_id = str(chat_provider_id or "").strip()
        if per_hook_budget_seconds is not None:
            self.per_hook_budget_seconds = min(
                max(float(per_hook_budget_seconds), 0.5), 30.0
            )
        if total_hook_budget_seconds is not None:
            self.total_hook_budget_seconds = min(
                max(float(total_hook_budget_seconds), 1.0), 60.0
            )
        if llm_timeout_seconds is not None:
            self.llm_timeout_seconds = min(max(float(llm_timeout_seconds), 5.0), 120.0)
        if memory_cache_ttl_seconds is not None:
            self.memory_cache_ttl_seconds = min(
                max(float(memory_cache_ttl_seconds), 0.0), 600.0
            )
        if excluded_plugins is not None:
            self.excluded_plugins = frozenset(
                str(name).strip() for name in excluded_plugins if str(name).strip()
            )

    # -------------------------------------------------------------- availability
    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.platform_id:
            return "trusted_platform_not_configured"
        if not self.chat_provider_id:
            return "chat_provider_not_configured"
        try:
            platform_getter = self.context.get_platform_inst
        except AttributeError:
            return "astrbot_event_api_unavailable"
        try:
            platform = platform_getter(self.platform_id)
            if platform is None:
                return "trusted_platform_unavailable"
            if not callable(platform.create_event):
                return "astrbot_event_factory_unavailable"
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return "astrbot_event_factory_unavailable"
        if _llm_generate(self.context) is None:
            return "astrbot_llm_api_unavailable"
        return "ready"

    # ------------------------------------------------------------------ generate
    async def generate(
        self,
        *,
        session: SessionState,
        user_text: str,
        fast_action_active: bool = False,
        fast_action_feedback: dict[str, object] | None = None,
        action_facts: list[dict[str, Any]] | None = None,
    ) -> ModelDecision:
        started = time.perf_counter()
        current_turn = getattr(session, "current_turn", None)
        trace_id = str(getattr(current_turn, "trace_id", "") or "")[:16]
        recorder = getattr(self.diagnostic_log, "record", None)
        shared_trace = getattr(current_turn, "timing_trace", None)
        owns_trace = not isinstance(shared_trace, TimingTrace)
        trace = (
            shared_trace
            if isinstance(shared_trace, TimingTrace)
            else TimingTrace(
                self.diagnostic_log,
                enabled=bool(getattr(self.diagnostic_log, "enabled", False)),
                trace_id=trace_id,
            )
        )

        def record(event: str, *, status: str, **fields: Any) -> None:
            if not callable(recorder):
                return
            try:
                recorder(
                    event,
                    component="quest_chain",
                    phase="bridge",
                    status=status,
                    trace_id=trace_id,
                    **fields,
                )
            except Exception:
                return

        if not self.enabled:
            raise MessagePipelineUnavailable("quest_enriched_pipeline_disabled")
        if not session.protected_context_authorized:
            raise MessagePipelineUnavailable("protected_context_not_authorized")
        if not self.platform_id:
            raise MessagePipelineUnavailable("trusted_platform_not_configured")
        if not self.chat_provider_id:
            raise MessagePipelineUnavailable("chat_provider_not_configured")

        try:
            platform = self.context.get_platform_inst(self.platform_id)
        except BaseException:
            platform = None
        if platform is None or not callable(getattr(platform, "create_event", None)):
            raise MessagePipelineUnavailable("trusted_platform_unavailable")

        if owns_trace:
            trace.start_event_loop_monitor()

        # ① 复用现有合成事件构造，保证全部桥接标记与访问器与事件总线路径一致。
        with trace.span("quest_chain.event_create", kind="quest_chain"):
            event = _build_capture_event(
                platform=platform,
                platform_meta=platform.meta(),
                user_text=user_text,
                user_id=session.user_id,
                bot_id=session.bot_id,
                group_id=session.group_id,
                protected_context_authorized=session.protected_context_authorized,
                spatial_context=_session_spatial_context(session),
                fast_action_active=fast_action_active,
                fast_action_feedback=fast_action_feedback,
                action_facts=None,
                supported_actions=None,
            )
        self._current_event = event
        session_key = str(getattr(session, "session_id", "") or "")[:200]

        try:
            # ② 构建 ProviderRequest。
            with trace.span("quest_chain.build_request", kind="quest_chain"):
                req = self._build_provider_request(event, session, user_text)
            record(
                "quest_chain.request_built",
                status="ok",
                base_system_prompt_chars=len(req.system_prompt or ""),
                context_turns=len(req.contexts or []),
            )

            # ③ OnLLMRequestEvent 钩子（带超时熔断 + 缓存兜底）。
            await self._run_request_hooks(event, req, session_key, trace, record)
            if event.is_stopped():
                record("quest_chain.stopped_before_llm", status="stopped")
                return self._stopped_decision(event, started)
            enriched_system_prompt = str(getattr(req, "system_prompt", "") or "")

            # ④ 直管 LLM 调用。
            with trace.span(
                "quest_chain.llm", kind="llm_provider", category="provider"
            ):
                resp = await self._call_llm(req, trace)

            # ⑤ OnLLMResponseEvent 钩子（带超时）。
            await self._run_response_hooks(event, req, resp, trace, record)

            # ⑥ 提取回复。
            reply = _extract_llm_text(resp).strip()
            if not reply:
                # 钩子可能通过 stop_event + 写 result 的方式接管回复。
                reply = _extract_event_result_text(event).strip()
            if not reply:
                if requires_text_reply(user_text):
                    record(
                        "quest_chain.empty_reply",
                        status="error",
                        reason_code="quest_enriched_pipeline_reply_required_missing",
                    )
                    raise MessagePipelineEmpty(
                        "quest_enriched_pipeline_reply_required_missing"
                    )
                record("quest_chain.empty_reply", status="empty")
                return self._silent_decision(started)

            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            record(
                "quest_chain.completed",
                status="ok",
                reply_chars=len(reply),
                duration_ms=duration_ms,
            )
            return ModelDecision(
                should_reply=True,
                reply_text=reply[:4000],
                intent=ProposedIntent(
                    emotion=Emotion.NEUTRAL,
                    gesture=Gesture.TALK,
                    look_at=LookAt.USER,
                    intensity=0.38,
                    duration_ms=min(8_000, max(1_200, len(reply) * 85)),
                    reason_code="quest_enriched_pipeline",
                ),
            )
        except (MessagePipelineUnavailable, MessagePipelineEmpty):
            raise
        except asyncio.CancelledError:
            _abort_synthetic_event(event, reason="turn_interrupted")
            raise
        except Exception as exc:
            record(
                "quest_chain.error",
                status="error",
                error_type=type(exc).__name__,
            )
            raise MessagePipelineUnavailable(
                f"quest_enriched_pipeline_error:{type(exc).__name__}"
            ) from exc
        finally:
            self._current_event = None
            if owns_trace:
                await trace.close()

    # ------------------------------------------------------------- build request
    def _build_provider_request(
        self, event: Any, session: SessionState, user_text: str
    ) -> Any:
        ProviderRequest = _provider_request_type()
        req = ProviderRequest()
        req.prompt = str(user_text)
        req.session_id = str(getattr(event, "unified_msg_origin", "") or "")
        # 对话历史沿用会话快照（[{role, content}]），供需要上下文的钩子使用。
        history = _session_history_snapshot(session)
        req.contexts = [dict(item) for item in history if isinstance(item, dict)]
        # 人设基底留空：人设/记忆/知识/关系/环境由各插件钩子注入，避免重复。
        req.system_prompt = ""
        # v1 dialogue-only：不开放 LLM 工具多轮调用。
        req.func_tool = None
        return req

    # -------------------------------------------------------------- hook dispatch
    async def _run_request_hooks(
        self,
        event: Any,
        req: Any,
        session_key: str,
        trace: TimingTrace,
        record: Any,
    ) -> None:
        handlers = _request_handlers(event)
        record(
            "quest_chain.request_hooks_begin",
            status="started",
            hook_count=len(handlers),
        )
        deadline = time.monotonic() + self.total_hook_budget_seconds
        with trace.span("quest_chain.request_hooks", kind="quest_chain"):
            for handler in handlers:
                plugin = _handler_plugin_name(handler)
                hook_name = str(getattr(handler, "handler_name", "") or "")[:64]
                if plugin in self.excluded_plugins:
                    record(
                        "quest_chain.hook_skipped",
                        status="excluded",
                        plugin_name=plugin,
                        hook=hook_name,
                    )
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record(
                        "quest_chain.hook_budget_exhausted",
                        status="timeout",
                        plugin_name=plugin,
                        hook=hook_name,
                    )
                    break
                timeout = min(self.per_hook_budget_seconds, remaining)
                before_system = str(getattr(req, "system_prompt", "") or "")
                before_prompt = str(getattr(req, "prompt", "") or "")
                hook_started = time.perf_counter()
                span_id = trace.start_span(
                    f"quest_chain.hook.{_safe_span_token(plugin)}",
                    kind="quest_chain_hook",
                )
                try:
                    await asyncio.wait_for(
                        handler.handler(event, req), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    # 熔断：回滚该钩子可能的部分写入，再用缓存片段兜底。
                    req.system_prompt = before_system
                    req.prompt = before_prompt
                    applied = self._apply_cached_contribution(
                        session_key, plugin, req
                    )
                    trace.finish_span(
                        span_id,
                        status="timeout",
                        timeout=True,
                        cache_hit=applied,
                    )
                    record(
                        "quest_chain.hook_timeout",
                        status="timeout",
                        plugin_name=plugin,
                        hook=hook_name,
                        budget_ms=int(timeout * 1000),
                        cache_applied=applied,
                    )
                    continue
                except asyncio.CancelledError:
                    trace.finish_span(span_id, status="cancelled")
                    raise
                except Exception as exc:
                    # 与 AstrBot 一致：单插件异常不中断链路，记日志后继续。
                    trace.finish_span(span_id, status="error")
                    record(
                        "quest_chain.hook_error",
                        status="error",
                        plugin_name=plugin,
                        hook=hook_name,
                        error_type=type(exc).__name__,
                    )
                    continue
                else:
                    wall_ms = int((time.perf_counter() - hook_started) * 1000)
                    trace.finish_span(span_id, status="ok")
                    # 记录该钩子对 system_prompt 的贡献，供超时熔断时兜底。
                    self._record_contribution(
                        session_key, plugin, before_system, req.system_prompt
                    )
                    record(
                        "quest_chain.hook_ok",
                        status="ok",
                        plugin_name=plugin,
                        hook=hook_name,
                        wall_ms=wall_ms,
                    )
                if event.is_stopped():
                    record(
                        "quest_chain.hook_stopped",
                        status="stopped",
                        plugin_name=plugin,
                        hook=hook_name,
                    )
                    break

    async def _run_response_hooks(
        self,
        event: Any,
        req: Any,
        resp: Any,
        trace: TimingTrace,
        record: Any,
    ) -> None:
        handlers = _response_handlers(event)
        if not handlers:
            return
        with trace.span("quest_chain.response_hooks", kind="quest_chain"):
            for handler in handlers:
                plugin = _handler_plugin_name(handler)
                if plugin in self.excluded_plugins:
                    continue
                try:
                    await asyncio.wait_for(
                        handler.handler(event, resp),
                        timeout=self.per_hook_budget_seconds,
                    )
                except asyncio.TimeoutError:
                    record(
                        "quest_chain.response_hook_timeout",
                        status="timeout",
                        plugin_name=plugin,
                    )
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    record(
                        "quest_chain.response_hook_error",
                        status="error",
                        plugin_name=plugin,
                        error_type=type(exc).__name__,
                    )
                    continue
                if event.is_stopped():
                    break

    # -------------------------------------------------------------------- llm
    async def _call_llm(self, req: Any, trace: TimingTrace) -> Any:
        llm_generate = _llm_generate(self.context)
        if llm_generate is None:
            raise MessagePipelineUnavailable("astrbot_llm_api_unavailable")
        contexts = [dict(item) for item in (req.contexts or []) if isinstance(item, dict)]
        try:
            return await asyncio.wait_for(
                llm_generate(
                    chat_provider_id=self.chat_provider_id,
                    prompt=str(getattr(req, "prompt", "") or ""),
                    system_prompt=str(getattr(req, "system_prompt", "") or ""),
                    contexts=contexts,
                    tools=None,
                    image_urls=list(getattr(req, "image_urls", None) or []),
                ),
                timeout=self.llm_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise MessagePipelineUnavailable("quest_enriched_pipeline_llm_timeout") from exc

    # ------------------------------------------------------ contribution cache
    def _record_contribution(
        self, session_key: str, plugin: str, before: str, after: str
    ) -> None:
        if not session_key or not plugin or before == after:
            return
        fragment, position = _extract_fragment(before, after)
        if not fragment:
            return
        bucket = self._contribution_cache.setdefault(session_key, {})
        if len(bucket) >= _MAX_CACHE_ENTRIES and plugin not in bucket:
            # 简单逐出最旧的一条。
            oldest = min(bucket, key=lambda k: bucket[k][2])
            bucket.pop(oldest, None)
        bucket[plugin] = (position, fragment[:_MAX_CONTRIBUTION_FRAGMENT], time.monotonic())

    def _apply_cached_contribution(
        self, session_key: str, plugin: str, req: Any
    ) -> bool:
        bucket = self._contribution_cache.get(session_key)
        if not bucket:
            return False
        entry = bucket.get(plugin)
        if entry is None:
            return False
        position, fragment, ts = entry
        if self.memory_cache_ttl_seconds <= 0:
            return False
        if time.monotonic() - ts > self.memory_cache_ttl_seconds:
            return False
        current = str(getattr(req, "system_prompt", "") or "")
        if position == "prepend":
            req.system_prompt = (fragment + current)[:_MAX_CONTRIBUTION_FRAGMENT + len(current)]
        else:
            req.system_prompt = (current + fragment)[: len(current) + _MAX_CONTRIBUTION_FRAGMENT]
        return True

    # ----------------------------------------------------------------- outcomes
    def _stopped_decision(self, event: Any, started: float) -> ModelDecision:
        # 钩子主动 stop_event 且未产出文本：视为合法静默轮。
        text = _extract_event_result_text(event).strip()
        if text:
            return ModelDecision(
                should_reply=True,
                reply_text=text[:4000],
                intent=_talk_intent(text, "quest_enriched_pipeline_stopped"),
            )
        return self._silent_decision(started)

    def _silent_decision(self, started: float) -> ModelDecision:
        return ModelDecision(
            should_reply=False,
            reply_text="",
            intent=ProposedIntent(
                emotion=Emotion.NEUTRAL,
                gesture=Gesture.IDLE,
                look_at=LookAt.NONE,
                intensity=0.0,
                duration_ms=1_200,
                reason_code="quest_enriched_pipeline_silent",
            ),
        )

    # ----------------------------------------------------------------- lifecycle
    def abort_current_event(self, reason: str = "aborted") -> None:
        event = self._current_event
        if event is None:
            return
        _abort_synthetic_event(event, reason=reason)

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "availability_reason": self.availability_reason,
            "chat_provider_id": self.chat_provider_id,
            "per_hook_budget_seconds": self.per_hook_budget_seconds,
            "total_hook_budget_seconds": self.total_hook_budget_seconds,
            "llm_timeout_seconds": self.llm_timeout_seconds,
            "memory_cache_ttl_seconds": self.memory_cache_ttl_seconds,
            "excluded_plugins": sorted(self.excluded_plugins),
            "decision_path": "quest_enriched_pipeline",
            "mode": "quest_enriched_pipeline",
        }

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# AstrBot 内部访问（全部惰性导入，插件发现在旧版本上也能优雅降级）
# --------------------------------------------------------------------------- #


def _provider_request_type() -> Any:
    from astrbot.core.provider.entities import ProviderRequest

    return ProviderRequest


def _llm_generate(context: Any) -> Any:
    getter = getattr(context, "llm_generate", None)
    return getter if callable(getter) else None


def _request_handlers(event: Any) -> list[Any]:
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    return star_handlers_registry.get_handlers_by_event_type(
        EventType.OnLLMRequestEvent,
        plugins_name=getattr(event, "plugins_name", None),
    )


def _response_handlers(event: Any) -> list[Any]:
    from astrbot.core.star.star_handler import EventType, star_handlers_registry

    return star_handlers_registry.get_handlers_by_event_type(
        EventType.OnLLMResponseEvent,
        plugins_name=getattr(event, "plugins_name", None),
    )


def _handler_plugin_name(handler: Any) -> str:
    try:
        from astrbot.core.star.star_handler import star_map

        module_path = getattr(handler, "handler_module_path", "") or ""
        metadata = star_map.get(module_path)
        name = getattr(metadata, "name", "") if metadata is not None else ""
        return str(name or module_path or "unknown")[:96]
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- #
# 回复提取与片段缓存辅助
# --------------------------------------------------------------------------- #


def _extract_llm_text(resp: Any) -> str:
    if resp is None:
        return ""
    text = getattr(resp, "completion_text", None)
    if isinstance(text, str) and text.strip():
        return text
    chain = getattr(resp, "result_chain", None)
    if chain is not None:
        getter = getattr(chain, "get_plain_text", None)
        if callable(getter):
            try:
                value = getter()
                return value if isinstance(value, str) else ""
            except Exception:
                return ""
    return ""


def _extract_event_result_text(event: Any) -> str:
    getter = getattr(event, "get_result", None)
    if not callable(getter):
        return ""
    try:
        result = getter()
    except Exception:
        return ""
    plain = getattr(result, "get_plain_text", None)
    if not callable(plain):
        return ""
    try:
        value = plain()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _extract_fragment(before: str, after: str) -> tuple[str, str]:
    """Compute the delta a hook applied to the system prompt.

    Returns ``(fragment, position)`` where position is ``append`` or ``prepend``.
    Hooks that rewrite the prompt wholesale cannot be reliably cached and yield
    an empty fragment.
    """
    if after.startswith(before):
        return after[len(before) :], "append"
    if before and after.endswith(before):
        return after[: -len(before)], "prepend"
    if not before:
        return after, "append"
    return "", "append"


def _talk_intent(text: str, reason_code: str) -> ProposedIntent:
    return ProposedIntent(
        emotion=Emotion.NEUTRAL,
        gesture=Gesture.TALK,
        look_at=LookAt.USER,
        intensity=0.38,
        duration_ms=min(8_000, max(1_200, len(text) * 85)),
        reason_code=reason_code[:64],
    )


def _safe_span_token(plugin: str) -> str:
    token = "".join(ch if ch.isalnum() else "_" for ch in str(plugin or "unknown"))
    return token[:48] or "unknown"


def _session_history_snapshot(session: SessionState) -> list[dict[str, Any]]:
    """Project the bridge session turn history into OpenAI-style contexts.

    ``SessionState.history`` entries are ``{"role", "text"}``; provider
    ``contexts`` expect ``{"role", "content"}``.
    """
    history = getattr(session, "history", None)
    items = list(history) if history is not None else []
    contexts: list[dict[str, Any]] = []
    for entry in items[-20:]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip()
        content = str(entry.get("text") or entry.get("content") or "").strip()
        if role in {"user", "assistant", "system"} and content:
            contexts.append({"role": role, "content": content})
    return contexts
