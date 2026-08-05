from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

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

    def get_all_providers(self) -> list[Any]:
        return self.providers


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


def build_settings(
    *,
    config: NativeConfigStub | dict[str, Any],
    selected: str = "",
) -> OperatorSettings:
    return OperatorSettings(
        context=ContextStub(
            [
                ProviderStub("model-b", "gpt-b", "secret-b"),
                ProviderStub("model-a", "gpt-a", "secret-a"),
            ]
        ),
        config=config,
        llm=LlmStub(selected),
        relationship=RelationshipStub(),
        logger=LoggerStub(),
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
            character_name="Lingxi",
            character_self_reference="I",
            character_self_description="A Quest companion",
            character_user_relationship="trusted companion",
        )
        assert saved["character_name"] == "Lingxi"
        assert saved["persona_configured"] is True
        assert config.saves[-1] == {
            "character_name": "Lingxi",
            "character_self_reference": "I",
            "character_self_description": "A Quest companion",
            "character_user_relationship": "trusted companion",
        }

        config.fail = True
        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_character_persona(
                character_name="After",
                character_self_reference="me",
                character_self_description="changed",
                character_user_relationship="changed",
            )
        assert failed.value.code == "config_save_failed"
        assert settings.llm.character_name == "Lingxi"
        assert config["character_name"] == "Lingxi"

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
