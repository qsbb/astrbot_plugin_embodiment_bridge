from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from ..core.avatar_skills import AvatarSkillRegistry
from ..core.models import InteractionEvent, ProposedIntent


class FastActionUnavailable(RuntimeError):
    """Raised when the optional fast action provider cannot be used."""


FAST_ACTION_TIMEOUT_POLICY_REVISION = "v3"
LEGACY_DEFAULT_TIMEOUT_POLICY_REVISION = "legacy_default_v2"
DEFAULT_FAST_ACTION_TIMEOUT_SECONDS = 6.0
_PARSE_SELECTED = "selected"
_PARSE_NO_ACTION = "no_action"
_PARSE_INVALID = "invalid"


class FastActionDecisionAdapter:
    """Small, action-only LLM adapter kept outside the normal reply pipeline.

    The adapter deliberately receives only the current utterance and a short
    bounded history. It never generates reply text and never exposes provider
    configuration. The resulting action is still validated by the shared
    ``AvatarSkillRegistry`` before it reaches the client.
    """

    def __init__(
        self,
        context: Any,
        *,
        provider_id: str = "",
        enabled: bool = True,
        timeout_seconds: float = DEFAULT_FAST_ACTION_TIMEOUT_SECONDS,
        configured_timeout_seconds: float | None = None,
        timeout_policy_revision: str = "",
        diagnostic_log: Any | None = None,
    ) -> None:
        self.context = context
        self.provider_id = str(provider_id or "").strip()
        self.enabled = bool(enabled)
        configured = (
            float(timeout_seconds)
            if configured_timeout_seconds is None
            else float(configured_timeout_seconds)
        )
        self.configured_timeout_seconds = min(max(configured, 0.5), 15.0)
        self.timeout_policy_revision = str(timeout_policy_revision or "").strip()
        self.timeout_migrated = False
        if (
            configured_timeout_seconds is not None
            and self.timeout_policy_revision
            in {"", "v2", "legacy_default_v1"}
            and abs(self.configured_timeout_seconds - 4.0) < 0.001
        ):
            self.timeout_seconds = DEFAULT_FAST_ACTION_TIMEOUT_SECONDS
            self.timeout_policy_revision = LEGACY_DEFAULT_TIMEOUT_POLICY_REVISION
            self.timeout_migrated = True
        else:
            self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
        self.diagnostic_log = diagnostic_log
        self.last_status = "disabled" if not self.enabled else "not_configured"
        self.last_error = ""
        self.last_duration_ms = 0
        self.last_action = ""
        self.last_phase = "idle"
        self.last_method = "none"

    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.provider_id:
            return "provider_not_configured"
        generator = getattr(self.context, "llm_generate", None)
        catalog = getattr(self.context, "get_all_providers", None)
        if callable(catalog):
            try:
                providers = catalog()
            except Exception:
                return "provider_catalog_unavailable"
            matched = False
            method_available = False
            if isinstance(providers, (list, tuple)):
                for provider in providers:
                    try:
                        if str(provider.meta().id or "").strip() == self.provider_id:
                            matched = True
                            method_available = callable(
                                getattr(provider, "text_chat_stream", None)
                            ) or callable(getattr(provider, "text_chat", None))
                            break
                    except Exception:
                        continue
            if not matched:
                return "selected_missing"
            if method_available or callable(generator):
                return "ready"
            return "llm_api_unavailable"
        return "ready" if callable(generator) else "llm_api_unavailable"

    def configure(
        self,
        *,
        enabled: bool | None = None,
        provider_id: str | None = None,
        timeout_seconds: float | None = None,
        timeout_policy_revision: str | None = None,
        configured_timeout_seconds: float | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if provider_id is not None:
            self.provider_id = str(provider_id or "").strip()
        if timeout_seconds is not None:
            self.timeout_seconds = min(max(float(timeout_seconds), 0.5), 15.0)
            self.configured_timeout_seconds = (
                min(max(float(configured_timeout_seconds), 0.5), 15.0)
                if configured_timeout_seconds is not None
                else self.timeout_seconds
            )
            self.timeout_migrated = False
        if timeout_policy_revision is not None:
            self.timeout_policy_revision = str(timeout_policy_revision or "").strip()
            self.timeout_migrated = False
        self.last_status = "disabled" if not self.enabled else (
            "ready" if self.provider_id else "not_configured"
        )
        self.last_error = ""
        self.last_action = ""
        self.last_duration_ms = 0
        self.last_phase = "idle"
        self.last_method = "none"

    async def decide(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        interaction: InteractionEvent | None = None,
        supported_actions: tuple[str, ...] | None = None,
    ) -> ProposedIntent | None:
        if not self.enabled:
            self.last_status = "disabled"
            raise FastActionUnavailable("fast_action_disabled")
        if not self.provider_id:
            self.last_status = "not_configured"
            raise FastActionUnavailable("fast_action_provider_not_configured")
        availability = self.availability_reason
        if availability != "ready":
            self.last_status = "unavailable"
            self.last_error = availability
            raise FastActionUnavailable(f"fast_action_{availability}")
        payload = {
            "current_user_text": str(user_text or "")[:2_000],
            "recent_conversation": self._bounded_history(history),
            "interaction": (
                interaction.model_dump(mode="json")
                if interaction is not None
                else None
            ),
        }
        started = asyncio.get_running_loop().time()
        self.last_error = ""
        self.last_action = ""
        self.last_status = "processing"
        self.last_phase = "provider_resolve"
        self.last_method = "none"
        try:
            request = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            allowed_actions = (
                AvatarSkillRegistry.names()
                if supported_actions is None
                else AvatarSkillRegistry.supported_names(supported_actions)
            )
            provider = self._resolve_provider()
            self._record_phase(
                "fast_action.provider_resolved",
                started=started,
                phase="provider_resolve",
                status="ready",
            )
            self.last_phase = "request_queued"
            self._record_phase(
                "fast_action.request_queued",
                started=started,
                phase="request_queued",
                status="processing",
            )
            response = await asyncio.wait_for(
                self._generate(
                    provider,
                    prompt=request,
                    started=started,
                    allowed_actions=allowed_actions,
                ),
                timeout=self.timeout_seconds,
            )
            self.last_phase = "parse"
            parse_status, intent = self._parse_outcome(
                response,
                allowed_actions=allowed_actions,
            )
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            if parse_status == _PARSE_INVALID:
                self.last_status = "invalid_output"
                self.last_error = "fast_action_invalid_output"
                self._record_phase(
                    "fast_action.parse_invalid",
                    started=started,
                    phase="parse",
                    status="invalid",
                    reason_code="fast_action_invalid_output",
                    method=self.last_method,
                )
                raise FastActionUnavailable("fast_action_invalid_output")
            self.last_status = parse_status
            if parse_status == _PARSE_SELECTED and intent is not None:
                self.last_action = intent.gesture.value
            self._record_phase(
                (
                    "fast_action.parsed"
                    if parse_status == _PARSE_SELECTED
                    else "fast_action.parsed_no_action"
                ),
                started=started,
                phase="parse",
                status=self.last_status,
                operation=self.last_action or None,
            )
            return intent
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            self.last_status = "timeout"
            self.last_error = "fast_action_timeout"
            self._record_phase(
                "fast_action.timeout",
                started=started,
                phase=self.last_phase,
                status="timeout",
                reason_code="fast_action_timeout",
                method=self.last_method,
                configured_timeout_ms=self.configured_timeout_seconds * 1000,
                effective_timeout_ms=self.timeout_seconds * 1000,
            )
            raise FastActionUnavailable("fast_action_timeout") from exc
        except FastActionUnavailable:
            raise
        except Exception as exc:
            self.last_duration_ms = max(
                0, int(round((asyncio.get_running_loop().time() - started) * 1000))
            )
            self.last_status = "error"
            self.last_error = type(exc).__name__
            self._record_phase(
                "fast_action.provider_error",
                started=started,
                phase=self.last_phase,
                status="error",
                reason_code="fast_action_failed",
                error_type=type(exc).__name__,
                method=self.last_method,
            )
            raise FastActionUnavailable("fast_action_failed") from exc

    def _resolve_provider(self) -> Any | None:
        """Resolve the selected public Provider without warning on unknown IDs.

        A missing catalog is retained only for old SDK/test compatibility; current
        AstrBot versions expose ``get_all_providers`` and use the direct Provider
        path below.
        """

        catalog = getattr(self.context, "get_all_providers", None)
        if not callable(catalog):
            return None
        try:
            providers = catalog()
        except Exception as exc:
            raise FastActionUnavailable(
                "fast_action_provider_catalog_unavailable"
            ) from exc
        if not isinstance(providers, (list, tuple)):
            raise FastActionUnavailable("fast_action_provider_catalog_unavailable")
        for provider in providers:
            try:
                candidate = str(provider.meta().id or "").strip()
            except Exception:
                continue
            if candidate == self.provider_id:
                return provider
        raise FastActionUnavailable("fast_action_selected_missing")

    async def _generate(
        self,
        provider: Any | None,
        *,
        prompt: str,
        started: float,
        allowed_actions: tuple[str, ...],
    ) -> str:
        if provider is not None:
            stream_factory = getattr(provider, "text_chat_stream", None)
            if callable(stream_factory):
                try:
                    return await self._generate_stream(
                        stream_factory,
                        prompt=prompt,
                        started=started,
                        allowed_actions=allowed_actions,
                    )
                except (AttributeError, NotImplementedError, TypeError):
                    # The base AstrBot Provider exposes the streaming method even
                    # when a concrete adapter does not implement it.
                    pass

            chat = getattr(provider, "text_chat", None)
            if callable(chat):
                self.last_method = "direct_chat"
                response = await chat(
                    prompt=prompt,
                    system_prompt=self._system_prompt(allowed_actions),
                    func_tool=None,
                    request_max_retries=1,
                )
                self.last_phase = "complete"
                self._record_phase(
                    "fast_action.provider_completed",
                    started=started,
                    phase="complete",
                    status="completed",
                    method=self.last_method,
                )
                return self._completion_text(response)

        generator = getattr(self.context, "llm_generate", None)
        if not callable(generator):
            raise FastActionUnavailable("fast_action_llm_api_unavailable")
        self.last_method = "context_generate"
        response = await generator(
            chat_provider_id=self.provider_id,
            prompt=prompt,
            system_prompt=self._system_prompt(allowed_actions),
            request_max_retries=1,
        )
        self.last_phase = "complete"
        self._record_phase(
            "fast_action.provider_completed",
            started=started,
            phase="complete",
            status="completed",
            method=self.last_method,
        )
        return self._completion_text(response)

    async def _generate_stream(
        self,
        stream_factory: Any,
        *,
        prompt: str,
        started: float,
        allowed_actions: tuple[str, ...],
    ) -> str:
        self.last_method = "direct_stream"
        stream = stream_factory(
            prompt=prompt,
            system_prompt=self._system_prompt(allowed_actions),
            func_tool=None,
            request_max_retries=1,
        )
        iterator = stream.__aiter__()
        accumulated = ""
        final_text = ""
        early_text = ""
        received = False
        try:
            while True:
                try:
                    response = await anext(iterator)
                except StopAsyncIteration:
                    break
                if not received:
                    received = True
                    self.last_phase = "first_chunk"
                    self._record_phase(
                        "fast_action.first_chunk",
                        started=started,
                        phase="first_chunk",
                        status="processing",
                        method=self.last_method,
                    )
                text = self._completion_text(response)
                if bool(getattr(response, "is_chunk", False)):
                    if text:
                        accumulated = self._merge_stream_text(accumulated, text)
                        complete, _intent = self._complete_stream_result(
                            accumulated,
                            allowed_actions=allowed_actions,
                        )
                        if complete:
                            self.last_phase = "parse"
                            early_text = accumulated
                            break
                elif text:
                    final_text = text
        finally:
            close = getattr(iterator, "aclose", None)
            if callable(close):
                await self._close_iterator_bounded(close)
        self.last_phase = "complete"
        self._record_phase(
            "fast_action.provider_completed",
            started=started,
            phase="complete",
            status="completed",
            method=self.last_method,
        )
        return early_text or final_text or accumulated

    @staticmethod
    def _merge_stream_text(previous: str, piece: str) -> str:
        """Accept both delta chunks and cumulative-prefix chunks."""
        if not previous:
            return piece
        if piece.startswith(previous):
            return piece
        if previous.startswith(piece):
            return previous
        return previous + piece

    @classmethod
    def _complete_stream_result(
        cls,
        raw: str,
        *,
        allowed_actions: tuple[str, ...],
    ) -> tuple[bool, ProposedIntent | None]:
        text = raw.strip()
        if text.startswith("```"):
            if not re.search(r"\s*```$", text):
                return False, None
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        if not text:
            return False, None
        try:
            json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False, None
        _status, intent = cls._parse_outcome(
            text,
            allowed_actions=allowed_actions,
        )
        # Once one complete JSON value has arrived, schema validation can run
        # immediately. Invalid objects must not keep a hanging stream alive
        # until the provider timeout.
        return True, intent

    @staticmethod
    async def _close_iterator_bounded(close: Any) -> None:
        try:
            task = asyncio.create_task(close())
            done, _pending = await asyncio.wait({task}, timeout=0.25)
            if task not in done:
                task.cancel()
        except Exception:
            return

    @staticmethod
    def _completion_text(response: Any) -> str:
        raw = getattr(response, "completion_text", response)
        return raw if isinstance(raw, str) else ""

    def _record_phase(
        self,
        event: str,
        *,
        started: float,
        phase: str,
        status: str,
        **fields: Any,
    ) -> None:
        sink = self.diagnostic_log
        record = getattr(sink, "record", None)
        if not callable(record):
            return
        try:
            record(
                event,
                component="action",
                phase=phase,
                status=status,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                **{key: value for key, value in fields.items() if value is not None},
            )
        except Exception:
            return

    @staticmethod
    def _bounded_history(
        history: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        bounded: list[dict[str, str]] = []
        for item in (history or [])[-4:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            text = str(item.get("text") or "").strip()
            if role not in {"user", "assistant"} or not text:
                continue
            bounded.append({"role": role, "text": text[:800]})
        return bounded

    @staticmethod
    def _system_prompt(allowed_actions: tuple[str, ...] | None = None) -> str:
        names = ",".join(
            AvatarSkillRegistry.names()
            if allowed_actions is None
            else allowed_actions
        )
        return (
            "You are the fast action selector for an embodied avatar. "
            "Decide only whether the avatar should perform one allowlisted "
            "action in response to the user's current utterance. Prefer an "
            "explicitly requested action. For non-command conversation, autonomously "
            "choose one subtle natural action when the user's emotion, greeting, "
            "agreement, hesitation, or shared context gives a clear reason; this does "
            "not require an explicit action request. Use wave for a greeting, reunion, "
            "or farewell; bow for a clear thanks or apology; raise_hand for enthusiastic "
            "agreement, volunteering, or a strong celebration; and dance only for an "
            "unmistakably celebratory moment. Keep sit, lie, crouch, turn_half, and "
            "dance_next explicit-only unless the current utterance directly asks for "
            "them. Still use null for routine factual exchange or when no action adds "
            "meaning. Do not answer the user. "
            "Return exactly one compact JSON object and no markdown: "
            '{"action":null} or '
            '{"action":{"name":"<name>","arguments":{"emotion":"neutral",'
            '"intensity":0.45,"duration_ms":2000,"look_at":"user",'
            '"style":"natural"}}}. turn_half may use angle_degrees; crouch may '
            "use depth and hold_ms. "
            "Use null for routine factual exchange, descriptions, quoted requests, "
            "ambiguous or unsafe wording, and whenever no natural action adds "
            "meaning. Never invent names, "
            "files, bones, paths, or arguments. Allowlisted names: "
            + names
        )

    @staticmethod
    def _parse(
        raw: Any,
        *,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> ProposedIntent | None:
        _status, intent = FastActionDecisionAdapter._parse_outcome(
            raw,
            allowed_actions=allowed_actions,
        )
        return intent

    @staticmethod
    def _parse_outcome(
        raw: Any,
        *,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> tuple[str, ProposedIntent | None]:
        if not isinstance(raw, str) or not raw.strip():
            return _PARSE_INVALID, None
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _PARSE_INVALID, None
        if not isinstance(payload, dict):
            return _PARSE_INVALID, None
        if set(payload) != {"action"}:
            return _PARSE_INVALID, None
        action = payload.get("action")
        if action is None:
            return _PARSE_NO_ACTION, None
        if isinstance(action, str):
            name = action
            arguments = {}
        elif isinstance(action, dict):
            if "name" not in action or set(action) - {"name", "arguments"}:
                return _PARSE_INVALID, None
            name = action.get("name")
            arguments = action.get("arguments", {})
        else:
            return _PARSE_INVALID, None
        if not isinstance(arguments, dict):
            return _PARSE_INVALID, None
        normalized = AvatarSkillRegistry.normalize_action_name(name)
        if normalized is None:
            return _PARSE_INVALID, None
        if allowed_actions is not None and normalized not in allowed_actions:
            return _PARSE_INVALID, None
        intent = AvatarSkillRegistry.invoke(normalized, arguments)
        return (
            (_PARSE_SELECTED, intent)
            if intent is not None
            else (_PARSE_INVALID, None)
        )

    def snapshot(self) -> dict[str, Any]:
        availability = self.availability_reason
        status = self.last_status
        if availability != "ready" and status != "processing":
            status = availability
        elif availability == "ready" and status == "not_configured":
            status = "ready"
        return {
            "enabled": self.enabled,
            "available": self.available,
            "availability_reason": availability,
            "selected": bool(self.provider_id),
            "selected_id": self.provider_id,
            "status": status,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
            "last_action": self.last_action,
            "last_phase": self.last_phase,
            "last_method": self.last_method,
            "timeout_seconds": self.timeout_seconds,
            "configured_timeout_seconds": self.configured_timeout_seconds,
            "effective_timeout_seconds": self.timeout_seconds,
            "timeout_policy_revision": self.timeout_policy_revision
            or FAST_ACTION_TIMEOUT_POLICY_REVISION,
            "timeout_migrated": self.timeout_migrated,
        }

    async def close(self) -> None:
        return None
