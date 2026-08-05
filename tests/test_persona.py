from __future__ import annotations

from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_llm import AstrBotLLMAdapter


class ContextStub:
    pass


def build_adapter(**persona: str) -> AstrBotLLMAdapter:
    return AstrBotLLMAdapter(
        ContextStub(),
        chat_provider_id="provider",
        persona_prompt="",
        character_name=persona.get("character_name", ""),
        character_self_reference=persona.get("character_self_reference", ""),
        character_self_description=persona.get("character_self_description", ""),
        character_user_relationship=persona.get("character_user_relationship", ""),
    )


def test_configured_persona_is_structured_in_system_prompt() -> None:
    adapter = build_adapter(
        character_name="凌溪",
        character_self_reference="小溪",
        character_self_description="安静、坦诚的虚拟伙伴",
        character_user_relationship="与用户平等相处的朋友",
    )

    prompt = adapter._system_prompt()

    assert "角色姓名：凌溪" in prompt
    assert "角色自称：小溪" in prompt
    assert "角色自我描述：安静、坦诚的虚拟伙伴" in prompt
    assert "与用户的关系定位：与用户平等相处的朋友" in prompt
    assert "Meta Quest 3 混合现实空间" in prompt
    assert "不得为了显得真实而编造" in prompt
    assert adapter.persona_configured is True
    assert adapter.character_name_configured is True


def test_empty_persona_has_generic_identity_without_inventing_name() -> None:
    adapter = build_adapter()

    prompt = adapter._system_prompt()

    assert "未配置；不得自行编造姓名" in prompt
    assert "只能使用第一人称“我”" in prompt
    assert "不得擅自声称亲属、恋爱、主从或其他亲密关系" in prompt
    assert adapter.persona_configured is False
    assert adapter.character_name_configured is False


def test_relationship_person_id_cannot_define_character_identity() -> None:
    adapter = build_adapter(character_name="Bridge Character")

    prompt = adapter._system_prompt()

    assert "relationship_person_id 只是服务端关系快照选择器" in prompt
    assert "绝不能用于推断角色身份" in prompt
    assert "person-secret" not in prompt
