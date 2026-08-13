from __future__ import annotations

import asyncio
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from astrbot.api.web import error_response, json_response, request, stream_response
from pydantic import BaseModel, TypeAdapter, ValidationError

from ..adapters.stt import AstrBotSTTAdapter
from ..core.models import (
    AudioChunkRequest,
    AudioEndRequest,
    Identifier,
    InteractionEvent,
    InterruptRequest,
    PROTOCOL_VERSION,
    SessionCloseRequest,
    SessionStartRequest,
    SpatialContextRequest,
    TurnStartRequest,
)
from ..core.plugin_identity import (
    BRIDGE_AUTH_HEADER,
    LEGACY_BRIDGE_AUTH_HEADER,
    LEGACY_ROUTE_PREFIX,
    PLUGIN_ID,
    PUBLIC_API_PREFIX,
    ROUTE_PREFIX,
)
from ..core.session_manager import (
    BridgeStateError,
    QueueClosed,
    SessionConflict,
    SessionManager,
)
from ..core.service_control import BridgeServiceUnavailable
from ..core.turn_orchestrator import TurnOrchestrator


PLUGIN_NAME = PLUGIN_ID
ModelT = TypeVar("ModelT", bound=BaseModel)


class HttpApiError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class TransportConfig:
    bridge_api_key: str
    max_json_body_bytes: int = 65_536
    max_audio_request_bytes: int = 32_768
    sse_heartbeat_seconds: int = 15


