from __future__ import annotations

import re
from typing import Any


_CONTRACT_VERSION_RE = re.compile(r"^(?P<major>[1-9][0-9]*)\.(?P<minor>[0-9]+)$")


def find_active_provider(context: Any, plugin_name: str) -> Any | None:
    """Return one explicitly named active plugin instance.

    Cross-plugin callers still have to validate the provider's declared contract
    before invoking its public method.  Missing plugins are a supported deployment
    shape and therefore return ``None``.
    """

    try:
        providers = context.get_all_stars()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(providers, (list, tuple)):
        return None
    for metadata in providers:
        try:
            if metadata.name == plugin_name and metadata.activated:
                return metadata.star_cls
        except AttributeError:
            continue
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
    version_match = _CONTRACT_VERSION_RE.fullmatch(version)
    capabilities = contract.get("capabilities")
    if (
        contract.get("name") != name
        or version_match is None
        or version_match.group("major") != major
        or not isinstance(capabilities, (list, tuple))
        or capability not in capabilities
    ):
        return False
    return method is None or contract.get("method") == method
