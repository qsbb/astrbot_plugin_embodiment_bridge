from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.adapters.astrbot_persona import (
    AstrBotPersonaAdapter,
)
from astrbot_plugin_embodiment_bridge.core.operator_settings import (
    OperatorSettings,
    OperatorSettingsError,
)
from astrbot_plugin_embodiment_bridge.adapters.identity_control_plane import (
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


class LegacySyncConfigStub(dict[str, Any]):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.saves: list[dict[str, Any]] = []

    def save_config(self, changes: dict[str, Any]) -> None:
        self.update(changes)
        self.saves.append(dict(changes))


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


class SttSettingsStub:
    def __init__(self, provider_id: str = "stt-a") -> None:
        self.provider_id = provider_id
        self.providers = [
            {
                "id": "stt-a",
                "model": "speech-a",
                "adapter_type": "official-stt",
                "provider_type": "speech_to_text",
            },
            {
                "id": "stt-b",
                "model": "speech-b",
                "adapter_type": "third-party-stt",
                "provider_type": "speech_to_text",
            },
        ]

    def provider_catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.providers]

    def status_snapshot(self) -> dict[str, Any]:
        provider_ids = {item["id"] for item in self.providers}
        status = (
            "ready"
            if self.provider_id in provider_ids
            else "selected_missing"
            if self.provider_id
            else "disabled"
        )
        return {
            "source": "astrbot_stt_provider",
            "available": status == "ready",
            "status": status,
            "selected": bool(self.provider_id),
            "selected_id": self.provider_id,
            "legacy_default": False,
            "external_contract_status": "no_standard_contract",
            "providers": self.provider_catalog(),
        }

    def configure_provider(self, provider_id: str) -> None:
        self.provider_id = provider_id


class FastActionSettingsStub:
    def __init__(self, *, enabled: bool = True, provider_id: str = "") -> None:
        self.enabled = enabled
        self.provider_id = provider_id
        self.timeout_seconds = 6.0
        self.configured_timeout_seconds = 6.0
        self.timeout_policy_revision = "v3"
        self.timeout_migrated = False

    def configure(
        self,
        *,
        enabled: bool | None = None,
        provider_id: str | None = None,
        timeout_seconds: float | None = None,
        configured_timeout_seconds: float | None = None,
        timeout_policy_revision: str | None = None,
    ) -> None:
        if enabled is not None:
            self.enabled = enabled
        if provider_id is not None:
            self.provider_id = provider_id
        if timeout_seconds is not None:
            self.timeout_seconds = timeout_seconds
        if configured_timeout_seconds is not None:
            self.configured_timeout_seconds = configured_timeout_seconds
        if timeout_policy_revision is not None:
            self.timeout_policy_revision = timeout_policy_revision

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": bool(self.enabled and self.provider_id),
            "availability_reason": (
                "ready"
                if self.enabled and self.provider_id
                else "disabled"
                if not self.enabled
                else "provider_not_configured"
            ),
            "selected": bool(self.provider_id),
            "selected_id": self.provider_id,
            "configured_timeout_seconds": self.configured_timeout_seconds,
            "effective_timeout_seconds": self.timeout_seconds,
            "timeout_policy_revision": self.timeout_policy_revision,
            "timeout_migrated": self.timeout_migrated,
        }


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

    @property
    def local_binding_configured(self) -> bool:
        return bool(getattr(self, "local_binding", None))

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
        stt=SttSettingsStub(str(config.get("astrbot_stt_provider_id", "stt-a"))),
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


def test_astrbot_4265_sync_config_enables_all_operator_workflows() -> None:
    async def scenario() -> None:
        config = LegacySyncConfigStub({"chat_provider_id": "model-a"})
        settings = build_settings(config=config, selected="model-a")

        assert settings.snapshot()["config_writable"] is True
        assert settings.platform_snapshot()["config_writable"] is True
        assert settings.persona_snapshot()["config_writable"] is True
        assert (await settings.quest_identity_overview())["config_writable"] is True

        saved = await settings.save_chat_provider_id("model-b")
        assert saved["selected_id"] == "model-b"
        assert config.saves == [{"chat_provider_id": "model-b"}]

    asyncio.run(scenario())


