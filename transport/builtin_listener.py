from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from ..core.pairing import (
    PUBLIC_API_PATH,
    PairingError,
    PairingExchangeRequest,
    PairingExchangeService,
    normalize_pairing_exchange_url,
)
from ..core.plugin_identity import (
    BRIDGE_AUTH_HEADER,
    LEGACY_BRIDGE_AUTH_HEADER,
    LEGACY_PUBLIC_API_PREFIX,
    PUBLIC_API_PREFIX,
)


EXCHANGE_PATH = f"{PUBLIC_API_PATH}/pairing/exchange"
EVENTS_PATH_PREFIX = f"{PUBLIC_API_PATH}/events/"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
_FIXED_PROXY_ROUTES = {
    ("GET", f"{PUBLIC_API_PATH}/health"),
    ("POST", f"{PUBLIC_API_PATH}/session/start"),
    ("POST", f"{PUBLIC_API_PATH}/turn/start"),
    ("POST", f"{PUBLIC_API_PATH}/audio/chunk"),
    ("POST", f"{PUBLIC_API_PATH}/audio/end"),
    ("POST", f"{PUBLIC_API_PATH}/playback/receipt"),
    ("POST", f"{PUBLIC_API_PATH}/interaction"),
    ("POST", f"{PUBLIC_API_PATH}/action/result"),
    ("POST", f"{PUBLIC_API_PATH}/interrupt"),
    ("POST", f"{PUBLIC_API_PATH}/session/close"),
    ("POST", f"{PUBLIC_API_PATH}/spatial/context"),
}
_LEGACY_FIXED_PROXY_ROUTES = {
    (method, path.replace(PUBLIC_API_PREFIX, LEGACY_PUBLIC_API_PREFIX, 1))
    for method, path in _FIXED_PROXY_ROUTES
    if path != f"{PUBLIC_API_PATH}/spatial/context"
}
LEGACY_EVENTS_PATH_PREFIX = f"{LEGACY_PUBLIC_API_PREFIX}/events/"
_FORWARDED_REQUEST_HEADERS = {
    "authorization": "Authorization",
    BRIDGE_AUTH_HEADER.lower(): BRIDGE_AUTH_HEADER,
    LEGACY_BRIDGE_AUTH_HEADER.lower(): LEGACY_BRIDGE_AUTH_HEADER,
    "content-type": "Content-Type",
    "accept": "Accept",
    "last-event-id": "Last-Event-ID",
}
_FORWARDED_RESPONSE_HEADERS = {
    "content-type": "Content-Type",
    "retry-after": "Retry-After",
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_UNSAFE_RAW_PATH_RE = re.compile(r"(?i)(?:%2f|%5c|%2e|\\|://|\.\.)")


class ListenerHttpError(RuntimeError):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.public_message = message


@dataclass(frozen=True, slots=True)
class BuiltinListenerConfig:
    enabled: bool
    bind_host: str
    port: int
    upstream_base_url: str
    public_exchange_url: str
    validation_reason: str = ""
    public_url_reason: str = ""
    max_json_body_bytes: int = 65_536
    max_audio_request_bytes: int = 32_768
    exchange_body_bytes: int = 16_384
    max_connections: int = 32
    body_timeout_seconds: float = 10.0
    upstream_connect_timeout_seconds: float = 5.0
    upstream_response_timeout_seconds: float = 10.0

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        allow_private_http: bool,
        max_json_body_bytes: int,
        max_audio_request_bytes: int,
    ) -> BuiltinListenerConfig:
        enabled_raw = values.get("pairing_listener_enabled", False)
        enabled = enabled_raw if isinstance(enabled_raw, bool) else False
        reason = "" if isinstance(enabled_raw, bool) else "invalid_enabled"

        bind_host = ""
        try:
            address = ipaddress.ip_address(
                str(values.get("pairing_listener_host", "0.0.0.0") or "").strip()
            )
            if address.is_multicast:
                raise ValueError
            bind_host = str(address)
        except ValueError:
            reason = reason or "invalid_bind_host"

        port_raw = values.get("pairing_listener_port", 8520)
        if (
            isinstance(port_raw, bool)
            or not isinstance(port_raw, int)
            or not 1024 <= port_raw <= 65_535
        ):
            port = 8520
            reason = reason or "invalid_port"
        else:
            port = port_raw

        try:
            upstream = normalize_loopback_upstream(
                str(
                    values.get(
                        "pairing_listener_upstream_url",
                        "http://127.0.0.1:6185",
                    )
                    or ""
                )
            )
        except ValueError:
            upstream = ""
            reason = reason or "invalid_upstream_url"

        public_raw = str(values.get("pairing_listener_public_url", "") or "").strip()
        public_url = ""
        public_reason = ""
        if public_raw:
            try:
                public_url = normalize_listener_public_url(
                    public_raw,
                    allow_private_http=allow_private_http,
                )
            except PairingError as exc:
                public_reason = exc.code
        else:
            public_reason = "pairing_listener_public_url_missing"

        return cls(
            enabled=enabled,
            bind_host=bind_host,
            port=port,
            upstream_base_url=upstream,
            public_exchange_url=public_url,
            validation_reason=reason,
            public_url_reason=public_reason,
            max_json_body_bytes=max(4_096, min(262_144, max_json_body_bytes)),
            max_audio_request_bytes=max(
                8_192,
                min(131_072, max_audio_request_bytes),
            ),
        )


