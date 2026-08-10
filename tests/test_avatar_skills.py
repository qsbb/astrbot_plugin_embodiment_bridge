from __future__ import annotations

from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_llm import AstrBotLLMAdapter
from astrbot_plugin_quest_avatar_bridge.core.avatar_skills import AvatarSkillRegistry
from astrbot_plugin_quest_avatar_bridge.core.intent_parser import IntentParser


def test_skill_registry_is_allowlisted_and_bounded() -> None:
    intent = AvatarSkillRegistry.invoke(
        "dance",
        {"intensity": 4, "duration_ms": 999_999, "look_at": "user"},
    )
    assert intent is not None
    assert intent.gesture.value == "dance"
    assert intent.intensity == 1
    assert intent.duration_ms == 30_000
    assert AvatarSkillRegistry.invoke("play_file", {"path": "C:/bad"}) is None


def test_intent_parser_accepts_action_call_without_trusting_animation_paths() -> None:
    parsed = IntentParser().parse(
        '{"should_reply":true,"reply_text":"我来跳舞","action":'
        '{"name":"dance","arguments":{}},"intent":'
        '{"emotion":"neutral","gesture":"idle","look_at":"none",'
        '"intensity":0,"duration_ms":0,"reason_code":"placeholder"}}'
    )
    assert parsed.action is not None
    assert parsed.intent.gesture.value == "dance"


def test_llm_prompt_advertises_skill_calls() -> None:
    adapter = AstrBotLLMAdapter(
        object(), chat_provider_id="provider", persona_prompt=""
    )
    prompt = adapter._system_prompt()
    assert '"action": null' in prompt
    assert "Available skills" in prompt
    assert "dance" in prompt
