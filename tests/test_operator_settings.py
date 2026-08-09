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
from astrbot_plugin_quest_avatar_bridge.adapters.identity_control_plane import (
    IdentityControlPlaneError,
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


class PlatformStub:
    def __init__(
        self,
        platform_id: str,
        adapter_type: str,
        display_name: str,
    ) -> None:
        self.config = {"token": "platform-secret"}
        self._meta = SimpleNamespace(
            id=platform_id,
            name=adapter_type,
            adapter_display_name=display_name,
        )

    def meta(self) -> Any:
        return self._meta


class ContextStub:
    def __init__(self, providers: list[Any]) -> None:
        self.providers = providers
        self.persona_manager = PersonaManagerStub()
        self.platforms = {
            "platform-a": PlatformStub("platform-a", "aiocqhttp", "OneBot 11"),
            "platform-b": PlatformStub("platform-b", "telegram", "Telegram"),
        }
        self.platform_manager = SimpleNamespace(
            get_insts=lambda: list(self.platforms.values())
        )

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
        self.quest_persona_prompt = ""

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

    def configure_quest_persona(self, prompt: str) -> None:
        self.quest_persona_prompt = str(prompt or "")


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

    def configure_local_binding(self, **values: str) -> None:
        self.trusted_platform_id = values["platform_id"]
        self.local_binding = dict(values)

    def configure_relationship_person_id(self, person_id: str) -> None:
        self.relationship_person_id = person_id

    def configure_sync_ready(self, ready: bool) -> None:
        self.sync_ready = ready

    def clear_local_binding(self) -> None:
        self.local_binding = None
        self.relationship_person_id = ""
        self.sync_ready = True


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


class IdentityStoreStub:
    def __init__(self, bot_id: str = "", user_id: str = "") -> None:
        self.identity = (
            SimpleNamespace(bot_id=bot_id, user_id=user_id)
            if bot_id and user_id
            else None
        )

    async def save(self, *, bot_id: str, user_id: str) -> None:
        self.identity = SimpleNamespace(bot_id=bot_id, user_id=user_id)

    async def clear(self) -> None:
        self.identity = None


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
        identity_store=IdentityStoreStub(
            str(config.get("pairing_bot_id", "") or ""),
            str(config.get("pairing_user_id", "") or ""),
        ),
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


def test_platform_snapshot_lists_only_safe_loaded_platform_metadata() -> None:
    settings = build_settings(config={})

    snapshot = settings.platform_snapshot()

    assert snapshot["platforms_status"] == "ok"
    assert snapshot["platforms"] == [
        {
            "id": "platform-a",
            "adapter_type": "aiocqhttp",
            "display_name": "OneBot 11",
        },
        {
            "id": "platform-b",
            "adapter_type": "telegram",
            "display_name": "Telegram",
        },
    ]
    assert "platform-secret" not in repr(snapshot)


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
            "platforms_status": "ok",
            "platforms": [
                {
                    "id": "platform-a",
                    "adapter_type": "aiocqhttp",
                    "display_name": "OneBot 11",
                },
                {
                    "id": "platform-b",
                    "adapter_type": "telegram",
                    "display_name": "Telegram",
                },
            ],
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


def test_resolved_relationship_identity_updates_event_identity_in_one_save() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "trusted_client_id": "quest-room",
                "pairing_api_principal_digest": "sha256:" + "a" * 64,
            }
        )
        settings = build_settings(config=config)

        snapshot = await settings.save_resolved_relationship_identity(
            person_id="person-a",
            platform_id="platform-a",
            bot_id="real-bot",
            user_id="real-user",
        )

        assert snapshot["relationship_person_id"] == "person-a"
        assert config.saves == [
            {
                "relationship_person_id": "person-a",
                "trusted_platform_id": "platform-a",
                "pairing_bot_id": "",
                "pairing_user_id": "",
                "pairing_group_id": "",
                "pairing_identity_source": "relationship",
                "pairing_identity_sync_state": "pending",
            },
            {"pairing_identity_sync_state": "ready"},
        ]
        assert settings.relationship.person_id == "person-a"
        assert settings.identity.trusted_platform_id == "platform-a"
        assert settings.message_pipeline.platform_id == "platform-a"
        assert settings.identity_store.identity.bot_id == "real-bot"
        assert settings.identity_store.identity.user_id == "real-user"

    asyncio.run(scenario())


