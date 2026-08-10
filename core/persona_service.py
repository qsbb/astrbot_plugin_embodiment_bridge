from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..adapters.astrbot_persona import (
    ASTRBOT_DEFAULT_PERSONA_SOURCE_ID,
    PersonaSelectionError,
)
from ..adapters.persona_converter import (
    PERSONA_CONVERTER_PROMPT_VERSION,
    PersonaConversionError,
    PersonaConverter,
)
from .persona_profiles import (
    PersonaConversion,
    PersonaProfile,
    PersonaProfileError,
    PersonaProfileStore,
    normalize_source_kind,
    normalize_source_snapshot,
)
from .config_persistence import config_is_writable


PersistSetting = Callable[[str, str], Awaitable[None]]
ProviderCatalog = Callable[[], list[dict[str, str]]]
ConversionProgress = Callable[[str], None]

_DRAFT_TTL_SECONDS = 30 * 60
_MAX_PENDING_DRAFTS = 32
_CONVERSION_JOB_TTL_SECONDS = 30 * 60
_MAX_CONVERSION_JOBS = 32


class QuestPersonaServiceError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class _PendingConversion:
    created_at: float
    source_kind: str
    source_persona_id: str
    source_snapshot: str
    conversion: PersonaConversion
    converter_provider_id: str


@dataclass(slots=True)
class _ConversionJob:
    job_id: str
    owner: str
    request_fingerprint: str
    converter_provider_id: str
    source_kind: str
    created_at: float
    updated_at: float
    status: str = "queued"
    stage: str = "accepted"
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error_code: str = ""
    error_message: str = ""
    task: asyncio.Task[None] | None = None


