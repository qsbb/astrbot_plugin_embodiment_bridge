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
        "description": "选择聊天模型并从情读取自然人候选",
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
        "chat-provider-id",
        "save-model-button",
        "load-identity-candidates",
        "relationship-person-select",
        "save-identity-button",
    ):
        assert f'id="{element_id}"' in html

    assert 'apiGet("pairing/operator-settings")' in js
    assert 'apiPost("pairing/operator-settings"' in js
    assert 'apiGet("pairing/identity-candidates")' in js
    assert 'apiPost("pairing/identity-selection"' in js
    assert "provider.id" in js
    assert "provider?.model" in js
    assert "candidate.display_name" in js
    assert "candidate.person_id" in js
    assert "candidate.account_count" in js
    assert "不包含平台 UID、Bot ID 或 UMO" in html
    assert "自然人绑定不会授予权限" in html
    assert 'button[aria-busy="true"]' in css
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
        "trusted_platform_id",
        "localStorage",
        "console.log",
    ):
        assert forbidden not in combined

    assert "http://" not in combined.lower()
    assert "https://" not in combined.lower()