def test_fast_action_settings_validate_persist_and_apply_atomically() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {"fast_action_enabled": True, "fast_action_provider_id": "model-a"}
        )
        settings = build_settings(config=config, selected="model-a")
        fast = FastActionSettingsStub(enabled=True, provider_id="model-a")
        settings.fast_action = fast

        saved = await settings.save_fast_action_settings(
            enabled=True,
            provider_id="model-b",
            timeout_seconds=4.0,
        )
        assert saved["enabled"] is True
        assert saved["selected_id"] == "model-b"
        assert fast.provider_id == "model-b"
        assert config.saves[-1] == {
            "fast_action_enabled": True,
            "fast_action_provider_id": "model-b",
            "fast_action_timeout_seconds": 4.0,
            "fast_action_timeout_policy_revision": "v3",
        }
        assert saved["configured_timeout_seconds"] == 4.0
        assert saved["effective_timeout_seconds"] == 4.0
        assert saved["timeout_policy_revision"] == "v3"

        legacy_client_saved = await settings.save_fast_action_settings(
            enabled=True,
            provider_id="model-a",
        )
        assert config.saves[-1] == {
            "fast_action_enabled": True,
            "fast_action_provider_id": "model-a",
        }
        assert legacy_client_saved["effective_timeout_seconds"] == 4.0
        assert legacy_client_saved["timeout_policy_revision"] == "v3"
        assert {item["id"] for item in saved["providers"]} == {
            "model-a",
            "model-b",
        }

        disabled = await settings.save_fast_action_settings(
            enabled=False,
            provider_id="",
        )
        assert disabled["enabled"] is False
        assert disabled["selected_id"] == ""

        with pytest.raises(
            OperatorSettingsError,
            match="快速动作处理前必须选择模型",
        ):
            await settings.save_fast_action_settings(enabled=True, provider_id="")

        with pytest.raises(
            OperatorSettingsError,
            match="不存在或当前不可用",
        ):
            await settings.save_fast_action_settings(
                enabled=True,
                provider_id="missing",
            )

        with pytest.raises(OperatorSettingsError, match="0.5到15秒"):
            await settings.save_fast_action_settings(
                enabled=False,
                provider_id="",
                timeout_seconds=0.4,
            )

    asyncio.run(scenario())


def test_empty_relationship_selection_is_valid_without_relationship_plugin() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"relationship_person_id": ""})
        settings = build_settings(config=config)

        snapshot = await settings.clear_resolved_relationship_identity()

        assert snapshot["relationship_person_id"] == ""
        assert settings.relationship.person_id == ""
        assert settings.identity.relationship_person_id == ""

    asyncio.run(scenario())


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


def test_direct_dialogue_mode_is_explicit_and_does_not_require_identity_fields() -> (
    None
):
    class Orchestrator:
        allow_direct_provider_fallback = False

    async def scenario() -> None:
        config = LegacySyncConfigStub({"chat_provider_id": "model-a"})
        settings = build_settings(config=config, selected="model-a")
        settings.orchestrator = Orchestrator()

        result = await settings.save_dialogue_mode(True)

        assert result == {
            "mode": "direct_provider",
            "direct_mode": True,
            "eventbus_enabled": False,
        }
        assert config["quest_direct_dialogue_mode"] is True
        assert settings.orchestrator.allow_direct_provider_fallback is True
        assert settings.message_pipeline.enabled is False

    asyncio.run(scenario())


def test_empty_relationship_save_uses_clear_path_and_preserves_base_binding() -> None:
    class ControlPlane:
        def __init__(self) -> None:
            self.revocations: list[dict[str, str]] = []

        async def revoke_quest_read_only_binding(self, **values: str) -> dict[str, Any]:
            self.revocations.append(dict(values))
            return {"status": "revoked"}

    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "trusted_client_id": "quest-room",
                "trusted_platform_id": "platform-a",
                "relationship_person_id": "person-a",
                "pairing_identity_source": "relationship",
                "pairing_identity_sync_state": "ready",
                "pairing_api_principal_digest": "sha256:" + "a" * 64,
                "pairing_bot_id": "real-bot",
                "pairing_user_id": "real-user",
            }
        )
        settings = build_settings(config=config)
        settings.relationship.configure_person_id("person-a")
        settings.identity.configure_local_binding(
            api_principal_digest="sha256:" + "a" * 64,
            client_id="quest-room",
            platform_id="platform-a",
            bot_id="real-bot",
            user_id="real-user",
            group_id="",
        )
        settings.identity.configure_relationship_person_id("person-a")
        settings.identity.configure_sync_ready(True)
        control = ControlPlane()
        settings.identity_control_plane = control

        snapshot = await settings.save_relationship_person_id("")

        assert snapshot["relationship_person_id"] == ""
        assert settings.relationship.person_id == ""
        assert settings.identity.relationship_person_id == ""
        assert settings.identity.local_binding_configured is True
        assert settings.identity.sync_ready is True
        assert config["pairing_identity_source"] == "preserved"
        assert control.revocations == [
            {"api_principal_digest": "sha256:" + "a" * 64, "client_id": "quest-room"}
        ]

    asyncio.run(scenario())


