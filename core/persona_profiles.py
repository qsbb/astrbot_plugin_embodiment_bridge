from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


PROFILE_SCHEMA_VERSION = "banxia.quest_persona/1.0"
PROFILE_ID_PATTERN = re.compile(r"^qp_[0-9a-f]{32}$")

PersonaSourceKind = Literal["astrbot", "manual"]
PersonaProfileStatus = Literal["draft", "ready"]

_SOURCE_KINDS = frozenset({"astrbot", "manual"})
_PROFILE_STATUSES = frozenset({"draft", "ready"})
_REPORT_KEYS = (
    "preserved",
    "adapted",
    "removed",
    "unresolved_questions",
)
_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "profile_id",
        "display_name",
        "aliases",
        "source_kind",
        "source_persona_id",
        "source_snapshot",
        "source_hash",
        "quest_persona_prompt",
        "conversion_report",
        "converter_provider_id",
        "converter_prompt_version",
        "status",
        "created_at",
        "updated_at",
    }
)

_MAX_FILE_BYTES = 256 * 1024
_MAX_SOURCE_CHARS = 24_000
_MIN_READY_PROMPT_CHARS = 200
_MAX_PROMPT_CHARS = 12_000
_MAX_ALIASES = 20
_MAX_REPORT_ITEMS = 32


class PersonaProfileError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PersonaConversion:
    display_name: str
    aliases: tuple[str, ...]
    quest_persona_prompt: str
    conversion_report: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class PersonaProfile:
    profile_id: str
    display_name: str
    aliases: tuple[str, ...]
    source_kind: PersonaSourceKind
    source_persona_id: str
    source_snapshot: str
    source_hash: str
    quest_persona_prompt: str
    conversion_report: dict[str, tuple[str, ...]]
    converter_provider_id: str
    converter_prompt_version: str
    status: PersonaProfileStatus
    created_at: str
    updated_at: str

    def summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "source_kind": self.source_kind,
            "source_persona_id": self.source_persona_id,
            "source_hash": self.source_hash,
            "converter_provider_id": self.converter_provider_id,
            "converter_prompt_version": self.converter_prompt_version,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            **self.summary(),
            "source_snapshot": self.source_snapshot,
            "quest_persona_prompt": self.quest_persona_prompt,
            "conversion_report": {
                key: list(self.conversion_report[key]) for key in _REPORT_KEYS
            },
        }


