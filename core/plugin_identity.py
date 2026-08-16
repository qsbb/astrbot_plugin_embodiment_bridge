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
BRIDGE_PROTECTED_CONTEXT_AUTHORIZED = (
    f"{BRIDGE_EVENT_MARKER}.protected_context_authorized"
)
BRIDGE_SPATIAL_CONTEXT = f"{BRIDGE_EVENT_MARKER}.spatial_context"
BRIDGE_FAST_ACTION_ACTIVE = f"{BRIDGE_EVENT_MARKER}.fast_action_active"
BRIDGE_FAST_ACTION_FEEDBACK = f"{BRIDGE_EVENT_MARKER}.fast_action_feedback"
BRIDGE_ACTION_FACTS = f"{BRIDGE_EVENT_MARKER}.action_facts"
BRIDGE_SUPPORTED_ACTIONS = f"{BRIDGE_EVENT_MARKER}.supported_actions"
BRIDGE_FAST_ACTION_EXPLICIT = f"{BRIDGE_EVENT_MARKER}.fast_action_explicit"
BRIDGE_FAST_ACTION_EVENT_SELECTED = f"{BRIDGE_EVENT_MARKER}.fast_action_event_selected"
BRIDGE_FAST_ACTION_SELECTED = f"{BRIDGE_EVENT_MARKER}.fast_action_selected"
BRIDGE_TEXT_REPLY_REQUIRED = f"{BRIDGE_EVENT_MARKER}.text_reply_required"

BRIDGE_AUTH_HEADER = "X-Embodiment-Bridge-Key"
LEGACY_BRIDGE_AUTH_HEADER = "X-Quest-Avatar-Key"

