from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from astrbot_plugin_embodiment_bridge.core.config_persistence import (
    config_is_writable,
    save_config_changes,
)


class AsyncConfig(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.saves: list[dict[str, Any]] = []

    async def save_config_async(self, changes: dict[str, Any]) -> bool:
        self.update(changes)
        self.saves.append(dict(changes))
        return True


class LegacySyncConfig(dict[str, Any]):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.saves: list[dict[str, Any]] = []
        self.save_thread_id = 0

    def save_config(self, changes: dict[str, Any]) -> None:
        self.save_thread_id = threading.get_ident()
        self.update(changes)
        if self.fail:
            raise OSError("disk failed")
        self.saves.append(dict(changes))


def test_async_config_persistence_remains_preferred() -> None:
    async def scenario() -> None:
        config = AsyncConfig()

        assert config_is_writable(config) is True
        assert await save_config_changes(config, {"chat_provider_id": "model-a"})
        assert config.saves == [{"chat_provider_id": "model-a"}]

    asyncio.run(scenario())


def test_astrbot_4265_sync_config_saves_off_event_loop() -> None:
    async def scenario() -> None:
        config = LegacySyncConfig()
        event_loop_thread_id = threading.get_ident()

        assert config_is_writable(config) is True
        assert await save_config_changes(config, {"pairing_listener_port": 8520})
        assert config.saves == [{"pairing_listener_port": 8520}]
        assert config.save_thread_id != event_loop_thread_id

    asyncio.run(scenario())


def test_sync_config_failure_restores_in_memory_values() -> None:
    async def scenario() -> None:
        config = LegacySyncConfig(fail=True)
        config["chat_provider_id"] = "before"

        with pytest.raises(OSError, match="disk failed"):
            await save_config_changes(
                config,
                {"chat_provider_id": "after", "new_value": "temporary"},
            )

        assert config == {"chat_provider_id": "before"}

    asyncio.run(scenario())


def test_plain_mapping_without_persistence_api_is_read_only() -> None:
    assert config_is_writable({}) is False
