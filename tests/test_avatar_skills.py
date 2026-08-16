from __future__ import annotations

from astrbot_plugin_embodiment_bridge.adapters.astrbot_llm import AstrBotLLMAdapter
from astrbot_plugin_embodiment_bridge.core.avatar_skills import AvatarSkillRegistry
from astrbot_plugin_embodiment_bridge.core.intent_parser import IntentParser


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


def test_semantic_aliases_normalize_without_accepting_paths() -> None:
    assert AvatarSkillRegistry.normalize_action_name("next_dance") == "dance_next"
    assert AvatarSkillRegistry.normalize_action_name("switch-dance") == "dance_next"
    assert AvatarSkillRegistry.normalize_action_name("hand_wave") == "wave"
    assert AvatarSkillRegistry.normalize_action_name("turn around") == "turn_half"
    assert AvatarSkillRegistry.normalize_action_name("C:/motion.vmd") is None


def test_unavailable_motion_catalog_is_semantic_only() -> None:
    assert AvatarSkillRegistry.catalog_status("dance") == "not_declared"
    assert AvatarSkillRegistry.catalog_status("wave") == "not_applicable"
    assert AvatarSkillRegistry.motion_selection("dance") == "recommended_imported"
    assert AvatarSkillRegistry.motion_selection("next_dance") == "next_imported"
    assert AvatarSkillRegistry.motion_selection("wave") == "none"


def test_autonomous_fallback_is_social_conservative_and_capability_bounded() -> None:
    supported = AvatarSkillRegistry.names()
    expected = {
        "心夏，你好呀": ("wave", "autonomous_greeting"),
        "介绍一下你自己": ("wave", "autonomous_introduction"),
        "谢谢你啦": ("bow", "autonomous_appreciation"),
        "好的": ("nod", "autonomous_agreement"),
        "成功了！": ("raise_hand", "autonomous_celebration"),
    }
    for text, (gesture, reason) in expected.items():
        intent = AvatarSkillRegistry.autonomous_fallback(text, supported)
        assert intent is not None
        assert intent.gesture.value == gesture
        assert intent.reason_code == reason

    for text in (
        "今天天气怎么样",
        "为什么要说谢谢",
        "翻译一下 hello",
        "他说“你好”是什么意思",
        "这是一段很长的普通事实描述，不应该因为其中偶然包含你好两个字就触发任何自主动作",
    ):
        assert AvatarSkillRegistry.autonomous_fallback(text, supported) is None
    assert AvatarSkillRegistry.autonomous_fallback("你好", ("talk",)) is None


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
