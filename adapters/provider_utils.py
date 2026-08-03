from __future__ import annotations

from typing import Any


def find_active_provider(context: Any, plugin_name: str) -> Any | None:
    """Return one explicitly named active plugin instance.

    Cross-plugin callers still have to validate the provider's declared contract
    before invoking its public method.  Missing plugins are a supported deployment
    shape and therefore return ``None``.
    """

    for metadata in context.get_all_stars():
        if metadata.name == plugin_name and metadata.activated:
            return metadata.star_cls
    return None


def contract_matches(
    contract: Any,
    *,
    name: str,
    major: str,
    capability: str,
    method: str | None = None,
) -> bool:
    if not isinstance(contract, dict):
        return False
    version = str(contract.get("version") or "")
    capabilities = contract.get("capabilities")
    if (
        contract.get("name") != name
        or version.split(".", 1)[0] != major
        or not isinstance(capabilities, (list, tuple))
        or capability not in capabilities
    ):
        return False
    return method is None or contract.get("method") == method
