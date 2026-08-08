from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_persona import (
    AstrBotPersonaAdapter,
)
from astrbot_plugin_quest_avatar_bridge.core.operator_settings import (
    OperatorSettings,
    OperatorSettingsError,
)


class LoggerStub:
    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass


class ProviderStub:
    def __init__(self, provider_id: str, model: str, secret: str) -> None:
        self.provider_config = {"id": provider_id, "key": secret}
        self._meta = SimpleNamespace(
            id=provider_id,
            model=model,
            type="openai",
            provider_type="chat_completion",
        )

    def meta(self) -> Any:
        return self._meta


class ContextStub:
    def __init__(self, providers: list[Any]) -> None:
        self.providers = providers
        self.persona_manager = PersonaManagerStub()
        self.platforms = {"platform-a": object(), "platform-b": object()}

    def get_all_providers(self) -> list[Any]:
        return self.providers

    def get_platform_inst(self, platform_id: str) -> Any | None:
        return self.platforms.get(platform_id)


class NativeConfigStub(dict[str, Any]):
    def __init__(self, *args: Any, fail: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fail = fail
        self.saves: list[dict[str, Any]] = []

    async def save_config_async(self, changes: dict[str, Any]) -> bool:
        self.update(changes)
        self.saves.append(dict(changes))
        if self.fail:
            raise OSError("disk failed")
        return True


class LlmStub:
    def __init__(self, provider_id: str = "") -> None:
        self.chat_provider_id = provider_id
        self.character_name = ""
        self.character_self_reference = ""
        self.character_self_description = ""
        self.character_user_relationship = ""

    def configure_provider(self, provider_id: str) -> None:
        self.chat_provider_id = provider_id

    @property
    def persona_configured(self) -> bool:
        return any(
            (
                self.character_name,
                self.character_self_reference,
                self.character_self_description,
                self.character_user_relationship,
            )
        )

    @property
    def character_name_configured(self) -> bool:
        return bool(self.character_name)

    def configure_persona(self, **values: str) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class RelationshipStub:
    def __init__(self, person_id: str = "") -> None:
        self.person_id = person_id

    def configure_person_id(self, person_id: str) -> None:
        self.person_id = person_id


class IdentityStub:
    def __init__(self, platform_id: str = "") -> None:
        self.trusted_platform_id = platform_id

    def configure_trusted_platform(self, platform_id: str) -> None:
        self.trusted_platform_id = platform_id


class MessagePipelineStub:
    def __init__(self, context: ContextStub, platform_id: str = "") -> None:
        self.context = context
        self.platform_id = platform_id

    @property
    def availability_reason(self) -> str:
        if not self.platform_id:
            return "trusted_platform_not_configured"
        if self.context.get_platform_inst(self.platform_id) is None:
            return "trusted_platform_unavailable"
        return "ready"

    def configure_platform(self, platform_id: str) -> None:
        self.platform_id = platform_id


class PersonaManagerStub:
    def __init__(self) -> None:
        self.personas = {
            "persona-a": SimpleNamespace(
                persona_id="persona-a",
                system_prompt="private persona prompt",
                tools=["private-tool"],
                begin_dialogs=["private-dialog"],
            )
        }

    async def get_persona(self, persona_id: str) -> Any:
        if persona_id not in self.personas:
            raise ValueError("missing")
        return self.personas[persona_id]

    async def get_default_persona_v3(self, umo: Any = None) -> dict[str, str]:
        assert umo is None
        return {"name": "default", "prompt": "private default prompt"}

    async def get_all_personas(self) -> list[Any]:
        return list(self.personas.values())


def build_settings(
    *,
    config: NativeConfigStub | dict[str, Any],
    selected: str = "",
) -> OperatorSettings:
    context = ContextStub(
        [
            ProviderStub("model-b", "gpt-b", "secret-b"),
            ProviderStub("model-a", "gpt-a", "secret-a"),
        ]
    )
    identity = IdentityStub(str(config.get("trusted_platform_id", "") or ""))
    pipeline = MessagePipelineStub(context, identity.trusted_platform_id)
    return OperatorSettings(
        context=context,
        config=config,
        llm=LlmStub(selected),
        relationship=RelationshipStub(),
        persona=AstrBotPersonaAdapter(context),
        logger=LoggerStub(),
        identity=identity,
        message_pipeline=pipeline,
    )


def test_snapshot_lists_only_safe_provider_metadata() -> None:
    settings = build_settings(config={}, selected="model-a")

    snapshot = settings.snapshot()

    assert snapshot["status"] == "ok"
    assert snapshot["selected_available"] is True
    assert snapshot["providers"] == [
        {
            "id": "model-a",
            "model": "gpt-a",
            "adapter_type": "openai",
            "provider_type": "chat_completion",
        },
        {
            "id": "model-b",
            "model": "gpt-b",
            "adapter_type": "openai",
            "provider_type": "chat_completion",
        },
    ]
    assert "key" not in repr(snapshot)
    assert snapshot["config_writable"] is False


def test_model_and_relationship_selection_persist_before_runtime_switch() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"chat_provider_id": "model-a"})
        settings = build_settings(config=config, selected="model-a")

        model_snapshot = await settings.save_chat_provider_id("model-b")
        assert model_snapshot["selected_id"] == "model-b"
        assert settings.llm.chat_provider_id == "model-b"
        assert config["chat_provider_id"] == "model-b"

        await settings.save_relationship_person_id("person-a")
        assert settings.relationship.person_id == "person-a"
        assert config["relationship_person_id"] == "person-a"
        assert config.saves == [
            {"chat_provider_id": "model-b"},
            {"relationship_person_id": "person-a"},
        ]

    asyncio.run(scenario())


