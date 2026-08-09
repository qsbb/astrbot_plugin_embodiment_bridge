from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from astrbot_plugin_quest_avatar_bridge.core.persona_profiles import (
    PersonaConversion,
    PersonaProfileError,
    PersonaProfileStore,
    source_hash,
)


def _conversion(*, prompt: str | None = None) -> PersonaConversion:
    return PersonaConversion(
        display_name="心夏",
        aliases=("Kokona",),
        quest_persona_prompt=prompt or ("人格" * 1_000),
        conversion_report={
            "preserved": ("保留身份与性格",),
            "adapted": ("转换为面对面表达",),
            "removed": ("移除 QQ 渠道规则",),
            "unresolved_questions": (),
        },
    )


def test_create_save_list_get_and_activate_independent_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        draft = await store.create_draft(
            display_name="心夏",
            source_kind="astrbot",
            source_persona_id="kokona-main",
            source_snapshot="原始 AstrBot 人格",
        )

        assert draft.profile_id.startswith("qp_")
        assert draft.status == "draft"
        assert draft.source_hash == source_hash("原始 AstrBot 人格")
        files = list((tmp_path / "personas").glob("*.json"))
        assert [item.name for item in files] == [f"{draft.profile_id}.json"]

        ready = await store.save_conversion(
            draft.profile_id,
            conversion=_conversion(),
            converter_provider_id="converter-model",
            converter_prompt_version="banxia-persona-converter/1.0",
        )
        assert ready.status == "ready"
        assert (await store.get(draft.profile_id)) == ready
        assert await store.activate(draft.profile_id) == ready
        assert await store.list_profiles() == [ready]

        payload = json.loads(files[0].read_text(encoding="utf-8"))
        assert payload["source_snapshot"] == "原始 AstrBot 人格"
        assert payload["source_hash"] == source_hash("原始 AstrBot 人格")
        assert payload["converter_provider_id"] == "converter-model"
        assert payload["status"] == "ready"

    asyncio.run(scenario())


def test_server_generated_id_and_path_traversal_are_enforced(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        with pytest.raises(PersonaProfileError, match="profile_id_invalid"):
            await store.get("../../outside")
        with pytest.raises(PersonaProfileError, match="profile_id_invalid"):
            await store.activate("qp_" + ("a" * 31) + "/")
        assert not (tmp_path / "outside.json").exists()

    asyncio.run(scenario())


def test_draft_cannot_activate_and_model_conversion_is_strict(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        draft = await store.create_draft(
            display_name="角色",
            source_kind="astrbot",
            source_persona_id="source",
            source_snapshot="source",
        )
        with pytest.raises(PersonaProfileError, match="profile_not_ready"):
            await store.activate(draft.profile_id)
        with pytest.raises(PersonaProfileError, match="quest_persona_prompt_invalid"):
            await store.save_conversion(
                draft.profile_id,
                conversion=_conversion(prompt="过短" * 100),
                converter_provider_id="provider",
                converter_prompt_version="version",
            )

    asyncio.run(scenario())


def test_manual_profile_can_be_authored_but_not_mixed_with_astrbot_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        manual = await store.create_draft(
            display_name="手动人格",
            source_kind="manual",
            source_snapshot="管理员手动创建",
        )
        ready = await store.save_manual(
            manual.profile_id,
            display_name="手动人格",
            aliases=["别名", "别名"],
            quest_persona_prompt="自然面对面人格规则。" * 30,
        )
        assert ready.status == "ready"
        assert ready.aliases == ("别名",)
        assert ready.converter_provider_id == ""
        assert ready.converter_prompt_version == "manual"

        inherited = await store.create_draft(
            display_name="导入人格",
            source_kind="astrbot",
            source_persona_id="source",
            source_snapshot="source",
        )
        with pytest.raises(PersonaProfileError, match="manual_save_not_allowed"):
            await store.save_manual(
                inherited.profile_id,
                display_name="不允许",
                aliases=[],
                quest_persona_prompt="自然面对面人格规则。" * 30,
            )

    asyncio.run(scenario())


def test_invalid_source_combinations_and_limits_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        with pytest.raises(PersonaProfileError, match="source_persona_id_required"):
            await store.create_draft(
                display_name="角色",
                source_kind="astrbot",
                source_snapshot="source",
            )
        with pytest.raises(PersonaProfileError, match="source_persona_id_not_allowed"):
            await store.create_draft(
                display_name="角色",
                source_kind="manual",
                source_persona_id="must-not-exist",
                source_snapshot="source",
            )
        with pytest.raises(PersonaProfileError, match="source_snapshot_invalid"):
            await store.create_draft(
                display_name="角色",
                source_kind="manual",
                source_snapshot="x" * 24_001,
            )

    asyncio.run(scenario())


def test_corrupt_or_tampered_files_are_never_returned(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        draft = await store.create_draft(
            display_name="角色",
            source_kind="manual",
            source_snapshot="source",
        )
        path = tmp_path / "personas" / f"{draft.profile_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source_snapshot"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(PersonaProfileError, match="source_hash_mismatch"):
            await store.get(draft.profile_id)
        assert await store.list_profiles() == []

    asyncio.run(scenario())


def test_store_serializes_concurrent_writes_without_temp_leaks(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        drafts = await asyncio.gather(
            *(
                store.create_draft(
                    display_name=f"角色 {index}",
                    source_kind="manual",
                    source_snapshot=f"source {index}",
                )
                for index in range(12)
            )
        )
        assert len({item.profile_id for item in drafts}) == 12
        assert len(await store.list_profiles()) == 12
        assert list((tmp_path / "personas").glob("*.tmp")) == []

    asyncio.run(scenario())


def test_delete_is_bounded_and_returns_removed_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = PersonaProfileStore(tmp_path)
        profile = await store.create_draft(
            display_name="待删除",
            source_kind="manual",
            source_snapshot="source",
        )
        path = tmp_path / "personas" / f"{profile.profile_id}.json"

        assert await store.delete(profile.profile_id) == profile
        assert not path.exists()
        with pytest.raises(PersonaProfileError, match="profile_not_found"):
            await store.delete(profile.profile_id)
        with pytest.raises(PersonaProfileError, match="profile_id_invalid"):
            await store.delete("../outside")

    asyncio.run(scenario())


def test_profile_file_permissions_are_restricted_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def record_chmod(path: str | os.PathLike[str], mode: int) -> None:
        calls.append((Path(path), mode))
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", record_chmod)

    async def scenario() -> Path:
        store = PersonaProfileStore(tmp_path)
        profile = await store.create_draft(
            display_name="权限测试",
            source_kind="manual",
            source_snapshot="source",
        )
        return tmp_path / "personas" / f"{profile.profile_id}.json"

    path = asyncio.run(scenario())
    assert calls[-1] == (path, 0o600)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