class PersonaProfileStore:
    """Store one strictly validated Quest persona per server-generated JSON file."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir).resolve()
        self.directory = self.root / "personas"
        if _path_is_linklike(self.directory):
            raise PersonaProfileError("persona_directory_invalid")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._assert_directory_safe()
        self._lock = asyncio.Lock()

    async def list_profiles(self) -> list[PersonaProfile]:
        async with self._lock:
            return await asyncio.to_thread(self._list_sync)

    async def get(self, profile_id: object) -> PersonaProfile:
        normalized = validate_profile_id(profile_id)
        async with self._lock:
            return await asyncio.to_thread(self._load_path, self._path(normalized))

    async def create_draft(
        self,
        *,
        display_name: object,
        source_kind: object,
        source_snapshot: object,
        source_persona_id: object = "",
    ) -> PersonaProfile:
        normalized_source = normalize_source_snapshot(source_snapshot)
        normalized_kind = normalize_source_kind(source_kind)
        normalized_source_id = normalize_source_persona_id(source_persona_id)
        if normalized_kind == "astrbot" and not normalized_source_id:
            raise PersonaProfileError("source_persona_id_required")
        if normalized_kind == "manual" and normalized_source_id:
            raise PersonaProfileError("source_persona_id_not_allowed")

        now = _utc_now()
        async with self._lock:
            profile_id = self._new_id()
            profile = PersonaProfile(
                profile_id=profile_id,
                display_name=normalize_display_name(display_name),
                aliases=(),
                source_kind=normalized_kind,
                source_persona_id=normalized_source_id,
                source_snapshot=normalized_source,
                source_hash=source_hash(normalized_source),
                quest_persona_prompt="",
                conversion_report=_empty_report(),
                converter_provider_id="",
                converter_prompt_version="",
                status="draft",
                created_at=now,
                updated_at=now,
            )
            await asyncio.to_thread(self._write, profile)
            return profile

    async def create_conversion(
        self,
        *,
        source_kind: object,
        source_snapshot: object,
        source_persona_id: object,
        conversion: PersonaConversion,
        converter_provider_id: object,
        converter_prompt_version: object,
    ) -> PersonaProfile:
        """Create one reviewed converted profile with a single atomic replace."""
        normalized_source = normalize_source_snapshot(source_snapshot)
        normalized_kind = normalize_source_kind(source_kind)
        normalized_source_id = normalize_source_persona_id(source_persona_id)
        if normalized_kind == "astrbot" and not normalized_source_id:
            raise PersonaProfileError("source_persona_id_required")
        if normalized_kind == "manual" and normalized_source_id:
            raise PersonaProfileError("source_persona_id_not_allowed")
        normalized_conversion = validate_conversion(conversion, converted=True)
        provider_id = normalize_provider_id(converter_provider_id)
        prompt_version = normalize_prompt_version(converter_prompt_version)
        now = _utc_now()
        async with self._lock:
            profile = PersonaProfile(
                profile_id=self._new_id(),
                display_name=normalized_conversion.display_name,
                aliases=normalized_conversion.aliases,
                source_kind=normalized_kind,
                source_persona_id=normalized_source_id,
                source_snapshot=normalized_source,
                source_hash=source_hash(normalized_source),
                quest_persona_prompt=normalized_conversion.quest_persona_prompt,
                conversion_report=normalized_conversion.conversion_report,
                converter_provider_id=provider_id,
                converter_prompt_version=prompt_version,
                status="ready",
                created_at=now,
                updated_at=now,
            )
            await asyncio.to_thread(self._write, profile)
            return profile

    async def create_manual(
        self,
        *,
        display_name: object,
        aliases: object,
        source_snapshot: object,
        quest_persona_prompt: object,
    ) -> PersonaProfile:
        """Create one administrator-authored ready profile atomically."""
        normalized_source = normalize_source_snapshot(source_snapshot)
        conversion = validate_conversion(
            PersonaConversion(
                display_name=display_name,
                aliases=aliases,
                quest_persona_prompt=quest_persona_prompt,
                conversion_report=_empty_report(),
            ),
            converted=False,
        )
        now = _utc_now()
        async with self._lock:
            profile = PersonaProfile(
                profile_id=self._new_id(),
                display_name=conversion.display_name,
                aliases=conversion.aliases,
                source_kind="manual",
                source_persona_id="",
                source_snapshot=normalized_source,
                source_hash=source_hash(normalized_source),
                quest_persona_prompt=conversion.quest_persona_prompt,
                conversion_report=conversion.conversion_report,
                converter_provider_id="",
                converter_prompt_version="manual",
                status="ready",
                created_at=now,
                updated_at=now,
            )
            await asyncio.to_thread(self._write, profile)
            return profile

    async def save_conversion(
        self,
        profile_id: object,
        *,
        conversion: PersonaConversion,
        converter_provider_id: object,
        converter_prompt_version: object,
        source_snapshot: object | None = None,
        source_persona_id: object | None = None,
    ) -> PersonaProfile:
        normalized_id = validate_profile_id(profile_id)
        provider_id = normalize_provider_id(converter_provider_id)
        prompt_version = normalize_prompt_version(converter_prompt_version)
        normalized_conversion = validate_conversion(conversion, converted=True)
        async with self._lock:
            current = await asyncio.to_thread(
                self._load_path, self._path(normalized_id)
            )
            next_source = (
                current.source_snapshot
                if source_snapshot is None
                else normalize_source_snapshot(source_snapshot)
            )
            next_source_id = (
                current.source_persona_id
                if source_persona_id is None
                else normalize_source_persona_id(source_persona_id)
            )
            if current.source_kind == "astrbot" and not next_source_id:
                raise PersonaProfileError("source_persona_id_required")
            if current.source_kind == "manual" and next_source_id:
                raise PersonaProfileError("source_persona_id_not_allowed")
            updated = PersonaProfile(
                profile_id=current.profile_id,
                display_name=normalized_conversion.display_name,
                aliases=normalized_conversion.aliases,
                source_kind=current.source_kind,
                source_persona_id=next_source_id,
                source_snapshot=next_source,
                source_hash=source_hash(next_source),
                quest_persona_prompt=normalized_conversion.quest_persona_prompt,
                conversion_report=normalized_conversion.conversion_report,
                converter_provider_id=provider_id,
                converter_prompt_version=prompt_version,
                status="ready",
                created_at=current.created_at,
                updated_at=_utc_now(),
            )
            await asyncio.to_thread(self._write, updated)
            return updated

    async def save_manual(
        self,
        profile_id: object,
        *,
        display_name: object,
        aliases: object,
        quest_persona_prompt: object,
        source_snapshot: object | None = None,
    ) -> PersonaProfile:
        """Save an administrator-authored Quest persona without invoking a model."""
        conversion = PersonaConversion(
            display_name=normalize_display_name(display_name),
            aliases=normalize_aliases(aliases),
            quest_persona_prompt=normalize_quest_prompt(
                quest_persona_prompt, minimum=_MIN_READY_PROMPT_CHARS
            ),
            conversion_report=_empty_report(),
        )
        normalized_id = validate_profile_id(profile_id)
        async with self._lock:
            current = await asyncio.to_thread(
                self._load_path, self._path(normalized_id)
            )
            if current.source_kind != "manual":
                raise PersonaProfileError("manual_save_not_allowed")
            next_source = (
                current.source_snapshot
                if source_snapshot is None
                else normalize_source_snapshot(source_snapshot)
            )
            updated = PersonaProfile(
                profile_id=current.profile_id,
                display_name=conversion.display_name,
                aliases=conversion.aliases,
                source_kind=current.source_kind,
                source_persona_id="",
                source_snapshot=next_source,
                source_hash=source_hash(next_source),
                quest_persona_prompt=conversion.quest_persona_prompt,
                conversion_report=conversion.conversion_report,
                converter_provider_id="",
                converter_prompt_version="manual",
                status="ready",
                created_at=current.created_at,
                updated_at=_utc_now(),
            )
            await asyncio.to_thread(self._write, updated)
            return updated

    async def activate(self, profile_id: object) -> PersonaProfile:
        """Return a ready snapshot for the caller to persist as the active config ID."""
        profile = await self.get(profile_id)
        if profile.status != "ready" or not profile.quest_persona_prompt:
            raise PersonaProfileError("profile_not_ready")
        return profile

    async def get_activatable(self, profile_id: object) -> PersonaProfile:
        return await self.activate(profile_id)

    async def delete(self, profile_id: object) -> PersonaProfile:
        normalized = validate_profile_id(profile_id)
        async with self._lock:
            return await asyncio.to_thread(self._delete_sync, normalized)

    def _list_sync(self) -> list[PersonaProfile]:
        self._assert_directory_safe()
        profiles: list[PersonaProfile] = []
        for path in self.directory.glob("qp_*.json"):
            try:
                profile = self._load_path(path)
            except PersonaProfileError:
                continue
            profiles.append(profile)
        profiles.sort(key=lambda item: (item.display_name.casefold(), item.profile_id))
        return profiles

    def _load_path(self, path: Path) -> PersonaProfile:
        if path.is_symlink():
            raise PersonaProfileError("profile_file_invalid")
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise PersonaProfileError("profile_not_found") from exc
        except OSError as exc:
            raise PersonaProfileError("profile_read_failed") from exc
        if len(raw) > _MAX_FILE_BYTES:
            raise PersonaProfileError("profile_file_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise PersonaProfileError("profile_file_invalid") from exc
        return _profile_from_payload(payload, expected_id=path.stem)

    def _write(self, profile: PersonaProfile) -> None:
        payload = profile.to_dict()
        # Validate the complete serialized schema before touching the existing file.
        _profile_from_payload(payload, expected_id=profile.profile_id)
        path = self._path(profile.profile_id)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{profile.profile_id}.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._restrict_permissions(path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _path(self, profile_id: str) -> Path:
        # profile_id is an allowlisted server-generated token, never a path fragment.
        self._assert_directory_safe()
        return self.directory / f"{validate_profile_id(profile_id)}.json"

    def _assert_directory_safe(self) -> None:
        if _path_is_linklike(self.directory):
            raise PersonaProfileError("persona_directory_invalid")
        try:
            resolved = self.directory.resolve(strict=True)
        except OSError as exc:
            raise PersonaProfileError("persona_directory_invalid") from exc
        if resolved.parent != self.root:
            raise PersonaProfileError("persona_directory_invalid")

    def _delete_sync(self, profile_id: str) -> PersonaProfile:
        path = self._path(profile_id)
        profile = self._load_path(path)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise PersonaProfileError("profile_not_found") from exc
        except OSError as exc:
            raise PersonaProfileError("profile_delete_failed") from exc
        return profile

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Windows and some mounted data directories do not expose POSIX modes.
            pass

    def _new_id(self) -> str:
        for _ in range(16):
            profile_id = f"qp_{secrets.token_hex(16)}"
            if not self._path(profile_id).exists():
                return profile_id
        raise PersonaProfileError("profile_id_generation_failed")


def validate_profile_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not PROFILE_ID_PATTERN.fullmatch(normalized):
        raise PersonaProfileError("profile_id_invalid")
    return normalized


def normalize_source_kind(value: object) -> PersonaSourceKind:
    normalized = str(value or "").strip().lower()
    if normalized not in _SOURCE_KINDS:
        raise PersonaProfileError("source_kind_invalid")
    return normalized  # type: ignore[return-value]


def normalize_display_name(value: object) -> str:
    return _bounded_text(value, "display_name", minimum=1, maximum=80, single=True)


def normalize_source_persona_id(value: object) -> str:
    return _bounded_text(
        value, "source_persona_id", minimum=0, maximum=255, single=True
    )


def normalize_source_snapshot(value: object) -> str:
    return _bounded_text(value, "source_snapshot", minimum=1, maximum=_MAX_SOURCE_CHARS)


def normalize_provider_id(value: object) -> str:
    return _bounded_text(
        value, "converter_provider_id", minimum=1, maximum=256, single=True
    )


def normalize_prompt_version(value: object) -> str:
    return _bounded_text(
        value, "converter_prompt_version", minimum=1, maximum=64, single=True
    )


def normalize_quest_prompt(value: object, *, minimum: int) -> str:
    return _bounded_text(
        value,
        "quest_persona_prompt",
        minimum=minimum,
        maximum=_MAX_PROMPT_CHARS,
    )


def normalize_aliases(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_ALIASES:
        raise PersonaProfileError("aliases_invalid")
    aliases: list[str] = []
    seen: set[str] = set()
    for item in value:
        alias = _bounded_text(item, "alias", minimum=1, maximum=80, single=True)
        folded = alias.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        aliases.append(alias)
    return tuple(aliases)


def normalize_report(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != set(_REPORT_KEYS):
        raise PersonaProfileError("conversion_report_invalid")
    result: dict[str, tuple[str, ...]] = {}
    for key in _REPORT_KEYS:
        items = value[key]
        if not isinstance(items, (list, tuple)) or len(items) > _MAX_REPORT_ITEMS:
            raise PersonaProfileError("conversion_report_invalid")
        result[key] = tuple(
            _bounded_text(item, key, minimum=1, maximum=240, single=True)
            for item in items
        )
    return result


def validate_conversion(
    value: PersonaConversion, *, converted: bool = True
) -> PersonaConversion:
    if not isinstance(value, PersonaConversion):
        raise PersonaProfileError("conversion_invalid")
    return PersonaConversion(
        display_name=normalize_display_name(value.display_name),
        aliases=normalize_aliases(value.aliases),
        quest_persona_prompt=normalize_quest_prompt(
            value.quest_persona_prompt,
            minimum=2_000 if converted else _MIN_READY_PROMPT_CHARS,
        ),
        conversion_report=normalize_report(value.conversion_report),
    )


def source_hash(source_snapshot: str) -> str:
    normalized = normalize_source_snapshot(source_snapshot)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _profile_from_payload(payload: object, *, expected_id: str) -> PersonaProfile:
    if not isinstance(payload, dict) or set(payload) != _PROFILE_KEYS:
        raise PersonaProfileError("profile_schema_invalid")
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise PersonaProfileError("profile_schema_unsupported")
    profile_id = validate_profile_id(payload["profile_id"])
    if profile_id != expected_id:
        raise PersonaProfileError("profile_id_mismatch")
    source_kind = normalize_source_kind(payload["source_kind"])
    source_persona_id = normalize_source_persona_id(payload["source_persona_id"])
    if source_kind == "astrbot" and not source_persona_id:
        raise PersonaProfileError("source_persona_id_required")
    if source_kind == "manual" and source_persona_id:
        raise PersonaProfileError("source_persona_id_not_allowed")
    snapshot = normalize_source_snapshot(payload["source_snapshot"])
    if payload["source_hash"] != source_hash(snapshot):
        raise PersonaProfileError("source_hash_mismatch")
    status = str(payload["status"] or "")
    if status not in _PROFILE_STATUSES:
        raise PersonaProfileError("profile_status_invalid")
    prompt = str(payload["quest_persona_prompt"] or "")
    report = normalize_report(payload["conversion_report"])
    provider_id = str(payload["converter_provider_id"] or "")
    prompt_version = str(payload["converter_prompt_version"] or "")
    if status == "draft":
        if prompt or provider_id or prompt_version or any(report.values()):
            raise PersonaProfileError("draft_payload_invalid")
        normalized_prompt = ""
    else:
        normalized_prompt = normalize_quest_prompt(
            prompt, minimum=_MIN_READY_PROMPT_CHARS
        )
        prompt_version = normalize_prompt_version(prompt_version)
        if prompt_version == "manual":
            if source_kind != "manual" or provider_id:
                raise PersonaProfileError("manual_payload_invalid")
        else:
            provider_id = normalize_provider_id(provider_id)
    return PersonaProfile(
        profile_id=profile_id,
        display_name=normalize_display_name(payload["display_name"]),
        aliases=normalize_aliases(payload["aliases"]),
        source_kind=source_kind,
        source_persona_id=source_persona_id,
        source_snapshot=snapshot,
        source_hash=payload["source_hash"],
        quest_persona_prompt=normalized_prompt,
        conversion_report=report,
        converter_provider_id=provider_id,
        converter_prompt_version=prompt_version,
        status=status,  # type: ignore[arg-type]
        created_at=_normalize_timestamp(payload["created_at"]),
        updated_at=_normalize_timestamp(payload["updated_at"]),
    )


def _bounded_text(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
    single: bool = False,
) -> str:
    if not isinstance(value, str):
        raise PersonaProfileError(f"{field}_invalid")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if single and any(char in normalized for char in "\n\t"):
        raise PersonaProfileError(f"{field}_invalid")
    if any(
        (ord(char) < 32 and char not in "\n\t")
        or ord(char) == 127
        or 0xD800 <= ord(char) <= 0xDFFF
        for char in normalized
    ):
        raise PersonaProfileError(f"{field}_invalid")
    if not minimum <= len(normalized) <= maximum:
        raise PersonaProfileError(f"{field}_invalid")
    return normalized


def _normalize_timestamp(value: object) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise PersonaProfileError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersonaProfileError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise PersonaProfileError("timestamp_invalid")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _empty_report() -> dict[str, tuple[str, ...]]:
    return {key: () for key in _REPORT_KEYS}


def _path_is_linklike(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        )
    except OSError:
        return True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