def test_stt_selection_uses_safe_catalog_and_clears_legacy_private_settings() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "astrbot_stt_provider_id": "stt-a",
                "enable_astrbot_stt": True,
                "enable_plugin_mimo_stt": True,
                "plugin_mimo_stt_api_base": "sensitive-value",
                "plugin_mimo_stt_api_key": "sensitive-value",
                "plugin_mimo_stt_model": "sensitive-value",
            }
        )
        settings = build_settings(config=config)

        before = settings.stt_snapshot()
        assert before["selected_id"] == "stt-a"
        assert before["providers"][1]["adapter_type"] == "third-party-stt"
        assert "sensitive-value" not in repr(before)

        saved = await settings.save_stt_provider_id("stt-b")

        assert saved["selected_id"] == "stt-b"
        assert saved["available"] is True
        assert settings.stt.provider_id == "stt-b"
        assert config.saves == [
            {
                "astrbot_stt_provider_id": "stt-b",
                "enable_astrbot_stt": False,
                "enable_plugin_mimo_stt": False,
                "plugin_mimo_stt_api_base": "",
                "plugin_mimo_stt_api_key": "",
                "plugin_mimo_stt_model": "",
            }
        ]
        assert "sensitive-value" not in repr(saved)

        disabled = await settings.save_stt_provider_id("")
        assert disabled["selected_id"] == ""
        assert disabled["status"] == "disabled"
        assert settings.stt.provider_id == ""

    asyncio.run(scenario())


def test_invalid_or_failed_stt_save_does_not_change_runtime_selection() -> None:
    async def scenario() -> None:
        config = NativeConfigStub({"astrbot_stt_provider_id": "stt-a"})
        settings = build_settings(config=config)

        with pytest.raises(OperatorSettingsError) as missing:
            await settings.save_stt_provider_id("missing")
        assert missing.value.code == "stt_provider_not_available"
        assert settings.stt.provider_id == "stt-a"

        config.fail = True
        with pytest.raises(OperatorSettingsError) as failed:
            await settings.save_stt_provider_id("stt-b")
        assert failed.value.code == "config_save_failed"
        assert settings.stt.provider_id == "stt-a"
        assert config["astrbot_stt_provider_id"] == "stt-a"

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


def test_clear_relationship_context_preserves_verified_server_identity() -> None:
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
                "trusted_platform_id": "platform-a",
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
        settings.identity.configure_local_binding(
            api_principal_digest="sha256:" + "a" * 64,
            client_id="quest-room",
            platform_id="platform-a",
            bot_id="real-bot",
            user_id="real-user",
            group_id="",
        )
        settings.identity.configure_sync_ready(True)
        settings.identity.configure_relationship_person_id("person-a")
        previous_binding = dict(settings.identity.local_binding)

        snapshot = await settings.clear_resolved_relationship_identity()

        assert snapshot["relationship_person_id"] == ""
        assert settings.identity_store.identity.bot_id == "real-bot"
        assert settings.identity_store.identity.user_id == "real-user"
        assert settings.identity.sync_ready is True
        assert settings.identity.relationship_person_id == ""
        assert settings.identity.local_binding == previous_binding
        assert config["pairing_identity_source"] == "preserved"
        assert config["pairing_bot_id"] == "real-bot"
        assert config["pairing_user_id"] == "real-user"
        assert control.requests == [
            {
                "api_principal_digest": "sha256:" + "a" * 64,
                "client_id": "quest-room",
            }
        ]

    asyncio.run(scenario())


def test_clear_pending_relationship_restores_verified_base_identity() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "trusted_client_id": "quest-room",
                "trusted_platform_id": "platform-a",
                "relationship_person_id": "person-a",
                "pairing_identity_source": "relationship",
                "pairing_identity_sync_state": "pending",
                "pairing_api_principal_digest": "sha256:" + "a" * 64,
                "pairing_bot_id": "real-bot",
                "pairing_user_id": "real-user",
            }
        )
        settings = build_settings(config=config)
        # Simulate a fresh plugin process: the persisted server tuple exists,
        # but main intentionally did not preload it while sync was pending.
        settings.relationship.configure_person_id("person-a")
        settings.identity.configure_trusted_platform("platform-a")
        settings.identity.configure_sync_ready(False)
        settings.identity.configure_relationship_person_id("person-a")

        snapshot = await settings.clear_resolved_relationship_identity()

        assert snapshot["relationship_person_id"] == ""
        assert config["pairing_identity_source"] == "preserved"
        assert config["pairing_identity_sync_state"] == "ready"
        assert settings.identity.sync_ready is True
        assert settings.identity.relationship_person_id == ""
        assert settings.identity.local_binding["bot_id"] == "real-bot"
        assert settings.identity.local_binding["user_id"] == "real-user"
        assert settings.message_pipeline.platform_id == "platform-a"

    asyncio.run(scenario())


def test_clear_relationship_does_not_claim_ready_without_base_identity() -> None:
    async def scenario() -> None:
        config = NativeConfigStub(
            {
                "relationship_person_id": "person-a",
                "pairing_identity_source": "relationship",
                "pairing_identity_sync_state": "pending",
            }
        )
        settings = build_settings(config=config)
        settings.identity.configure_sync_ready(False)

        await settings.clear_resolved_relationship_identity()

        assert config["pairing_identity_source"] == "preserved"
        assert config["pairing_identity_sync_state"] == "pending"
        assert settings.identity.sync_ready is False

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