def test_trusted_platform_persists_and_updates_runtime_immediately() -> None:
    async def scenario() -> None:
        config = NativeConfigStub()
        settings = build_settings(config=config)

        saved = await settings.save_trusted_platform_id("platform-a")

        assert saved == {
            "trusted_platform_id": "platform-a",
            "configured": True,
            "available": True,
            "availability_reason": "ready",
            "config_writable": True,
        }
        assert config.saves == [{"trusted_platform_id": "platform-a"}]
        assert settings.identity.trusted_platform_id == "platform-a"
        assert settings.message_pipeline.platform_id == "platform-a"

        cleared = await settings.save_trusted_platform_id("")
        assert cleared["availability_reason"] == "trusted_platform_not_configured"
        assert settings.identity.trusted_platform_id == ""
        assert settings.message_pipeline.platform_id == ""

    asyncio.run(scenario())


def test_invalid_missing_or_failed_platform_save_keeps_runtime_selection() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"trusted_platform_id": "platform-a"})
        settings = build_settings(config=config)

        for value, code in (
            ("bad|platform", "invalid_trusted_platform_id"),
            ("bad platform", "invalid_trusted_platform_id"),
            ("missing", "trusted_platform_not_available"),
        ):
            with pytest.raises(OperatorSettingsError) as invalid:
                await settings.save_trusted_platform_id(value)
            assert invalid.value.code == code
            assert settings.identity.trusted_platform_id == "platform-a"
            assert settings.message_pipeline.platform_id == "platform-a"

        config.fail = True
        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_trusted_platform_id("platform-b")
        assert failed.value.code == "config_save_failed"
        assert config["trusted_platform_id"] == "platform-a"
        assert settings.identity.trusted_platform_id == "platform-a"
        assert settings.message_pipeline.platform_id == "platform-a"

    asyncio.run(scenario())


def test_unknown_provider_and_save_failure_do_not_change_runtime() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"chat_provider_id": "model-a"}, fail=True)
        settings = build_settings(config=config, selected="model-a")

        with pytest.raises(OperatorSettingsError) as unknown:
            await settings.save_chat_provider_id("missing")
        assert unknown.value.code == "chat_provider_not_available"

        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_chat_provider_id("model-b")
        assert failed.value.code == "config_save_failed"
        assert settings.llm.chat_provider_id == "model-a"
        assert config["chat_provider_id"] == "model-a"

    asyncio.run(scenario())