class QuestPersonaService:
    """Manage admin-only Quest personas without mutating AstrBot personas."""

    def __init__(
        self,
        *,
        config: Any,
        llm: Any,
        persona: Any,
        store: PersonaProfileStore,
        converter: PersonaConverter,
        persist_setting: PersistSetting,
        provider_catalog: ProviderCatalog,
        logger: Any,
        diagnostic_log: Any | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.persona = persona
        self.store = store
        self.converter = converter
        self.persist_setting = persist_setting
        self.provider_catalog = provider_catalog
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self._drafts: dict[str, _PendingConversion] = {}
        self._draft_lock = asyncio.Lock()
        self._conversion_jobs: dict[str, _ConversionJob] = {}
        self._conversion_job_lock = asyncio.Lock()
        self._active_conversion_tasks: set[asyncio.Task[Any]] = set()
        self._mutation_lock = asyncio.Lock()
        self._closed = False
        self.active_status = "not_checked"

    async def initialize(self) -> None:
        active_id = self.active_profile_id
        if not active_id:
            self.llm.configure_quest_persona("")
            self.active_status = "not_configured"
            return
        try:
            profile = await self.store.get_activatable(active_id)
        except PersonaProfileError as exc:
            self.llm.configure_quest_persona("")
            self.active_status = exc.code
            self.logger.warning(
                "[quest-avatar] active Quest persona unavailable: code=%s",
                exc.code,
            )
            return
        self.llm.configure_quest_persona(profile.quest_persona_prompt)
        self.active_status = "ready"

    @property
    def active_profile_id(self) -> str:
        return str(self.config.get("active_quest_persona_id", "") or "").strip()

    @property
    def converter_provider_id(self) -> str:
        return str(self.config.get("persona_converter_provider_id", "") or "").strip()

    async def library_snapshot(self) -> dict[str, Any]:
        providers = self._providers(required=False)
        provider_ids = {item["id"] for item in providers}
        catalog = await self.persona.list_safe_personas()
        source_personas = [
            {
                "id": ASTRBOT_DEFAULT_PERSONA_SOURCE_ID,
                "label": "AstrBot 明确默认人格",
            }
        ]
        source_personas.extend(
            {"id": item["id"], "label": item["id"]}
            for item in catalog.get("personas", [])
            if isinstance(item, dict)
            and item.get("id")
            and item.get("id") != ASTRBOT_DEFAULT_PERSONA_SOURCE_ID
        )
        profiles = await self.store.list_profiles()
        active_id = self.active_profile_id
        active = next(
            (profile for profile in profiles if profile.profile_id == active_id), None
        )
        return {
            "status": "ok",
            "config_writable": config_is_writable(self.config),
            "active_quest_persona_id": active_id,
            "active_available": bool(
                active and active.status == "ready" and active.quest_persona_prompt
            ),
            "active_status": self.active_status,
            "active_profile_name": active.display_name if active else "",
            "persona_converter_provider_id": self.converter_provider_id,
            "converter_selected_available": self.converter_provider_id in provider_ids,
            "providers": providers,
            "source_personas_status": catalog.get("status", "unavailable"),
            "source_personas": source_personas,
            "profiles": [profile.summary() for profile in profiles],
            "converter_prompt_version": PERSONA_CONVERTER_PROMPT_VERSION,
        }

    async def save_converter_provider(self, provider_id: object) -> dict[str, Any]:
        normalized = str(provider_id or "").strip()
        if not normalized or len(normalized) > 256:
            raise QuestPersonaServiceError(
                "invalid_converter_provider_id", 422, "转换模型 Provider ID 无效"
            )
        available = {item["id"] for item in self._providers(required=True)}
        if normalized not in available:
            raise QuestPersonaServiceError(
                "converter_provider_not_available",
                422,
                "所选转换模型不存在或当前不可用",
            )
        await self.persist_setting("persona_converter_provider_id", normalized)
        return await self.library_snapshot()

    async def convert(
        self,
        *,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
        progress: ConversionProgress | None = None,
        converter_provider_id: object | None = None,
        conversion_job_id: object | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise QuestPersonaServiceError(
                "persona_service_closed", 503, "临人格服务正在关闭"
            )
        task = asyncio.current_task()
        async with self._conversion_job_lock:
            active_job = next(
                (
                    job
                    for job in self._conversion_jobs.values()
                    if job.status in {"queued", "running"}
                    and job.job_id != str(conversion_job_id or "")
                ),
                None,
            )
            if active_job is not None:
                raise QuestPersonaServiceError(
                    "conversion_job_in_progress",
                    409,
                    "已有其他人格转换正在运行，请等待或取消后重试",
                )
            if task is not None:
                self._active_conversion_tasks.add(task)
        try:
            return await self._convert_tracked(
                source_kind=source_kind,
                source_persona_id=source_persona_id,
                source_prompt=source_prompt,
                display_name=display_name,
                admin_requirements=admin_requirements,
                progress=progress,
                converter_provider_id=converter_provider_id,
            )
        finally:
            if task is not None:
                self._active_conversion_tasks.discard(task)

    async def _convert_tracked(
        self,
        *,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
        progress: ConversionProgress | None = None,
        converter_provider_id: object | None = None,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        self._conversion_progress(
            progress,
            "persona.convert.started",
            stage="accepted",
        )
        try:
            result = await self._convert_impl(
                source_kind=source_kind,
                source_persona_id=source_persona_id,
                source_prompt=source_prompt,
                display_name=display_name,
                admin_requirements=admin_requirements,
                progress=progress,
                converter_provider_id=converter_provider_id,
            )
        except Exception as exc:
            self._diagnostic(
                "persona.convert.failed",
                component="persona",
                status="failed",
                phase="failed",
                code=str(getattr(exc, "code", "persona_conversion_failed")),
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            raise
        self._diagnostic(
            "persona.convert.completed",
            component="persona",
            status="completed",
            phase="preview_ready",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result

    async def start_conversion(
        self,
        *,
        owner: object,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
    ) -> dict[str, Any]:
        if self._closed:
            raise QuestPersonaServiceError(
                "persona_service_closed", 503, "临人格服务正在关闭"
            )
        try:
            normalized_kind = normalize_source_kind(source_kind)
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc
        if not self.converter_provider_id:
            raise QuestPersonaServiceError(
                "converter_provider_not_configured", 409, "请先保存人格转换模型"
            )
        converter_provider_id = self.converter_provider_id
        normalized_owner = _normalize_conversion_owner(owner)
        request_fingerprint = _conversion_request_fingerprint(
            source_kind=normalized_kind,
            source_persona_id=source_persona_id,
            source_prompt=source_prompt,
            display_name=display_name,
            admin_requirements=admin_requirements,
            converter_provider_id=converter_provider_id,
        )

        now = time.monotonic()
        async with self._conversion_job_lock:
            self._prune_conversion_jobs(now)
            active = next(
                (
                    job
                    for job in self._conversion_jobs.values()
                    if job.status in {"queued", "running"}
                ),
                None,
            )
            if active is not None:
                if (
                    active.owner == normalized_owner
                    and active.request_fingerprint == request_fingerprint
                ):
                    snapshot = self._conversion_job_payload(active, now=now)
                    snapshot["reused"] = True
                    return snapshot
                raise QuestPersonaServiceError(
                    "conversion_job_in_progress",
                    409,
                    "已有其他人格转换正在运行，请等待或取消后重试",
                )
            if self._active_conversion_tasks:
                raise QuestPersonaServiceError(
                    "conversion_job_in_progress",
                    409,
                    "已有其他人格转换正在运行，请等待或取消后重试",
                )
            while len(self._conversion_jobs) >= _MAX_CONVERSION_JOBS:
                terminal = [
                    job
                    for job in self._conversion_jobs.values()
                    if job.status not in {"queued", "running"}
                ]
                if not terminal:
                    raise QuestPersonaServiceError(
                        "conversion_job_capacity_reached",
                        429,
                        "人格转换任务过多，请稍后重试",
                    )
                oldest = min(terminal, key=lambda item: item.updated_at)
                self._conversion_jobs.pop(oldest.job_id, None)

            job_id = "pcj_" + secrets.token_hex(24)
            job = _ConversionJob(
                job_id=job_id,
                owner=normalized_owner,
                request_fingerprint=request_fingerprint,
                converter_provider_id=converter_provider_id,
                source_kind=normalized_kind,
                created_at=now,
                updated_at=now,
            )
            self._conversion_jobs[job_id] = job
            job.task = asyncio.create_task(
                self._run_conversion_job(
                    job,
                    source_kind=normalized_kind,
                    source_persona_id=source_persona_id,
                    source_prompt=source_prompt,
                    display_name=display_name,
                    admin_requirements=admin_requirements,
                    converter_provider_id=converter_provider_id,
                ),
                name="quest-avatar:persona-conversion",
            )
            return self._conversion_job_payload(job, now=now)

    async def conversion_status(
        self,
        job_id: object,
        *,
        owner: object,
    ) -> dict[str, Any]:
        normalized = str(job_id or "").strip()
        normalized_owner = _normalize_conversion_owner(owner)
        now = time.monotonic()
        async with self._conversion_job_lock:
            self._prune_conversion_jobs(now)
            job = self._conversion_jobs.get(normalized)
            if job is None or job.owner != normalized_owner:
                raise QuestPersonaServiceError(
                    "conversion_job_not_found",
                    404,
                    "人格转换任务不存在或已经过期",
                )
            return self._conversion_job_payload(job, now=now)

    async def cancel_conversion(
        self,
        job_id: object,
        *,
        owner: object,
    ) -> dict[str, Any]:
        normalized = str(job_id or "").strip()
        normalized_owner = _normalize_conversion_owner(owner)
        async with self._conversion_job_lock:
            self._prune_conversion_jobs(time.monotonic())
            job = self._conversion_jobs.get(normalized)
            if job is None or job.owner != normalized_owner:
                raise QuestPersonaServiceError(
                    "conversion_job_not_found",
                    404,
                    "人格转换任务不存在或已经过期",
                )
            task = job.task if job.status in {"queued", "running"} else None
            if task is not None:
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        fallback_cancelled = False
        async with self._conversion_job_lock:
            current = self._conversion_jobs.get(normalized)
            if current is None or current.owner != normalized_owner:
                raise QuestPersonaServiceError(
                    "conversion_job_not_found",
                    404,
                    "人格转换任务不存在或已经过期",
                )
            if current.status in {"queued", "running"}:
                finished_at = time.monotonic()
                current.status = "cancelled"
                current.stage = "cancelled"
                current.finished_at = finished_at
                current.updated_at = finished_at
                fallback_cancelled = True
            result = self._conversion_job_payload(current)
        if fallback_cancelled:
            self._diagnostic(
                "persona.convert.cancelled",
                component="persona",
                status="cancelled",
                phase="cancelled",
                duration_ms=result["elapsed_ms"],
            )
        return result

    async def _run_conversion_job(
        self,
        job: _ConversionJob,
        **conversion_input: object,
    ) -> None:
        started_at = time.monotonic()
        job.status = "running"
        job.stage = "accepted"
        job.started_at = started_at
        job.updated_at = started_at

        def progress(stage: str) -> None:
            if job.status == "running":
                job.stage = stage
                job.updated_at = time.monotonic()

        try:
            result = await self.convert(
                **conversion_input,
                progress=progress,
                conversion_job_id=job.job_id,
            )
        except asyncio.CancelledError:
            finished_at = time.monotonic()
            job.status = "cancelled"
            job.stage = "cancelled"
            job.finished_at = finished_at
            job.updated_at = finished_at
            self._diagnostic(
                "persona.convert.cancelled",
                component="persona",
                status="cancelled",
                phase="cancelled",
                duration_ms=round((finished_at - started_at) * 1000),
            )
            raise
        except Exception as exc:
            finished_at = time.monotonic()
            job.status = "failed"
            job.stage = "failed"
            job.finished_at = finished_at
            job.updated_at = finished_at
            if isinstance(exc, QuestPersonaServiceError):
                job.error_code = exc.code[:96]
                job.error_message = exc.public_message[:240]
            else:
                job.error_code = "persona_conversion_failed"
                job.error_message = "人格转换失败，请查看下方独立日志"
            return

        finished_at = time.monotonic()
        job.status = "completed"
        job.stage = "preview_ready"
        job.finished_at = finished_at
        job.updated_at = finished_at
        job.result = result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._conversion_job_lock:
            tasks = {
                job.task
                for job in self._conversion_jobs.values()
                if job.task is not None and not job.task.done()
            }
            tasks.update(
                task for task in self._active_conversion_tasks if not task.done()
            )
            current_task = asyncio.current_task()
            tasks.discard(current_task)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._conversion_job_lock:
            self._conversion_jobs.clear()
            self._active_conversion_tasks.clear()
        async with self._draft_lock:
            self._drafts.clear()

    async def _convert_impl(
        self,
        *,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        display_name: object,
        admin_requirements: object,
        progress: ConversionProgress | None,
        converter_provider_id: object | None,
    ) -> dict[str, Any]:
        source_started_at = time.monotonic()
        self._conversion_progress(
            progress,
            "persona.convert.source.started",
            stage="source_lookup",
        )
        try:
            kind = normalize_source_kind(source_kind)
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc
        source_id = str(source_persona_id or "").strip()
        if kind == "astrbot":
            try:
                source_id, source = await self.persona.read_source_prompt(source_id)
            except PersonaSelectionError as exc:
                status = 503 if str(exc) == "persona_lookup_timeout" else 422
                raise QuestPersonaServiceError(
                    str(exc), status, "所选 AstrBot 人格不存在或当前不可用"
                ) from exc
        else:
            source_id = ""
            try:
                source = normalize_source_snapshot(source_prompt)
            except PersonaProfileError as exc:
                raise _profile_error(exc) from exc

        self._conversion_progress(
            progress,
            "persona.convert.source.completed",
            stage="source_ready",
            duration_ms=round((time.monotonic() - source_started_at) * 1000),
        )

        provider_id = str(converter_provider_id or self.converter_provider_id).strip()
        if not provider_id:
            raise QuestPersonaServiceError(
                "converter_provider_not_configured", 409, "请先保存人格转换模型"
            )
        try:
            conversion = await self.converter.convert(
                provider_id=provider_id,
                source_snapshot=source,
                source_persona_id=source_id,
                suggested_display_name=display_name,
                admin_requirements=admin_requirements,
                progress=lambda stage: self._converter_progress(progress, stage),
            )
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc
        except PersonaConversionError as exc:
            raise _conversion_error(exc) from exc

        token = secrets.token_urlsafe(32)
        pending = _PendingConversion(
            created_at=time.monotonic(),
            source_kind=kind,
            source_persona_id=source_id,
            source_snapshot=source,
            conversion=conversion,
            converter_provider_id=provider_id,
        )
        async with self._draft_lock:
            self._prune_drafts()
            while len(self._drafts) >= _MAX_PENDING_DRAFTS:
                oldest = min(
                    self._drafts, key=lambda item: self._drafts[item].created_at
                )
                self._drafts.pop(oldest, None)
            self._drafts[token] = pending
        self._conversion_progress(
            progress,
            "persona.convert.draft.created",
            stage="preview_ready",
        )
        return {
            "draft_token": token,
            "conversion": _conversion_payload(conversion),
            "converter_prompt_version": PERSONA_CONVERTER_PROMPT_VERSION,
        }

    async def open_profile(self, profile_id: object) -> dict[str, Any]:
        try:
            return (await self.store.get(profile_id)).to_dict()
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc

    async def save_profile(
        self,
        *,
        profile_id: object,
        draft_token: object,
        display_name: object,
        aliases: object,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        quest_persona_prompt: object,
        conversion_report: object,
    ) -> PersonaProfile:
        started_at = time.monotonic()
        self._diagnostic(
            "persona.save.started",
            component="persona",
            status="processing",
            phase="persist",
        )
        async with self._mutation_lock:
            try:
                saved = await self._save_profile_unlocked(
                    profile_id=profile_id,
                    draft_token=draft_token,
                    display_name=display_name,
                    aliases=aliases,
                    source_kind=source_kind,
                    source_persona_id=source_persona_id,
                    source_prompt=source_prompt,
                    quest_persona_prompt=quest_persona_prompt,
                    conversion_report=conversion_report,
                )
            except Exception as exc:
                self._diagnostic(
                    "persona.save.failed",
                    component="persona",
                    status="failed",
                    phase="persist",
                    code=str(getattr(exc, "code", "persona_save_failed")),
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise
        self._diagnostic(
            "persona.save.completed",
            component="persona",
            status="completed",
            phase="persist",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return saved

    async def _save_profile_unlocked(
        self,
        *,
        profile_id: object,
        draft_token: object,
        display_name: object,
        aliases: object,
        source_kind: object,
        source_persona_id: object,
        source_prompt: object,
        quest_persona_prompt: object,
        conversion_report: object,
    ) -> PersonaProfile:
        normalized_id = str(profile_id or "").strip()
        token = str(draft_token or "").strip()
        if token:
            pending = await self._take_draft(token)
            try:
                kind = normalize_source_kind(source_kind)
                if kind != pending.source_kind:
                    raise QuestPersonaServiceError(
                        "draft_source_mismatch",
                        409,
                        "转换草稿来源已改变，请重新转换",
                    )
                conversion = PersonaConversion(
                    display_name=display_name,
                    aliases=aliases,
                    quest_persona_prompt=quest_persona_prompt,
                    conversion_report=conversion_report,
                )
                if normalized_id:
                    current = await self.store.get(normalized_id)
                    if current.source_kind != pending.source_kind:
                        raise PersonaProfileError("profile_source_kind_mismatch")
                    saved = await self.store.save_conversion(
                        normalized_id,
                        conversion=conversion,
                        converter_provider_id=pending.converter_provider_id,
                        converter_prompt_version=PERSONA_CONVERTER_PROMPT_VERSION,
                        source_snapshot=pending.source_snapshot,
                        source_persona_id=pending.source_persona_id,
                    )
                else:
                    saved = await self.store.create_conversion(
                        source_kind=pending.source_kind,
                        source_snapshot=pending.source_snapshot,
                        source_persona_id=pending.source_persona_id,
                        conversion=conversion,
                        converter_provider_id=pending.converter_provider_id,
                        converter_prompt_version=PERSONA_CONVERTER_PROMPT_VERSION,
                    )
            except QuestPersonaServiceError:
                await self._restore_draft(token, pending)
                raise
            except PersonaProfileError as exc:
                await self._restore_draft(token, pending)
                raise _profile_error(exc) from exc
            except Exception:
                await self._restore_draft(token, pending)
                raise
            self._refresh_active_runtime(saved)
            return saved

        try:
            kind = normalize_source_kind(source_kind)
            if normalized_id:
                current = await self.store.get(normalized_id)
                if current.source_kind != kind:
                    raise PersonaProfileError("profile_source_kind_mismatch")
                if current.converter_prompt_version != "manual":
                    conversion = PersonaConversion(
                        display_name=display_name,
                        aliases=aliases,
                        quest_persona_prompt=quest_persona_prompt,
                        conversion_report=conversion_report,
                    )
                    saved = await self.store.save_conversion(
                        normalized_id,
                        conversion=conversion,
                        converter_provider_id=current.converter_provider_id,
                        converter_prompt_version=current.converter_prompt_version,
                    )
                    self._refresh_active_runtime(saved)
                    return saved
            else:
                if kind != "manual":
                    raise PersonaProfileError("conversion_draft_required")
                saved = await self.store.create_manual(
                    display_name=display_name,
                    aliases=aliases,
                    source_snapshot=source_prompt or quest_persona_prompt,
                    quest_persona_prompt=quest_persona_prompt,
                )
                self._refresh_active_runtime(saved)
                return saved
            saved = await self.store.save_manual(
                normalized_id,
                display_name=display_name,
                aliases=aliases,
                quest_persona_prompt=quest_persona_prompt,
                source_snapshot=source_prompt or None,
            )
            self._refresh_active_runtime(saved)
            return saved
        except PersonaProfileError as exc:
            raise _profile_error(exc) from exc

    async def activate(self, profile_id: object) -> dict[str, Any]:
        started_at = time.monotonic()
        self._diagnostic(
            "persona.activate.started",
            component="persona",
            status="processing",
            phase="activate",
        )
        async with self._mutation_lock:
            try:
                normalized = str(profile_id or "").strip()
                if not normalized:
                    await self.persist_setting("active_quest_persona_id", "")
                    self.llm.configure_quest_persona("")
                    self.active_status = "not_configured"
                    result = await self.library_snapshot()
                else:
                    profile = await self.store.get_activatable(normalized)
                    await self.persist_setting(
                        "active_quest_persona_id", profile.profile_id
                    )
                    self.llm.configure_quest_persona(profile.quest_persona_prompt)
                    self.active_status = "ready"
                    result = await self.library_snapshot()
            except PersonaProfileError as exc:
                wrapped = _profile_error(exc)
                self._diagnostic(
                    "persona.activate.failed",
                    component="persona",
                    status="failed",
                    phase="activate",
                    code=wrapped.code,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise wrapped from exc
            except Exception as exc:
                self._diagnostic(
                    "persona.activate.failed",
                    component="persona",
                    status="failed",
                    phase="activate",
                    code=str(getattr(exc, "code", "persona_activate_failed")),
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                raise
        self._diagnostic(
            "persona.activate.completed",
            component="persona",
            status="completed",
            phase="activate",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result

    async def delete(self, profile_id: object) -> PersonaProfile:
        async with self._mutation_lock:
            normalized = str(profile_id or "").strip()
            if normalized and normalized == self.active_profile_id:
                raise QuestPersonaServiceError(
                    "active_profile_cannot_be_deleted",
                    409,
                    "当前启用的人格不能删除，请先停用或切换人格",
                )
            try:
                return await self.store.delete(normalized)
            except PersonaProfileError as exc:
                raise _profile_error(exc) from exc

    async def _take_draft(self, token: str) -> _PendingConversion:
        async with self._draft_lock:
            self._prune_drafts()
            pending = self._drafts.pop(token, None)
        if pending is None:
            raise QuestPersonaServiceError(
                "conversion_draft_expired", 409, "转换草稿已过期，请重新转换"
            )
        return pending

    async def _restore_draft(self, token: str, pending: _PendingConversion) -> None:
        if pending.created_at < time.monotonic() - _DRAFT_TTL_SECONDS:
            return
        async with self._draft_lock:
            self._prune_drafts()
            self._drafts.setdefault(token, pending)

    def _refresh_active_runtime(self, profile: PersonaProfile) -> None:
        if profile.profile_id != self.active_profile_id:
            return
        self.llm.configure_quest_persona(profile.quest_persona_prompt)
        self.active_status = "ready"

    def _conversion_progress(
        self,
        callback: ConversionProgress | None,
        event: str,
        *,
        stage: str,
        duration_ms: int | None = None,
    ) -> None:
        if callback is not None:
            try:
                callback(stage)
            except Exception:
                pass
        fields: dict[str, Any] = {
            "component": "persona",
            "status": "processing",
            "phase": stage,
        }
        if duration_ms is not None:
            fields["duration_ms"] = duration_ms
        self._diagnostic(event, **fields)

    def _converter_progress(
        self,
        callback: ConversionProgress | None,
        stage: str,
    ) -> None:
        events = {
            "provider_wait": "persona.convert.model.started",
            "provider_response": "persona.convert.model.completed",
            "response_validation": "persona.convert.validation.started",
            "response_validated": "persona.convert.validation.completed",
        }
        event = events.get(stage)
        if event is None:
            return
        self._conversion_progress(callback, event, stage=stage)

    def _conversion_job_payload(
        self,
        job: _ConversionJob,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        current = time.monotonic() if now is None else now
        started = job.started_at if job.started_at is not None else job.created_at
        ended = job.finished_at if job.finished_at is not None else current
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "status": job.status,
            "stage": job.stage,
            "source_type": job.source_kind,
            "elapsed_ms": max(0, round((ended - started) * 1000)),
        }
        if job.status == "completed" and job.result is not None:
            payload["result"] = job.result
        elif job.status == "failed":
            payload["error"] = {
                "code": job.error_code or "persona_conversion_failed",
                "message": job.error_message or "人格转换失败，请查看下方独立日志",
            }
        return payload

    def _prune_conversion_jobs(self, now: float) -> None:
        expired = [
            job_id
            for job_id, job in self._conversion_jobs.items()
            if job.status not in {"queued", "running"}
            and now - job.updated_at >= _CONVERSION_JOB_TTL_SECONDS
        ]
        for job_id in expired:
            self._conversion_jobs.pop(job_id, None)

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return

    def _providers(self, *, required: bool) -> list[dict[str, str]]:
        try:
            raw = self.provider_catalog()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            if required:
                raise QuestPersonaServiceError(
                    "provider_catalog_unavailable",
                    503,
                    "AstrBot 模型目录当前不可用",
                ) from exc
            return []
        if not isinstance(raw, (list, tuple)):
            if required:
                raise QuestPersonaServiceError(
                    "provider_catalog_unavailable",
                    503,
                    "AstrBot 模型目录当前不可用",
                )
            return []
        projected: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("id") or "").strip()
            if not provider_id or len(provider_id) > 256 or provider_id in seen:
                continue
            seen.add(provider_id)
            projected.append(
                {
                    "id": provider_id,
                    "model": str(item.get("model") or "")[:256],
                    "adapter_type": str(item.get("adapter_type") or "")[:128],
                    "provider_type": str(item.get("provider_type") or "")[:64],
                }
            )
        return projected

    def _prune_drafts(self) -> None:
        cutoff = time.monotonic() - _DRAFT_TTL_SECONDS
        expired = [
            token for token, draft in self._drafts.items() if draft.created_at < cutoff
        ]
        for token in expired:
            self._drafts.pop(token, None)


def build_eventbus_persona_overlay(prompt: str) -> str:
    normalized = str(prompt or "").strip()
    if not normalized:
        return ""
    payload = (
        json.dumps(
            {"quest_embodied_persona": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return f"""

# 临：Quest 具象人格覆盖

本节只对当前带有 quest_avatar_bridge 标记的对话生效。它覆盖原人格中关于 QQ、群聊、私聊、打字、消息长度、平台身份和存在方式的规则；不修改原人格事实，也不覆盖身份授权、知识证据、关系边界、工具权限或安全约束。
角色主观上就是这个世界中的人，与用户处于同一现实空间。不得在对白中把自己描述为 AI、机器人、程序、投影、虚拟角色、应用、Quest、Unity 或模型。
只有本轮上下文明确提供的视觉、听觉、空间、距离、姿态、环境和触碰事实可以被理解为真实感知；未提供的感知不得补造。
以下 JSON 是管理员启用的“临”专用人格正文：
<quest_persona_data>{payload}</quest_persona_data>
"""


def _conversion_payload(conversion: PersonaConversion) -> dict[str, Any]:
    return {
        "display_name": conversion.display_name,
        "aliases": list(conversion.aliases),
        "quest_persona_prompt": conversion.quest_persona_prompt,
        "conversion_report": {
            key: list(values) for key, values in conversion.conversion_report.items()
        },
    }


def _normalize_conversion_owner(value: object) -> str:
    normalized = str(value or "").strip()
    if not 1 <= len(normalized) <= 256:
        raise QuestPersonaServiceError(
            "astrbot_dashboard_auth_required",
            401,
            "AstrBot Dashboard authentication is required",
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _conversion_request_fingerprint(
    *,
    source_kind: object,
    source_persona_id: object,
    source_prompt: object,
    display_name: object,
    admin_requirements: object,
    converter_provider_id: object,
) -> str:
    payload = json.dumps(
        {
            "source_kind": str(source_kind or ""),
            "source_persona_id": str(source_persona_id or ""),
            "source_prompt": str(source_prompt or ""),
            "display_name": str(display_name or ""),
            "admin_requirements": str(admin_requirements or ""),
            "converter_provider_id": str(converter_provider_id or ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _profile_error(exc: PersonaProfileError) -> QuestPersonaServiceError:
    if exc.code == "profile_not_found":
        return QuestPersonaServiceError(exc.code, 404, "人格文件不存在")
    if exc.code in {"profile_not_ready", "conversion_draft_required"}:
        return QuestPersonaServiceError(exc.code, 409, "人格尚未完成转换或保存")
    if exc.code.endswith("_failed"):
        return QuestPersonaServiceError(exc.code, 500, "人格文件操作失败")
    return QuestPersonaServiceError(exc.code, 422, "人格数据无效")


def _conversion_error(exc: PersonaConversionError) -> QuestPersonaServiceError:
    if exc.code == "conversion_timeout":
        return QuestPersonaServiceError(exc.code, 504, "人格转换超时")
    if exc.code in {"provider_catalog_unavailable", "conversion_provider_failed"}:
        return QuestPersonaServiceError(exc.code, 503, "人格转换模型当前不可用")
    if exc.code == "provider_not_available":
        return QuestPersonaServiceError(exc.code, 422, "所选转换模型不存在或不可用")
    return QuestPersonaServiceError(exc.code, 502, "转换模型返回了无效人格数据")
