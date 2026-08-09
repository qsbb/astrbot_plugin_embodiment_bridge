from __future__ import annotations

import asyncio
import json

from astrbot_plugin_quest_avatar_bridge.core.server_identity import (
    ServerIdentityStore,
)


def test_server_identity_is_atomic_persistent_and_repr_redacted(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "server_identity.json"
        store = ServerIdentityStore(path)
        await store.save(bot_id="private-bot", user_id="private-user")

        assert store.identity is not None
        assert "private-bot" not in repr(store.identity)
        assert "private-user" not in repr(store.identity)
        assert not list(tmp_path.glob("*.tmp"))
        reloaded = ServerIdentityStore(path)
        assert reloaded.identity is not None
        assert reloaded.identity.bot_id == "private-bot"
        assert reloaded.identity.user_id == "private-user"

        await reloaded.clear()
        assert reloaded.identity is None
        assert not path.exists()

    asyncio.run(scenario())


def test_legacy_identity_stays_in_memory_until_flushed(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "server_identity.json"
        store = ServerIdentityStore(path)

        assert store.import_legacy(bot_id="legacy-bot", user_id="legacy-user")
        assert not path.exists()
        await store.flush()

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload == {
            "version": 1,
            "bot_id": "legacy-bot",
            "user_id": "legacy-user",
        }
        assert store.import_legacy(bot_id="other", user_id="other") is False

    asyncio.run(scenario())


def test_invalid_or_corrupt_identity_fails_closed(tmp_path) -> None:
    path = tmp_path / "server_identity.json"
    path.write_text('{"version":1,"bot_id":"bad|bot","user_id":"user"}', encoding="utf-8")
    assert ServerIdentityStore(path).identity is None

    path.write_text("not-json", encoding="utf-8")
    assert ServerIdentityStore(path).identity is None
