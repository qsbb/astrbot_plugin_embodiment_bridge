from __future__ import annotations

import asyncio
from typing import Any

from astrbot_plugin_embodiment_bridge.adapters.model_router import (
    resolve_model_route,
    resolve_model_route_if_sync,
    resolve_provider_id,
    resolve_provider_id_if_sync,
)


CONTRACT = {
    "name": "series.model_router@1.0",
    "version": "1.1",
    "read_only": True,
    "capabilities": ("resolve", "status"),
}


def core_route(**overrides: Any) -> dict[str, Any]:
    route = {
        "source": "core",
        "available": True,
        "provider_id": "core-provider",
    }
    route.update(overrides)
    return route


class Router:
    def __init__(
        self,
        route: dict[str, Any] | None = None,
        *,
        contract: Any = CONTRACT,
        resolve_error: Exception | None = None,
    ) -> None:
        self.route = route if route is not None else core_route()
        self.contract = contract
        self.resolve_error = resolve_error
        self.kinds: list[str] = []

    def series_model_router_contract(self) -> Any:
        if isinstance(self.contract, Exception):
            raise self.contract
        return self.contract

    def resolve_model_route(self, kind: str, **kwargs: Any) -> Any:
        del kwargs
        self.kinds.append(kind)
        if self.resolve_error is not None:
            raise self.resolve_error
        return {**self.route, "kind": kind}


class LegacyRouter(Router):
    def resolve_model_route(self, kind: str) -> Any:
        self.kinds.append(kind)
        if self.resolve_error is not None:
            raise self.resolve_error
        return {**self.route, "kind": kind}


class Context:
    def __init__(
        self,
        router: Any,
        *,
        providers: tuple[str, ...] = ("core-provider",),
        async_getter: bool = False,
    ) -> None:
        self.router = router
        self.provider_ids = set(providers)
        self.async_getter = async_getter

    def _get_star_instance(self, plugin_name: str) -> Any:
        if plugin_name != "astrbot_plugin_update_manager":
            return None
        return self.router

    def get_star_instance(self, plugin_name: str) -> Any:
        if self.async_getter:
            return self._get_star_instance_async(plugin_name)
        return self._get_star_instance(plugin_name)

    async def _get_star_instance_async(self, plugin_name: str) -> Any:
        return self._get_star_instance(plugin_name)

    def get_provider_by_id(self, provider_id: str) -> Any:
        return object() if provider_id in self.provider_ids else None


def test_resolves_contract_1_1_and_exposes_model_and_voice() -> None:
    router = Router(
        core_route(
            provider_id="core-chat",
            model="chat-mini",
            voice="voice-a",
            fallback_from="plugin",
        )
    )
    context = Context(router, providers=("core-chat",))

    async def scenario() -> tuple[str, dict[str, Any]]:
        return (
            await resolve_provider_id(context, "conversation"),
            await resolve_model_route(context, "conversation"),
        )

    provider_id, route = asyncio.run(scenario())
    assert provider_id == "core-chat"
    assert route == {
        "provider_id": "core-chat",
        "model": "chat-mini",
        "voice": "voice-a",
        "source": "core",
        "available": True,
        "fallback_from": "plugin",
    }
    assert router.kinds == ["conversation", "conversation"]


def test_accepts_async_getter_and_async_resolver() -> None:
    class AsyncRouter(Router):
        async def resolve_model_route(self, kind: str, **kwargs: Any) -> Any:
            del kwargs
            self.kinds.append(kind)
            return {**self.route, "kind": kind}

    router = AsyncRouter(core_route(provider_id="async-core"))
    context = Context(
        router,
        providers=("async-core",),
        async_getter=True,
    )

    assert asyncio.run(resolve_provider_id(context, "stt")) == "async-core"


def test_accepts_legacy_resolver_signature() -> None:
    router = LegacyRouter(core_route(provider_id="legacy-core"))
    context = Context(router, providers=("legacy-core",))

    assert asyncio.run(resolve_provider_id(context, "tts")) == "legacy-core"
    assert router.kinds == ["tts"]


def test_rejects_missing_router_and_bad_contracts() -> None:
    bad_contracts = (
        {**CONTRACT, "name": "series.other@1.0"},
        {**CONTRACT, "version": "2.0"},
        {**CONTRACT, "read_only": False},
        {**CONTRACT, "capabilities": ("status",)},
        {**CONTRACT, "capabilities": "resolve"},
        None,
    )
    assert asyncio.run(resolve_provider_id(Context(None), "conversation")) == ""
    for contract in bad_contracts:
        router = Router(core_route(), contract=contract)
        assert asyncio.run(resolve_provider_id(Context(router), "conversation")) == ""


