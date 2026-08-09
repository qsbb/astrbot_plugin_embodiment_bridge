from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .identity_control_plane import validate_principal_digest


PLUGIN_NAME = "astrbot_plugin_quest_avatar_bridge"
PROOF_PATH = (
    f"/api/v1/plugins/extensions/{PLUGIN_NAME}/pairing/api-principal-proof"
)
MAX_PROOF_RESPONSE_BYTES = 4_096


class ApiPrincipalVerificationError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


class AstrBotApiPrincipalVerifier:
    """Resolve a raw key through AstrBot's own auth layer over loopback HTTP."""

    def __init__(self, upstream_base_url: object, *, timeout_seconds: float = 3.0) -> None:
        self.proof_url = _proof_url(upstream_base_url)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 10.0))

    async def resolve_digest(self, api_key: object) -> str:
        credential = str(api_key or "").strip()
        if len(credential) < 16 or len(credential) > 4_096:
            raise ApiPrincipalVerificationError(
                "pairing_astrbot_api_key_missing",
                422,
                "A valid Quest AstrBot API Key is required",
            )
        if not self.proof_url:
            raise ApiPrincipalVerificationError(
                "api_principal_verification_unavailable",
                503,
                "AstrBot API Key verification is unavailable",
            )

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(1.0, self.timeout_seconds),
        )
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(
                    self.proof_url,
                    headers={
                        "Authorization": f"ApiKey {credential}",
                        "Accept": "application/json",
                    },
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise ApiPrincipalVerificationError(
                            "astrbot_api_key_auth_failed",
                            401,
                            "AstrBot rejected the Quest API Key",
                        )
                    if response.status != 200:
                        raise ApiPrincipalVerificationError(
                            "api_principal_verification_failed",
                            503,
                            "AstrBot API Key verification failed",
                        )
                    body = await response.content.read(MAX_PROOF_RESPONSE_BYTES + 1)
        except ApiPrincipalVerificationError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ApiPrincipalVerificationError(
                "api_principal_verification_unavailable",
                503,
                "AstrBot API Key verification is unavailable",
            ) from exc

        if len(body) > MAX_PROOF_RESPONSE_BYTES:
            raise ApiPrincipalVerificationError(
                "api_principal_proof_invalid",
                503,
                "AstrBot returned an invalid API Key proof",
            )
        try:
            payload: Any = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiPrincipalVerificationError(
                "api_principal_proof_invalid",
                503,
                "AstrBot returned an invalid API Key proof",
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "success",
            "api_principal_digest",
        }:
            raise ApiPrincipalVerificationError(
                "api_principal_proof_invalid",
                503,
                "AstrBot returned an invalid API Key proof",
            )
        if payload.get("success") is not True:
            raise ApiPrincipalVerificationError(
                "api_principal_proof_invalid",
                503,
                "AstrBot returned an invalid API Key proof",
            )
        try:
            return validate_principal_digest(payload.get("api_principal_digest"))
        except ValueError as exc:
            raise ApiPrincipalVerificationError(
                "api_principal_proof_invalid",
                503,
                "AstrBot returned an invalid API Key proof",
            ) from exc


def _proof_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() != "http"
        or not host
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if not address.is_loopback:
        return ""
    normalized_host = f"[{address}]" if address.version == 6 else str(address)
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit(("http", netloc, PROOF_PATH, "", ""))
