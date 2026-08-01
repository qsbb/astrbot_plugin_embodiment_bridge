from __future__ import annotations

import json
from typing import Any, Protocol

from ..core.intent_parser import IntentParser
from ..core.models import InteractionEvent, ModelDecision


class DecisionGenerator(Protocol):
    async def generate(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]],
        interaction: InteractionEvent | None,
        relationship: dict[str, Any] | None,
    ) -> ModelDecision: ...

    async def close(self) -> None: ...


class AstrBotLLMAdapter:
    def __init__(
        self,
        context: Any,
        *,
        chat_provider_id: str,
        persona_prompt: str,
        parser: IntentParser | None = None,
    ) -> None:
        self.context = context
        self.chat_provider_id = chat_provider_id.strip()
        self.persona_prompt = persona_prompt.strip()
        self.parser = parser or IntentParser()

    @property
    def available(self) -> bool:
        return bool(self.chat_provider_id)

    async def generate(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]],
        interaction: InteractionEvent | None,
        relationship: dict[str, Any] | None,
    ) -> ModelDecision:
        if not self.chat_provider_id:
            raise RuntimeError("chat_provider_id is not configured")

        input_payload: dict[str, Any] = {
            "current_user_text": user_text,
            "recent_conversation": history[-20:],
            "relationship_snapshot": relationship or {},
            "interaction": (
                interaction.model_dump(mode="json") if interaction is not None else None
            ),
        }
        response = await self.context.llm_generate(
            chat_provider_id=self.chat_provider_id,
            prompt=json.dumps(input_payload, ensure_ascii=False, separators=(",", ":")),
            system_prompt=self._system_prompt(),
        )
        return self.parser.parse(response.completion_text)

    def _system_prompt(self) -> str:
        persona = self.persona_prompt or "保持自然、尊重边界、不过度亲昵的角色。"
        return f"""你是 Quest 3 中角色的决策层。角色设定：
{persona}

你必须结合当前对话、关系快照和交互事实，独立决定是否回应以及角色反应。触碰名称不是固定情绪映射：摸头不必开心，捏脸不必害羞；可以接受、拒绝、回避、口头回应或不回应。不要输出骨骼名、Morph 名、Unity 对象名、动画路径或任何模型相关标识。

只输出一个 JSON 对象，不要 Markdown，不要解释，不要增加字段。结构：
{{
  "should_reply": true,
  "reply_text": "简短自然的回复；不回应时为空字符串",
  "intent": {{
    "emotion": "neutral|happy|shy|surprised|concerned|uncomfortable",
    "gesture": "idle|talk|wave|bow|handshake|head_pat|cheek_pinch|refuse|step_back",
    "look_at": "user|hand|away|none",
    "intensity": 0.0,
    "duration_ms": 0,
    "reason_code": "小写英文下划线原因码"
  }}
}}
intensity 必须在 0 到 1，duration_ms 必须在 0 到 30000。"""

    async def close(self) -> None:
        return None
