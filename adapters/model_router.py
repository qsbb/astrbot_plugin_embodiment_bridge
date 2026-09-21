"""Optional adapter for 核's versioned model-router contract."""

from __future__ import annotations

import inspect
from typing import Any


ROUTER_PLUGIN_NAME = "astrbot_plugin_update_manager"
ROUTER_CONTRACT_NAME = "series.model_router@1.0"
ROUTER_CONTRACT_MAJOR = "1"

# AstrBot 4.x 把运行中实例放在 ``StarMetadata.star_cls``；其余别名兼容旧版
# 包装层与集成测试。
_STAR_INSTANCE_ATTRIBUTES = (
    "star_cls",
    "star",
    "instance",
    "star_instance",
    "plugin",
)


async def resolve_provider_id(context: Any, kind: str) -> str:
    """Return a core-routed provider id, or ``""`` when unavailable."""
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return ""
    return route["provider_id"]


async def resolve_model_route(context: Any, kind: str) -> dict[str, Any]:
    """Return the core route for ``kind``, or ``{}`` when unavailable.

    校验规则与 :func:`resolve_provider_id` 完全一致：契约名精确匹配、版本只
    比较主版本、只读、声明 ``resolve`` 能力，路由必须匹配 kind、来源为
    ``core``、``available is True``，且 provider 仍在 AstrBot 注册表中。
    """
    route = await _resolve_validated_route(context, kind)
    if route is None:
        return {}
    return {
        "provider_id": route["provider_id"],
        "model": _text(route.get("model")),
        "voice": _text(route.get("voice")),
        "source": "core",
        "available": True,
        "fallback_from": _text(route.get("fallback_from")),
    }


def resolve_provider_id_if_sync(context: Any, kind: str) -> str:
    """Best-effort sync view used by status properties.

    The public contract remains the async pair above.  This helper never runs an
    event loop, so a layer that only returns awaitables is skipped (fail-closed
    for that layer) instead of being awaited.
    """
    route = _resolve_validated_route_sync(context, kind)
    if route is None:
        return ""
    return route["provider_id"]


def resolve_model_route_if_sync(context: Any, kind: str) -> dict[str, Any]:
    """Best-effort sync variant of :func:`resolve_model_route`."""
    route = _resolve_validated_route_sync(context, kind)
    if route is None:
        return {}
    return {
        "provider_id": route["provider_id"],
        "model": _text(route.get("model")),
        "voice": _text(route.get("voice")),
        "source": "core",
        "available": True,
        "fallback_from": _text(route.get("fallback_from")),
    }


async def _resolve_router_plugin(context: Any) -> Any | None:
    """解析核的运行中插件实例，解析不到返回 ``None``。

    解析顺序（一层拿不到才进入下一层）：

    1. ``context.get_star_instance(name)``——部分部署/包装层提供的接口，返回值
       可能是 awaitable；
    2. ``context.get_registered_star(name)`` 返回的 ``StarMetadata``，按
       ``star_cls`` / ``star`` / ``instance`` / ``star_instance`` / ``plugin``
       顺序取运行中实例（AstrBot 4.x 用 ``star_cls``，实例挂在 metadata 上）；
    3. 都拿不到 → ``None``。

    每一步独立 try/except：异常、``None``、以及只拿到类对象（class 而非实例）
    都视为该层不可用，继续下一层，最终 fail-closed。真正的契约校验仍然交给
    :func:`_compatible`，本函数只负责取实例。
    """
    instance = _as_instance(
        await _await_candidate(_call_star_getter(context, "get_star_instance"))
    )
    if instance is not None:
        return instance
    metadata = await _await_candidate(
        _call_star_getter(context, "get_registered_star")
    )
    return _metadata_instance(metadata)


def _resolve_router_plugin_if_sync(context: Any) -> Any | None:
    """同步版本的 :func:`_resolve_router_plugin`（只读状态属性专用）。

    同步路径不能 await：某一层的接口返回 awaitable 时跳过该层继续回退（并关掉
    协程避免 "never awaited"），绝不在同步函数里跑事件循环。
    """
    instance = _as_instance(
        _sync_candidate(_call_star_getter(context, "get_star_instance"))
    )
    if instance is not None:
        return instance
    metadata = _sync_candidate(_call_star_getter(context, "get_registered_star"))
    return _metadata_instance(metadata)