def test_rejects_source_availability_kind_and_stale_provider() -> None:
    invalid_routes = (
        core_route(source="astrbot"),
        core_route(source="plugin"),
        core_route(available=False),
        core_route(available="true"),
        core_route(provider_id=""),
    )
    for route in invalid_routes:
        router = Router(route)
        context = Context(router, providers=("core-provider", "other"))
        assert asyncio.run(resolve_model_route(context, "conversation")) == {}

    mismatch = Router(core_route())
    mismatch.resolve_model_route = lambda kind, **kwargs: {  # type: ignore[method-assign]
        **core_route(),
        "kind": "tts",
    }
    assert asyncio.run(resolve_model_route(Context(mismatch), "conversation")) == {}

    stale = Context(Router(core_route()), providers=("other",))
    assert asyncio.run(resolve_provider_id(stale, "conversation")) == ""


def test_swallows_contract_and_resolver_exceptions() -> None:
    broken_contract = Router(core_route(), contract=RuntimeError("broken"))
    broken_resolver = Router(core_route(), resolve_error=RuntimeError("broken"))
    assert asyncio.run(
        resolve_provider_id(Context(broken_contract), "conversation")
    ) == ""
    assert asyncio.run(
        resolve_provider_id(Context(broken_resolver), "conversation")
    ) == ""


def test_rejects_blank_kind() -> None:
    for kind in ("", "   ", None):
        assert asyncio.run(resolve_provider_id(Context(Router()), kind)) == ""
        assert asyncio.run(resolve_model_route(Context(Router()), kind)) == {}


def test_sync_fast_path_matches_sync_core_contract() -> None:
    router = Router(core_route(provider_id="sync-core", model="sync-model"))
    context = Context(router, providers=("sync-core",))

    assert resolve_provider_id_if_sync(context, "conversation") == "sync-core"
    assert resolve_model_route_if_sync(context, "conversation") == {
        "provider_id": "sync-core",
        "model": "sync-model",
        "voice": "",
        "source": "core",
        "available": True,
        "fallback_from": "",
    }


def test_sync_fast_path_fails_closed_for_async_layers() -> None:
    context = Context(Router(), async_getter=True)
    assert resolve_provider_id_if_sync(context, "conversation") == ""
    assert resolve_model_route_if_sync(context, "conversation") == {}


# --------------------------------------------------------------------------- #
# StarMetadata 回退（官方 API：get_registered_star + star_cls）
# --------------------------------------------------------------------------- #


class StarMetadataStub:
    """最小 StarMetadata 替身：只暴露 AstrBot 4.x 使用的实例属性。"""

    def __init__(self, **values: Any) -> None:
        for attribute in (
            "star_cls",
            "star",
            "instance",
            "star_instance",
            "plugin",
        ):
            setattr(self, attribute, values.get(attribute))


