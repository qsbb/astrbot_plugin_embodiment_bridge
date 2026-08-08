from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PAGE_ROOT = PLUGIN_ROOT / "pages" / "pairing"


def test_pairing_page_is_discoverable_and_uses_page_bridge() -> None:
    metadata = json.loads(
        (PLUGIN_ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["pages"]["pairing"]["title"] == "Quest 快速绑定"
    assert metadata["pages"]["pairing"]["description"] == (
        "生成一次性二维码或 6 位配对码，在 Quest 中完成绑定"
    )

    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<script src="/api/plugin/page/bridge-sdk.js"></script>' in html
    assert '<script type="module" src="./app.js"></script>' in html
    assert html.index("bridge-sdk.js") < html.index("./app.js")
    assert 'name="viewport"' in html
    assert "凝心溯溪-临｜Quest 快速绑定" in html
    assert 'id="startup-error"' in html
    assert 'role="alert"' in html


def test_pairing_page_contains_complete_safe_workflow() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")

    for element_id in (
        "generate-button",
        "qr-image",
        "short-code",
        "countdown",
        "copy-code",
        "revoke",
    ):
        assert f'id="{element_id}"' in html

    for removed_id in (
        "chat-provider-id",
        "relationship-person-select",
        "public-url",
        "port",
        "expected-remote-ip",
        "allow-insecure-http",
        "astrbot-api-key",
        "trusted-platform",
        "client-id",
        "user-id",
        "bot-id",
        "group-id",
        "relationship-profile-id",
        "ttl",
    ):
        assert f'id="{removed_id}"' not in html

    for removed_label in (
        "Quest IP",
        "AstrBot API Key",
        "平台身份",
        "客户端 ID",
        "用户 ID",
        "机器人 ID",
        "群组 ID",
        "关系档案 ID",
        "有效期",
    ):
        assert removed_label not in html

    assert "二维码不包含长期密钥，兑换后立即失效" in html
    assert "PAIR BACKEND" in html + js
    assert 'apiPost("pairing/create", {' in js
    assert 'protocol_version: "1.0"' in js
    assert 'apiPost("pairing/status"' in js
    assert 'apiPost("pairing/revoke"' in js
    assert "pairing/exchange" not in js
    assert "pairing/operator-settings" not in js
    assert "pairing/identity-candidates" not in js
    assert "pairing/identity-selection" not in js
    assert "astrbot_api_key" not in html + js
    assert "expected_remote_ip" not in html + js
    assert "trusted_client_id" not in html + js
    assert "trusted_platform_id" not in html + js
    assert "pairing/platform-settings" not in html + js
    assert "localStorage" not in js
    assert "quick_pairing_ready" in js
    assert "qr_svg_data_uri" in js
    assert "window.setInterval" in js
    assert "function setButtonBusy" in js
    assert 'aria-busy="false"' in html
    assert 'button[aria-busy="true"]' in css
    assert 'const normalizedState = knownStates.has(state) ? state : "unknown"' in js
    assert "配对状态暂时无法识别，请重新生成配对码" in js
    assert "label.className = state" not in js
    assert 'setButtonBusy(button, true, "正在复制…")' in js
    assert 'setButtonBusy(button, true, "正在撤销…")' in js
    assert "finally" in js
    assert "@media (max-width: 900px)" in css
    assert "@media (max-width: 620px)" in css
    assert "prefers-reduced-motion" in css


def test_pairing_page_has_no_external_runtime_assets_or_embedded_secrets() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    assert "cdn." not in html.lower()
    assert "http://" not in html.lower()
    assert "bridge_api_key" not in html
    assert "token=" not in html
    assert "console.log" not in js
    for forbidden in (
        "provider_config",
        "/api/v1/providers",
        "identity_registry",
        "_page_identities",
        "/astrbot_plugin_relationship/identities",
    ):
        assert forbidden not in html + js


def test_pairing_page_documentation_explains_busy_and_unknown_states() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    assert "对应按钮会暂时禁用" in readme
    assert "尚不认识的配对状态" in readme
