from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class BridgeServiceUnavailable(RuntimeError):
    code = "bridge_service_disabled"
    status_code = 503
    public_message = "Quest Bridge 服务已由管理员关闭"


class BridgeServiceControlError(RuntimeError):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.public_message = message


class BridgeServiceControl:
    """Administrator-controlled runtime gate for the Quest listener and sessions."""

    def __init__(
        self,
        *,
        config: Any,
        listener: Any,
        sessions: Any,
        orchestrator: Any,
        logger: Any,
        diagnostic_log: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self.config = config
        self.listener = listener
        self.sessions = sessions
        self.orchestrator = orchestrator
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self.enabled = bool(enabled)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.enabled:
            await self.listener.start()
        else:
            await self.listener.stop(reason="service_disabled")

    def require_enabled(self) -> None:
        if not self.enabled:
            raise BridgeServiceUnavailable

    async def status_snapshot(self) -> dict[str, Any]:
        listener = self.listener.status_snapshot()
        stats = await self.sessions.stats()
        if not self.enabled:
            status = "stopped"
            reason = "service_disabled"
        elif listener.get("ready") is True:
            status = "running"
            reason = "ready"
        else:
            status = "degraded"
            reason = str(listener.get("reason") or "listener_unavailable")

        integrations = self.orchestrator.integration_status()
        pipeline = integrations.get("astrbot_message_pipeline", {})
        identity = integrations.get("identity", {})
        return {
            "enabled": self.enabled,
            "ready": status == "running",
            "status": status,
            "reason": reason,
            "listener": {
                "configured": listener.get("enabled") is True,
                "ready": listener.get("ready") is True,
                "reason": str(listener.get("reason") or "unknown")[:64],
                "bind_host": str(listener.get("bind_host") or "")[:64],
                "port": int(listener.get("port") or 0),
            },
            "sessions": stats,
            "capabilities": {
                "dialogue": bool(getattr(self.orchestrator.llm, "available", False)),
                "eventbus": pipeline.get("available") is True,
                "identity_configured": identity.get("configured") is True,
                "stt": bool(getattr(self.orchestrator.stt, "available", False)),
                "tts": bool(getattr(self.orchestrator.tts, "available", False)),
                "avatar_actions": True,
            },
            "config_writable": callable(
                getattr(self.config, "save_config_async", None)
            ),
        }

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        desired = bool(enabled)
        async with self._lock:
            if desired == self.enabled:
                return await self.status_snapshot()
            await self._persist(desired)
            self.enabled = desired
            if desired:
                await self.listener.start()
                event = "service.started"
            else:
                await self.sessions.close_all_sessions()
                await self.listener.stop(reason="service_disabled")
                event = "service.stopped"
            snapshot = await self.status_snapshot()
            self._diagnostic(
                event,
                component="service",
                status=snapshot["status"],
                enabled=snapshot["enabled"],
                ready=snapshot["ready"],
            )
            return snapshot

    async def set_listener_port(self, port: int) -> dict[str, Any]:
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1024 <= port <= 65_535
        ):
            raise BridgeServiceControlError(
                "invalid_listener_port",
                422,
                "监听端口必须在 1024 到 65535 之间",
            )
        async with self._lock:
            current_port = int(getattr(self.listener.config, "port", 8520))
            if port == current_port:
                return await self.status_snapshot()

            changes: dict[str, Any] = {"pairing_listener_port": port}
            for key in ("pairing_listener_public_url", "pairing_public_url"):
                current_url = str(self.config.get(key, "") or "").strip()
                if current_url:
                    changes[key] = _url_with_port(current_url, port)
            await self._persist_changes(changes)

            if self.listener.ready:
                await self.sessions.close_all_sessions()
            await self.listener.stop(reason="port_reconfiguring")
            self.listener.configure_port(port)
            if self.enabled:
                await self.listener.start()
            snapshot = await self.status_snapshot()
            self._diagnostic(
                "listener.port_updated",
                component="listener",
                status=snapshot["status"],
                ready=snapshot["ready"],
                port=port,
            )
            return snapshot

    async def close(self) -> None:
        await self.listener.close()

    async def _persist(self, enabled: bool) -> None:
        await self._persist_changes({"bridge_service_enabled": enabled})

    async def _persist_changes(self, changes: dict[str, Any]) -> None:
        save = getattr(self.config, "save_config_async", None)
        if not callable(save):
            raise BridgeServiceControlError(
                "native_config_unavailable",
                503,
                "当前 AstrBot 配置对象不支持安全异步保存",
            )
        try:
            committed = await save(dict(changes))
        except Exception as exc:
            self.logger.warning(
                "[quest-avatar] service setting save failed: error_type=%s",
                type(exc).__name__,
            )
            raise BridgeServiceControlError(
                "config_save_failed",
                500,
                "服务状态保存失败，运行状态未改变",
            ) from exc
        if committed is not True:
            raise BridgeServiceControlError(
                "config_save_superseded",
                409,
                "配置已被更新，请刷新页面后重试",
            )

    def _diagnostic(self, event: str, **fields: Any) -> None:
        if self.diagnostic_log is None:
            return
        try:
            self.diagnostic_log.record(event, **fields)
        except Exception:
            return


def _url_with_port(value: str, port: int) -> str:
    raw = str(value or "").strip()
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
            f"{normalized_host}:{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
