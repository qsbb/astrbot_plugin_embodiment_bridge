from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_quest_avatar_bridge.adapters.astrbot_persona import (
    AstrBotPersonaAdapter,
    PersonaSelectionError,
)


class PersonaManagerStub:
    def __init__(self) -> None:
        self.personas = {
            "quest-identity": SimpleNamespace(
                persona_id="quest-identity",
                system_prompt="你是已配置的 Quest 角色。",
                tools=["must-not-leak"],
                begin_dialogs=["must-not-leak"],
            )
        }
        self.default = {"name": "default", "prompt": "你是默认角色。"}
        self.block = False

    async def get_persona(self, persona_id: str) -> Any:
        if self.block:
            await asyncio.Event().wait()
        if persona_id not in self.personas:
            raise ValueError("missing")
        return self.personas[persona_id]

    async def get_default_persona_v3(self, umo: Any = None) -> dict[str, Any]:
        assert umo is None
        return self.default

    async def get_all_personas(self) -> list[Any]:
        return list(self.personas.values())


class ContextStub:
    def __init__(self, manager: Any) -> None:
        self.persona_manager = manager


def test_explicit_and_default_astrbot_personas_are_resolved() -> None:
    async def scenario() -> None:
        manager = PersonaManagerStub()
        selected = AstrBotPersonaAdapter(
            ContextStub(manager),
            source_mode="astrbot",
            persona_id="quest-identity",
        )
        selected_snapshot = await selected.resolve()
        assert selected_snapshot.source == "astrbot_selected"
        assert selected_snapshot.status == "ready"
        assert selected_snapshot.prompt == "你是已配置的 Quest 角色。"

        default = AstrBotPersonaAdapter(ContextStub(manager))
        default_snapshot = await default.resolve()
        assert default_snapshot.source == "astrbot_default"
        assert default_snapshot.status == "ready"
        assert default_snapshot.prompt == "你是默认角色。"

    asyncio.run(scenario())


def test_deleted_selected_persona_falls_back_generic_without_default_switch() -> None:
    async def scenario() -> None:
        manager = PersonaManagerStub()
        adapter = AstrBotPersonaAdapter(
            ContextStub(manager),
            persona_id="deleted-persona",
        )
        snapshot = await adapter.resolve()
        assert snapshot.source == "generic"
        assert snapshot.status == "selected_missing"
        assert snapshot.prompt == ""

    asyncio.run(scenario())


def test_unavailable_or_malformed_persona_falls_back_generic() -> None:
    async def scenario() -> None:
        unavailable = AstrBotPersonaAdapter(SimpleNamespace())
        assert (await unavailable.resolve()).source == "generic"

        manager = PersonaManagerStub()
        manager.default = {"name": "broken", "prompt": ""}
        malformed = AstrBotPersonaAdapter(ContextStub(manager))
        snapshot = await malformed.resolve()
        assert snapshot.source == "generic"
        assert snapshot.status == "default_missing"

        invalid_config = AstrBotPersonaAdapter(
            ContextStub(manager),
            persona_id="x" * 256,
        )
        invalid_snapshot = await invalid_config.resolve()
        assert invalid_snapshot.source == "generic"
        assert invalid_snapshot.status == "configuration_invalid"

    asyncio.run(scenario())


def test_safe_catalog_projects_only_ids_and_validation_is_exact() -> None:
    async def scenario() -> None:
        manager = PersonaManagerStub()
        adapter = AstrBotPersonaAdapter(ContextStub(manager))
        catalog = await adapter.list_safe_personas()
        assert catalog == {
            "status": "ok",
            "personas": [{"id": "quest-identity"}],
        }
        serialized = repr(catalog)
        assert "Quest 角色" not in serialized
        assert "must-not-leak" not in serialized
        assert await adapter.validate_selection("quest-identity") == "quest-identity"
        with pytest.raises(PersonaSelectionError, match="persona_not_available"):
            await adapter.validate_selection("missing")

    asyncio.run(scenario())


def test_timeout_is_bounded_and_cancellation_propagates() -> None:
    async def scenario() -> None:
        manager = PersonaManagerStub()
        manager.block = True
        adapter = AstrBotPersonaAdapter(
            ContextStub(manager),
            persona_id="quest-identity",
            timeout_seconds=0.05,
        )
        snapshot = await adapter.resolve()
        assert snapshot.source == "generic"
        assert snapshot.status == "timeout"

        task = asyncio.create_task(adapter.resolve())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
