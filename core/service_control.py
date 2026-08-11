from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config_persistence import config_is_writable, save_config_changes


class BridgeServiceUnavailable(RuntimeError):
    code = "bridge_service_disabled"
    status_code = 503
    public_message = "具身桥接服务已由管理员关闭"


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
        config_save_lock: asyncio.Lock | None = None,
    ) -> None:
        self.config = config
        self.listener = listener
        self.sessions = sessions
        self.orchestrator = orchestrator
        self.logger = logger
        self.diagnostic_log = diagnostic_log
        self.enabled = bool(enabled)
        self._lock = asyncio.Lock()
        self._config_save_lock = config_save_lock or asyncio.Lock()

    async def initialize(self) -> None:
        await self.sessions.set_accepting(self.enabled)
        if self.enabled:
            try:
                await self.listener.start()
            except Exception:
                # A configured-but-unreachable listener must never leave the
                # in-process session API accepting work that cannot be served.
                await self.sessions.set_accepting(False)
                raise
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
            "config_writable": config_is_writable(self.config),
        }

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        desired = bool(enabled)
        async with self._lock:
            if desired == self.enabled:
                return await self.status_snapshot()
            previous = self.enabled
            await self._persist(desired)
            if desired:
                # Open the session gate only after the socket is actually ready.
                # Otherwise a bind/start failure creates sessions with no usable
                # transport and leaves the persisted switch out of sync.
                await self.sessions.set_accepting(False)
                try:
                    await self.listener.start()
                except Exception as exc:
                    await self.sessions.set_accepting(False)
                    try:
                        await self.listener.stop(reason="service_start_failed")
                    except Exception:
                        pass
                    try:
                        await self._persist(previous)
                    except Exception:
                        self.logger.warning(
                            "[embodiment-bridge] failed to roll back service setting after listener start failure"
                        )
                    self.enabled = previous
                    raise BridgeServiceControlError(
                        "service_start_failed",
                        503,
                        "具身桥接服务启动失败，已恢复为关闭状态",
                    ) from exc
                self.enabled = True
                await self.sessions.set_accepting(True)
                event = "service.started"
            else:
                self.enabled = False
                await self.sessions.set_accepting(False)
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
            rollback_changes: dict[str, Any] = {"pairing_listener_port": current_port}
            for key in ("pairing_listener_public_url", "pairing_public_url"):
                current_url = str(self.config.get(key, "") or "").strip()
                if current_url:
                    changes[key] = _url_with_port(current_url, port)
                    rollback_changes[key] = current_url
            await self._persist_changes(changes)

            await self.sessions.set_accepting(False)
            try:
                if self.listener.ready:
                    await self.sessions.close_all_sessions()
                await self.listener.stop(reason="port_reconfiguring")
                self.listener.configure_port(port)
                if self.enabled:
                    await self.listener.start()
            except Exception as exc:
                rollback_ready = False
                try:
                    await self.listener.stop(reason="port_update_failed")
                    self.listener.configure_port(current_port)
                    if self.enabled:
                        await self.listener.start()
                        rollback_ready = True
                except Exception:
                    self.logger.warning(
                        "[embodiment-bridge] failed to restart previous listener after port update failure"
                    )
                try:
                    await self._persist_changes(rollback_changes)
                except Exception:
                    self.logger.warning(
                        "[embodiment-bridge] failed to roll back listener port configuration"
                    )
                await self.sessions.set_accepting(self.enabled and rollback_ready)
                raise BridgeServiceControlError(
                    "listener_port_update_failed",
                    503,
                    "监听端口切换失败，已尝试恢复原端口",
                ) from exc
            await self.sessions.set_accepting(self.enabled)
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
        if not config_is_writable(self.config):
            raise BridgeServiceControlError(
                "native_config_unavailable",
                503,
                "当前 AstrBot 配置对象不支持安全保存",
            )
        async with self._config_save_lock:
            try:
                committed = await save_config_changes(self.config, changes)
            except Exception as exc:
                self.logger.warning(
                    "[embodiment-bridge] service setting save failed: error_type=%s",
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