class HttpSseTransport:
    def __init__(
        self,
        *,
        context: Any,
        sessions: SessionManager,
        orchestrator: TurnOrchestrator,
        listener: Any,
        service: Any,
        config: TransportConfig,
        logger: Any,
        diagnostic_log: Any | None = None,
    ) -> None:
        self.context = context
        self.sessions = sessions
        self.orchestrator = orchestrator
        self.listener = listener
        self.service = service
        self.config = config
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self._identifier_adapter = TypeAdapter(Identifier)

    def configure_bridge_api_key(self, value: str) -> None:
        self.config = TransportConfig(
            bridge_api_key=str(value or ""),
            max_json_body_bytes=self.config.max_json_body_bytes,
            max_audio_request_bytes=self.config.max_audio_request_bytes,
            sse_heartbeat_seconds=self.config.sse_heartbeat_seconds,
        )

    def configure_identity_refresh(self, callback: Any) -> None:
        self._identity_refresh = callback

    def register(self) -> None:
        routes = (
            (
                "session/start",
                self.session_start,
                ["POST"],
                "Start embodied-client session",
            ),
            ("events/<session_id>", self.events, ["GET"], "Embodied-client SSE events"),
            ("turn/start", self.turn_start, ["POST"], "Start embodied-client turn"),
            ("audio/chunk", self.audio_chunk, ["POST"], "Append client PCM16 audio"),
            ("audio/end", self.audio_end, ["POST"], "Finish client PCM16 audio"),
            (
                "interaction",
                self.interaction,
                ["POST"],
                "Submit embodied interaction fact",
            ),
            ("interrupt", self.interrupt, ["POST"], "Interrupt active client turn"),
            (
                "session/close",
                self.session_close,
                ["POST"],
                "Close embodied-client session",
            ),
            ("health", self.health, ["GET"], "Embodiment bridge health"),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"{ROUTE_PREFIX}/{suffix}", handler, methods, description
            )
            # One bounded compatibility cycle for already-bound clients. These
            # aliases still pass AstrBot auth and bridge-key auth; the standalone
            # listener never exposes the legacy anonymous exchange endpoint.
            self.context.register_web_api(
                f"{LEGACY_ROUTE_PREFIX}/{suffix}",
                handler,
                methods,
                f"Legacy authenticated alias: {description}",
            )
        self.context.register_web_api(
            f"{ROUTE_PREFIX}/spatial/context",
            self.spatial_context,
            ["POST"],
            "Update coarse embodied spatial context",
        )

    async def session_start(self) -> Any:
        async def action(owner: str, payload: SessionStartRequest) -> Any:
            refresh = getattr(self, "_identity_refresh", None)
            if callable(refresh):
                await refresh()
            payload = self.orchestrator.canonicalize_session_request(payload)
            authorization = await self.orchestrator.authorize_session(owner, payload)
            session = await self.sessions.start_session(
                payload,
                owner,
                protected_context_authorized=authorization.authorized,
                context_authorization_reason=authorization.reason,
            )
            self._diagnostic(
                "session.started",
                component="session",
                phase="session_start",
                status="ready" if authorization.authorized else "limited",
                authorized=authorization.authorized,
                reason_code=authorization.reason,
            )
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "protocol_version": PROTOCOL_VERSION,
                        "session_id": session.session_id,
                        "events_url": f"{PUBLIC_API_PREFIX}/events/{session.session_id}",
                        "protected_context": {
                            "authorized": authorization.authorized,
                            "reason": authorization.reason,
                        },
                    },
                },
                status_code=201,
            )

        return await self._json_endpoint(SessionStartRequest, action)

    async def events(self, session_id: str) -> Any:
        try:
            owner = self._authenticate()
            self.service.require_enabled()
            validated_session_id = self._identifier_adapter.validate_python(session_id)
            session = await self.sessions.get_owned(validated_session_id, owner)
            if not await self.sessions.attach_stream(session):
                raise SessionConflict("an SSE stream is already attached")
            self._diagnostic(
                "sse.connected",
                component="sse",
                phase="stream",
                status="connected",
                attached_streams=1,
            )

            async def event_stream():
                try:
                    yield ": connected\n\n"
                    while True:
                        try:
                            item = await asyncio.wait_for(
                                session.queue.get(),
                                timeout=self.config.sse_heartbeat_seconds,
                            )
                        except TimeoutError:
                            yield ": keep-alive\n\n"
                            continue
                        except QueueClosed:
                            break
                        event_type = item.event_type or "message"
                        data = json.dumps(
                            item.payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.CancelledError:
                    raise
                finally:
                    await self.sessions.detach_stream(session)
                    self._diagnostic(
                        "sse.disconnected",
                        component="sse",
                        phase="stream",
                        status="closed",
                        attached_streams=0,
                    )

            return stream_response(
                event_stream(),
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        except Exception as exc:
            self._diagnostic(
                "sse.error",
                component="sse",
                code=getattr(exc, "code", "sse_failed"),
                error_type=type(exc).__name__,
            )
            return self._error(exc, "events")

    async def turn_start(self) -> Any:
        async def action(owner: str, payload: TurnStartRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            turn = await self.orchestrator.start_turn(session, payload)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "turn_id": turn.turn_id,
                        "state": "processing" if payload.text else "awaiting_audio",
                    },
                },
                status_code=202,
            )

        return await self._json_endpoint(TurnStartRequest, action)

    async def audio_chunk(self) -> Any:
        async def action(owner: str, payload: AudioChunkRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            total_bytes = await self.sessions.add_audio_chunk(session, payload)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "turn_id": payload.turn_id,
                        "sequence": payload.sequence,
                        "buffered_bytes": total_bytes,
                    },
                },
                status_code=202,
            )

        return await self._json_endpoint(
            AudioChunkRequest,
            action,
            body_limit=self.config.max_audio_request_bytes,
        )

    async def audio_end(self) -> Any:
        async def action(owner: str, payload: AudioEndRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            turn = await self.orchestrator.finish_audio(session, payload.turn_id)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "turn_id": turn.turn_id,
                        "state": "processing",
                    },
                },
                status_code=202,
            )

        return await self._json_endpoint(AudioEndRequest, action)

    async def interaction(self) -> Any:
        async def action(owner: str, payload: InteractionEvent) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            turn = await self.orchestrator.submit_interaction(session, payload)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "event_id": payload.event_id,
                        "accepted": turn is not None,
                        "turn_id": turn.turn_id if turn is not None else None,
                        "reason": "accepted"
                        if turn is not None
                        else "duplicate_or_debounced",
                    },
                },
                status_code=202,
            )

        return await self._json_endpoint(InteractionEvent, action)

    async def interrupt(self) -> Any:
        async def action(owner: str, payload: InterruptRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            cancelled = await self.sessions.cancel_current(session, payload.turn_id)
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "turn_id": payload.turn_id,
                        "cancelled": cancelled,
                    },
                }
            )

        return await self._json_endpoint(InterruptRequest, action)

    async def session_close(self) -> Any:
        async def action(owner: str, payload: SessionCloseRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            await self.sessions.close_session(session)
            return json_response(
                {
                    "status": "ok",
                    "data": {"session_id": payload.session_id, "closed": True},
                }
            )

        return await self._json_endpoint(SessionCloseRequest, action)

    async def spatial_context(self) -> Any:
        async def action(owner: str, payload: SpatialContextRequest) -> Any:
            session = await self.sessions.get_owned(payload.session_id, owner)
            state, snapshot = await self.sessions.update_spatial_context(
                session,
                payload,
            )
            return json_response(
                {
                    "status": "ok",
                    "data": {
                        "session_id": session.session_id,
                        "schema_version": snapshot.schema_version,
                        "revision": snapshot.revision,
                        "state": state,
                    },
                }
            )

        return await self._json_endpoint(SpatialContextRequest, action)

    async def health(self) -> Any:
        started = time.perf_counter()
        try:
            self._authenticate()
            await self.orchestrator.refresh_runtime_diagnostics()
            stats = await self.sessions.stats()
            service = await self.service.status_snapshot()
            response = json_response(
                {
                    "status": "ok",
                    "data": {
                        "protocol_version": PROTOCOL_VERSION,
                        "transport": "http+sse",
                        "input_audio": {
                            "format": "pcm16",
                            "sample_rate": 16_000,
                            "channels": 1,
                            "stt_available": self.orchestrator.stt.available,
                            "stt_source": self._stt_health_snapshot(),
                        },
                        "output_audio": {
                            "format": "pcm16",
                            "sample_rate": 24_000,
                            "channels": 1,
                            "tts_available": self.orchestrator.tts.available,
                        },
                        "pairing_listener": self.listener.status_snapshot(),
                        "service": service,
                        "diagnostic_log": (
                            self.diagnostic_log.status_snapshot()
                            if self.diagnostic_log is not None
                            else {
                                "enabled": False,
                                "status": "disabled",
                                "write_failures": 0,
                            }
                        ),
                        "series_integrations": self.orchestrator.integration_status(),
                        **stats,
                    },
                }
            )
            self._diagnostic(
                "http.health",
                component="health",
                status=getattr(response, "status_code", 200),
                http_status=getattr(response, "status_code", 200),
                duration_ms=(time.perf_counter() - started) * 1000,
                available=self.orchestrator.stt.available,
                ready=self.listener.status_snapshot().get("ready", False),
                active_sessions=stats.get("active_sessions", 0),
                attached_streams=stats.get("attached_streams", 0),
            )
            return response
        except Exception as exc:
            self._diagnostic(
                "http.error",
                component="health",
                code=getattr(exc, "code", "health_failed"),
                http_status=getattr(exc, "status_code", 500),
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._error(exc, "health")

    def _stt_health_snapshot(self) -> dict[str, Any]:
        adapter = self.orchestrator.stt
        if isinstance(adapter, AstrBotSTTAdapter):
            snapshot = adapter.status_snapshot()
            return {
                "source": snapshot["source"],
                "available": snapshot["available"],
                "status": snapshot["status"],
                "selected": snapshot["selected"],
                "legacy_default": snapshot["legacy_default"],
                "external_contract_status": snapshot["external_contract_status"],
            }
        return {
            "source": "adapter",
            "available": adapter.available,
            "status": "ready" if adapter.available else "unavailable",
        }

    async def _json_endpoint(
        self,
        model: type[ModelT],
        action: Callable[[str, ModelT], Awaitable[Any]],
        *,
        body_limit: int | None = None,
    ) -> Any:
        started = time.perf_counter()
        operation = model.__name__.removesuffix("Request").lower()
        try:
            owner = self._authenticate()
            self.service.require_enabled()
            payload = await self._read_model(
                model,
                body_limit or self.config.max_json_body_bytes,
            )
            response = await action(owner, payload)
            self._diagnostic(
                "http.request",
                component="transport",
                operation=operation,
                status=getattr(response, "status_code", 200),
                http_status=getattr(response, "status_code", 200),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            self._diagnostic(
                "http.error",
                component="transport",
                operation=operation,
                code=getattr(exc, "code", "request_failed"),
                http_status=getattr(exc, "status_code", 500),
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return self._error(exc, model.__name__)

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return

    def _authenticate(self) -> str:
        owner = str(request.username or "").strip()
        if not owner:
            raise HttpApiError(
                "astrbot_auth_required", 401, "AstrBot API authentication is required"
            )
        configured_key = self.config.bridge_api_key
        if len(configured_key) < 32:
            raise HttpApiError(
                "bridge_not_configured",
                503,
                "Embodiment bridge API key is not configured",
            )
        supplied_key = str(
            request.headers.get(BRIDGE_AUTH_HEADER)
            or request.headers.get(BRIDGE_AUTH_HEADER.lower())
            or request.headers.get(LEGACY_BRIDGE_AUTH_HEADER)
            or request.headers.get(LEGACY_BRIDGE_AUTH_HEADER.lower())
            or ""
        )
        if not hmac.compare_digest(supplied_key, configured_key):
            raise HttpApiError(
                "bridge_auth_failed", 401, "Embodiment bridge authentication failed"
            )
        return owner

    async def _read_model(self, model: type[ModelT], limit: int) -> ModelT:
        content_type = str(request.content_type or "").lower()
        if not content_type.startswith("application/json"):
            raise HttpApiError(
                "unsupported_media_type", 415, "Content-Type must be application/json"
            )
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    raise HttpApiError(
                        "payload_too_large", 413, "Request body is too large"
                    )
            except ValueError as exc:
                raise HttpApiError(
                    "invalid_content_length", 400, "Content-Length is invalid"
                ) from exc
        body = await request.body()
        if not body:
            raise HttpApiError("empty_body", 400, "JSON request body is required")
        if len(body) > limit:
            raise HttpApiError("payload_too_large", 413, "Request body is too large")
        try:
            return model.model_validate_json(body)
        except ValidationError as exc:
            fields = sorted(
                {
                    ".".join(str(part) for part in item.get("loc", ())) or "body"
                    for item in exc.errors(include_input=False)
                }
            )
            raise HttpApiError(
                "schema_validation_failed",
                422,
                "Request schema validation failed",
            ) from ValueError(",".join(fields))

    def _error(self, exc: Exception, operation: str) -> Any:
        if isinstance(exc, HttpApiError):
            return error_response(
                exc.public_message,
                status_code=exc.status_code,
                data={"code": exc.code},
            )
        if isinstance(exc, BridgeStateError):
            return error_response(
                str(exc),
                status_code=exc.status_code,
                data={"code": exc.code},
            )
        if isinstance(exc, BridgeServiceUnavailable):
            return error_response(
                exc.public_message,
                status_code=exc.status_code,
                data={"code": exc.code},
            )
        if isinstance(exc, ValidationError):
            return error_response(
                "Request schema validation failed",
                status_code=422,
                data={"code": "schema_validation_failed"},
            )
        self.logger.error(
            "[embodiment-bridge] HTTP operation failed: operation=%s error_type=%s",
            operation,
            type(exc).__name__,
            exc_info=True,
        )
        return error_response(
            "Internal bridge error",
            status_code=500,
            data={"code": "internal_error"},
        )