def _call_star_getter(context: Any, name: str) -> Any | None:
    """调用官方星标查询接口，接口缺失或抛异常一律返回 ``None``。"""
    try:
        getter = getattr(context, name, None)
    except Exception:
        return None
    if not callable(getter):
        return None
    try:
        return getter(ROUTER_PLUGIN_NAME)
    except Exception:
        return None


async def _await_candidate(value: Any) -> Any | None:
    """兼容 awaitable：不是 awaitable 直接返回，await 失败视为不可用。"""
    if not inspect.isawaitable(value):
        return value
    try:
        return await value
    except Exception:
        return None


def _sync_candidate(value: Any) -> Any | None:
    """同步路径取值：awaitable 不可同步消费，关闭并视为不可用。"""
    if not inspect.isawaitable(value):
        return value
    _close_awaitable(value)
    return None


def _as_instance(value: Any) -> Any | None:
    """只接受真实实例：``None`` 与类对象（class 而非实例）都不可用。"""
    if value is None or inspect.isclass(value):
        return None
    return value


def _metadata_instance(metadata: Any) -> Any | None:
    """从 ``StarMetadata`` 逐属性取运行中实例，取不到返回 ``None``。"""
    metadata = _as_instance(metadata)
    if metadata is None:
        return None
    for attribute in _STAR_INSTANCE_ATTRIBUTES:
        try:
            candidate = getattr(metadata, attribute, None)
        except Exception:
            continue
        instance = _as_instance(candidate)
        if instance is not None:
            return instance
    return None


async def _resolve_validated_route(context: Any, kind: str) -> dict[str, Any] | None:
    """Fetch and validate one core route, or ``None`` when unusable."""
    kind = _normalize_kind(kind)
    if not kind:
        return None
    plugin = await _resolve_router_plugin(context)
    if plugin is None or not _compatible(plugin):
        return None
    resolver = getattr(plugin, "resolve_model_route", None)
    if not callable(resolver):
        return None
    try:
        try:
            route = resolver(kind, plugin_override=None)
        except TypeError:
            # 旧版/精简版核只接受 kind 位置参数。
            route = resolver(kind)
        if inspect.isawaitable(route):
            route = await route
    except Exception:
        return None
    return _validated_route(context, kind, route)


def _resolve_validated_route_sync(context: Any, kind: str) -> dict[str, Any] | None:
    """Best-effort synchronous route resolution for sync status properties."""
    kind = _normalize_kind(kind)
    if not kind:
        return None
    plugin = _resolve_router_plugin_if_sync(context)
    if plugin is None or not _compatible(plugin):
        return None
    resolver = getattr(plugin, "resolve_model_route", None)
    if not callable(resolver):
        return None
    try:
        try:
            route = resolver(kind, plugin_override=None)
        except TypeError:
            route = resolver(kind)
        if inspect.isawaitable(route):
            _close_awaitable(route)
            return None
    except Exception:
        return None
    return _validated_route(context, kind, route)


def _validated_route(
    context: Any, kind: str, route: Any
) -> dict[str, Any] | None:
    if not isinstance(route, dict):
        return None
    if (
        route.get("kind") != kind
        or route.get("source") != "core"
        or route.get("available") is not True
    ):
        return None
    provider_id = _text(route.get("provider_id"))
    if not provider_id:
        return None
    provider_getter = getattr(context, "get_provider_by_id", None)
    if callable(provider_getter):
        try:
            if provider_getter(provider_id) is None:
                return None
        except Exception:
            return None
    return {**route, "provider_id": provider_id}


def _normalize_kind(kind: Any) -> str:
    if not isinstance(kind, str):
        return ""
    return kind.strip()


def _text(value: Any, limit: int = 256) -> str:
    """Return a trimmed string field, ignoring non-string values."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _compatible(plugin: Any) -> bool:
    declare = getattr(plugin, "series_model_router_contract", None)
    if not callable(declare):
        return False
    try:
        contract = declare()
        if inspect.isawaitable(contract):
            _close_awaitable(contract)
            return False
    except Exception:
        return False
    if not isinstance(contract, dict):
        return False
    try:
        return (
            contract.get("name") == ROUTER_CONTRACT_NAME
            and str(contract.get("version") or "").split(".", 1)[0]
            == ROUTER_CONTRACT_MAJOR
            and contract.get("read_only") is True
            and isinstance(contract.get("capabilities"), (list, tuple, set))
            and "resolve" in contract["capabilities"]
        )
    except Exception:
        return False


def _close_awaitable(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
