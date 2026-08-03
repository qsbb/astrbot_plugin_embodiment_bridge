from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator

from .models import Identifier, OptionalScope, StrictModel


PAIRING_PROTOCOL_VERSION = "1.0"
PAIRING_PAYLOAD_TYPE = "astrbot.quest.pair"
PLUGIN_NAME = "astrbot_plugin_quest_avatar_bridge"
PUBLIC_API_PATH = f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300
DEFAULT_TTL_SECONDS = 120


class PairingCreateRequest(StrictModel):
    protocol_version: Literal["1.0"] = PAIRING_PROTOCOL_VERSION
    public_url: str = Field(min_length=1, max_length=2048)
    port: int | None = Field(default=None, ge=1, le=65535)
    astrbot_api_key: SecretStr
    client_id: Identifier
    user_id: OptionalScope
    bot_id: OptionalScope
    group_id: OptionalScope = ""
    relationship_profile_id: OptionalScope = ""
    ttl_seconds: int = Field(
        default=DEFAULT_TTL_SECONDS,
        ge=MIN_TTL_SECONDS,
        le=MAX_TTL_SECONDS,
    )

    @model_validator(mode="after")
    def require_identity_and_key(self) -> PairingCreateRequest:
        if not self.user_id or not self.bot_id:
            raise ValueError("user_id and bot_id are required")
        key = self.astrbot_api_key.get_secret_value()
        if not key or len(key) > 4096:
            raise ValueError("astrbot_api_key is required and must be at most 4096 chars")
        return self


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
            "allow_insecure_http": False,
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
        clock: Callable[[], float] = time.time,
        max_active_sessions: int = 32,
        max_owner_sessions: int = 5,
        exchange_attempts_per_minute: int = 12,
    ) -> None:
        self.bridge_api_key = str(bridge_api_key or "")
        self.clock = clock
        self.max_active_sessions = max(1, max_active_sessions)
        self.max_owner_sessions = max(1, max_owner_sessions)
        self.exchange_attempts_per_minute = max(1, exchange_attempts_per_minute)
        self._sessions: dict[str, PairingSession] = {}
        self._token_index: dict[str, str] = {}
        self._code_index: dict[str, str] = {}
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

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

        base_url = normalize_public_base_url(payload.public_url, payload.port)
        now = self.clock()
        token = secrets.token_urlsafe(32)
        token_hash = _credential_hash(token)
        pairing_id = secrets.token_hex(16)
        expires_at = now + payload.ttl_seconds
        exchange_url = f"{base_url}/pairing/exchange"
        configuration = PairingConfiguration(
            base_url=base_url,
            astrbot_api_key=payload.astrbot_api_key.get_secret_value(),
            bridge_api_key=self.bridge_api_key,
            client_id=payload.client_id,
            user_id=payload.user_id,
            bot_id=payload.bot_id,
            group_id=payload.group_id,
            relationship_profile_id=payload.relationship_profile_id,
        )

        with self._lock:
            self._expire_locked(now)
            active = [item for item in self._sessions.values() if item.state == "waiting"]
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
        remote_key = str(remote or "unknown")[:256]
        with self._lock:
            self._rate_limit_locked(remote_key, now)
            self._expire_locked(now)
            if payload.token:
                pairing_id = self._token_index.get(_credential_hash(payload.token))
            else:
                pairing_id = self._code_index.get(payload.code)
            session = self._sessions.get(pairing_id or "")
            if session is None or session.state != "waiting" or session.configuration is None:
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

    def _owned_session_locked(self, owner: str, pairing_id: str) -> PairingSession:
        if not owner:
            raise PairingError(
                "astrbot_auth_required",
                401,
                "AstrBot Dashboard authentication is required",
            )
        session = self._sessions.get(pairing_id)
        if session is None or not secrets.compare_digest(session.owner, owner):
            raise PairingError("pairing_not_found", 404, "Pairing session was not found")
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
        attempts = self._attempts.setdefault(remote, deque())
        cutoff = now - 60.0
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


def normalize_public_base_url(value: str, port: int | None = None) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as exc:
        raise PairingError("invalid_public_url", 422, "Public URL is invalid") from exc

    if parsed.scheme.lower() != "https":
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
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    selected_port = port if port is not None else parsed_port
    netloc = host if selected_port is None else f"{host}:{selected_port}"
    return urlunsplit(("https", netloc, PUBLIC_API_PATH, "", ""))


def _credential_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()