def normalize_loopback_upstream(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("invalid upstream URL") from exc
    if (
        parsed.scheme.lower() != "http"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("upstream must be a path-free loopback HTTP URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("upstream host must be an IP literal") from exc
    if not address.is_loopback:
        raise ValueError("upstream host must be loopback")
    normalized_host = f"[{address}]" if address.version == 6 else str(address)
    netloc = normalized_host
    if parsed_port is not None:
        netloc = f"{normalized_host}:{parsed_port}"
    return urlunsplit(("http", netloc, "", "", ""))


def normalize_listener_public_url(
    value: str,
    *,
    allow_private_http: bool,
) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise PairingError(
            "invalid_pairing_listener_public_url",
            422,
            "Built-in listener public URL is invalid",
        ) from exc
    if (
        not parsed.scheme
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PairingError(
            "invalid_pairing_listener_public_url",
            422,
            "Built-in listener public URL is invalid",
        )
    path = parsed.path.rstrip("/")
    if path in {"", PUBLIC_API_PATH}:
        path = EXCHANGE_PATH
    elif path != EXCHANGE_PATH:
        raise PairingError(
            "invalid_pairing_listener_public_path",
            422,
            "Built-in listener public path is invalid",
        )
    exact = urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))
    return normalize_pairing_exchange_url(
        exact,
        allow_private_http=allow_private_http,
    )


def _replace_url_port(value: str, port: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    host = parsed.hostname
    if not parsed.scheme or not host or parsed.username or parsed.password:
        return raw
    normalized_host = f"[{host}]" if ":" in host else host
    return urlunsplit(
        (
            parsed.scheme,
            f"{normalized_host}:{int(port)}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


class BuiltinQuestListener:
    """Minimal standalone bootstrap and allowlist proxy bound by the plugin."""

    def __init__(
        self,
        *,
        config: BuiltinListenerConfig,
        exchange_service: PairingExchangeService,
        logger: Any,
        diagnostic_log: Any | None = None,
    ) -> None:
        self.config = config
        self.exchange_service = exchange_service
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._client: aiohttp.ClientSession | None = None
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(config.max_connections)
        self._ready = False
        self._closed = False
        self._bound_port = config.port
        self._reason = "disabled" if not config.enabled else "not_started"

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def public_exchange_url(self) -> str:
        return self.config.public_exchange_url if self._ready else ""

    def status_snapshot(self) -> dict[str, object]:
        return {
            "enabled": self.config.enabled,
            "ready": self._ready,
            "bind_host": self.config.bind_host,
            "port": self._bound_port,
            "upstream_kind": "loopback_http",
            "reason": self._reason,
        }

    def configure_port(self, port: int) -> None:
        if self._ready or self._runner is not None or self._site is not None:
            raise RuntimeError("listener must be stopped before changing port")
        normalized = int(port)
        if not 1024 <= normalized <= 65_535:
            raise ValueError("listener port is outside the supported range")
        self.config = replace(
            self.config,
            port=normalized,
            public_exchange_url=_replace_url_port(
                self.config.public_exchange_url,
                normalized,
            ),
        )
        self._bound_port = normalized
        self._reason = "disabled" if not self.config.enabled else "not_started"

    async def start(self) -> None:
        started = asyncio.get_running_loop().time()
        async with self._lifecycle_lock:
            if self._ready or self._closed:
                return
            if not self.config.enabled:
                self._reason = "disabled"
                return
            if self.config.validation_reason:
                self._reason = self.config.validation_reason
                return
            try:
                timeout = aiohttp.ClientTimeout(
                    total=None,
                    connect=self.config.upstream_connect_timeout_seconds,
                    sock_connect=self.config.upstream_connect_timeout_seconds,
                    sock_read=None,
                )
                self._client = aiohttp.ClientSession(
                    timeout=timeout,
                    connector=aiohttp.TCPConnector(
                        limit=self.config.max_connections,
                        ttl_dns_cache=0,
                    ),
                )
                app = web.Application(
                    client_max_size=max(
                        self.config.max_json_body_bytes,
                        self.config.max_audio_request_bytes,
                        self.config.exchange_body_bytes,
                    )
                    + 1
                )
                app.router.add_route("*", "/{tail:.*}", self._dispatch)
                self._runner = web.AppRunner(
                    app,
                    shutdown_timeout=2.0,
                    access_log=None,
                    handler_cancellation=True,
                )
                await self._runner.setup()
                self._site = web.TCPSite(
                    self._runner,
                    self.config.bind_host,
                    self.config.port,
                )
                await self._site.start()
                server = self._site._server
                if server is not None and server.sockets:
                    self._bound_port = int(server.sockets[0].getsockname()[1])
                self._ready = True
                self._reason = (
                    "ready"
                    if not self.config.public_url_reason
                    else self.config.public_url_reason
                )
                self._diagnostic(
                    "listener.started",
                    component="listener",
                    status="ready",
                    ready=True,
                    enabled=True,
                    duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                )
            except asyncio.CancelledError:
                await self._shutdown_components()
                raise
            except OSError as exc:
                self._reason = "bind_failed"
                self.logger.warning(
                    "[embodiment-bridge] built-in listener unavailable: reason=%s error_type=%s",
                    self._reason,
                    type(exc).__name__,
                )
                await self._shutdown_components()
                self._diagnostic(
                    "listener.start_error",
                    component="listener",
                    code="bind_failed",
                    error_type=type(exc).__name__,
                    duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                )
            except Exception as exc:
                self._reason = "start_failed"
                self.logger.warning(
                    "[embodiment-bridge] built-in listener unavailable: reason=%s error_type=%s",
                    self._reason,
                    type(exc).__name__,
                )
                await self._shutdown_components()
                self._diagnostic(
                    "listener.start_error",
                    component="listener",
                    code="start_failed",
                    error_type=type(exc).__name__,
                    duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                )

    async def close(self) -> None:
        started = asyncio.get_running_loop().time()
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            await self._stop_locked("closed")
            self._diagnostic(
                "listener.closed",
                component="listener",
                status="closed",
                ready=False,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

    async def stop(self, *, reason: str = "service_disabled") -> None:
        started = asyncio.get_running_loop().time()
        async with self._lifecycle_lock:
            if self._closed:
                return
            await self._stop_locked(str(reason or "stopped")[:64])
            self._diagnostic(
                "listener.stopped",
                component="listener",
                status=self._reason,
                ready=False,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

    async def _stop_locked(self, reason: str) -> None:
        self._ready = False
        self._reason = reason
        site = self._site
        self._site = None
        if site is not None:
            await site.stop()
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._active_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._shutdown_components()

    async def _shutdown_components(self) -> None:
        runner = self._runner
        client = self._client
        self._site = None
        self._runner = None
        self._client = None
        self._ready = False
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                pass
        if client is not None and not client.closed:
            await client.close()

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.add(task)
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._capacity.acquire(), timeout=2.0)
                acquired = True
            except TimeoutError:
                return self._error_response(
                    "listener_busy",
                    503,
                    "Embodiment Bridge listener is busy",
                )

            path = self._validated_path(request)
            if path == EXCHANGE_PATH:
                if request.method != "POST":
                    return self._error_response(
                        "method_not_allowed",
                        405,
                        "Method is not allowed",
                        headers={"Allow": "POST"},
                    )
                return await self._exchange(request)

            if not self._proxy_route_allowed(request.method, path):
                return self._error_response(
                    "route_not_allowed",
                    404,
                    "Route is not available on the Embodiment Bridge listener",
                )
            return await self._proxy(request, path)
        except ListenerHttpError as exc:
            return self._error_response(
                exc.code,
                exc.status,
                exc.public_message,
            )
        except asyncio.CancelledError:
            raise
        finally:
            if acquired:
                self._capacity.release()
            if task is not None:
                self._active_tasks.discard(task)

    @staticmethod
    def _validated_path(request: web.Request) -> str:
        raw_path = request.raw_path
        if (
            request.query_string
            or raw_path != request.path
            or _UNSAFE_RAW_PATH_RE.search(raw_path)
        ):
            raise ListenerHttpError(
                "invalid_path",
                400,
                "Request path is invalid",
            )
        return request.path

    @staticmethod
    def _proxy_route_allowed(method: str, path: str) -> bool:
        if (method, path) in _FIXED_PROXY_ROUTES or (
            method,
            path,
        ) in _LEGACY_FIXED_PROXY_ROUTES:
            return True
        if method != "GET":
            return False
        if path.startswith(EVENTS_PATH_PREFIX):
            session_id = path.removeprefix(EVENTS_PATH_PREFIX)
        elif path.startswith(LEGACY_EVENTS_PATH_PREFIX):
            session_id = path.removeprefix(LEGACY_EVENTS_PATH_PREFIX)
        else:
            return False
        if not _SESSION_ID_RE.fullmatch(session_id):
            return False
        return True

    async def _exchange(self, request: web.Request) -> web.Response:
        started = asyncio.get_running_loop().time()
        if request.content_type != "application/json":
            raise ListenerHttpError(
                "unsupported_media_type",
                415,
                "Content-Type must be application/json",
            )
        body = await self._read_exchange_body(request)
        if not body:
            raise ListenerHttpError(
                "empty_body",
                400,
                "JSON request body is required",
            )
        try:
            payload = PairingExchangeRequest.model_validate_json(body)
        except ValidationError as exc:
            raise ListenerHttpError(
                "schema_validation_failed",
                422,
                "Request schema validation failed",
            ) from exc
        try:
            result = self.exchange_service.exchange(
                payload,
                remote=_canonical_peer(request.remote),
            )
            self._diagnostic(
                "pairing.exchange",
                component="pairing",
                status=200,
                result="ok",
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            return web.json_response(result, headers=dict(NO_STORE_HEADERS))
        except PairingError as exc:
            self._diagnostic(
                "pairing.exchange_error",
                component="pairing",
                status=exc.status_code,
                code=exc.code,
                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            headers: dict[str, str] = {}
            data: dict[str, object] = {"code": exc.code}
            if exc.retry_after is not None:
                headers["Retry-After"] = str(exc.retry_after)
                data["retry_after"] = exc.retry_after
            return self._error_response(
                exc.code,
                exc.status_code,
                exc.public_message,
                data=data,
                headers=headers,
            )

    async def _read_exchange_body(self, request: web.Request) -> bytes:
        transfer_encoding = request.headers.get("Transfer-Encoding")
        lengths = request.headers.getall("Content-Length", [])
        if transfer_encoding:
            body = await self._read_bounded_body(
                request,
                self.config.exchange_body_bytes,
            )
            if len(body) > self.config.exchange_body_bytes:
                raise ListenerHttpError(
                    "payload_too_large",
                    413,
                    "Request body is too large",
                )
            raise ListenerHttpError(
                "content_length_required",
                411,
                "Content-Length is required",
            )
        if len(lengths) != 1:
            raise ListenerHttpError(
                "content_length_required",
                411,
                "Content-Length is required",
            )
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ListenerHttpError(
                "invalid_content_length",
                400,
                "Content-Length is invalid",
            )
        expected = int(raw_length)
        if expected > self.config.exchange_body_bytes:
            raise ListenerHttpError(
                "payload_too_large",
                413,
                "Request body is too large",
            )
        body = await self._read_bounded_body(
            request,
            self.config.exchange_body_bytes,
        )
        if len(body) != expected:
            raise ListenerHttpError(
                "invalid_content_length",
                400,
                "Content-Length does not match the request body",
            )
        return body

    async def _read_bounded_body(
        self,
        request: web.Request,
        limit: int,
    ) -> bytes:
        body = bytearray()
        try:
            async with asyncio.timeout(self.config.body_timeout_seconds):
                async for chunk in request.content.iter_chunked(16 * 1024):
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ListenerHttpError(
                            "payload_too_large",
                            413,
                            "Request body is too large",
                        )
        except TimeoutError as exc:
            raise ListenerHttpError(
                "request_body_timeout",
                408,
                "Request body timed out",
            ) from exc
        return bytes(body)

    async def _proxy(
        self,
        request: web.Request,
        path: str,
    ) -> web.StreamResponse:
        client = self._client
        if client is None or client.closed:
            return self._error_response(
                "listener_upstream_unavailable",
                503,
                "AstrBot upstream is unavailable",
            )
        body: bytes | None = None
        if request.method == "POST":
            limit = (
                self.config.max_audio_request_bytes
                if path == f"{PUBLIC_API_PATH}/audio/chunk"
                else self.config.max_json_body_bytes
            )
            body = await self._read_bounded_body(request, limit)
        elif request.headers.get("Content-Length") not in {None, "0"}:
            raise ListenerHttpError(
                "unexpected_body",
                400,
                "GET request body is not allowed",
            )

        headers = {
            output_name: value
            for lower_name, output_name in _FORWARDED_REQUEST_HEADERS.items()
            if (value := request.headers.get(lower_name)) is not None
        }
        upstream_url = self.config.upstream_base_url + path
        try:
            upstream = await asyncio.wait_for(
                client.request(
                    request.method,
                    upstream_url,
                    headers=headers,
                    data=body,
                    allow_redirects=False,
                ),
                timeout=self.config.upstream_response_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            return self._error_response(
                "listener_upstream_unavailable",
                503,
                "AstrBot upstream is unavailable",
            )

        content_type = str(upstream.headers.get("Content-Type") or "")
        if (
            path.startswith(EVENTS_PATH_PREFIX)
            and upstream.status < 400
            and content_type.lower().startswith("text/event-stream")
        ):
            return await self._stream_upstream(request, upstream)
        try:
            response_body = await self._read_upstream_body(upstream)
        except ListenerHttpError as exc:
            upstream.close()
            return self._error_response(exc.code, exc.status, exc.public_message)
        finally:
            upstream.release()
        return web.Response(
            body=response_body,
            status=upstream.status,
            headers=self._upstream_response_headers(upstream, stream=False),
        )

    async def _read_upstream_body(
        self,
        upstream: aiohttp.ClientResponse,
    ) -> bytes:
        body = bytearray()
        try:
            async with asyncio.timeout(self.config.upstream_response_timeout_seconds):
                async for chunk in upstream.content.iter_chunked(16 * 1024):
                    body.extend(chunk)
                    if len(body) > 512 * 1024:
                        raise ListenerHttpError(
                            "listener_upstream_response_too_large",
                            502,
                            "AstrBot upstream response is too large",
                        )
        except TimeoutError as exc:
            raise ListenerHttpError(
                "listener_upstream_timeout",
                503,
                "AstrBot upstream response timed out",
            ) from exc
        except aiohttp.ClientError as exc:
            raise ListenerHttpError(
                "listener_upstream_disconnected",
                503,
                "AstrBot upstream disconnected",
            ) from exc
        return bytes(body)

    async def _stream_upstream(
        self,
        request: web.Request,
        upstream: aiohttp.ClientResponse,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=upstream.status,
            headers=self._upstream_response_headers(upstream, stream=True),
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                if chunk:
                    await response.write(chunk)
        except (ConnectionResetError, aiohttp.ClientError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            upstream.close()
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    @staticmethod
    def _upstream_response_headers(
        upstream: aiohttp.ClientResponse,
        *,
        stream: bool,
    ) -> dict[str, str]:
        headers = {
            output_name: value
            for lower_name, output_name in _FORWARDED_RESPONSE_HEADERS.items()
            if (value := upstream.headers.get(lower_name)) is not None
        }
        headers.update(NO_STORE_HEADERS)
        if stream:
            headers["Cache-Control"] = "no-cache, no-transform"
            headers["X-Accel-Buffering"] = "no"
        return headers

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return

    @staticmethod
    def _error_response(
        code: str,
        status: int,
        message: str,
        *,
        data: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> web.Response:
        response_headers = dict(NO_STORE_HEADERS)
        response_headers.update(headers or {})
        return web.json_response(
            {
                "status": "error",
                "message": message,
                "data": data or {"code": code},
            },
            status=status,
            headers=response_headers,
        )


def _canonical_peer(value: str | None) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return "invalid"
    if address.is_unspecified or address.is_multicast:
        return "invalid"
    return str(address)
