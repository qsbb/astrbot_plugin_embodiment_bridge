from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, Star, StarTools

from .adapters.astrbot_llm import AstrBotLLMAdapter
from .adapters.relationship import RelationshipSnapshotAdapter
from .adapters.stt import DisabledSTTAdapter
from .adapters.tts import DisabledTTSAdapter
from .core.interaction_policy import InteractionPolicy
from .core.session_manager import SessionManager
from .core.turn_orchestrator import TurnOrchestrator
from .transport.http_sse import HttpSseTransport, PLUGIN_NAME, TransportConfig


__version__ = "0.1.0"


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
        self.llm = AstrBotLLMAdapter(
            context,
            chat_provider_id=str(config.get("chat_provider_id", "") or ""),
            persona_prompt=str(config.get("persona_prompt", "") or ""),
        )
        self.stt = DisabledSTTAdapter()
        self.tts = DisabledTTSAdapter()
        self.relationship = RelationshipSnapshotAdapter(context, logger)
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
            logger=logger,
            output_chunk_ms=self._int_config("output_chunk_ms", 50, 40, 100),
        )
        self.transport = HttpSseTransport(
            context=context,
            sessions=self.sessions,
            orchestrator=self.orchestrator,
            config=TransportConfig(
                bridge_api_key=str(config.get("bridge_api_key", "") or ""),
                max_json_body_bytes=self._int_config(
                    "max_json_body_bytes", 65_536, 4_096, 262_144
                ),
                max_audio_request_bytes=self._int_config(
                    "max_audio_request_bytes", 32_768, 8_192, 131_072
                ),
                sse_heartbeat_seconds=self._int_config(
                    "sse_heartbeat_seconds", 15, 5, 60
                ),
            ),
            logger=logger,
        )
        self.transport.register()

    async def initialize(self) -> None:
        logger.info(
            "[quest-avatar] bridge initialized: version=%s transport=http+sse llm=%s stt=%s tts=%s",
            __version__,
            self.llm.available,
            self.stt.available,
            self.tts.available,
        )

    def plugin_health(self) -> dict[str, object]:
        checks = {
            "transport_registered": self.transport is not None,
            "bridge_api_key_configured": len(self.transport.config.bridge_api_key)
            >= 32,
            "chat_provider_configured": self.llm.available,
            "data_dir_ready": self.data_dir.is_dir(),
        }
        reasons = [name.upper() for name, passed in checks.items() if not passed]
        return {
            "status": "ok" if not reasons else "degraded",
            "checks": checks,
            "reasons": reasons,
            "version": __version__,
        }

    async def terminate(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        await self.orchestrator.close()
        logger.info("[quest-avatar] bridge terminated")

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return int(self._number_config(key, default, minimum, maximum))

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
