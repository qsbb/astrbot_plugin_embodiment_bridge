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

from ..core.models import (
    AudioChunkRequest,
    AudioEndRequest,
    Identifier,
    InteractionEvent,
    InterruptRequest,
    PROTOCOL_VERSION,
    SessionCloseRequest,
    SessionStartRequest,
    TurnStartRequest,
)
from ..core.session_manager import (
    BridgeStateError,
    QueueClosed,
    SessionConflict,
    SessionManager,
)
from ..core.service_control import BridgeServiceUnavailable
from ..core.turn_orchestrator import TurnOrchestrator


PLUGIN_NAME = "astrbot_plugin_quest_avatar_bridge"
ROUTE_PREFIX = f"/{PLUGIN_NAME}"
PUBLIC_API_PREFIX = f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
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

    def register(self) -> None:
        routes = (
            (
                "session/start",
                self.session_start,
                ["POST"],
                "Start Quest avatar session",
            ),
            ("events/<session_id>", self.events, ["GET"], "Quest avatar SSE events"),
            ("turn/start", self.turn_start, ["POST"], "Start Quest avatar turn"),
            ("audio/chunk", self.audio_chunk, ["POST"], "Append Quest PCM16 audio"),
            ("audio/end", self.audio_end, ["POST"], "Finish Quest PCM16 audio"),
            (
                "interaction",
                self.interaction,
                ["POST"],
                "Submit Quest interaction fact",
            ),
            ("interrupt", self.interrupt, ["POST"], "Interrupt active Quest turn"),
            (
                "session/close",
                self.session_close,
                ["POST"],
                "Close Quest avatar session",
            ),
            ("health", self.health, ["GET"], "Quest avatar bridge health"),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"{ROUTE_PREFIX}/{suffix}",
                handler,
                methods,
                description,
            )

    async def session_start(self) -> Any:
        async def action(owner: str, payload: SessionStartRequest) -> Any:
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
                "bridge_not_configured", 503, "Quest bridge API key is not configured"
            )
        supplied_key = str(request.headers.get("x-quest-avatar-key") or "")
        if not hmac.compare_digest(supplied_key, configured_key):
            raise HttpApiError(
                "bridge_auth_failed", 401, "Quest bridge authentication failed"
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
            "[quest-avatar] HTTP operation failed: operation=%s error_type=%s",
            operation,
            type(exc).__name__,
            exc_info=True,
        )
        return error_response(
            "Internal bridge error",
            status_code=500,
            data={"code": "internal_error"},
        )