class RegistryContext:
    """只暴露官方 ``get_registered_star`` 的上下文（没有 get_star_instance）。"""

    def __init__(
        self,
        metadata: Any = None,
        *,
        providers: tuple[str, ...] = ("core-provider",),
        awaitable: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.metadata = metadata
        self.provider_ids = set(providers)
        self.awaitable = awaitable
        self.error = error
        self.requested: list[str] = []

    def get_registered_star(self, star_name: str) -> Any:
        self.requested.append(star_name)
        if self.error is not None:
            raise self.error
        if self.awaitable:
            return self._metadata_async()
        return self.metadata

    async def _metadata_async(self) -> Any:
        return self.metadata

    def get_provider_by_id(self, provider_id: str) -> Any:
        return object() if provider_id in self.provider_ids else None


class InstanceFirstContext(RegistryContext):
    """同时暴露 ``get_star_instance``，用于验证优先级与 awaitable 兼容。"""

    def __init__(
        self,
        instance: Any,
        metadata: Any = None,
        *,
        instance_awaitable: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(metadata, **kwargs)
        self.instance = instance
        self.instance_awaitable = instance_awaitable
        self.instance_calls = 0
        self.coroutines: list[Any] = []

    def get_star_instance(self, star_name: str) -> Any:
        self.instance_calls += 1
        if isinstance(self.instance, Exception):
            raise self.instance
        if self.instance_awaitable:
            coroutine = self._instance_async()
            self.coroutines.append(coroutine)
            return coroutine
        return self.instance

    async def _instance_async(self) -> Any:
        return self.instance


def test_registered_star_fallback_resolves_running_instance() -> None:
    router = Router(core_route(provider_id="registry-core", model="registry-model"))
    context = RegistryContext(
        StarMetadataStub(star_cls=router), providers=("registry-core",)
    )

    assert asyncio.run(resolve_provider_id(context, "conversation")) == "registry-core"
    route = asyncio.run(resolve_model_route(context, "conversation"))
    assert route["model"] == "registry-model"
    assert context.requested == ["astrbot_plugin_update_manager"] * 2


def test_registered_star_may_be_awaitable() -> None:
    router = Router(core_route(provider_id="registry-core"))
    context = RegistryContext(
        StarMetadataStub(star_cls=router),
        providers=("registry-core",),
        awaitable=True,
    )

    assert asyncio.run(resolve_provider_id(context, "conversation")) == "registry-core"


def test_get_star_instance_precedes_registered_star() -> None:
    direct = Router(core_route(provider_id="direct-core"))
    fallback = Router(core_route(provider_id="registry-core"))
    context = InstanceFirstContext(
        direct,
        StarMetadataStub(star_cls=fallback),
        providers=("direct-core", "registry-core"),
    )

    assert asyncio.run(resolve_provider_id(context, "conversation")) == "direct-core"
    assert context.instance_calls == 1
    # 首层命中后不再读取 StarMetadata
    assert context.requested == []


def test_star_metadata_attribute_fallback_order() -> None:
    first = Router(core_route(provider_id="first-core"))
    second = Router(core_route(provider_id="second-core"))

    both = RegistryContext(
        StarMetadataStub(star_cls=first, star=second),
        providers=("first-core", "second-core"),
    )
    assert asyncio.run(resolve_provider_id(both, "conversation")) == "first-core"

    # star_cls 为空时按序取下一个别名
    alias = RegistryContext(
        StarMetadataStub(instance=second), providers=("second-core",)
    )
    assert asyncio.run(resolve_provider_id(alias, "conversation")) == "second-core"


def test_router_resolution_fails_closed_without_usable_instance() -> None:
    # get_registered_star 返回 None / 抛异常 → 没有可用实例
    assert asyncio.run(resolve_provider_id(RegistryContext(None), "conversation")) == ""
    assert (
        asyncio.run(
            resolve_provider_id(
                RegistryContext(None, error=RuntimeError("boom")), "conversation"
            )
        )
        == ""
    )
    # 元数据属性只给类对象（class 而非实例）→ 跳过
    assert (
        asyncio.run(
            resolve_provider_id(
                RegistryContext(StarMetadataStub(star_cls=Router)), "conversation"
            )
        )
        == ""
    )
    # get_star_instance 只给类对象 → 不得命中
    assert (
        asyncio.run(resolve_provider_id(InstanceFirstContext(Router, None), "conversation"))
        == ""
    )


def test_broken_instance_getter_falls_back_to_registered_star() -> None:
    for instance in (None, RuntimeError("boom")):
        fallback = Router(core_route(provider_id="registry-core"))
        context = InstanceFirstContext(
            instance,
            StarMetadataStub(star_cls=fallback),
            providers=("registry-core",),
        )
        assert asyncio.run(resolve_provider_id(context, "conversation")) == (
            "registry-core"
        )
        assert context.instance_calls == 1


def test_sync_path_uses_sync_registered_star() -> None:
    router = Router(core_route(provider_id="sync-core", model="sync-model"))
    context = RegistryContext(
        StarMetadataStub(star_cls=router), providers=("sync-core",)
    )

    assert resolve_provider_id_if_sync(context, "conversation") == "sync-core"
    assert resolve_model_route_if_sync(context, "conversation") == {
        "provider_id": "sync-core",
        "model": "sync-model",
        "voice": "",
        "source": "core",
        "available": True,
        "fallback_from": "",
    }


def test_sync_path_fails_closed_for_awaitable_metadata() -> None:
    context = RegistryContext(StarMetadataStub(star_cls=Router()), awaitable=True)

    assert resolve_provider_id_if_sync(context, "conversation") == ""
    assert resolve_model_route_if_sync(context, "conversation") == {}


def test_sync_path_skips_awaitable_instance_getter_without_running_it() -> None:
    fallback = Router(core_route(provider_id="sync-core"))
    context = InstanceFirstContext(
        fallback,
        StarMetadataStub(star_cls=fallback),
        instance_awaitable=True,
        providers=("sync-core",),
    )

    assert resolve_provider_id_if_sync(context, "conversation") == "sync-core"
    # 该层的协程被关闭（cr_frame 为 None），没有在同步函数里跑事件循环
    assert context.coroutines
    assert all(coroutine.cr_frame is None for coroutine in context.coroutines)
