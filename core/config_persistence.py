from __future__ import annotations

import asyncio
from typing import Any


def config_is_writable(config: Any) -> bool:
    """Return whether this AstrBot config exposes a supported persistence API."""

    return callable(getattr(config, "save_config_async", None)) or callable(
        getattr(config, "save_config", None)
    )


async def save_config_changes(config: Any, changes: dict[str, Any]) -> bool:
    """Persist changes through the newest API available on the running Core."""

    previous: dict[str, tuple[bool, Any]] = {}
    for key in changes:
        try:
            exists = key in config
        except TypeError:
            exists = False
        try:
            value = config.get(key)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            value = None
        previous[key] = (exists, value)

    save_async = getattr(config, "save_config_async", None)
    try:
        if callable(save_async):
            return await save_async(dict(changes))

        save_sync = getattr(config, "save_config", None)
        if not callable(save_sync):
            raise RuntimeError("AstrBot config persistence API is unavailable")

        # AstrBot 4.26.5 writes through a temporary file and os.replace(), but
        # only exposes a synchronous API. Moving that operation off-loop keeps
        # live Quest sessions responsive while the caller's lock serializes it.
        await asyncio.to_thread(save_sync, dict(changes))
        return True
    except Exception:
        for key, (existed, value) in previous.items():
            if existed:
                config[key] = value
            else:
                config.pop(key, None)
        raise
