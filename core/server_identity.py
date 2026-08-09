from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True, repr=False)
class ServerIdentity:
    bot_id: str
    user_id: str


class ServerIdentityStore:
    """Keep raw EventBus identity outside AstrBot's browser-exposed config."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._identity = self._load()
        self._dirty = False
        self._lock = asyncio.Lock()

    @property
    def identity(self) -> ServerIdentity | None:
        return self._identity

    @property
    def configured(self) -> bool:
        return self._identity is not None

    def import_legacy(self, *, bot_id: object, user_id: object) -> bool:
        if self._identity is not None:
            return False
        bot = self._value(bot_id)
        user = self._value(user_id)
        if not bot or not user:
            return False
        self._identity = ServerIdentity(bot, user)
        self._dirty = True
        return True

    async def flush(self) -> None:
        async with self._lock:
            if self._dirty:
                await asyncio.to_thread(self._write, self._identity)
                self._dirty = False

    async def save(self, *, bot_id: str, user_id: str) -> None:
        identity = ServerIdentity(self._value(bot_id), self._value(user_id))
        if not identity.bot_id or not identity.user_id:
            raise ValueError("server identity is incomplete")
        async with self._lock:
            await asyncio.to_thread(self._write, identity)
            self._identity = identity
            self._dirty = False

    async def clear(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, None)
            self._identity = None
            self._dirty = False

    def _load(self) -> ServerIdentity | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "bot_id",
            "user_id",
        }:
            return None
        if payload.get("version") != 1:
            return None
        bot = self._value(payload.get("bot_id"))
        user = self._value(payload.get("user_id"))
        return ServerIdentity(bot, user) if bot and user else None

    def _write(self, identity: ServerIdentity | None) -> None:
        if identity is None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        payload: dict[str, Any] = {
            "version": 1,
            "bot_id": identity.bot_id,
            "user_id": identity.user_id,
        }
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _value(value: object) -> str:
        normalized = str(value or "").strip()
        if (
            not normalized
            or len(normalized) > 128
            or "|" in normalized
            or any(char.isspace() or ord(char) < 33 for char in normalized)
        ):
            return ""
        return normalized
