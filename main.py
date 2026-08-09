from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools

from .adapters.astrbot_llm import AstrBotLLMAdapter
from .adapters.astrbot_persona import AstrBotPersonaAdapter
from .adapters.astrbot_pipeline import AstrBotMessagePipelineAdapter
from .adapters.environment import CachedEnvironmentAdapter
from .adapters.identity import QuestSessionAuthorizationAdapter
from .adapters.knowledge import GlobalKnowledgeAdapter
from .adapters.relationship import RelationshipSnapshotAdapter
from .adapters.relationship_candidates import RelationshipIdentityCandidatesAdapter
from .adapters.runtime import SeriesRuntimeAdapter
from .adapters.stt import (
    AstrBotSTTAdapter,
    ConfiguredMiMoSTTAdapter,
    select_stt_adapter,
)
from .adapters.tts import AstrBotTTSAdapter
from .adapters.voice_hub_tts import FallbackTTSAdapter, VoiceHubTTSAdapter
from .core.diagnostic_log import (
    DiagnosticLog,
    DiagnosticLogSink,
)
from .core.interaction_policy import InteractionPolicy
from .core.operator_settings import OperatorSettings
from .core.pairing import PairingExchangeService, PairingManager
from .core.session_manager import SessionManager
from .core.service_control import BridgeServiceControl
from .core.turn_orchestrator import TurnOrchestrator
from .transport.builtin_listener import (
    BuiltinListenerConfig,
    BuiltinQuestListener,
)
from .transport.http_sse import HttpSseTransport, PLUGIN_NAME, TransportConfig
from .transport.pairing import PairingHttpApi


__version__ = "0.4.8"


