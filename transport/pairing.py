from __future__ import annotations

import base64
import ipaddress
import time
from io import BytesIO
from typing import Any, TypeVar

import qrcode
from astrbot.api.web import error_response, json_response, request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from qrcode.constants import ERROR_CORRECT_M
from qrcode.image.svg import SvgPathImage

from ..adapters.api_principal import ApiPrincipalVerificationError
from ..adapters.identity_control_plane import authenticated_principal_digest
from ..core.pairing import (
    PAIRING_PROTOCOL_VERSION,
    PUBLIC_API_PATH,
    PairingCreateRequest,
    PairingError,
    PairingExchangeRequest,
    PairingExchangeService,
    PairingManager,
    PairingRevokeRequest,
    PairingStatusRequest,
)
from ..core.operator_settings import OperatorSettingsError
from ..core.plugin_identity import PLUGIN_ID, ROUTE_PREFIX
from ..core.persona_service import QuestPersonaServiceError
from ..core.service_control import BridgeServiceControlError


PLUGIN_NAME = PLUGIN_ID
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
ModelT = TypeVar("ModelT", bound=BaseModel)


class ChatProviderSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    chat_provider_id: str = Field(min_length=1, max_length=256)
    direct_mode: bool | None = None


class FastActionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    provider_id: str = Field(default="", max_length=256)
    timeout_seconds: float | None = Field(default=None, ge=0.5, le=15.0)


class STTProviderSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider_id: str = Field(default="", max_length=256)


class TrustedPlatformSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    trusted_platform_id: str = Field(default="", max_length=128)


class QuestIdentitySettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_id: str = Field(min_length=1, max_length=64)
    platform_id: str = Field(min_length=1, max_length=128)
    bot_id: str = Field(default="", max_length=128)
    user_id: str = Field(default="", max_length=128)
    api_key: SecretStr = SecretStr("")


class ServiceControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


class ListenerPortSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    port: int = Field(ge=1024, le=65_535)


class RelationshipPersonSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_id: str = Field(
        default="",
        max_length=64,
        pattern=r"^(?:|[A-Za-z0-9_.-]{1,64})$",
    )


class CharacterPersonaSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    persona_source_mode: str = Field(
        default="astrbot",
        pattern=r"^(?:astrbot|manual_override)$",
    )
    astrbot_persona_id: str = Field(default="", max_length=255)
    character_name: str = Field(default="", max_length=64)
    character_self_reference: str = Field(default="", max_length=64)
    character_self_description: str = Field(default="", max_length=2_000)
    character_user_relationship: str = Field(default="", max_length=256)


class PersonaConverterSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    persona_converter_provider_id: str = Field(min_length=1, max_length=256)


class PersonaConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_type: str = Field(pattern=r"^(?:astrbot|manual)$")
    source_persona_id: str = Field(default="", max_length=255)
    source_prompt: str = Field(default="", max_length=24_000)
    display_name: str = Field(default="", max_length=80)
    admin_requirements: str = Field(default="", max_length=2_000)


class PersonaConversionJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: str = Field(pattern=r"^pcj_[0-9a-f]{48}$")


class PersonaConversionReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preserved: list[str] = Field(default_factory=list, max_length=32)
    adapted: list[str] = Field(default_factory=list, max_length=32)
    removed: list[str] = Field(default_factory=list, max_length=32)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=32)


class PersonaProfileSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_id: str = Field(default="", pattern=r"^(?:|qp_[0-9a-f]{32})$")
    draft_token: str = Field(default="", max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    source_type: str = Field(pattern=r"^(?:astrbot|manual)$")
    source_persona_id: str = Field(default="", max_length=255)
    source_prompt: str = Field(default="", max_length=24_000)
    quest_persona_prompt: str = Field(min_length=1, max_length=12_000)
    conversion_report: PersonaConversionReportRequest = Field(
        default_factory=PersonaConversionReportRequest
    )


class PersonaProfileIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_id: str = Field(pattern=r"^qp_[0-9a-f]{32}$")


class PersonaProfileActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    profile_id: str = Field(default="", pattern=r"^(?:|qp_[0-9a-f]{32})$")


class PairingHttpApi:
    def __init__(
        self,
        *,
        context: Any,
        manager: PairingManager,
        exchange_service: PairingExchangeService,
        listener: Any,
        service: Any,
        logger: Any,
        trusted_client_id: str,
        trusted_platform_id: str,
        operator_settings: Any,
        relationship_candidates: Any,
        relationship_event_identity: Any,
        api_principal_verifier: Any,
        persona_service: Any | None = None,
        diagnostic_log: Any | None = None,
        pairing_defaults: dict[str, Any] | None = None,
        trusted_proxy_ip: str = "",
        max_json_body_bytes: int = 16_384,
    ) -> None:
        self.context = context
        self.manager = manager
        self.exchange_service = exchange_service
        self.listener = listener
        self.service = service
        self.logger = logger
        self.trusted_client_id = str(trusted_client_id or "").strip()
        self.trusted_platform_id = str(trusted_platform_id or "").strip()
        self.trusted_proxy_ip = _canonical_ip(trusted_proxy_ip)
        self.operator_settings = operator_settings
        self.relationship_candidates = relationship_candidates
        self.relationship_event_identity = relationship_event_identity
        self.api_principal_verifier = api_principal_verifier
        self.persona_service = persona_service
        self.diagnostic_log = diagnostic_log
        self.pairing_defaults = dict(pairing_defaults or {})
        self.relationship_refresh_ready = not bool(
            str(
                self.operator_settings.config.get("relationship_person_id", "") or ""
            ).strip()
        )
        self.max_json_body_bytes = max(4096, min(65_536, max_json_body_bytes))

    def register(self) -> None:
        routes = (
            (
                "pairing/service-status",
                self.service_status,
                ["GET"],
                "Read Embodiment Bridge service status",
            ),
            (
                "pairing/service-control",
                self.service_control,
                ["POST"],
                "Start or stop Embodiment Bridge service",
            ),
            (
                "pairing/listener-port",
                self.save_listener_port,
                ["POST"],
                "Persist and apply the built-in Quest listener port",
            ),
            (
                "pairing/operator-settings",
                self.operator_settings_overview,
                ["GET"],
                "Read safe Quest operator model settings",
            ),
            (
                "pairing/operator-settings",
                self.save_operator_settings,
                ["POST"],
                "Save Quest chat model selection",
            ),
            (
                "pairing/fast-action-settings",
                self.fast_action_settings_overview,
                ["GET"],
                "Read safe fast avatar-action model settings",
            ),
            (
                "pairing/fast-action-settings",
                self.save_fast_action_settings,
                ["POST"],
                "Save the optional fast avatar-action model",
            ),
            (
                "pairing/stt-settings",
                self.stt_settings_overview,
                ["GET"],
                "Read safe Quest speech recognition provider settings",
            ),
            (
                "pairing/stt-settings",
                self.save_stt_settings,
                ["POST"],
                "Save Quest speech recognition provider selection",
            ),
            (
                "pairing/platform-settings",
                self.platform_settings_overview,
                ["GET"],
                "Read safe Quest AstrBot platform settings",
            ),
            (
                "pairing/platform-settings",
                self.save_platform_settings,
                ["POST"],
                "Save Quest AstrBot platform selection",
            ),
            (
                "pairing/persona-settings",
                self.persona_settings_overview,
                ["GET"],
                "Read safe Quest character persona settings",
            ),
            (
                "pairing/persona-settings",
                self.save_persona_settings,
                ["POST"],
                "Save Quest character persona settings",
            ),
            (
                "pairing/persona-library",
                self.persona_library,
                ["GET"],
                "Read safe Quest persona profile summaries",
            ),
            (
                "pairing/persona-converter-settings",
                self.save_persona_converter_settings,
                ["POST"],
                "Save the dedicated Quest persona conversion Provider",
            ),
            (
                "pairing/persona-convert",
                self.convert_persona,
                ["POST"],
                "Convert an AstrBot or manual persona into a preview draft",
            ),
            (
                "pairing/persona-conversion-start",
                self.start_persona_conversion,
                ["POST"],
                "Start a recoverable Quest persona conversion job",
            ),
            (
                "pairing/persona-conversion-status",
                self.persona_conversion_status,
                ["POST"],
                "Read a recoverable Quest persona conversion job",
            ),
            (
                "pairing/persona-conversion-cancel",
                self.cancel_persona_conversion,
                ["POST"],
                "Cancel a recoverable Quest persona conversion job",
            ),
            (
                "pairing/persona-profile-open",
                self.open_persona_profile,
                ["POST"],
                "Explicitly open one saved Quest persona profile",
            ),
            (
                "pairing/persona-profile-save",
                self.save_persona_profile,
                ["POST"],
                "Save a reviewed Quest persona profile",
            ),
            (
                "pairing/persona-profile-activate",
                self.activate_persona_profile,
                ["POST"],
                "Activate or disable a saved Quest persona profile",
            ),
            (
                "pairing/persona-profile-delete",
                self.delete_persona_profile,
                ["POST"],
                "Delete an inactive Quest persona profile",
            ),
            (
                "pairing/quest-identity-settings",
                self.quest_identity_settings_overview,
                ["GET"],
                "Read redacted Quest identity settings",
            ),
            (
                "pairing/quest-identity-settings",
                self.save_quest_identity_settings,
                ["POST"],
                "Verify an AstrBot API-key principal and save Quest identity",
            ),
            (
                "pairing/diagnostics",
                self.diagnostics_overview,
                ["GET"],
                "Read redacted Embodiment Bridge diagnostics",
            ),
            (
                "pairing/identity-candidates",
                self.identity_candidates,
                ["GET"],
                "Read minimal natural-person candidates from relationship contract",
            ),
            (
                "pairing/identity-selection",
                self.save_identity_selection,
                ["POST"],
                "Save Quest relationship natural-person selection",
            ),
            (
                "pairing/api-principal-proof",
                self.api_principal_proof,
                ["GET"],
                "Read the authenticated AstrBot API-key principal digest",
            ),
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

    async def service_status(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {"success": True, "service": await self.service.status_snapshot()}
            )
        except Exception as exc:
            return self._error(exc, "service_status")

    async def service_control(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(ServiceControlRequest)
            service = await self.service.set_enabled(payload.enabled)
            return _json_no_store({"success": True, "service": service})
        except Exception as exc:
            return self._error(exc, "service_control")

    async def save_listener_port(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(ListenerPortSettingsRequest)
            service = await self.service.set_listener_port(payload.port)
            public_url = str(
                self.operator_settings.config.get("pairing_public_url", "") or ""
            ).strip()
            if public_url:
                self.pairing_defaults["public_url"] = public_url
            if self.listener.config.enabled:
                if self.listener.ready and self.listener.public_exchange_url:
                    self.manager.configure_exchange_url(
                        self.listener.public_exchange_url,
                        missing_reason="pairing_listener_public_url_missing",
                    )
                else:
                    status = self.listener.status_snapshot()
                    self.manager.configure_exchange_url(
                        "",
                        missing_reason=str(
                            status.get("reason") or "listener_unavailable"
                        ),
                    )
            return _json_no_store({"success": True, "service": service})
        except Exception as exc:
            return self._error(exc, "save_listener_port")

    async def operator_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "settings": self.operator_settings.snapshot(),
                }
            )
        except Exception as exc:
            return self._error(exc, "operator_settings_overview")

    async def save_operator_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(ChatProviderSelectionRequest)
            settings = await self.operator_settings.save_chat_provider_id(
                payload.chat_provider_id
            )
            if payload.direct_mode is not None:
                settings["dialogue_mode"] = await self.operator_settings.save_dialogue_mode(
                    payload.direct_mode
                )
                self.pairing_defaults["direct_dialogue_mode"] = bool(
                    payload.direct_mode
                )
                if payload.direct_mode:
                    self.pairing_defaults["server_identity_ready"] = True
            return _json_no_store({"success": True, "settings": settings})
        except Exception as exc:
            return self._error(exc, "save_operator_settings")

    async def stt_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {"success": True, "stt": self.operator_settings.stt_snapshot()}
            )
        except Exception as exc:
            return self._error(exc, "stt_settings_overview")

    async def fast_action_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "fast_action": self.operator_settings.fast_action_snapshot(),
                }
            )
        except Exception as exc:
            return self._error(exc, "fast_action_settings_overview")

    async def save_fast_action_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(FastActionSettingsRequest)
            settings = await self.operator_settings.save_fast_action_settings(
                enabled=payload.enabled,
                provider_id=payload.provider_id,
                timeout_seconds=payload.timeout_seconds,
            )
            return _json_no_store({"success": True, "fast_action": settings})
        except Exception as exc:
            return self._error(exc, "save_fast_action_settings")

    async def save_stt_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(STTProviderSelectionRequest)
            stt = await self.operator_settings.save_stt_provider_id(payload.provider_id)
            return _json_no_store({"success": True, "stt": stt})
        except Exception as exc:
            return self._error(exc, "save_stt_settings")

    async def platform_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "platform": self.operator_settings.platform_snapshot(),
                }
            )
        except Exception as exc:
            return self._error(exc, "platform_settings_overview")

    async def save_platform_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(TrustedPlatformSettingsRequest)
            platform = await self.operator_settings.save_trusted_platform_id(
                payload.trusted_platform_id
            )
            self.trusted_platform_id = platform["trusted_platform_id"]
            return _json_no_store({"success": True, "platform": platform})
        except Exception as exc:
            return self._error(exc, "save_platform_settings")

    async def persona_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "persona": await self.operator_settings.persona_overview(),
                }
            )
        except Exception as exc:
            return self._error(exc, "persona_settings_overview")

    async def save_persona_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(CharacterPersonaSettingsRequest)
            persona = await self.operator_settings.save_character_persona(
                persona_source_mode=payload.persona_source_mode,
                astrbot_persona_id=payload.astrbot_persona_id,
                character_name=payload.character_name,
                character_self_reference=payload.character_self_reference,
                character_self_description=payload.character_self_description,
                character_user_relationship=payload.character_user_relationship,
                deactivate_quest_persona=True,
            )
            if self.persona_service is not None:
                await self.persona_service.initialize()
            return _json_no_store({"success": True, "persona": persona})
        except Exception as exc:
            return self._error(exc, "save_persona_settings")

    async def persona_library(self) -> Any:
        try:
            self._dashboard_owner()
            service = self._persona_service()
            return _json_no_store(
                {"success": True, "library": await service.library_snapshot()}
            )
        except Exception as exc:
            return self._error(exc, "persona_library")

    async def save_persona_converter_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaConverterSettingsRequest)
            library = await self._persona_service().save_converter_provider(
                payload.persona_converter_provider_id
            )
            return _json_no_store({"success": True, "library": library})
        except Exception as exc:
            return self._error(exc, "save_persona_converter_settings")

    async def convert_persona(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaConvertRequest)
            result = await self._persona_service().convert(
                source_kind=payload.source_type,
                source_persona_id=payload.source_persona_id,
                source_prompt=payload.source_prompt,
                display_name=payload.display_name,
                admin_requirements=payload.admin_requirements,
            )
            return _json_no_store({"success": True, **result})
        except Exception as exc:
            return self._error(exc, "convert_persona")

    async def start_persona_conversion(self) -> Any:
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PersonaConvertRequest)
            job = await self._persona_service().start_conversion(
                owner=owner,
                source_kind=payload.source_type,
                source_persona_id=payload.source_persona_id,
                source_prompt=payload.source_prompt,
                display_name=payload.display_name,
                admin_requirements=payload.admin_requirements,
            )
            return _json_no_store(
                {"success": True, "job": job},
                status_code=200 if job.get("reused") is True else 202,
            )
        except Exception as exc:
            return self._error(exc, "start_persona_conversion")

    async def persona_conversion_status(self) -> Any:
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PersonaConversionJobRequest)
            job = await self._persona_service().conversion_status(
                payload.job_id,
                owner=owner,
            )
            return _json_no_store({"success": True, "job": job})
        except Exception as exc:
            return self._error(exc, "persona_conversion_status")

    async def cancel_persona_conversion(self) -> Any:
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PersonaConversionJobRequest)
            job = await self._persona_service().cancel_conversion(
                payload.job_id,
                owner=owner,
            )
            return _json_no_store({"success": True, "job": job})
        except Exception as exc:
            return self._error(exc, "cancel_persona_conversion")

    async def open_persona_profile(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaProfileIdRequest)
            profile = await self._persona_service().open_profile(payload.profile_id)
            return _json_no_store({"success": True, "profile": profile})
        except Exception as exc:
            return self._error(exc, "open_persona_profile")

    async def save_persona_profile(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaProfileSaveRequest)
            profile = await self._persona_service().save_profile(
                profile_id=payload.profile_id,
                draft_token=payload.draft_token,
                display_name=payload.display_name,
                aliases=payload.aliases,
                source_kind=payload.source_type,
                source_persona_id=payload.source_persona_id,
                source_prompt=payload.source_prompt,
                quest_persona_prompt=payload.quest_persona_prompt,
                conversion_report=payload.conversion_report.model_dump(),
            )
            return _json_no_store({"success": True, "profile": profile.to_dict()})
        except Exception as exc:
            return self._error(exc, "save_persona_profile")

    async def activate_persona_profile(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaProfileActivationRequest)
            library = await self._persona_service().activate(payload.profile_id)
            return _json_no_store({"success": True, "library": library})
        except Exception as exc:
            return self._error(exc, "activate_persona_profile")

    async def delete_persona_profile(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(PersonaProfileIdRequest)
            deleted = await self._persona_service().delete(payload.profile_id)
            return _json_no_store(
                {
                    "success": True,
                    "deleted": {
                        "profile_id": deleted.profile_id,
                        "display_name": deleted.display_name,
                    },
                }
            )
        except Exception as exc:
            return self._error(exc, "delete_persona_profile")

    async def quest_identity_settings_overview(self) -> Any:
        try:
            self._dashboard_owner()
            return _json_no_store(
                {
                    "success": True,
                    "identity": await self.operator_settings.quest_identity_overview(),
                }
            )
        except Exception as exc:
            return self._error(exc, "quest_identity_settings_overview")

    async def save_quest_identity_settings(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(QuestIdentitySettingsRequest)
            api_key = (
                payload.api_key.get_secret_value().strip()
                or str(
                    self.operator_settings.config.get("pairing_astrbot_api_key", "")
                    or ""
                ).strip()
            )
            principal_digest = await self.api_principal_verifier.resolve_digest(api_key)
            identity_source = str(
                self.operator_settings.config.get("pairing_identity_source", "manual")
                or "manual"
            ).strip()
            stored_bot_id, stored_user_id = (
                self.operator_settings.server_identity_values()
            )
            identity = await self.operator_settings.save_quest_identity(
                client_id=payload.client_id,
                platform_id=payload.platform_id,
                bot_id=payload.bot_id
                or (
                    stored_bot_id if identity_source in {"manual", "preserved"} else ""
                ),
                user_id=payload.user_id
                or (
                    stored_user_id if identity_source in {"manual", "preserved"} else ""
                ),
                api_principal_digest=principal_digest,
                astrbot_api_key=api_key,
            )
            self.trusted_client_id = identity["client_id"]
            self.trusted_platform_id = identity["platform_id"]
            self.pairing_defaults.update(
                client_id=identity["client_id"],
                user_id="server-managed-user",
                bot_id="server-managed-bot",
                server_identity_ready=True,
                group_id="",
                astrbot_api_key=str(
                    self.operator_settings.config.get("pairing_astrbot_api_key", "")
                    or ""
                ),
            )
            await self.service.sessions.close_all_sessions()
            return _json_no_store({"success": True, "identity": identity})
        except Exception as exc:
            return self._error(exc, "save_quest_identity_settings")

    async def api_principal_proof(self) -> Any:
        try:
            principal = self._api_key_owner()
            return _json_no_store(
                {
                    "success": True,
                    "api_principal_digest": authenticated_principal_digest(principal),
                }
            )
        except Exception as exc:
            return self._error(exc, "api_principal_proof")

    async def diagnostics_overview(self) -> Any:
        try:
            self._dashboard_owner()
            if self.diagnostic_log is None:
                return _json_no_store(
                    {
                        "success": True,
                        "diagnostics": {"status": "disabled", "events": []},
                    }
                )
            snapshot = self.diagnostic_log.diagnostic_events(after_seq=0, limit=200)
            projected: list[dict[str, Any]] = []
            for event in snapshot.get("events", []):
                if not isinstance(event, dict):
                    continue
                details = event.get("details")
                details = details if isinstance(details, dict) else {}
                projected.append(
                    {
                        "timestamp": str(event.get("timestamp") or "")[:40],
                        "event": str(event.get("code") or "")[:80],
                        "component": str(details.get("component") or "")[:64],
                        "code": str(details.get("code") or "")[:80],
                        "reason_code": str(details.get("reason_code") or "")[:80],
                        "error_type": str(details.get("error_type") or "")[:80],
                        "phase": str(details.get("phase") or "")[:48],
                        "operation": str(details.get("operation") or "")[:48],
                        "duration_ms": details.get("duration_ms"),
                        "http_status": details.get("http_status"),
                        "status": str(details.get("status") or "")[:32],
                        "bytes": details.get("bytes"),
                        "chunks": details.get("chunks"),
                        "event_count": details.get("event_count"),
                        "queue_depth": details.get("queue_depth"),
                        "authorized": details.get("authorized"),
                        "text_sent": details.get("text_sent"),
                        "audio_sent": details.get("audio_sent"),
                    }
                )
            root_cause = {"stage": "", "code": ""}
            failure_statuses = {
                "blocked",
                "error",
                "failed",
                "limited",
                "timeout",
                "unavailable",
            }
            success_statuses = {"ok", "ready", "authorized", "completed", "connected"}
            for item in projected:
                status = str(item.get("status") or "")
                code = str(item.get("reason_code") or item.get("code") or "")
                stage = str(item.get("component") or item.get("phase") or "")[:64]
                if code and status in failure_statuses:
                    root_cause = {
                        "stage": stage,
                        "code": code[:80],
                    }
                elif status in success_statuses and stage == root_cause["stage"]:
                    root_cause = {"stage": "", "code": ""}
            return _json_no_store(
                {
                    "success": True,
                    "diagnostics": {
                        "status": str(snapshot.get("status") or "unavailable")[:32],
                        "reason": str(snapshot.get("reason") or "")[:48],
                        "root_cause": root_cause,
                        "events": projected,
                    },
                }
            )
        except Exception as exc:
            return self._error(exc, "diagnostics_overview")

    async def identity_candidates(self) -> Any:
        try:
            self._dashboard_owner()
            result = await self.relationship_candidates.list_candidates()
            return _json_no_store({"success": True, "identity_catalog": result})
        except Exception as exc:
            return self._error(exc, "identity_candidates")

    async def refresh_selected_relationship_identity(self) -> dict[str, str]:
        person_id = str(
            self.operator_settings.config.get("relationship_person_id", "") or ""
        ).strip()
        if not person_id:
            self.relationship_refresh_ready = True
            return {"status": "not_configured", "reason": "not_configured"}
        resolution = await self.relationship_event_identity.resolve(
            person_id=person_id,
            platform_candidates=(
                self.operator_settings.quest_identity_platform_candidates()
            ),
        )
        identity = resolution.identity
        if identity is None:
            await self.operator_settings.mark_relationship_identity_sync_pending()
            self.relationship_refresh_ready = False
            return {"status": resolution.status, "reason": resolution.reason}
        try:
            await self.operator_settings.save_resolved_relationship_identity(
                person_id=person_id,
                platform_id=identity.platform_id,
                bot_id=identity.bot_id,
                user_id=identity.user_id,
            )
        except OperatorSettingsError as exc:
            self.relationship_refresh_ready = False
            return {"status": "pending", "reason": exc.code}
        self.trusted_platform_id = identity.platform_id
        self.pairing_defaults.update(
            client_id=str(
                self.operator_settings.config.get("trusted_client_id", "") or ""
            ).strip(),
            user_id="server-managed-user",
            bot_id="server-managed-bot",
            group_id="",
            server_identity_ready=True,
        )
        self.relationship_refresh_ready = True
        return {"status": "resolved", "reason": "resolved"}

    async def ensure_selected_relationship_identity(self) -> dict[str, str]:
        if self.relationship_refresh_ready:
            return {"status": "ready", "reason": "already_refreshed"}
        return await self.refresh_selected_relationship_identity()

    async def save_identity_selection(self) -> Any:
        try:
            self._dashboard_owner()
            payload = await self._read_model(RelationshipPersonSelectionRequest)
            if payload.person_id:
                catalog = await self.relationship_candidates.list_candidates()
                if catalog.get("status") != "ok":
                    raise PairingError(
                        "relationship_identity_contract_unavailable",
                        503,
                        "情当前版本未提供可用的自然人候选读取契约",
                    )
                candidate_ids = {
                    str(item.get("person_id") or "")
                    for item in catalog.get("candidates", [])
                    if isinstance(item, dict)
                }
                if payload.person_id not in candidate_ids:
                    raise PairingError(
                        "relationship_person_not_available",
                        422,
                        "所选自然人不存在或已经不可用",
                    )
                platforms = self.operator_settings.quest_identity_platform_candidates()
                resolution = await self.relationship_event_identity.resolve(
                    person_id=payload.person_id,
                    platform_candidates=platforms,
                )
                identity = resolution.identity
                if identity is None:
                    code = resolution.reason or (
                        "relationship_quest_event_identity_unavailable"
                    )
                    if code == "identity_transaction_pending":
                        status_code = 409
                    elif resolution.status == "invalid_response":
                        status_code = 502
                    elif (
                        resolution.status
                        in {
                            "provider_unavailable",
                            "contract_unavailable",
                            "timeout",
                            "error",
                        }
                        or code == "active_platform_api_unavailable"
                    ):
                        status_code = 503
                    else:
                        status_code = 422
                    raise PairingError(
                        code,
                        status_code,
                        "所选自然人没有可用于 Quest 对话的唯一活跃私聊账号",
                    )
                settings = await (
                    self.operator_settings.save_resolved_relationship_identity(
                        person_id=payload.person_id,
                        platform_id=identity.platform_id,
                        bot_id=identity.bot_id,
                        user_id=identity.user_id,
                    )
                )
                self.trusted_platform_id = identity.platform_id
                self.pairing_defaults.update(
                    client_id=str(
                        self.operator_settings.config.get("trusted_client_id", "") or ""
                    ).strip(),
                    user_id="server-managed-user",
                    bot_id="server-managed-bot",
                    server_identity_ready=True,
                    group_id="",
                )
                await self.service.sessions.close_all_sessions()
                self.relationship_refresh_ready = True
                return _json_no_store(
                    {
                        "success": True,
                        "settings": settings,
                        "event_identity": {
                            "status": "resolved",
                            "source": "relationship.quest_event_identity@1.0",
                        },
                    }
                )
            settings = await (
                self.operator_settings.clear_resolved_relationship_identity()
            )
            server_identity_ready = bool(
                all(self.operator_settings.server_identity_values())
                and str(
                    self.operator_settings.config.get(
                        "pairing_identity_sync_state", "pending"
                    )
                    or "pending"
                )
                == "ready"
                and getattr(
                    self.operator_settings.identity,
                    "local_binding_configured",
                    False,
                )
            )
            self.pairing_defaults.update(
                user_id="server-managed-user",
                bot_id="server-managed-bot",
                group_id="",
                server_identity_ready=server_identity_ready,
            )
            await self.service.sessions.close_all_sessions()
            self.relationship_refresh_ready = True
            return _json_no_store(
                {
                    "success": True,
                    "settings": settings,
                    "event_identity": {
                        "status": (
                            "relationship_disabled"
                            if server_identity_ready
                            else "base_identity_unavailable"
                        ),
                        "source": (
                            "server_identity_preserved"
                            if server_identity_ready
                            else "server_identity_incomplete"
                        ),
                    },
                }
            )
        except Exception as exc:
            return self._error(exc, "save_identity_selection")

    async def overview(self) -> Any:
        started = time.perf_counter()
        try:
            self._dashboard_owner()
            quick_pairing_ready, quick_pairing_reason = self._quick_pairing_status()
            response = _json_no_store(
                {
                    "success": True,
                    "pairing_protocol_version": PAIRING_PROTOCOL_VERSION,
                    "public_api_path": PUBLIC_API_PATH,
                    "bridge_key_configured": len(self.manager.bridge_api_key) >= 32,
                    "requires_https": not self.manager.allow_private_http,
                    "allow_private_http": self.manager.allow_private_http,
                    "bootstrap_ready": self.manager.bootstrap_ready,
                    "bootstrap_reason": self.manager.bootstrap_reason,
                    "exchange_url": self.manager.exchange_url,
                    "listener": self.listener.status_snapshot(),
                    "quick_pairing_ready": quick_pairing_ready,
                    "quick_pairing_reason": quick_pairing_reason,
                },
            )
            self._diagnostic(
                "pairing.overview",
                component="pairing",
                status=200,
                ready=quick_pairing_ready,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            return self._error(exc, "overview")

    async def create(self) -> Any:
        started = time.perf_counter()
        owner = ""
        pairing_id = ""
        try:
            owner = self._dashboard_owner()
            payload = self._complete_create_request(
                await self._read_model(PairingCreateRequest)
            )
            result = self.manager.create(owner, payload)
            pairing_id = result.pairing_id
            qr_svg_data_uri = _qr_svg_data_uri(result.qr_payload)
            response = _json_no_store(
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
            self._diagnostic(
                "pairing.create",
                component="pairing",
                status=201,
                result="ok",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            if owner and pairing_id:
                try:
                    self.manager.revoke(owner, pairing_id)
                except PairingError:
                    pass
            return self._error(exc, "create")

    def _quick_pairing_status(self) -> tuple[bool, str]:
        if len(self.manager.bridge_api_key) < 32:
            return False, "bridge_key_missing"
        if not self.manager.bootstrap_ready:
            return (
                False,
                self.manager.bootstrap_reason or "pairing_bootstrap_unavailable",
            )
        required = (
            "public_url",
            "astrbot_api_key",
            "client_id",
        )
        if any(
            not str(self.pairing_defaults.get(key) or "").strip() for key in required
        ):
            return False, "quick_pairing_defaults_missing"
        if (
            self.pairing_defaults.get("server_identity_ready") is not True
            and self.pairing_defaults.get("direct_dialogue_mode") is not True
        ):
            return False, "quick_pairing_server_identity_missing"
        return True, "ready"

    def _complete_create_request(
        self,
        payload: PairingCreateRequest,
    ) -> PairingCreateRequest:
        if payload.public_url:
            if (
                self.pairing_defaults.get("server_identity_ready") is not True
                and self.pairing_defaults.get("direct_dialogue_mode") is not True
            ):
                raise PairingError(
                    "quick_pairing_server_identity_missing",
                    503,
                    "Quest server identity is incomplete",
                )
            return payload.model_copy(
                update={
                    "user_id": "server-managed-user",
                    "bot_id": "server-managed-bot",
                    "group_id": "",
                }
            )
        ready, reason = self._quick_pairing_status()
        if not ready:
            raise PairingError(
                reason,
                503,
                "Quest quick-pairing server settings are incomplete",
            )
        values = {
            "protocol_version": payload.protocol_version,
            "public_url": str(self.pairing_defaults.get("public_url") or "").strip(),
            "port": None,
            "astrbot_api_key": str(self.pairing_defaults.get("astrbot_api_key") or ""),
            "client_id": str(self.pairing_defaults.get("client_id") or "").strip(),
            "user_id": str(self.pairing_defaults.get("user_id") or "").strip(),
            "bot_id": str(self.pairing_defaults.get("bot_id") or "").strip(),
            "group_id": str(self.pairing_defaults.get("group_id") or "").strip(),
            "relationship_profile_id": str(
                self.pairing_defaults.get("relationship_profile_id") or ""
            ).strip(),
            "expected_remote_ip": "",
            "allow_insecure_http": bool(
                self.pairing_defaults.get("allow_insecure_http", False)
            ),
            "ttl_seconds": self.pairing_defaults.get("ttl_seconds", 120),
        }
        if self.pairing_defaults.get("direct_dialogue_mode") is True:
            values["user_id"] = "quest-direct-user"
            values["bot_id"] = "quest-direct-bridge"
            values["group_id"] = ""
        return PairingCreateRequest.model_validate(values)

    async def status(self) -> Any:
        started = time.perf_counter()
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PairingStatusRequest)
            response = _json_no_store(
                {
                    "success": True,
                    "pairing": self.manager.status(owner, payload.pairing_id),
                },
            )
            self._diagnostic(
                "pairing.status",
                component="pairing",
                status=200,
                result="ok",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            return self._error(exc, "status")

    async def revoke(self) -> Any:
        started = time.perf_counter()
        try:
            owner = self._dashboard_owner()
            payload = await self._read_model(PairingRevokeRequest)
            response = _json_no_store(
                {
                    "success": True,
                    "pairing": self.manager.revoke(owner, payload.pairing_id),
                },
            )
            self._diagnostic(
                "pairing.revoke",
                component="pairing",
                status=200,
                result="ok",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            return self._error(exc, "revoke")

    async def exchange(self) -> Any:
        started = time.perf_counter()
        try:
            self._dashboard_owner()
            payload = await self._read_model(PairingExchangeRequest)
            response = _json_no_store(
                self.exchange_service.exchange(
                    payload,
                    remote=self._remote_address(),
                )
            )
            self._diagnostic(
                "pairing.exchange",
                component="pairing",
                status=200,
                result="ok",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return response
        except Exception as exc:
            return self._error(exc, "exchange")

    def _dashboard_owner(self) -> str:
        owner = self._authenticated_owner()
        if owner.startswith("api_key:"):
            raise PairingError(
                "astrbot_dashboard_auth_required",
                401,
                "AstrBot Dashboard authentication is required",
            )
        return owner

    def _api_key_owner(self) -> str:
        owner = self._authenticated_owner()
        if not owner.startswith("api_key:"):
            raise PairingError(
                "api_key_auth_required",
                401,
                "AstrBot API Key authentication is required",
            )
        return owner

    @staticmethod
    def _authenticated_owner() -> str:
        owner = str(request.username or "").strip()
        if not owner:
            raise PairingError(
                "astrbot_auth_required",
                401,
                "AstrBot authentication is required",
            )
        return owner

    def _remote_address(self) -> str:
        try:
            # AstrBot 4.26.8 exposes the authenticated plugin request's direct
            # peer as ``client_host``.  Do not use framework-private request
            # attributes here: a missing peer must fail closed instead of
            # trusting a client-supplied forwarding header.
            direct = _canonical_ip(request.client_host)
            if self.trusted_proxy_ip and direct == self.trusted_proxy_ip:
                forwarded = str(
                    request.headers.get("x-quest-pairing-source") or ""
                ).strip()
                if "," in forwarded:
                    return "invalid"
                return _canonical_ip(forwarded) or "invalid"
            return direct or "invalid"
        except (AttributeError, RuntimeError):
            return "invalid"

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

    def _persona_service(self) -> Any:
        if self.persona_service is None:
            raise QuestPersonaServiceError(
                "persona_service_unavailable",
                503,
                "临人格服务当前不可用",
            )
        return self.persona_service

    def _error(self, exc: Exception, operation: str) -> Any:
        self._diagnostic(
            "pairing.error",
            component="pairing",
            operation=operation,
            code=getattr(exc, "code", "pairing_failed"),
            error_type=type(exc).__name__,
        )
        headers = dict(NO_STORE_HEADERS)
        if isinstance(
            exc,
            (
                PairingError,
                OperatorSettingsError,
                QuestPersonaServiceError,
                BridgeServiceControlError,
                ApiPrincipalVerificationError,
            ),
        ):
            data: dict[str, object] = {"code": exc.code}
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                data["retry_after"] = retry_after
                headers["Retry-After"] = str(retry_after)
            response = error_response(
                exc.public_message,
                status_code=exc.status_code,
                data=data,
            )
            return _with_headers(response, headers)
        self.logger.error(
            "[embodiment-bridge] pairing operation failed: operation=%s error_type=%s",
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

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return


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


def _canonical_ip(value: object) -> str:
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return ""
    if address.is_unspecified or address.is_multicast:
        return ""
    return str(address)
