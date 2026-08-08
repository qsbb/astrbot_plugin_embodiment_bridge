from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ..core.models import (
    Emotion,
    Gesture,
    LookAt,
    ModelDecision,
    ProposedIntent,
)
from ..core.session_manager import SessionState


class MessagePipelineUnavailable(RuntimeError):
    pass


class MessagePipelineEmpty(RuntimeError):
    pass


class AstrBotMessagePipelineAdapter:
    """Submit an authorized Quest utterance to AstrBot's normal EventBus."""

    def __init__(
        self,
        context: Any,
        logger: Any,
        *,
        enabled: bool = True,
        platform_id: str = "",
        timeout_seconds: float = 90.0,
    ) -> None:
        self.context = context
        self.logger = logger
        self.enabled = bool(enabled)
        self.platform_id = str(platform_id or "").strip()
        self.timeout_seconds = min(max(float(timeout_seconds), 10.0), 180.0)
        self.status = "enabled" if self.enabled else "disabled"

    @property
    def available(self) -> bool:
        return self.availability_reason == "ready"

    @property
    def availability_reason(self) -> str:
        if not self.enabled:
            return "disabled"
        if not self.platform_id:
            return "trusted_platform_not_configured"
        queue_getter = getattr(self.context, "get_event_queue", None)
        platform_getter = getattr(self.context, "get_platform_inst", None)
        if not callable(queue_getter) or not callable(platform_getter):
            return "astrbot_event_api_unavailable"
        try:
            if platform_getter(self.platform_id) is None:
                return "trusted_platform_unavailable"
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return "trusted_platform_unavailable"
        return "ready"

    def configure_platform(self, platform_id: str) -> None:
        self.platform_id = str(platform_id or "").strip()
        self.status = "enabled" if self.enabled else "disabled"

    async def generate(
        self,
        *,
        session: SessionState,
        user_text: str,
    ) -> ModelDecision:
        if not self.enabled:
            raise MessagePipelineUnavailable("message_pipeline_disabled")
        if not session.protected_context_authorized:
            raise MessagePipelineUnavailable("protected_context_not_authorized")
        if not self.platform_id:
            raise MessagePipelineUnavailable("trusted_platform_not_configured")

        platform_getter = getattr(self.context, "get_platform_inst", None)
        queue_getter = getattr(self.context, "get_event_queue", None)
        if not callable(platform_getter) or not callable(queue_getter):
            raise MessagePipelineUnavailable("astrbot_event_api_unavailable")
        platform = platform_getter(self.platform_id)
        if platform is None:
            raise MessagePipelineUnavailable("trusted_platform_unavailable")

        event = _build_capture_event(
            platform_meta=platform.meta(),
            user_text=user_text,
            user_id=session.user_id,
            bot_id=session.bot_id,
            group_id=session.group_id,
        )
        try:
            queue_getter().put_nowait(event)
        except (AttributeError, asyncio.QueueFull, RuntimeError) as exc:
            self.status = "queue_unavailable"
            raise MessagePipelineUnavailable("astrbot_event_queue_unavailable") from exc

        self.status = "processing"
        try:
            await asyncio.wait_for(event.wait_completed(), timeout=self.timeout_seconds)
        except TimeoutError as exc:
            self.status = "timeout"
            raise MessagePipelineUnavailable("astrbot_pipeline_timeout") from exc

        reply = event.captured_text().strip()
        if not reply:
            reply = _delivery_plan_text(event).strip()
        if not reply:
            self.status = "empty_reply"
            raise MessagePipelineEmpty("astrbot_pipeline_empty_reply")

        self.status = "ok"
        reply = reply[:4000]
        return ModelDecision(
            should_reply=True,
            reply_text=reply,
            intent=ProposedIntent(
                emotion=Emotion.NEUTRAL,
                gesture=Gesture.TALK,
                look_at=LookAt.USER,
                intensity=0.38,
                duration_ms=min(8_000, max(1_200, len(reply) * 85)),
                reason_code="astrbot_message_pipeline",
            ),
        )

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "availability_reason": self.availability_reason,
            "status": self.status,
            "mode": "astrbot_event_bus",
            "admin_inheritance": False,
            "server_tts_suppressed": True,
        }

    async def close(self) -> None:
        return None


