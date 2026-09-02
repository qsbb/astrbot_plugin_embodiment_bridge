"""Reply suggestion service (M5, ``reply.suggestions`` SSE event).

After 伴夏 finishes a reply the bridge may generate up to three short
candidate replies *for the user* and push them as an auxiliary SSE event.
The event is deliberately decoupled from the turn lifecycle:

* it is emitted *after* ``reply.end`` so reply delivery is never delayed,
* generation failures are silent (diagnostic counters only),
* the client treats it as optional (missing event ⇒ no suggestion chips).

The adapter mirrors ``FastActionDecisionAdapter``: a dedicated optional
chat Provider, a bounded timeout, and availability probing. It never
generates main reply text and never mutates conversation history.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

DEFAULT_REPLY_SUGGESTION_TIMEOUT_SECONDS = 6.0
MAX_SUGGESTIONS = 3
MAX_SUGGESTION_CHARS = 200
_HISTORY_WINDOW = 6

_SYSTEM_PROMPT = (
    "你是一个聊天回复建议器。根据最近对话，以用户本人的口吻生成 3 条简短的"
    "候选回复。每条不超过 30 个字，语气自然，可直接发送。"
    "只输出一个 JSON 数组，例如 [\"好啊\",\"那然后呢\",\"哈哈哈笑死\"]。"
    "不要输出任何其他文字。"
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class ReplySuggestionService:
    """Small side-channel LLM adapter for user quick replies."""

    def __init__(
        self,
        context: Any,
        *,
        enabled: bool = True,
        provider_id: str = "",
        timeout_seconds: float = DEFAULT_REPLY_SUGGESTION_TIMEOUT_SECONDS,
        diagnostic_log: Any | None = None,
    ) -> None:
        self.context = context
        self.enabled = bool(enabled)
        self.provider_id = str(provider_id or "").strip()
        self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
        self.diagnostic_log = diagnostic_log
        self.last_status = "disabled" if not self.enabled else "not_configured"
        self.last_error = ""

    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        generator = getattr(self.context, "llm_generate", None)
        if callable(generator):
            return "ready"
        if not self.provider_id:
            return "provider_not_configured"
        catalog = getattr(self.context, "get_all_providers", None)
        if callable(catalog):
            try:
                providers = catalog()
            except Exception:
                return "provider_catalog_unavailable"
            if isinstance(providers, (list, tuple)):
                for provider in providers:
                    try:
                        candidate = str(provider.meta().id or "").strip()
                    except Exception:
                        continue
                    if candidate == self.provider_id:
                        return "ready"
                return "selected_missing"
        return "llm_api_unavailable"

    def configure(
        self,
        *,
        enabled: bool | None = None,
        provider_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if provider_id is not None:
            self.provider_id = str(provider_id or "").strip()
        if timeout_seconds is not None:
            self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
        self.last_status = "disabled" if not self.enabled else "not_configured"

    async def generate(self, history: list[dict[str, str]]) -> list[str]:
        """Return up to three sanitized suggestion strings (may be empty)."""
        if not self.enabled:
            return []
        window = [
            entry
            for entry in (history or [])
            if isinstance(entry, dict) and str(entry.get("text", "")).strip()
        ][-_HISTORY_WINDOW:]
        if not window:
            return []
        self.last_status = "generating"
        prompt = self._build_prompt(window)
        try:
            raw = await asyncio.wait_for(
                self._invoke_llm(prompt), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            self.last_status = "timeout"
            self._diagnostic_event("reply_suggestions_timeout")
            return []
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # provider offline, config drift, etc.
            self.last_status = "failed"
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._diagnostic_event("reply_suggestions_failed", error=self.last_error)
            return []
        suggestions = self._parse(raw)
        self.last_status = "emitted" if suggestions else "empty_parse"
        if not suggestions:
            self._diagnostic_event("reply_suggestions_empty_parse")
        return suggestions

    def _build_prompt(self, window: list[dict[str, str]]) -> str:
        lines = []
        for entry in window:
            role = "伴夏" if str(entry.get("role", "")) == "assistant" else "我"
            text = str(entry.get("text", "")).strip()[:400]
            lines.append(f"{role}：{text}")
        transcript = "\n".join(lines)
        return (
            "以下是最近对话（最后一行是伴夏刚说的话）：\n"
            f"{transcript}\n\n"
            "请生成 3 条我现在可以发送的回复，JSON 数组输出。"
        )

    async def _invoke_llm(self, prompt: str) -> str:
        provider = self._resolve_provider()
        if provider is not None:
            chat = getattr(provider, "text_chat", None)
            if callable(chat):
                response = await chat(
                    prompt=prompt,
                    system_prompt=_SYSTEM_PROMPT,
                    func_tool=None,
                    request_max_retries=1,
                )
                return self._completion_text(response)
            stream_factory = getattr(provider, "text_chat_stream", None)
            if callable(stream_factory):
                chunks: list[str] = []
                async for chunk in stream_factory(
                    prompt=prompt, system_prompt=_SYSTEM_PROMPT
                ):
                    piece = self._completion_text(chunk)
                    if piece:
                        chunks.append(piece)
                if chunks:
                    return "".join(chunks)
        generator = getattr(self.context, "llm_generate", None)
        if not callable(generator):
            raise RuntimeError("reply_suggestions_llm_api_unavailable")
        response = await generator(
            chat_provider_id=self.provider_id,
            prompt=prompt,
            system_prompt=_SYSTEM_PROMPT,
            request_max_retries=1,
        )
        return self._completion_text(response)

    def _resolve_provider(self) -> Any | None:
        if not self.provider_id:
            return None
        catalog = getattr(self.context, "get_all_providers", None)
        if not callable(catalog):
            return None
        try:
            providers = catalog()
        except Exception:
            return None
        if not isinstance(providers, (list, tuple)):
            return None
        for provider in providers:
            try:
                candidate = str(provider.meta().id or "").strip()
            except Exception:
                continue
            if candidate == self.provider_id:
                return provider
        return None

    @staticmethod
    def _completion_text(response: Any) -> str:
        raw = getattr(response, "completion_text", response)
        return raw if isinstance(raw, str) else ""

    @staticmethod
    def _parse(raw: str) -> list[str]:
        """Extract a JSON string array from an LLM completion, defensively."""
        text = str(raw or "").strip()
        if not text:
            return []
        match = _JSON_ARRAY_RE.search(text)
        if match is None:
            return []
        try:
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        suggestions: list[str] = []
        for item in data:
            if not isinstance(item, str):
                continue
            cleaned = item.strip()
            if not cleaned:
                continue
            if len(cleaned) > MAX_SUGGESTION_CHARS:
                cleaned = cleaned[:MAX_SUGGESTION_CHARS]
            suggestions.append(cleaned)
            if len(suggestions) >= MAX_SUGGESTIONS:
                break
        return suggestions

    def _diagnostic_event(self, event: str, **fields: Any) -> None:
        logger = self.diagnostic_log
        record = getattr(logger, "record", None)
        if callable(record):
            try:
                record("reply_suggestions", event, **fields)
            except Exception:
                pass
        else:
            log = getattr(logger, "warning", None)
            if callable(log):
                try:
                    log("[reply_suggestions] %s %s", event, fields)
                except Exception:
                    pass
