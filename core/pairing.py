from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from .models import OptionalScope, StrictModel
from .plugin_identity import PLUGIN_ID, PUBLIC_API_PREFIX


PAIRING_PROTOCOL_VERSION = "1.0"
PAIRING_PAYLOAD_TYPE = "astrbot.quest.pair"
PLUGIN_NAME = PLUGIN_ID
PUBLIC_API_PATH = PUBLIC_API_PREFIX
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 120
PRIVATE_HTTP_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class PairingCreateRequest(StrictModel):
    protocol_version: Literal["1.0"] = PAIRING_PROTOCOL_VERSION
    public_url: str = Field(default="", max_length=2048)
    port: int | None = Field(default=None, ge=1, le=65535)
    astrbot_api_key: SecretStr = SecretStr("")
    client_id: OptionalScope = ""
    user_id: OptionalScope = ""
    bot_id: OptionalScope = ""
    group_id: OptionalScope = ""
    relationship_profile_id: OptionalScope = ""
    expected_remote_ip: str = Field(default="", max_length=64)
    allow_insecure_http: bool = False
    ttl_seconds: int = Field(
        default=DEFAULT_TTL_SECONDS,
        ge=MIN_TTL_SECONDS,
        le=MAX_TTL_SECONDS,
    )

    @field_validator("expected_remote_ip")
    @classmethod
    def validate_expected_remote_ip(cls, value: str) -> str:
        if not value:
            return ""
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("expected_remote_ip must be an IP literal") from exc
        if parsed.is_unspecified or parsed.is_multicast:
            raise ValueError("expected_remote_ip must be a unicast address")
        return str(parsed)


class PairingStatusRequest(StrictModel):
    pairing_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class PairingRevokeRequest(PairingStatusRequest):
    pass


class PairingExchangeRequest(StrictModel):
    protocol_version: Literal["1.0"] = PAIRING_PROTOCOL_VERSION
    token: str = Field(default="", max_length=128)
    code: str = Field(default="", pattern=r"^(?:|[0-9]{6})$")

    @model_validator(mode="after")
    def require_one_credential(self) -> PairingExchangeRequest:
        has_token = bool(self.token)
        has_code = bool(self.code)
        if has_token == has_code:
            raise ValueError("exactly one of token or code is required")
        if has_token and len(self.token) < 32:
            raise ValueError("token is invalid")
        return self


class PairingError(RuntimeError):
    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        *,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message
        self.retry_after = retry_after


@dataclass(slots=True)
class PairingConfiguration:
    base_url: str
    astrbot_api_key: str = field(repr=False)
    bridge_api_key: str = field(repr=False)
    client_id: str
    user_id: str
    bot_id: str
    group_id: str = ""
    relationship_profile_id: str = ""
    allow_insecure_http: bool = False

    def exchange_payload(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "astrbot_api_key": self.astrbot_api_key,
            "bridge_api_key": self.bridge_api_key,
            "client_id": self.client_id,
            "user_id": self.user_id,
            "bot_id": self.bot_id,
            "group_id": self.group_id,
            "relationship_profile_id": self.relationship_profile_id,
            "allow_insecure_http": self.allow_insecure_http,
        }

    def wipe(self) -> None:
        self.astrbot_api_key = ""
        self.bridge_api_key = ""


@dataclass(slots=True)
class PairingSession:
    pairing_id: str
    owner: str
    token_hash: str
    short_code: str
    created_at: float
    expires_at: float
    exchange_url: str
    expected_remote_ip: str
    configuration: PairingConfiguration | None = field(repr=False)
    state: str = "waiting"
    consumed_at: float | None = None

    def status_payload(self, now: float) -> dict[str, object]:
        return {
            "pairing_id": self.pairing_id,
            "state": self.state,
            "expires_at": self.expires_at,
            "remaining_seconds": max(0, int(self.expires_at - now)),
            "consumed_at": self.consumed_at,
        }


@dataclass(frozen=True, slots=True)
class PairingCreateResult:
    pairing_id: str
    short_code: str
    created_at: float
    expires_at: float
    exchange_url: str
    qr_payload: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PairingExchangeResult:
    pairing_id: str
    configuration: dict[str, object] = field(repr=False)


class PairingExchangeService:
    """Transport-neutral one-time credential exchange service.

    The authenticated plugin route and built-in bootstrap listener use this
    same service, so all pairing state remains owned by one manager.
    """

    def __init__(self, manager: "PairingManager") -> None:
        self.manager = manager

    def exchange(
        self,
        payload: PairingExchangeRequest,
        *,
        remote: str,
    ) -> dict[str, object]:
        result = self.manager.exchange(payload, remote=remote)
        return {
            "status": "ok",
            "data": {
                "pairing_protocol_version": PAIRING_PROTOCOL_VERSION,
                "pairing_id": result.pairing_id,
                "configuration": result.configuration,
            },
        }


