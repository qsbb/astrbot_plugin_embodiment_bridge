from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import time
import wave

import pytest

from astrbot_plugin_embodiment_bridge.adapters.environment import (
    CachedEnvironmentAdapter,
)
from astrbot_plugin_embodiment_bridge.adapters.identity import (
    ProtectedContextDecision,
    QuestSessionAuthorizationAdapter,
)
from astrbot_plugin_embodiment_bridge.adapters.relationship_event_identity import (
    QuestEventIdentity,
    QuestEventIdentityResolution,
)
from astrbot_plugin_embodiment_bridge.adapters.knowledge import (
    GlobalKnowledgeAdapter,
)
from astrbot_plugin_embodiment_bridge.adapters.runtime import SeriesRuntimeAdapter
from astrbot_plugin_embodiment_bridge.adapters.stt import AdapterUnavailable
from astrbot_plugin_embodiment_bridge.adapters.voice_hub_tts import (
    VoiceHubTTSAdapter,
)


class LoggerStub:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(message)


class ContextStub:
    def __init__(self, plugin_name: str, provider: Any) -> None:
        self.plugin_name = plugin_name
        self.provider = provider

    def get_all_stars(self) -> list[Any]:
        return [
            SimpleNamespace(
                name=self.plugin_name,
                activated=True,
                star_cls=self.provider,
            )
        ]


class EmptyContextStub:
    def get_all_stars(self) -> list[Any]:
        return []


class IdentityProvider:
    def __init__(self, version: str = "1.0") -> None:
        self.version = version
        self.requests: list[dict[str, Any]] = []

    def quest_session_authorization_contract(self) -> dict[str, Any]:
        return {
            "name": "identity.quest_session_authorization",
            "version": self.version,
            "capabilities": ("authorize_read_only_session",),
            "method": "authorize_quest_session",
            "timeout_ms": 1000,
            "permission_identity_mode": "raw_platform_identity_tuple",
            "permission_identity_fields": ("platform_id", "bot_id", "user_id"),
            "cross_platform_inheritance": False,
            "grants_platform_action": False,
        }

    def authorize_quest_session(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "contract_version": "1.0",
            "status": "authorized",
            "authorized": True,
            "reason": "authorized_private_owner_identity",
            "access": "read_only_context",
            "owner_confirmed": True,
            "grants_platform_action": False,
        }