def _build_capture_event(
    *,
    platform_meta: Any,
    user_text: str,
    user_id: str,
    bot_id: str,
    group_id: str,
) -> Any:
    # Imports stay lazy so plugin discovery still degrades cleanly on older
    # AstrBot builds that do not expose the complete EventBus ABI.
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.message_components import Plain
    from astrbot.core.platform import (
        AstrBotMessage,
        Group,
        MessageMember,
        MessageType,
    )

    class QuestPipelineEvent(AstrMessageEvent):
        def __init__(self, message_obj: Any, session_id: str) -> None:
            super().__init__(
                message_str=user_text,
                message_obj=message_obj,
                platform_meta=platform_meta,
                session_id=session_id,
            )
            self._quest_done = asyncio.Event()
            self._quest_messages: list[str] = []
            self._quest_stream = ""

        async def send(self, message: Any) -> None:
            self._has_send_oper = True
            self._capture(message, streaming=False)

        async def send_streaming(
            self,
            generator: Any,
            use_fallback: bool = False,
        ) -> None:
            del use_fallback
            self._has_send_oper = True
            async for message in generator:
                self._capture(message, streaming=True)

        async def send_typing(self) -> None:
            return None

        async def stop_typing(self) -> None:
            return None

        def cleanup_temporary_local_files(self) -> None:
            try:
                super().cleanup_temporary_local_files()
            finally:
                self._quest_done.set()

        async def wait_completed(self) -> None:
            await self._quest_done.wait()

        def captured_text(self) -> str:
            values = [value for value in self._quest_messages if value.strip()]
            if self._quest_stream.strip():
                values.append(self._quest_stream)
            deduplicated: list[str] = []
            for value in values:
                cleaned = value.strip()
                if cleaned and (not deduplicated or deduplicated[-1] != cleaned):
                    deduplicated.append(cleaned)
            return "\n".join(deduplicated)

        def _capture(self, message: Any, *, streaming: bool) -> None:
            if message is None:
                return
            getter = getattr(message, "get_plain_text", None)
            text = str(getter() if callable(getter) else "")
            if not text:
                return
            if not streaming:
                self._quest_messages.append(text)
                return
            if text.startswith(self._quest_stream):
                self._quest_stream = text
            elif not self._quest_stream.endswith(text):
                self._quest_stream += text

    message = AstrBotMessage()
    message.self_id = str(bot_id)
    message.sender = MessageMember(str(user_id), "Quest")
    message.type = MessageType.GROUP_MESSAGE if group_id else MessageType.FRIEND_MESSAGE
    message.session_id = str(group_id or user_id)
    message.message_id = "quest-" + uuid.uuid4().hex
    message.message = [Plain(str(user_text))]
    message.message_str = str(user_text)
    message.raw_message = {"source": "quest_avatar_bridge"}
    message.timestamp = int(time.time())
    if group_id:
        message.group = Group(group_id=str(group_id))

    event = QuestPipelineEvent(message, message.session_id)
    # A Quest bridge session can never inherit AstrBot administrator role from
    # the bound raw account. Authorization remains the identity plugin's job.
    event.set_extra("_api_key_allow_admin_role", False)
    event.set_extra("quest_avatar_bridge", True)
    # Quest streams TTS through Protocol 1.0 after the text decision. Mark the
    # synthetic event handled so voice_hub does not synthesize the same reply.
    event.set_extra("mimo_tts_handled", True)
    return event


def _delivery_plan_text(event: Any) -> str:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return ""
    plan = getter("conversation_flow.delivery_plan")
    if isinstance(plan, dict) and plan.get("version") == "1.0":
        return str(plan.get("original_text") or "")
    request_context = getter("ningxin.request_context.v1")
    if not isinstance(request_context, dict):
        return ""
    artifacts = request_context.get("artifacts")
    if not isinstance(artifacts, dict):
        return ""
    conversation_flow = artifacts.get("conversation_flow")
    if not isinstance(conversation_flow, dict):
        return ""
    plan = conversation_flow.get("delivery_plan")
    return str(plan.get("original_text") or "") if isinstance(plan, dict) else ""
