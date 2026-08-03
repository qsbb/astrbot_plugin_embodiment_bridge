from __future__ import annotations

import base64
from io import BytesIO
from typing import Any, TypeVar

import qrcode
from astrbot.api.web import error_response, json_response, request
from pydantic import BaseModel, ValidationError
from qrcode.constants import ERROR_CORRECT_M
from qrcode.image.svg import SvgPathImage

from ..core.pairing import (
    PAIRING_PROTOCOL_VERSION,
    PUBLIC_API_PATH,
    PairingCreateRequest,
    PairingError,
    PairingExchangeRequest,
    PairingManager,
    PairingRevokeRequest,
    PairingStatusRequest,
)


PLUGIN_NAME = "astrbot_plugin_quest_avatar_bridge"
ROUTE_PREFIX = f"/{PLUGIN_NAME}"
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
ModelT = TypeVar("ModelT", bound=BaseModel)


class PairingHttpApi:
    def __init__(
        self,
        *,
        context: Any,
        manager: PairingManager,
        logger: Any,
        trusted_client_id: str,
        trusted_platform_id: str,
        max_json_body_bytes: int = 16_384,
    ) -> None:
        self.context = context
        self.manager = manager
        self.logger = logger
        self.trusted_client_id = str(trusted_client_id or "").strip()
        self.trusted_platform_id = str(trusted_platform_id or "").strip()
        self.max_json_body_bytes = max(4096, min(65_536, max_json_body_bytes))

    def register(self) -> None:
        routes = (
            (
                "pairing/overview",
                self.overview,
                ["GET"],
                "Read Quest one-time pairing capabilities",
            ),
            (
                "pairing/create",
                self.create,
                ["POST"],
                "Create a one-time Quest pairing session",
            ),
            (
                "pairing/status",
                self.status,
                ["POST"],
                "Read a Quest pairing session status",
            ),
            (
                "pairing/revoke",
                self.revoke,
                ["POST"],
                "Revoke a Quest pairing session",
            ),
            (
                "pairing/exchange",
                self.exchange,
                ["POST"],
                "Exchange a one-time Quest pairing credential",
            ),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"{ROUTE_PREFIX}/{suffix}",
                handler,
                methods,
                description,
            )

    async def overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "pairing_protocol_version": PAIRING_PROTOCOL_VERSION,
                    "public_api_path": PUBLIC_API_PATH,
                    "bridge_key_configured": len(self.manager.bridge_api_key) >= 32,
                    "trusted_client_id": self.trusted_client_id,
                    "trusted_platform_id": self.trusted_platform_id,
                    "requires_https": True,
                    "ttl_options": [60, 120, 180, 300],
                },
            )
        except Exception as exc:
            return self._error(exc, "overview")

    async def create(self) -> Any:
        owner = ""
        pairing_id = ""
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PairingCreateRequest)
            result = self.manager.create(owner, payload)
            pairing_id = result.pairing_id
            qr_svg_data_uri = _qr_svg_data_uri(result.qr_payload)
            return _json_no_store(
                {
                    "success": True,
                    "pairing": {
                        "pairing_id": result.pairing_id,
                        "short_code": result.short_code,
                        "created_at": result.created_at,
                        "expires_at": result.expires_at,
                        "exchange_url": result.exchange_url,
                        "qr_svg_data_uri": qr_svg_data_uri,
                        "state": "waiting",
                    },
                },
                status_code=201,
            )
        except Exception as exc:
            if owner and pairing_id:
                try:
                    self.manager.revoke(owner, pairing_id)
                except PairingError:
                    pass
            return self._error(exc, "create")

    async def status(self) -> Any:
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PairingStatusRequest)
            return _json_no_store(
                {
                    "success": True,
                    "pairing": self.manager.status(owner, payload.pairing_id),
                },
            )
        except Exception as exc:
            return self._error(exc, "status")

    async def revoke(self) -> Any:
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PairingRevokeRequest)
            return _json_no_store(
                {
                    "success": True,
                    "pairing": self.manager.revoke(owner, payload.pairing_id),
                },
            )
        except Exception as exc:
            return self._error(exc, "revoke")

    async def exchange(self) -> Any:
        try:
            payload = await self._read_model(PairingExchangeRequest)
            result = self.manager.exchange(payload, remote=self._remote_address())
            return _json_no_store(
                {
                    "status": "ok",
                    "data": {
                        "pairing_protocol_version": PAIRING_PROTOCOL_VERSION,
                        "pairing_id": result.pairing_id,
                        "configuration": result.configuration,
                    },
                },
            )
        except Exception as exc:
            return self._error(exc, "exchange")

    def _dashboard_owner(self) -> str:
        owner = str(request.username or "").strip()
        if not owner:
            raise PairingError(
                "astrbot_auth_required",
                401,
                "AstrBot Dashboard authentication is required",
            )
        return owner

    def _remote_address(self) -> str:
        try:
            return str(request.remote_addr or "unknown")
        except (AttributeError, RuntimeError):
            return "unknown"

    async def _read_model(self, model: type[ModelT]) -> ModelT:
        content_type = str(request.content_type or "").lower()
        if not content_type.startswith("application/json"):
            raise PairingError(
                "unsupported_media_type",
                415,
                "Content-Type must be application/json",
            )
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_json_body_bytes:
                    raise PairingError(
                        "payload_too_large",
                        413,
                        "Request body is too large",
                    )
            except ValueError as exc:
                raise PairingError(
                    "invalid_content_length",
                    400,
                    "Content-Length is invalid",
                ) from exc
        body = await request.body()
        if not body:
            raise PairingError("empty_body", 400, "JSON request body is required")
        if len(body) > self.max_json_body_bytes:
            raise PairingError("payload_too_large", 413, "Request body is too large")
        try:
            return model.model_validate_json(body)
        except ValidationError as exc:
            fields = sorted(
                {
                    ".".join(str(part) for part in item.get("loc", ())) or "body"
                    for item in exc.errors(include_input=False)
                }
            )
            raise PairingError(
                "schema_validation_failed",
                422,
                "Request schema validation failed: " + ", ".join(fields),
            ) from exc

    def _error(self, exc: Exception, operation: str) -> Any:
        headers = dict(NO_STORE_HEADERS)
        if isinstance(exc, PairingError):
            data: dict[str, object] = {"code": exc.code}
            if exc.retry_after is not None:
                data["retry_after"] = exc.retry_after
                headers["Retry-After"] = str(exc.retry_after)
            response = error_response(
                exc.public_message,
                status_code=exc.status_code,
                data=data,
            )
            return _with_headers(response, headers)
        self.logger.error(
            "[quest-avatar] pairing operation failed: operation=%s error_type=%s",
            operation,
            type(exc).__name__,
            exc_info=True,
        )
        response = error_response(
            "Internal pairing error",
            status_code=500,
            data={"code": "internal_pairing_error"},
        )
        return _with_headers(response, headers)


def _json_no_store(data: dict[str, object], *, status_code: int = 200) -> Any:
    response = json_response(data, status_code=status_code)
    return _with_headers(response, NO_STORE_HEADERS)


def _with_headers(response: Any, headers: dict[str, str]) -> Any:
    try:
        for name, value in headers.items():
            response.headers[name] = value
    except (AttributeError, TypeError):
        # The Page still functions on an older compatible response wrapper;
        # AstrBot 4.26.8 responses expose a mutable headers mapping.
        pass
    return response


def _qr_svg_data_uri(payload: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    buffer = BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return "data:image/svg+xml;base64," + encoded