def test_identity_authorization_uses_server_config_and_fails_closed() -> None:
    async def scenario() -> None:
        provider = IdentityProvider()
        adapter = QuestSessionAuthorizationAdapter(
            ContextStub("astrbot_plugin_identity_guardian", provider),
            LoggerStub(),
            trusted_client_id="quest-living-room",
            trusted_platform_id="aiocqhttp",
        )
        decision = await adapter.authorize(
            api_principal="astrbot-api",
            declared_client_id="quest-living-room",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert decision.authorized is True
        assert provider.requests == [
            {
                "api_principal": "astrbot-api",
                "client_id": "quest-living-room",
                "platform_id": "aiocqhttp",
                "bot_id": "bot",
                "user_id": "user",
                "group_id": None,
            }
        ]

        denied = await adapter.authorize(
            api_principal="astrbot-api",
            declared_client_id="unity-claims-another-client",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert denied.authorized is False
        assert denied.reason == "client_id_mismatch"
        assert len(provider.requests) == 1

        incompatible = IdentityProvider(version="2.0")
        adapter = QuestSessionAuthorizationAdapter(
            ContextStub("astrbot_plugin_identity_guardian", incompatible),
            LoggerStub(),
            trusted_client_id="quest-living-room",
            trusted_platform_id="aiocqhttp",
        )
        denied = await adapter.authorize(
            api_principal="astrbot-api",
            declared_client_id="quest-living-room",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert denied.authorized is False
        assert incompatible.requests == []

        invalid_version = IdentityProvider(version="1")
        adapter = QuestSessionAuthorizationAdapter(
            ContextStub("astrbot_plugin_identity_guardian", invalid_version),
            LoggerStub(),
            trusted_client_id="quest-living-room",
            trusted_platform_id="aiocqhttp",
        )
        denied = await adapter.authorize(
            api_principal="astrbot-api",
            declared_client_id="quest-living-room",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert denied.authorized is False
        assert denied.reason == "contract_incompatible"

    asyncio.run(scenario())


def test_relationship_identity_is_rechecked_for_every_session_authorization() -> None:
    class Resolver:
        def __init__(self) -> None:
            self.resolution = QuestEventIdentityResolution(
                "ok",
                "resolved_unique_active_private_account",
                QuestEventIdentity(
                    platform_id="aiocqhttp",
                    bot_id="bot",
                    user_id="user",
                    session_id="aiocqhttp:FriendMessage:user",
                ),
            )

        async def resolve(self, **request: Any) -> QuestEventIdentityResolution:
            assert request == {
                "person_id": "person-a",
                "platform_candidates": ("aiocqhttp",),
            }
            return self.resolution

    class ReadOnlyIdentityProvider(IdentityProvider):
        def authorize_quest_session(self, request: dict[str, Any]) -> dict[str, Any]:
            self.requests.append(request)
            return {
                "contract_version": "1.0",
                "status": "authorized",
                "authorized": True,
                "reason": "authorized_private_quest_identity",
                "access": "read_only_context",
                "owner_confirmed": False,
                "grants_platform_action": False,
            }

    async def scenario() -> None:
        provider = ReadOnlyIdentityProvider()
        resolver = Resolver()
        adapter = QuestSessionAuthorizationAdapter(
            ContextStub("astrbot_plugin_identity_guardian", provider),
            LoggerStub(),
            trusted_client_id="quest-room",
            trusted_platform_id="aiocqhttp",
            relationship_identity_resolver=resolver,
            relationship_person_id="person-a",
        )

        authorized = await adapter.authorize(
            api_principal="api",
            declared_client_id="quest-room",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert authorized == ProtectedContextDecision(
            True, "authorized_private_quest_identity"
        )

        mismatch = await adapter.authorize(
            api_principal="api",
            declared_client_id="quest-room",
            bot_id="old-bot",
            user_id="user",
            group_id="",
        )
        assert mismatch.reason == "relationship_event_identity_mismatch"
        assert len(provider.requests) == 1

        resolver.resolution = QuestEventIdentityResolution(
            "unavailable", "person_not_found"
        )
        removed = await adapter.authorize(
            api_principal="api",
            declared_client_id="quest-room",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert removed.reason == "relationship_event_identity_person_not_found"
        assert len(provider.requests) == 1

    asyncio.run(scenario())


def test_missing_series_providers_degrade_without_enabling_private_context() -> None:
    async def scenario() -> None:
        logger = LoggerStub()
        identity = QuestSessionAuthorizationAdapter(
            EmptyContextStub(),
            logger,
            trusted_client_id="",
            trusted_platform_id="",
        )
        assert identity.status_snapshot() == {
            "contract": "identity.quest_session_authorization@1.0",
            "configured": False,
            "status": "trusted_client_id_missing",
            "default_access": "denied",
            "api_principal_source": "astrbot_authenticated_request",
            "client_id_source": "bridge_server_config",
            "platform_id_source": "bridge_server_config",
            "unity_trusted_source_fields": False,
            "fallback_mode": "exact_local_binding",
            "local_binding_configured": False,
        }
        decision = await identity.authorize(
            api_principal="api",
            declared_client_id="unity",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert decision.authorized is False
        assert decision.reason == "trusted_client_id_missing"

        knowledge = GlobalKnowledgeAdapter(EmptyContextStub(), logger)
        assert await knowledge.recall("query") == []
        assert knowledge.status == "provider_unavailable"

        environment = CachedEnvironmentAdapter(EmptyContextStub(), logger)
        assert await environment.read() is None
        assert environment.status == "provider_unavailable"

        runtime = SeriesRuntimeAdapter(EmptyContextStub(), logger)
        assert (await runtime.refresh())["reason"] == "provider_unavailable"

        voice = VoiceHubTTSAdapter(EmptyContextStub(), logger)
        assert voice.available is False
        assert voice.status == "provider_unavailable"
        with pytest.raises(AdapterUnavailable):
            _ = [chunk async for chunk in voice.synthesize("hello", emotion="")]

    asyncio.run(scenario())


def test_missing_identity_guardian_uses_only_exact_local_binding() -> None:
    async def scenario() -> None:
        adapter = QuestSessionAuthorizationAdapter(
            EmptyContextStub(),
            LoggerStub(),
            trusted_client_id="quest-room",
            trusted_platform_id="platform-test",
            local_api_principal_digest=(
                "sha256:" + hashlib.sha256(b"api_key:plugin-scope-key").hexdigest()
            ),
            local_bot_id="bot-test",
            local_user_id="user-test",
            local_group_id="",
        )
        authorized = await adapter.authorize(
            api_principal="api_key:plugin-scope-key",
            declared_client_id="quest-room",
            bot_id="bot-test",
            user_id="user-test",
            group_id="",
        )
        assert authorized == ProtectedContextDecision(
            True,
            "authorized_local_owner_identity",
        )

        attempts = (
            {"api_principal": "api_key:other"},
            {"declared_client_id": "other-client"},
            {"bot_id": "other-bot"},
            {"user_id": "other-user"},
            {"group_id": "group-test"},
        )
        baseline = {
            "api_principal": "api_key:plugin-scope-key",
            "declared_client_id": "quest-room",
            "bot_id": "bot-test",
            "user_id": "user-test",
            "group_id": "",
        }
        for changes in attempts:
            denied = await adapter.authorize(**{**baseline, **changes})
            assert denied.authorized is False

        incompatible = QuestSessionAuthorizationAdapter(
            ContextStub(
                "astrbot_plugin_identity_guardian",
                IdentityProvider(version="2.0"),
            ),
            LoggerStub(),
            trusted_client_id="quest-room",
            trusted_platform_id="platform-test",
            local_api_principal_digest=(
                "sha256:" + hashlib.sha256(b"api_key:plugin-scope-key").hexdigest()
            ),
            local_bot_id="bot-test",
            local_user_id="user-test",
        )
        denied = await incompatible.authorize(**baseline)
        assert denied.authorized is False
        assert denied.reason == "contract_incompatible"

    asyncio.run(scenario())


def test_pending_identity_sync_denies_new_session_with_previous_runtime_tuple() -> None:
    async def scenario() -> None:
        adapter = QuestSessionAuthorizationAdapter(
            EmptyContextStub(),
            LoggerStub(),
            trusted_client_id="quest-room",
            trusted_platform_id="platform-test",
            local_api_principal_digest=(
                "sha256:" + hashlib.sha256(b"api_key:old-key").hexdigest()
            ),
            local_bot_id="old-bot",
            local_user_id="old-user",
        )
        adapter.configure_sync_ready(False)

        denied = await adapter.authorize(
            api_principal="api_key:old-key",
            declared_client_id="quest-room",
            bot_id="old-bot",
            user_id="old-user",
            group_id="",
        )

        assert denied == ProtectedContextDecision(False, "identity_sync_pending")

    asyncio.run(scenario())


def test_series_adapter_timeouts_degrade_or_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class SlowIdentityProvider(IdentityProvider):
        def authorize_quest_session(self, request: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.05)
            return super().authorize_quest_session(request)

    class SlowKnowledgeProvider(KnowledgeProvider):
        async def recall(
            self, query: str, *, scope: str, top_k: int
        ) -> list[dict[str, Any]]:
            await asyncio.sleep(0.2)
            return await super().recall(query, scope=scope, top_k=top_k)

    class SlowEnvironmentProvider(EnvironmentProvider):
        def get_cached_opportunity(self, *, allow_stale: bool) -> dict[str, Any]:
            time.sleep(0.05)
            return super().get_cached_opportunity(allow_stale=allow_stale)

    class SlowRuntimeProvider(RuntimeProvider):
        async def get_series_runtime_snapshot(
            self, *, timeout_seconds: float
        ) -> dict[str, Any]:
            await asyncio.sleep(1)
            return await super().get_series_runtime_snapshot(
                timeout_seconds=timeout_seconds
            )

    class SlowVoiceProvider(VoiceProvider):
        async def render_pcm_wav(
            self,
            text: str,
            *,
            emotion: str,
            voice: str,
            context: str,
            session_id: str,
        ) -> dict[str, Any]:
            await asyncio.sleep(0.05)
            return await super().render_pcm_wav(
                text,
                emotion=emotion,
                voice=voice,
                context=context,
                session_id=session_id,
            )

    async def scenario() -> None:
        monkeypatch.setattr(
            "astrbot_plugin_embodiment_bridge.adapters.identity.IDENTITY_TIMEOUT_SECONDS",
            0.01,
        )
        logger = LoggerStub()
        identity = QuestSessionAuthorizationAdapter(
            ContextStub("astrbot_plugin_identity_guardian", SlowIdentityProvider()),
            logger,
            trusted_client_id="quest",
            trusted_platform_id="platform",
        )
        decision = await identity.authorize(
            api_principal="api",
            declared_client_id="quest",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert decision.authorized is False
        assert decision.reason == "authorization_timeout"

        knowledge = GlobalKnowledgeAdapter(
            ContextStub("astrbot_plugin_active_learner", SlowKnowledgeProvider()),
            logger,
            timeout_seconds=0.1,
        )
        assert await knowledge.recall("query") == []
        assert knowledge.status == "timeout"

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        environment = CachedEnvironmentAdapter(
            ContextStub(
                "astrbot_plugin_environment_awareness",
                SlowEnvironmentProvider(future),
            ),
            logger,
            timeout_seconds=0.01,
        )
        assert await environment.read() is None
        assert environment.status == "timeout"

        runtime = SeriesRuntimeAdapter(
            ContextStub("astrbot_plugin_update_manager", SlowRuntimeProvider()),
            logger,
            timeout_seconds=0.05,
        )
        assert (await runtime.refresh())["reason"] == "DIAGNOSTIC_TIMEOUT"

        wav_path = tmp_path / "slow.wav"
        with wave.open(str(wav_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\x00\x00" * 2_400)
        voice_adapter = VoiceHubTTSAdapter(
            ContextStub("astrbot_plugin_voice_hub", SlowVoiceProvider(wav_path)),
            logger,
        )
        voice_adapter.timeout_seconds = 0.01
        with pytest.raises(AdapterUnavailable):
            _ = [
                chunk
                async for chunk in voice_adapter.synthesize("hello", emotion="neutral")
            ]
        assert voice_adapter.status == "timeout"

    asyncio.run(scenario())


class KnowledgeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def knowledge_contract(self) -> dict[str, Any]:
        return {
            "name": "active_learner.knowledge",
            "version": "1.0",
            "capabilities": ("recall",),
        }

    async def recall(
        self, query: str, *, scope: str, top_k: int
    ) -> list[dict[str, Any]]:
        self.calls.append((query, scope, top_k))
        return [
            {
                "content": "verified public fact",
                "source": "memory",
                "score": 0.8,
                "topic": "test",
                "verified": True,
                "confidence": 0.9,
                "private": "must not leak",
            },
            {
                "content": "non-finite evidence",
                "source": "memory",
                "score": float("nan"),
                "topic": "test",
                "verified": True,
                "confidence": 0.9,
            },
            {"content": "missing contract fields"},
        ]


def test_knowledge_adapter_is_global_only_and_sanitizes_items() -> None:
    async def scenario() -> None:
        provider = KnowledgeProvider()
        adapter = GlobalKnowledgeAdapter(
            ContextStub("astrbot_plugin_active_learner", provider),
            LoggerStub(),
            top_k=5,
        )
        evidence = await adapter.recall("room context")
        assert provider.calls == [("room context", "global", 5)]
        assert evidence == [
            {
                "content": "verified public fact",
                "source": "memory",
                "score": 0.8,
                "topic": "test",
                "verified": True,
                "confidence": 0.9,
            }
        ]

    asyncio.run(scenario())


class EnvironmentProvider:
    def __init__(self, valid_until: str) -> None:
        self.valid_until = valid_until
        self.calls = 0

    def environment_opportunity_contract(self) -> dict[str, Any]:
        return {
            "name": "environment.opportunity",
            "version": "1.0",
            "capabilities": ("cached_read", "background_refresh"),
            "request_hook_network": False,
        }

    def get_cached_opportunity(self, *, allow_stale: bool) -> dict[str, Any]:
        assert allow_stale is True
        self.calls += 1
        return {
            "contract": "environment.opportunity",
            "version": "1.0",
            "event_key": "heat",
            "revision": "r1",
            "kind": "weather_alert",
            "severity": "high",
            "severity_rank": 2,
            "severity_basis": ("official",),
            "facts": {"temperature": 36},
            "location": {"key": "home", "name": "Home", "timezone": "Asia/Shanghai"},
            "observed_at": None,
            "fetched_at": None,
            "stale": False,
            "provenance": {
                "authority": "official",
                "provider": "cache",
                "local_assessment": "none",
            },
            "valid_from": "2026-08-03T00:00:00+00:00",
            "valid_until": self.valid_until,
        }


def test_environment_adapter_reads_cache_and_drops_expired_payload() -> None:
    async def scenario() -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        provider = EnvironmentProvider(future)
        adapter = CachedEnvironmentAdapter(
            ContextStub("astrbot_plugin_environment_awareness", provider),
            LoggerStub(),
            timeout_seconds=0.5,
        )
        payload = await adapter.read()
        assert payload is not None
        assert payload["facts"] == {"temperature": 36}

        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        expired = EnvironmentProvider(past)
        adapter = CachedEnvironmentAdapter(
            ContextStub("astrbot_plugin_environment_awareness", expired),
            LoggerStub(),
            timeout_seconds=0.5,
        )
        assert await adapter.read() is None

    asyncio.run(scenario())


class RuntimeProvider:
    def series_runtime_contract(self) -> dict[str, Any]:
        return {
            "name": "update_manager.series_runtime",
            "version": "1.0",
            "capabilities": ("read_runtime_snapshot",),
            "method": "get_series_runtime_snapshot",
            "network_access": False,
            "update_side_effects": False,
        }

    async def get_series_runtime_snapshot(
        self, *, timeout_seconds: float
    ) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        return {
            "contract_name": "update_manager.series_runtime",
            "contract_version": "1.0",
            "capability": "read_runtime_snapshot",
            "status": "ok",
            "reason": "HEALTHY",
            "members": [
                {
                    "plugin_id": "astrbot_plugin_voice_hub",
                    "label": "voice",
                    "installed": True,
                    "loaded": True,
                    "activated": True,
                    "version": "1.0.0",
                    "health_status": "ok",
                    "reason": "HEALTHY",
                }
            ],
            "healthy": 1,
            "total": 1,
        }


def test_runtime_adapter_is_read_only_and_validates_snapshot() -> None:
    async def scenario() -> None:
        adapter = SeriesRuntimeAdapter(
            ContextStub("astrbot_plugin_update_manager", RuntimeProvider()),
            LoggerStub(),
        )
        snapshot = await adapter.refresh()
        assert snapshot["status"] == "ok"
        assert snapshot["healthy"] == 1
        assert snapshot["members"][0]["plugin_id"] == "astrbot_plugin_voice_hub"

    asyncio.run(scenario())


class VoiceProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[tuple[str, str]] = []

    def voice_audio_output_contract(self) -> dict[str, Any]:
        return {
            "name": "voice.audio_output",
            "version": "1.0",
            "capabilities": ("render_pcm_wav",),
            "method": "render_pcm_wav",
            "sends_message": False,
        }

    async def render_pcm_wav(
        self,
        text: str,
        *,
        emotion: str,
        voice: str,
        context: str,
        session_id: str,
    ) -> dict[str, Any]:
        assert voice == context == session_id == ""
        self.calls.append((text, emotion))
        return {
            "contract_name": "voice.audio_output",
            "contract_version": "1.0",
            "capability": "render_pcm_wav",
            "status": "ok",
            "error_code": "",
            "path": str(self.path.resolve()),
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 48_000,
            "channels": 2,
            "sample_width": 2,
            "frame_count": 4_800,
            "duration_ms": 100,
            "ownership": "provider_managed",
            "consumer_may_delete": False,
        }


def test_voice_hub_adapter_normalizes_without_deleting_provider_file(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "provider.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48_000)
            output.writeframes(b"\x00\x00" * 4_800 * 2)
        provider = VoiceProvider(path)
        adapter = VoiceHubTTSAdapter(
            ContextStub("astrbot_plugin_voice_hub", provider),
            LoggerStub(),
        )
        chunks = [chunk async for chunk in adapter.synthesize("hello", emotion="happy")]
        assert b"".join(chunks) == b"\x00\x00" * 2_400
        assert provider.calls == [("hello", "happy")]
        assert path.is_file()

    asyncio.run(scenario())


def test_malformed_series_payloads_fail_closed(tmp_path: Path) -> None:
    class MalformedIdentityProvider(IdentityProvider):
        def quest_session_authorization_contract(self) -> dict[str, Any]:
            payload = super().quest_session_authorization_contract()
            payload["permission_identity_fields"] = {"unexpected": True}
            return payload

    class MalformedEnvironmentProvider(EnvironmentProvider):
        def get_cached_opportunity(self, *, allow_stale: bool) -> dict[str, Any]:
            payload = super().get_cached_opportunity(allow_stale=allow_stale)
            payload["severity_rank"] = {"not": "an integer"}
            return payload

    class UnhashableEnvironmentProvider(EnvironmentProvider):
        def get_cached_opportunity(self, *, allow_stale: bool) -> dict[str, Any]:
            payload = super().get_cached_opportunity(allow_stale=allow_stale)
            payload["severity"] = {"not": "a string"}
            return payload

    class MalformedRuntimeProvider(RuntimeProvider):
        async def get_series_runtime_snapshot(
            self, *, timeout_seconds: float
        ) -> dict[str, Any]:
            payload = await super().get_series_runtime_snapshot(
                timeout_seconds=timeout_seconds
            )
            payload["unexpected"] = True
            return payload

    class UnhashableRuntimeProvider(RuntimeProvider):
        async def get_series_runtime_snapshot(
            self, *, timeout_seconds: float
        ) -> dict[str, Any]:
            payload = await super().get_series_runtime_snapshot(
                timeout_seconds=timeout_seconds
            )
            payload["status"] = {"not": "a string"}
            return payload

    class ErrorVoiceProvider(VoiceProvider):
        async def render_pcm_wav(
            self,
            text: str,
            *,
            emotion: str,
            voice: str,
            context: str,
            session_id: str,
        ) -> dict[str, Any]:
            payload = await super().render_pcm_wav(
                text,
                emotion=emotion,
                voice=voice,
                context=context,
                session_id=session_id,
            )
            payload.update(status="error", error_code="unsupported_audio_format")
            return payload

    async def scenario() -> None:
        identity = QuestSessionAuthorizationAdapter(
            ContextStub(
                "astrbot_plugin_identity_guardian",
                MalformedIdentityProvider(),
            ),
            LoggerStub(),
            trusted_client_id="quest",
            trusted_platform_id="platform",
        )
        decision = await identity.authorize(
            api_principal="api",
            declared_client_id="quest",
            bot_id="bot",
            user_id="user",
            group_id="",
        )
        assert decision.authorized is False
        assert decision.reason == "contract_incompatible"

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        environment = CachedEnvironmentAdapter(
            ContextStub(
                "astrbot_plugin_environment_awareness",
                MalformedEnvironmentProvider(future),
            ),
            LoggerStub(),
            timeout_seconds=0.5,
        )
        assert await environment.read() is None

        unhashable_environment = CachedEnvironmentAdapter(
            ContextStub(
                "astrbot_plugin_environment_awareness",
                UnhashableEnvironmentProvider(future),
            ),
            LoggerStub(),
            timeout_seconds=0.5,
        )
        assert await unhashable_environment.read() is None

        runtime = SeriesRuntimeAdapter(
            ContextStub("astrbot_plugin_update_manager", MalformedRuntimeProvider()),
            LoggerStub(),
        )
        snapshot = await runtime.refresh()
        assert snapshot["status"] == "error"
        assert snapshot["reason"] == "DIAGNOSTIC_INVALID"

        unhashable_runtime = SeriesRuntimeAdapter(
            ContextStub("astrbot_plugin_update_manager", UnhashableRuntimeProvider()),
            LoggerStub(),
        )
        snapshot = await unhashable_runtime.refresh()
        assert snapshot["status"] == "error"
        assert snapshot["reason"] == "DIAGNOSTIC_INVALID"

        path = tmp_path / "error-provider.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\x00\x00" * 2_400)
        voice_adapter = VoiceHubTTSAdapter(
            ContextStub("astrbot_plugin_voice_hub", ErrorVoiceProvider(path)),
            LoggerStub(),
        )
        try:
            _ = [
                chunk
                async for chunk in voice_adapter.synthesize("hello", emotion="neutral")
            ]
        except RuntimeError as exc:
            assert "unsupported_audio_format" in str(exc)
        else:  # pragma: no cover - safety assertion
            raise AssertionError("malformed voice result must fail closed")

    asyncio.run(scenario())