def test_persona_persists_atomically_and_save_failure_keeps_runtime() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"character_name": "Before"})
        settings = build_settings(config=config)
        settings.llm.character_name = "Before"

        saved = await settings.save_character_persona(
            persona_source_mode="manual_override",
            astrbot_persona_id="",
            character_name="Lingxi",
            character_self_reference="I",
            character_self_description="A Quest companion",
            character_user_relationship="trusted companion",
        )
        assert saved["character_name"] == "Lingxi"
        assert saved["persona_configured"] is True
        assert config.saves[-1] == {
            "persona_source_mode": "manual_override",
            "astrbot_persona_id": "",
            "character_name": "Lingxi",
            "character_self_reference": "I",
            "character_self_description": "A Quest companion",
            "character_user_relationship": "trusted companion",
        }

        config.fail = True
        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_character_persona(
                persona_source_mode="manual_override",
                astrbot_persona_id="",
                character_name="After",
                character_self_reference="me",
                character_self_description="changed",
                character_user_relationship="changed",
            )
        assert failed.value.code == "config_save_failed"
        assert settings.llm.character_name == "Lingxi"
        assert config["character_name"] == "Lingxi"

    asyncio.run(scenario())


def test_astrbot_persona_selection_is_safe_and_prompt_is_never_projected() -> None:
    async def scenario() -> None:
        config = NativeConfigStub()
        settings = build_settings(config=config)

        overview = await settings.persona_overview()
        assert overview["source"] == "astrbot_default"
        assert overview["personas"] == [{"id": "persona-a"}]
        assert "private persona prompt" not in repr(overview)
        assert "private default prompt" not in repr(overview)
        assert "private-tool" not in repr(overview)

        saved = await settings.save_character_persona(
            persona_source_mode="astrbot",
            astrbot_persona_id="persona-a",
            character_name="manual fallback",
            character_self_reference="I",
            character_self_description="manual description",
            character_user_relationship="friend",
        )
        assert saved["source"] == "astrbot_selected"
        assert saved["astrbot_persona_id"] == "persona-a"
        assert saved["character_name_configured"] is False
        assert config["persona_source_mode"] == "astrbot"

        with pytest.raises(OperatorSettingsError) as missing:
            await settings.save_character_persona(
                persona_source_mode="astrbot",
                astrbot_persona_id="missing",
                character_name="",
                character_self_reference="",
                character_self_description="",
                character_user_relationship="",
            )
        assert missing.value.code == "persona_not_available"

    asyncio.run(scenario())


def test_persona_source_save_failure_keeps_runtime_selection() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {"persona_source_mode": "astrbot", "astrbot_persona_id": ""},
            fail=True,
        )
        settings = build_settings(config=config)

        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_character_persona(
                persona_source_mode="astrbot",
                astrbot_persona_id="persona-a",
                character_name="",
                character_self_reference="",
                character_self_description="",
                character_user_relationship="",
            )
        assert failed.value.code == "config_save_failed"
        assert settings.persona.persona_id == ""
        assert settings.persona.source_mode == "astrbot"

    asyncio.run(scenario())


def test_superseded_config_revision_does_not_change_runtime() -> None:
    class SupersededConfigStub(NativeConfigStub):
        async def save_config_async(self, changes: dict[str, Any]) -> bool:
            self.update(changes)
            self.saves.append(dict(changes))
            return False

    async def scenario() -> None:
        config = SupersededConfigStub({"chat_provider_id": "model-a"})
        settings = build_settings(config=config, selected="model-a")

        with pytest.raises(OperatorSettingsError) as superseded:
            await settings.save_chat_provider_id("model-b")
        assert superseded.value.code == "config_save_superseded"
        assert settings.llm.chat_provider_id == "model-a"
