from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PAGE_ROOT = PLUGIN_ROOT / "pages" / "operator"


def test_operator_page_is_discoverable_and_uses_page_bridge() -> None:
    metadata = json.loads(
        (PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["pages"]["operator"] == {
        "title": "Quest 角色设置",
        "description": "控制 Quest 服务并设置 AstrBot 消息平台、人格、聊天模型与关系自然人",
    }

    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<script src="/api/plugin/page/bridge-sdk.js"></script>' in html
    assert '<script type="module" src="./app.js"></script>' in html
    assert html.index("bridge-sdk.js") < html.index("./app.js")
    assert "凝心溯溪-临｜Quest 角色设置" in html
    assert 'id="startup-error"' in html
    assert 'role="alert"' in html


def test_operator_page_exposes_only_safe_model_and_identity_workflows() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")

    for element_id in (
        "service-status-badge",
        "refresh-service-button",
        "service-control-button",
        "active-session-count",
        "attached-stream-count",
        "queued-event-count",
        "chat-provider-id",
        "save-model-button",
        "trusted-platform-id",
        "save-platform-button",
        "persona-source-mode",
        "astrbot-persona-id",
        "character-name",
        "character-self-reference",
        "character-user-relationship",
        "character-self-description",
        "save-persona-button",
        "quest-client-id",
        "quest-bot-id",
        "quest-user-id",
        "quest-api-key",
        "quest-identity-status",
        "save-quest-identity-button",
        "load-identity-candidates",
        "relationship-person-select",
        "save-identity-button",
        "load-diagnostics",
        "diagnostics-status",
        "diagnostics-root-cause",
        "diagnostics-summary",
        "diagnostics-events",
    ):
        assert f'id="{element_id}"' in html

    assert 'apiGet("pairing/service-status")' in js
    assert 'apiPost("pairing/service-control", { enabled })' in js
    assert "关闭服务会断开当前 Quest 会话" in js
    for capability in (
        "dialogue",
        "eventbus",
        "identity_configured",
        "stt",
        "tts",
        "avatar_actions",
    ):
        assert f'data-capability="{capability}"' in html
    assert 'apiGet("pairing/operator-settings")' in js
    assert 'apiPost("pairing/operator-settings"' in js
    assert 'apiGet("pairing/platform-settings")' in js
    assert 'apiPost("pairing/platform-settings"' in js
    assert "platformSettings.platforms" in js
    assert 'select id="trusted-platform-id"' in html
    assert 'input id="trusted-platform-id"' not in html
    assert r"\u666e\u901a\u5bf9\u8bdd\u6682\u4e0d\u53ef\u7528" in js
    assert r"\u56de\u9000\u5230\u76f4\u63a5 Provider" not in js
    assert 'apiGet("pairing/persona-settings")' in js
    assert 'apiPost("pairing/persona-settings"' in js
    assert "persona_source_mode" in js
    assert "astrbot_persona_id" in js
    assert "personaSettings.personas" in js
    assert 'apiGet("pairing/quest-identity-settings")' in js
    assert 'apiPost("pairing/quest-identity-settings"' in js
    assert 'id="quest-api-key" type="password"' in html
    assert 'autocomplete="new-password"' in html
    assert "由“序”统一管理" in js
    assert "本地精确绑定" in js
    assert 'apiGet("pairing/identity-candidates")' in js
    assert 'apiPost("pairing/identity-selection"' in js
    assert "provider.id" in js
    assert "provider?.model" in js
    assert "candidate.display_name" in js
    assert "candidate.person_id" in js
    assert "candidate.account_count" in js
    assert "不包含平台 UID、Bot ID 或 UMO" in html
    assert "自然人绑定不会授予权限" in html
    assert 'apiGet("pairing/diagnostics")' in js
    assert "owner_not_configured" in js
    assert "当前根因" in js
    assert "renderDiagnosticEvents" in js
    assert "JSON.stringify" not in js
    assert ".diagnostic-line.status-failed" in css
    assert "阶段时间线（最新在下）" in html
    assert 'button[aria-busy="true"]' in css
    assert ".capability-strip" in css
    assert ".status-badge.running" in css
    assert "@media (max-width: 820px)" in css
    assert "prefers-reduced-motion" in css


def test_operator_page_does_not_expose_secrets_or_private_relationship_storage() -> (
    None
):
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    combined = html + js

    for forbidden in (
        "bridge_api_key",
        "astrbot_api_key",
        "provider_config",
        "/api/v1/providers",
        "identity_registry",
        "_page_identities",
        "/astrbot_plugin_relationship/identities",
        "trusted_client_id",
        "localStorage",
        "console.log",
        "system_prompt",
        "begin_dialogs",
        "private-tool",
    ):
        assert forbidden not in combined

    assert "http://" not in combined.lower()
    assert "https://" not in combined.lower()