def test_resolved_relationship_identity_requires_existing_principal_proof() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"trusted_client_id": "quest-room"})
        settings = build_settings(config=config)

        with pytest.raises(OperatorSettingsError) as missing:
            await settings.save_resolved_relationship_identity(
                person_id="person-a",
                platform_id="platform-a",
                bot_id="real-bot",
                user_id="real-user",
            )

        assert missing.value.code == "pairing_api_principal_digest_missing"
        assert config.saves == []

    asyncio.run(scenario())


def test_failed_authoritative_relationship_sync_stays_pending_and_does_not_switch_runtime() -> (
    None
):
    class FailingControlPlane:
        async def upsert_quest_read_only_binding(self, **values: str) -> dict[str, Any]:
            del values
            raise IdentityControlPlaneError("control_failed", "failed")

    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "trusted_client_id": "quest-room",
                "pairing_api_principal_digest": "sha256:" + "a" * 64,
                "pairing_bot_id": "old-bot",
                "pairing_user_id": "old-user",
            }
        )
        settings = build_settings(config=config)
        settings.identity_control_plane = FailingControlPlane()

        with pytest.raises(OperatorSettingsError):
            await settings.save_resolved_relationship_identity(
                person_id="person-a",
                platform_id="platform-a",
                bot_id="real-bot",
                user_id="real-user",
            )

        assert config["pairing_identity_sync_state"] == "pending"
        assert settings.relationship.person_id == ""
        assert settings.message_pipeline.platform_id == ""
        assert settings.identity.sync_ready is False
        assert settings.identity_store.identity.bot_id == "old-bot"
        assert settings.identity_store.identity.user_id == "old-user"

    asyncio.run(scenario())


def test_clear_relationship_identity_revokes_and_removes_server_identity() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.requests: list[dict[str, str]] = []

        async def revoke_quest_read_only_binding(self, **values: str) -> dict[str, Any]:
            self.requests.append(dict(values))
            return {"status": "revoked"}

    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "trusted_client_id": "quest-room",
                "relationship_person_id": "person-a",
                "pairing_identity_source": "relationship",
                "pairing_identity_sync_state": "ready",
                "pairing_api_principal_digest": "sha256:" + "a" * 64,
                "pairing_bot_id": "real-bot",
                "pairing_user_id": "real-user",
            }
        )
        settings = build_settings(config=config)
        control = ControlPlane()
        settings.identity_control_plane = control
        settings.relationship.configure_person_id("person-a")

        snapshot = await settings.clear_resolved_relationship_identity()

        assert snapshot["relationship_person_id"] == ""
        assert settings.identity_store.identity is None
        assert settings.identity.sync_ready is True
        assert settings.identity.relationship_person_id == ""
        assert config["pairing_identity_source"] == "none"
        assert config["pairing_bot_id"] == ""
        assert config["pairing_user_id"] == ""
        assert control.requests == [
            {
                "api_principal_digest": "sha256:" + "a" * 64,
                "client_id": "quest-room",
            }
        ]

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


def test_live_persona_save_atomically_deactivates_quest_profile() -> None:
    async def scenario() -> None:
        active_id = "qp_" + "a" * 32
        config = NativeConfigStub({"active_quest_persona_id": active_id})
        settings = build_settings(config=config)
        settings.llm.quest_persona_prompt = "旧临人格"

        await settings.save_character_persona(
            persona_source_mode="astrbot",
            astrbot_persona_id="persona-a",
            character_name="",
            character_self_reference="",
            character_self_description="",
            character_user_relationship="",
            deactivate_quest_persona=True,
        )

        assert config["active_quest_persona_id"] == ""
        assert config.saves[-1]["active_quest_persona_id"] == ""
        assert settings.llm.quest_persona_prompt == ""

        config["active_quest_persona_id"] = active_id
        settings.llm.quest_persona_prompt = "不得丢失的临人格"
        config.fail = True
        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_character_persona(
                persona_source_mode="astrbot",
                astrbot_persona_id="persona-a",
                character_name="",
                character_self_reference="",
                character_self_description="",
                character_user_relationship="",
                deactivate_quest_persona=True,
            )

        assert failed.value.code == "config_save_failed"
        assert config["active_quest_persona_id"] == active_id
        assert settings.llm.quest_persona_prompt == "不得丢失的临人格"

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
