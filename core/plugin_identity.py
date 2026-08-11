from __future__ import annotations


PLUGIN_ID = "astrbot_plugin_embodiment_bridge"
LEGACY_PLUGIN_ID = "astrbot_plugin_quest_avatar_bridge"
PLUGIN_DISPLAY_NAME = "临"

ROUTE_PREFIX = f"/{PLUGIN_ID}"
LEGACY_ROUTE_PREFIX = f"/{LEGACY_PLUGIN_ID}"
PUBLIC_API_PREFIX = f"/api/v1/plugins/extensions/{PLUGIN_ID}"
LEGACY_PUBLIC_API_PREFIX = f"/api/v1/plugins/extensions/{LEGACY_PLUGIN_ID}"

BRIDGE_EVENT_MARKER = "embodiment_bridge"
LEGACY_BRIDGE_EVENT_MARKER = "quest_avatar_bridge"
BRIDGE_IDENTITY_CONTEXT = f"{BRIDGE_EVENT_MARKER}.identity_context"
LEGACY_BRIDGE_IDENTITY_CONTEXT = f"{LEGACY_BRIDGE_EVENT_MARKER}.identity_context"

BRIDGE_AUTH_HEADER = "X-Embodiment-Bridge-Key"
LEGACY_BRIDGE_AUTH_HEADER = "X-Quest-Avatar-Key"