class QuestAvatarBridgePlugin(Star):
    """HTTP/SSE decision bridge for a model-independent Quest avatar client."""

    PLUGIN_HEALTH_CONTRACT = "plugin.health@1.0"

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.context = context
        self.config = config
        self._terminated = False

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostic_log = DiagnosticLog(
            self.data_dir,
            enabled=self._bool_config("diagnostic_log_enabled", False),
            max_bytes=self._int_config(
                "diagnostic_log_max_bytes", 1_048_576, 16_384, 16 * 1_048_576
            ),
            backup_count=self._int_config("diagnostic_log_backup_count", 3, 0, 10),
            platform_log_enabled=self._bool_config(
                "diagnostic_platform_log_enabled", False
            ),
        )
        self.diagnostic_log.record("plugin.constructed", component="plugin")
        self._component_logger = DiagnosticLogSink(self.diagnostic_log)

        bridge_api_key = str(config.get("bridge_api_key", "") or "")
        trusted_client_id = str(config.get("trusted_client_id", "") or "")
        trusted_platform_id = str(config.get("trusted_platform_id", "") or "")
        relationship_person_id = str(config.get("relationship_person_id", "") or "")
        pairing_exchange_proxy_url = str(
            config.get("pairing_exchange_proxy_url", "") or ""
        )
        pairing_trusted_proxy_ip = str(config.get("pairing_trusted_proxy_ip", "") or "")
        allow_private_http_pairing = self._bool_config(
            "allow_private_http_pairing", False
        )
        max_json_body_bytes = self._int_config(
            "max_json_body_bytes", 65_536, 4_096, 262_144
        )
        max_audio_request_bytes = self._int_config(
            "max_audio_request_bytes", 32_768, 8_192, 131_072
        )

        max_audio_seconds = self._int_config("max_audio_seconds", 60, 1, 120)
        self.sessions = SessionManager(
            max_sessions=self._int_config("max_sessions", 8, 1, 64),
            event_queue_size=self._int_config("event_queue_size", 64, 8, 512),
            max_audio_bytes=16_000 * 2 * max_audio_seconds,
            max_audio_chunk_bytes=self._int_config(
                "max_audio_chunk_bytes", 16_000, 3_200, 65_536
            ),
            interaction_debounce_ms=self._int_config(
                "interaction_debounce_ms", 250, 0, 2_000
            ),
        )
        self.persona = AstrBotPersonaAdapter(
            context,
            source_mode=str(config.get("persona_source_mode", "astrbot") or "astrbot"),
            persona_id=str(config.get("astrbot_persona_id", "") or ""),
        )
        self.llm = AstrBotLLMAdapter(
            context,
            chat_provider_id=str(config.get("chat_provider_id", "") or ""),
            persona_prompt=str(config.get("persona_prompt", "") or ""),
            character_name=str(config.get("character_name", "") or ""),
            character_self_reference=str(
                config.get("character_self_reference", "") or ""
            ),
            character_self_description=str(
                config.get("character_self_description", "") or ""
            ),
            character_user_relationship=str(
                config.get("character_user_relationship", "") or ""
            ),
            persona_adapter=self.persona,
        )
        self.astrbot_stt = AstrBotSTTAdapter(
            context,
            data_dir=self.data_dir / "stt_input",
            enabled=self._bool_config("enable_astrbot_stt", False),
            timeout_seconds=self._float_config("stt_timeout_seconds", 45.0, 1.0, 180.0),
        )
        self.plugin_stt = ConfiguredMiMoSTTAdapter(
            enabled=self._bool_config("enable_plugin_mimo_stt", False),
            api_base=str(
                config.get("plugin_mimo_stt_api_base", "https://api.xiaomimimo.com/v1")
                or ""
            ),
            api_key=str(config.get("plugin_mimo_stt_api_key", "") or ""),
            model=str(config.get("plugin_mimo_stt_model", "mimo-v2.5-asr") or ""),
            timeout_seconds=self._float_config("stt_timeout_seconds", 45.0, 1.0, 180.0),
        )
        self.stt = select_stt_adapter(self.plugin_stt, self.astrbot_stt)
        max_tts_audio_seconds = self._int_config("max_tts_audio_seconds", 120, 1, 300)
        self.astrbot_tts = AstrBotTTSAdapter(
            context,
            enabled=self._bool_config("enable_astrbot_tts", False),
            timeout_seconds=self._float_config("tts_timeout_seconds", 60.0, 1.0, 180.0),
            max_audio_seconds=max_tts_audio_seconds,
        )
        self.voice_hub_tts = VoiceHubTTSAdapter(
            context,
            self._component_logger,
            enabled=self._bool_config("enable_voice_hub_tts", True),
            timeout_seconds=65.0,
            max_audio_seconds=max_tts_audio_seconds,
        )
        self.tts = FallbackTTSAdapter(
            self.voice_hub_tts,
            self.astrbot_tts,
            self._component_logger,
        )
        self.identity = QuestSessionAuthorizationAdapter(
            context,
            self._component_logger,
            trusted_client_id=trusted_client_id,
            trusted_platform_id=trusted_platform_id,
        )
        self.message_pipeline = AstrBotMessagePipelineAdapter(
            context,
            self._component_logger,
            enabled=self._bool_config("enable_astrbot_message_pipeline", True),
            platform_id=trusted_platform_id,
        )
        self.knowledge = GlobalKnowledgeAdapter(
            context,
            self._component_logger,
            enabled=self._bool_config("enable_global_knowledge", True),
            top_k=self._int_config("global_knowledge_top_k", 5, 1, 10),
        )
        self.environment = CachedEnvironmentAdapter(
            context,
            self._component_logger,
            enabled=self._bool_config("enable_environment_context", True),
        )
        self.runtime = SeriesRuntimeAdapter(
            context,
            self._component_logger,
            enabled=self._bool_config("enable_runtime_diagnostics", True),
        )
        self.relationship = RelationshipSnapshotAdapter(
            context,
            self._component_logger,
            person_id=relationship_person_id,
        )
        self.relationship_candidates = RelationshipIdentityCandidatesAdapter(
            context,
            self._component_logger,
        )
        self.policy = InteractionPolicy(
            max_intensity=self._float_config("max_intensity", 0.85, 0.1, 1.0),
            max_duration_ms=self._int_config(
                "max_intent_duration_ms", 8_000, 100, 30_000
            ),
            max_continuous_touch_ms=self._int_config(
                "max_continuous_touch_ms", 15_000, 1_000, 120_000
            ),
            gesture_cooldown_seconds=(
                self._int_config("gesture_cooldown_ms", 350, 0, 10_000) / 1000
            ),
        )
        self.orchestrator = TurnOrchestrator(
            sessions=self.sessions,
            llm=self.llm,
            stt=self.stt,
            tts=self.tts,
            relationship=self.relationship,
            policy=self.policy,
            logger=self._component_logger,
            identity=self.identity,
            knowledge=self.knowledge,
            environment=self.environment,
            runtime=self.runtime,
            voice_audio=self.voice_hub_tts,
            message_pipeline=self.message_pipeline,
            allow_direct_provider_fallback=self._bool_config(
                "allow_direct_provider_fallback", False
            ),
            output_chunk_ms=self._int_config("output_chunk_ms", 50, 40, 100),
            diagnostic_log=self.diagnostic_log,
        )
        self.pairing = PairingManager(
            bridge_api_key=bridge_api_key,
            exchange_url=pairing_exchange_proxy_url,
            allow_private_http=allow_private_http_pairing,
        )
        self._fallback_exchange_url = self.pairing.exchange_url
        self._fallback_exchange_reason = self.pairing.bootstrap_reason
        self.pairing_exchange_service = PairingExchangeService(self.pairing)
        listener_config = BuiltinListenerConfig.from_mapping(
            config,
            allow_private_http=allow_private_http_pairing,
            max_json_body_bytes=max_json_body_bytes,
            max_audio_request_bytes=max_audio_request_bytes,
        )
        pairing_public_url = str(config.get("pairing_public_url", "") or "").strip()
        if not pairing_public_url and listener_config.public_exchange_url.endswith(
            "/pairing/exchange"
        ):
            pairing_public_url = listener_config.public_exchange_url[
                : -len("/pairing/exchange")
            ]
        self.pairing_listener = BuiltinQuestListener(
            config=listener_config,
            exchange_service=self.pairing_exchange_service,
            logger=self._component_logger,
            diagnostic_log=self.diagnostic_log,
        )
        self.service = BridgeServiceControl(
            config=config,
            listener=self.pairing_listener,
            sessions=self.sessions,
            orchestrator=self.orchestrator,
            logger=self._component_logger,
            diagnostic_log=self.diagnostic_log,
            enabled=self._bool_config("bridge_service_enabled", True),
        )
        self.transport = HttpSseTransport(
            context=context,
            sessions=self.sessions,
            orchestrator=self.orchestrator,
            listener=self.pairing_listener,
            service=self.service,
            config=TransportConfig(
                bridge_api_key=bridge_api_key,
                max_json_body_bytes=max_json_body_bytes,
                max_audio_request_bytes=max_audio_request_bytes,
                sse_heartbeat_seconds=self._int_config(
                    "sse_heartbeat_seconds", 15, 5, 60
                ),
            ),
            logger=self._component_logger,
            diagnostic_log=self.diagnostic_log,
        )
        self.operator_settings = OperatorSettings(
            context=context,
            config=config,
            llm=self.llm,
            relationship=self.relationship,
            persona=self.persona,
            logger=self._component_logger,
            diagnostic_log=self.diagnostic_log,
            identity=self.identity,
            message_pipeline=self.message_pipeline,
        )
        self.pairing_api = PairingHttpApi(
            context=context,
            manager=self.pairing,
            exchange_service=self.pairing_exchange_service,
            listener=self.pairing_listener,
            service=self.service,
            logger=self._component_logger,
            trusted_client_id=trusted_client_id,
            trusted_platform_id=trusted_platform_id,
            trusted_proxy_ip=pairing_trusted_proxy_ip,
            operator_settings=self.operator_settings,
            relationship_candidates=self.relationship_candidates,
            diagnostic_log=self.diagnostic_log,
            pairing_defaults={
                "public_url": pairing_public_url,
                "astrbot_api_key": str(config.get("pairing_astrbot_api_key", "") or ""),
                "client_id": trusted_client_id or "quest-living-room",
                "user_id": str(config.get("pairing_user_id", "") or ""),
                "bot_id": str(config.get("pairing_bot_id", "") or ""),
                "group_id": str(config.get("pairing_group_id", "") or ""),
                "relationship_profile_id": str(
                    config.get("pairing_relationship_profile_id", "") or ""
                ),
                "allow_insecure_http": allow_private_http_pairing,
                "ttl_seconds": self._int_config("pairing_ttl_seconds", 120, 60, 300),
            },
            max_json_body_bytes=min(max_json_body_bytes, 65_536),
        )

        # AstrBot 4.26.8 does not expose a Web API unregister operation. Keep
        # every potentially failing component construction above the first
        # registration so a constructor failure cannot leave bound methods
        # from a half-initialized plugin in the process-global route registry.
        # From this point onward, registration must remain the final action.
        self.transport.register()
        self.pairing_api.register()

    async def initialize(self) -> None:
        await self.diagnostic_log.start()
        await self.service.initialize()
        listener_status = self.pairing_listener.status_snapshot()
        if self.pairing_listener.ready and self.pairing_listener.public_exchange_url:
            self.pairing.configure_exchange_url(
                self.pairing_listener.public_exchange_url,
                missing_reason="pairing_listener_public_url_missing",
            )
        elif self._fallback_exchange_url:
            self.pairing.configure_exchange_url(
                self._fallback_exchange_url,
                missing_reason=self._fallback_exchange_reason,
            )
        else:
            self.pairing.configure_exchange_url(
                "",
                missing_reason=str(
                    listener_status.get("reason") or self._fallback_exchange_reason
                ),
            )
        if not self.pairing.bootstrap_ready:
            self._component_logger.warning(
                "[quest-avatar] pairing bootstrap disabled: reason=%s",
                self.pairing.bootstrap_reason,
            )
        runtime = await self.runtime.refresh()
        persona = await self.persona.resolve()
        self._component_logger.info(
            "[quest-avatar] bridge initialized: version=%s transport=http+sse listener=%s llm=%s stt=%s tts=%s runtime=%s",
            __version__,
            listener_status.get("reason"),
            self.llm.available,
            self.stt.available,
            self.tts.available,
            runtime.get("status"),
        )
        self.diagnostic_log.record(
            "plugin.initialized",
            component="plugin",
            status="ok",
            ready=listener_status.get("reason") == "ready",
            enabled=listener_status.get("enabled", False),
            available=self.llm.available,
        )
        self.diagnostic_log.record(
            "capabilities.status",
            component="capabilities",
            status=runtime.get("status", "unknown"),
            enabled=self.stt.available,
            available=self.tts.available,
            ready=self.llm.available,
        )
        self.diagnostic_log.record(
            "persona.status",
            component="persona",
            status=persona.status,
            persona_source=persona.source,
            persona_status=persona.status,
            persona_configured=(
                self.llm.persona_configured
                if persona.source == "manual_override"
                else persona.status == "ready"
            ),
            character_name_configured=(
                self.llm.character_name_configured
                if persona.source == "manual_override"
                else False
            ),
            name_configured=(
                self.llm.character_name_configured
                if persona.source == "manual_override"
                else persona.name_configured
            ),
        )

    def plugin_health(self) -> dict[str, object]:
        checks = {
            "bridge_service_enabled": self.service.enabled,
            "transport_registered": self.transport is not None,
            "pairing_registered": self.pairing_api is not None,
            "pairing_bootstrap_ready": self.pairing.bootstrap_ready,
            "pairing_listener_ready": (
                not self.pairing_listener.config.enabled or self.pairing_listener.ready
            ),
            "bridge_api_key_configured": len(self.transport.config.bridge_api_key)
            >= 32,
            "chat_provider_configured": self.llm.available,
            "data_dir_ready": self.data_dir.is_dir(),
            "identity_guard_configured": self.identity.configured,
            "series_runtime_checked": self.runtime.snapshot.get("status")
            != "not_checked",
        }
        reasons = [name.upper() for name, passed in checks.items() if not passed]
        result = {
            "status": "ok" if not reasons else "degraded",
            "checks": checks,
            "reasons": reasons,
            "version": __version__,
        }
        self.diagnostic_log.record(
            "health.status",
            component="health",
            status=result["status"],
            ready=bool(checks["pairing_listener_ready"]),
            available=bool(checks["chat_provider_configured"]),
        )
        return result

    def diagnostic_events(
        self, *, after_seq: int = 0, limit: int = 200
    ) -> dict[str, Any]:
        return self.diagnostic_log.diagnostic_events(after_seq=after_seq, limit=limit)

    def diagnostic_clear(self) -> None:
        self.diagnostic_log.diagnostic_clear()

    async def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        self.diagnostic_log.record(
            "plugin.terminate", component="plugin", status="start"
        )
        terminated_ok = False
        try:
            await self.service.close()
            self.pairing.close()
            await self.orchestrator.close()
            await self.relationship_candidates.close()
            self._component_logger.info("[quest-avatar] bridge terminated")
            terminated_ok = True
        except Exception as exc:
            self.diagnostic_log.record(
                "plugin.terminate_error",
                component="plugin",
                code="terminate_failed",
                error_type=type(exc).__name__,
            )
            raise
        finally:
            if terminated_ok:
                self.diagnostic_log.record(
                    "plugin.terminated", component="plugin", status="ok"
                )
            await self.diagnostic_log.close(timeout=2.0)

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return int(self._number_config(key, default, minimum, maximum))

    def _bool_config(self, key: str, default: bool) -> bool:
        value: Any = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _float_config(
        self,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        return float(self._number_config(key, default, minimum, maximum))

    def _number_config(
        self,
        key: str,
        default: int | float,
        minimum: int | float,
        maximum: int | float,
    ) -> int | float:
        value: Any = self.config.get(key, default)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float(default)
        return max(minimum, min(maximum, parsed))