class PairingManager:
    """In-memory, single-use Quest pairing sessions.

    Long-lived credentials exist only inside an active session. They are wiped
    when the session is consumed, revoked, or expires. Raw bearer tokens are
    never retained; only their SHA-256 digests are indexed.
    """

    def __init__(
        self,
        *,
        bridge_api_key: str,
        exchange_url: str = "",
        allow_private_http: bool = False,
        allow_remote_http: bool = False,
        clock: Callable[[], float] = time.time,
        max_active_sessions: int = 32,
        max_owner_sessions: int = 5,
        exchange_attempts_per_minute: int = 12,
        global_exchange_attempts_per_minute: int = 120,
    ) -> None:
        self.bridge_api_key = str(bridge_api_key or "")
        self.allow_private_http = bool(allow_private_http)
        self.allow_remote_http = bool(allow_remote_http)
        self.exchange_url = ""
        self.bootstrap_reason = "pairing_exchange_proxy_url_missing"
        if str(exchange_url or "").strip():
            try:
                self.exchange_url = normalize_pairing_exchange_url(
                    exchange_url,
                    allow_private_http=self.allow_private_http,
                    allow_remote_http=self.allow_remote_http,
                )
                self.bootstrap_reason = "ready"
            except PairingError as exc:
                self.bootstrap_reason = exc.code
        self.clock = clock
        self.max_active_sessions = max(1, max_active_sessions)
        self.max_owner_sessions = max(1, max_owner_sessions)
        self.exchange_attempts_per_minute = max(1, exchange_attempts_per_minute)
        self.global_exchange_attempts_per_minute = max(
            1, global_exchange_attempts_per_minute
        )
        self._sessions: dict[str, PairingSession] = {}
        self._token_index: dict[str, str] = {}
        self._code_index: dict[str, str] = {}
        self._attempts: dict[str, deque[float]] = {}
        self._global_attempts: deque[float] = deque()
        self._lock = threading.Lock()

    @property
    def bootstrap_ready(self) -> bool:
        return bool(self.exchange_url)

    def configure_exchange_url(self, value: str, *, missing_reason: str) -> None:
        """Atomically replace the URL embedded in newly-created credentials."""

        normalized = ""
        reason = str(missing_reason or "pairing_exchange_url_missing")[:128]
        if str(value or "").strip():
            try:
                normalized = normalize_pairing_exchange_url(
                    value,
                    allow_private_http=self.allow_private_http,
                )
                reason = "ready"
            except PairingError as exc:
                reason = exc.code
        with self._lock:
            self.exchange_url = normalized
            self.bootstrap_reason = reason

    def create(self, owner: str, payload: PairingCreateRequest) -> PairingCreateResult:
        principal = str(owner or "").strip()
        if not principal:
            raise PairingError(
                "astrbot_auth_required",
                401,
                "AstrBot Dashboard authentication is required",
            )
        if len(self.bridge_api_key) < 32:
            raise PairingError(
                "bridge_not_configured",
                503,
                "Quest bridge API key is not configured",
            )

        if not self.bootstrap_ready:
            raise PairingError(
                "pairing_bootstrap_unavailable",
                503,
                "A public pairing exchange proxy is not configured",
            )
        key = payload.astrbot_api_key.get_secret_value()
        if (
            not payload.public_url
            or not key
            or len(key) > 4096
            or not payload.client_id
            or not payload.user_id
            or not payload.bot_id
        ):
            raise PairingError(
                "pairing_server_configuration_incomplete",
                503,
                "Quest quick-pairing server settings are incomplete",
            )

        base_url = normalize_public_base_url(
            payload.public_url,
            payload.port,
            allow_private_http=(
                self.allow_private_http and payload.allow_insecure_http
            ),
            allow_remote_http=self.allow_remote_http,
        )
        now = self.clock()
        token = secrets.token_urlsafe(32)
        token_hash = _credential_hash(token)
        pairing_id = secrets.token_hex(16)
        expires_at = now + payload.ttl_seconds
        exchange_url = self.exchange_url
        configuration = PairingConfiguration(
            base_url=base_url,
            astrbot_api_key=payload.astrbot_api_key.get_secret_value(),
            bridge_api_key=self.bridge_api_key,
            client_id=payload.client_id,
            user_id=payload.user_id,
            bot_id=payload.bot_id,
            group_id=payload.group_id,
            relationship_profile_id=payload.relationship_profile_id,
            allow_insecure_http=urlsplit(base_url).scheme == "http",
        )

        with self._lock:
            self._expire_locked(now)
            active = [
                item for item in self._sessions.values() if item.state == "waiting"
            ]
            if len(active) >= self.max_active_sessions:
                configuration.wipe()
                raise PairingError(
                    "pairing_capacity_reached",
                    429,
                    "Too many active pairing sessions",
                )
            owner_active = sum(item.owner == principal for item in active)
            if owner_active >= self.max_owner_sessions:
                configuration.wipe()
                raise PairingError(
                    "pairing_owner_capacity_reached",
                    429,
                    "Too many active pairing sessions for this Dashboard user",
                )
            short_code = self._new_short_code_locked()
            session = PairingSession(
                pairing_id=pairing_id,
                owner=principal,
                token_hash=token_hash,
                short_code=short_code,
                created_at=now,
                expires_at=expires_at,
                exchange_url=exchange_url,
                expected_remote_ip=payload.expected_remote_ip,
                configuration=configuration,
            )
            self._sessions[pairing_id] = session
            self._token_index[token_hash] = pairing_id
            self._code_index[short_code] = pairing_id

        qr_payload = json.dumps(
            {
                "type": PAIRING_PAYLOAD_TYPE,
                "version": PAIRING_PROTOCOL_VERSION,
                "exchange_url": exchange_url,
                "token": token,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return PairingCreateResult(
            pairing_id=pairing_id,
            short_code=short_code,
            created_at=now,
            expires_at=expires_at,
            exchange_url=exchange_url,
            qr_payload=qr_payload,
        )

    def status(self, owner: str, pairing_id: str) -> dict[str, object]:
        principal = str(owner or "").strip()
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            session = self._owned_session_locked(principal, pairing_id)
            return session.status_payload(now)

    def revoke(self, owner: str, pairing_id: str) -> dict[str, object]:
        principal = str(owner or "").strip()
        now = self.clock()
        with self._lock:
            self._expire_locked(now)
            session = self._owned_session_locked(principal, pairing_id)
            if session.state == "waiting":
                self._deactivate_locked(session, "revoked", now)
            return session.status_payload(now)

    def exchange(
        self,
        payload: PairingExchangeRequest,
        *,
        remote: str,
    ) -> PairingExchangeResult:
        now = self.clock()
        remote_key = _canonical_remote_ip(remote)
        with self._lock:
            self._rate_limit_locked(remote_key, now)
            self._expire_locked(now)
            if payload.token:
                pairing_id = self._token_index.get(_credential_hash(payload.token))
            else:
                pairing_id = self._code_index.get(payload.code)
            session = self._sessions.get(pairing_id or "")
            if (
                session is None
                or session.state != "waiting"
                or session.configuration is None
            ):
                raise PairingError(
                    "pairing_not_available",
                    401,
                    "Pairing credential is invalid, expired, or already used",
                )
            if session.expected_remote_ip and not secrets.compare_digest(
                session.expected_remote_ip, remote_key
            ):
                raise PairingError(
                    "pairing_not_available",
                    401,
                    "Pairing credential is invalid, expired, or already used",
                )

            configuration = session.configuration.exchange_payload()
            self._deactivate_locked(session, "consumed", now)
            return PairingExchangeResult(
                pairing_id=session.pairing_id,
                configuration=configuration,
            )

    def close(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                if session.configuration is not None:
                    session.configuration.wipe()
                    session.configuration = None
            self._sessions.clear()
            self._token_index.clear()
            self._code_index.clear()
            self._attempts.clear()
            self._global_attempts.clear()

    def _owned_session_locked(self, owner: str, pairing_id: str) -> PairingSession:
        if not owner:
            raise PairingError(
                "astrbot_auth_required",
                401,
                "AstrBot Dashboard authentication is required",
            )
        session = self._sessions.get(pairing_id)
        if session is None or not secrets.compare_digest(session.owner, owner):
            raise PairingError(
                "pairing_not_found", 404, "Pairing session was not found"
            )
        return session

    def _new_short_code_locked(self) -> str:
        for _ in range(128):
            code = f"{secrets.randbelow(1_000_000):06d}"
            if code not in self._code_index:
                return code
        raise PairingError(
            "pairing_code_exhausted",
            503,
            "Could not allocate a pairing code",
        )

    def _rate_limit_locked(self, remote: str, now: float) -> None:
        cutoff = now - 60.0
        while self._global_attempts and self._global_attempts[0] <= cutoff:
            self._global_attempts.popleft()
        if len(self._global_attempts) >= self.global_exchange_attempts_per_minute:
            retry_after = max(1, int(60.0 - (now - self._global_attempts[0])))
            raise PairingError(
                "pairing_rate_limited",
                429,
                "Too many pairing attempts",
                retry_after=retry_after,
            )
        self._global_attempts.append(now)

        attempts = self._attempts.setdefault(remote, deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= self.exchange_attempts_per_minute:
            retry_after = max(1, int(60.0 - (now - attempts[0])))
            raise PairingError(
                "pairing_rate_limited",
                429,
                "Too many pairing attempts",
                retry_after=retry_after,
            )
        attempts.append(now)

    def _expire_locked(self, now: float) -> None:
        for session in self._sessions.values():
            if session.state == "waiting" and session.expires_at <= now:
                self._deactivate_locked(session, "expired", now)

        retention_cutoff = now - 300.0
        stale_ids = [
            pairing_id
            for pairing_id, session in self._sessions.items()
            if session.state != "waiting"
            and (session.consumed_at or session.expires_at) <= retention_cutoff
        ]
        for pairing_id in stale_ids:
            self._sessions.pop(pairing_id, None)

        attempt_cutoff = now - 60.0
        while self._global_attempts and self._global_attempts[0] <= attempt_cutoff:
            self._global_attempts.popleft()
        for remote, attempts in list(self._attempts.items()):
            while attempts and attempts[0] <= attempt_cutoff:
                attempts.popleft()
            if not attempts:
                self._attempts.pop(remote, None)

    def _deactivate_locked(
        self,
        session: PairingSession,
        state: str,
        now: float,
    ) -> None:
        self._token_index.pop(session.token_hash, None)
        self._code_index.pop(session.short_code, None)
        session.state = state
        session.consumed_at = now
        if session.configuration is not None:
            session.configuration.wipe()
            session.configuration = None


def normalize_public_base_url(
    value: str,
    port: int | None = None,
    *,
    allow_private_http: bool = False,
    allow_remote_http: bool = False,
) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as exc:
        raise PairingError("invalid_public_url", 422, "Public URL is invalid") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise PairingError(
            "https_required",
            422,
            "Quest pairing and Bridge connections require HTTPS",
        )
    if not parsed.hostname or parsed.username or parsed.password:
        raise PairingError("invalid_public_url", 422, "Public URL is invalid")
    if parsed.query or parsed.fragment:
        raise PairingError(
            "invalid_public_url",
            422,
            "Public URL must not contain a query or fragment",
        )

    path = parsed.path.rstrip("/")
    if path and path != PUBLIC_API_PATH:
        raise PairingError(
            "invalid_public_path",
            422,
            f"Public URL path must be empty or {PUBLIC_API_PATH}",
        )

    host = parsed.hostname
    if scheme == "http" and not (
        allow_remote_http or (allow_private_http and _is_private_lan_ip(host))
    ):
        raise PairingError(
            "https_required",
            422,
            "Plain HTTP requires an explicit private-IP or remote-HTTP opt-in",
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    selected_port = port if port is not None else parsed_port
    netloc = host if selected_port is None else f"{host}:{selected_port}"
    return urlunsplit((scheme, netloc, PUBLIC_API_PATH, "", ""))


def normalize_pairing_exchange_url(
    value: str,
    *,
    allow_private_http: bool = False,
    allow_remote_http: bool = False,
) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise PairingError(
            "invalid_pairing_exchange_url",
            422,
            "Pairing exchange proxy URL is invalid",
        )
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise PairingError(
            "invalid_pairing_exchange_url",
            422,
            "Pairing exchange proxy URL is invalid",
        ) from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path
        or parsed.path.endswith("/")
    ):
        raise PairingError(
            "invalid_pairing_exchange_url",
            422,
            "Pairing exchange proxy URL is invalid",
        )
    if scheme == "http" and not (
        allow_remote_http or (allow_private_http and _is_private_lan_ip(parsed.hostname))
    ):
        raise PairingError(
            "https_required",
            422,
            "Pairing exchange proxy must use HTTPS unless plain HTTP is enabled",
        )
    return urlunsplit((scheme, parsed.netloc, parsed.path, "", ""))


def _is_private_lan_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(
        address.version == network.version and address in network
        for network in PRIVATE_HTTP_NETWORKS
    )


def _canonical_remote_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return "invalid"
    if address.is_unspecified or address.is_multicast:
        return "invalid"
    return str(address)


def _credential_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
