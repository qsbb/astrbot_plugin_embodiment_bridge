from __future__ import annotations

import json
import re
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
        "title": "具身服务控制台",
        "description": "控制具身客户端服务：配对绑定、桥接平台、人格、聊天模型与关系自然人",
    }

    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<script src="/api/plugin/page/bridge-sdk.js"></script>' in html
    assert '<script type="module" src="./app.js?v=1.4.0-3"></script>' in html
    assert '<link rel="stylesheet" href="./style.css?v=1.4.0-3" />' in html
    assert html.index("bridge-sdk.js") < html.index("./app.js?v=1.4.0-3")
    assert "凝心溯溪-临｜具身服务控制台" in html
    assert 'id="startup-error"' in html
    assert 'role="alert"' in html


def test_operator_page_asset_cache_busters_follow_plugin_version() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    metadata = (PLUGIN_ROOT / "metadata.yaml").read_text(encoding="utf-8")
    version_match = re.search(r"^version:\s*([^\s]+)\s*$", metadata, re.MULTILINE)
    assert version_match is not None
    cache_busters = re.findall(
        r'\./(?:app\.js|style\.css)\?v=([^"\s]+)',
        html,
    )
    assert len(cache_busters) == 2
    assert len(set(cache_busters)) == 1
    assert cache_busters[0].startswith(version_match.group(1) + "-")


def test_operator_page_labels_fast_action_and_eventbus_terminal_states() -> None:
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    # 里程碑 3 起错误码→中文查表下沉到 core/diagnostic_labels.py，由后端在
    # 诊断投影时以 *_label 加法字段下发；前端只渲染 label 并保留码值回退。
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    assert '"fast_action.parsed_no_action"' in labels
    assert '"fast_action.parse_invalid"' in labels
    assert '"fast_action.local_fallback_selected"' in labels
    assert '"fast_action.cancelled"' in labels
    assert '"autonomous_greeting":' in labels
    assert '"autonomous_gesture_cooldown":' in labels
    assert "def action_label(" in labels
    assert "def action_source_label(" in labels
    assert '"local_context_fallback": "本地社交动作兜底"' in labels
    assert '"message_pipeline.stopped_after_fast_action"' in labels
    assert '"message_pipeline.required_reply_missing"' in labels
    assert '"avatar.action.tool_superseded"' in labels
    assert '"astrbot_pipeline_reply_required_missing":' in labels
    assert '"fast_action_invalid_output":' in labels
    assert '"astrbot_pipeline_event_stopped":' in labels
    # 前端渲染层：label 优先、码值兜底。
    assert "event.event_label || event.event" in js
    assert "event.status_label ||" in js
    assert "event.reason_label || reason" in js
    assert "event.operation_label || event.operation" in js
    assert "event.action_source_label || event.action_source" in js


def test_operator_page_exposes_only_safe_model_and_identity_workflows() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    assert "function initializeBridgeAndData()" in js
    assert "function showStartupError(error)" in js
    assert "retry-startup-button" in js
    assert "retry-initial-data-button" in js
    assert "eventsBound" in js
    assert "Promise.allSettled" in js
    assert "showInitialDataError(failedSections)" in js
    assert "页面已连接，但部分设置读取失败" in js
    assert "bridgeReady = false;\n    showStartupError(error);\n    return false;" in js
    assert "await loadInitialData();" in js

    data_loader = js[
        js.index("async function loadInitialData") : js.index(
            "function showStartupError"
        )
    ]
    assert "Promise.allSettled" in data_loader
    assert "bridgeReady = false" not in data_loader

    for element_id in (
        "service-status-badge",
        "refresh-service-button",
        "service-control-button",
        "listener-port",
        "save-listener-port-button",
        "active-session-count",
        "attached-stream-count",
        "queued-event-count",
        "chat-provider-id",
        "save-model-button",
        "fast-action-enabled",
        "fast-action-provider-id",
        "fast-action-status",
        "fast-action-timeout-seconds",
        "fast-action-timeout-help",
        "save-fast-action-button",
        "stt-provider-id",
        "stt-provider-help",
        "stt-status",
        "save-stt-button",
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
        "quest-identity-badge",
        "quest-identity-basic-status",
        "quest-identity-advanced",
        "quest-identity-advanced-toggle",
        "quest-client-help",
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
        "diagnostics-auto-scroll",
    ):
        assert f'id="{element_id}"' in html

    assert 'apiGet("pairing/service-status")' in js
    assert 'apiPost("pairing/service-control", { enabled })' in js
    assert 'apiPost("pairing/listener-port", { port })' in js
    assert 'value="8520"' in html
    assert "修改端口会断开当前具身会话" in js
    assert "关闭服务会断开当前具身会话" in js
    for capability in (
        "dialogue",
        "bridge",
        "identity_configured",
        "stt",
        "tts",
        "avatar_actions",
    ):
        assert f'data-capability="{capability}"' in html
    assert 'apiGet("pairing/operator-settings")' in js
    assert 'endpoint: "pairing/operator-settings"' in js
    assert 'apiGet("pairing/fast-action-settings")' in js
    assert 'endpoint: "pairing/fast-action-settings"' in js
    assert "timeout_seconds: timeoutSeconds" in js
    assert "effective_timeout_seconds" in js
    assert "timeout_policy_revision" in js
    assert "fastActionTimeoutInputValue(fastActionSettings)" in js
    assert "settings?.timeout_migrated === true" in js
    assert "load: loadFastActionSettings" in js
    assert "异步快速动作" in html
    assert "不等待主回复链路" in html
    assert "快速通道已经接管本轮后若超时、失败或没有选出动作" in html
    assert "启动前发现模型缺失时由原有 AstrBot 主回复链路处理" in html
    assert "fast_action_timeout" in labels
    assert '"fast_action.completed": "快速动作判断完成"' in labels
    assert 'apiGet("pairing/stt-settings")' in js
    assert 'endpoint: "pairing/stt-settings"' in js
    assert "load: loadSttSettings" in js
    assert 'stt: ["stt-status", "语音识别设置读取失败，可单独重试。"]' in js
    assert "if (serviceRefreshInFlight) return serviceRefreshInFlight;" in js
    assert "if (serviceRefreshInFlight) return true;" not in js
    assert 'apiGet("pairing/platform-settings")' in js
    assert 'endpoint: "pairing/platform-settings"' in js
    assert "platformSettings.platforms" in js
    assert 'select id="trusted-platform-id"' in html
    assert 'input id="trusted-platform-id"' not in html
    assert r"\u666e\u901a\u5bf9\u8bdd\u6682\u4e0d\u53ef\u7528" in js
    assert r"\u56de\u9000\u5230\u76f4\u63a5 Provider" not in js
    assert 'apiGet("pairing/persona-settings")' in js
    assert 'endpoint: "pairing/persona-settings"' in js
    assert "persona_source_mode" in js
    assert "astrbot_persona_id" in js
    assert "personaSettings.personas" in js
    assert 'apiGet("pairing/quest-identity-settings")' in js
    assert 'endpoint: "pairing/quest-identity-settings"' in js
    assert "apiKeyPost" not in js
    assert "fetch(" not in js
    assert '"api_principal":' not in js
    assert 'id="quest-api-key" type="password"' in html
    assert 'autocomplete="new-password"' in html
    assert 'aria-describedby="quest-api-key-help"' in html
    assert "已配置时可留空" in html
    assert "AstrBot 管理后台 → 设置 → API Key 管理 → 创建 API Key" in html
    assert "至少勾选 <code>plugin</code> 权限" in html
    assert "密钥明文只显示一次" in html
    assert "已配置，可留空并重新验证" in js
    assert "由“序”统一管理" in js
    assert "本地精确绑定" in js
    assert "尚未完成基础绑定，请展开高级身份设置补充首次验证材料" in js
    assert "identity-advanced" in html
    assert "基础绑定" in html
    assert "高级身份设置" in html
    assert "设备名只用于识别这台 Quest" in html
    assert "Bot/User 由“序”根据自然人映射管理" in js
    assert "advanced.open = true" in js
    assert 'apiGet("pairing/identity-candidates")' in js
    assert 'endpoint: "pairing/identity-selection"' in js
    assert "不使用“情”的关系上下文" in js
    assert "基础对话不受影响" in js
    assert "“情”自然人（可留空）" in html
    assert "provider.id" in js
    assert "provider?.model" in js
    assert "provider?.adapter_type" in js
    assert "provider?.provider_type" in js
    assert "可切换为空值并关闭关系上下文" in js
    assert "临直连 / 交互决策模型" in html
    assert "仍使用 AstrBot 平台或会话的默认聊天模型" in html
    assert "EventBus 基础对话仍可使用 AstrBot 默认模型" in js
    assert "实时对话不可用" not in js
    assert "candidate.display_name" in js
    assert "candidate.person_id" in js
    assert "candidate.account_count" in js
    assert "原始账号不会返回页面" in html
    assert "自然人绑定不会授予权限" in html
    assert 'apiGet("pairing/diagnostics")' in js
    assert "owner_not_configured" in labels
    assert "当前根因" in js
    assert "renderDiagnosticEvents" in js
    assert "JSON.stringify(event)" not in js
    assert "JSON.stringify(diagnostics)" not in js
    assert ".diagnostic-line.status-failed" in css
    assert "阶段时间线（最新在下）" in html
    assert 'button[aria-busy="true"]' in css
    assert ".capability-strip" in css
    assert ".status-badge.running" in css
    assert ".status-badge.ready" in css
    assert ".status-badge.loading" in css
    assert ".runtime-state.warning" in css
    assert "@media (max-width: 820px)" in css
    assert "prefers-reduced-motion" in css
    assert "页面 Bridge 请求超时" in js
    assert "AstrBot 暂无普通插件通用的 STT 契约" in html
    assert "正式 STTProvider 机制注册" in html
    assert "API 地址、密钥和原始 Provider 配置" in html
    assert "legacy_private_mimo_disabled" in js
    assert "不会自动切换其他模型" in js


def test_operator_page_identity_defaults_to_basic_flow_and_keeps_sensitive_fields_advanced() -> (
    None
):
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    advanced_start = html.index('<details id="quest-identity-advanced"')
    advanced_end = html.index("</details>", advanced_start)
    basic = html[html.index('class="identity-basic-card"') : advanced_start]
    advanced = html[advanced_start:advanced_end]

    assert 'id="quest-bot-id"' not in basic
    assert 'id="quest-user-id"' not in basic
    assert 'id="quest-api-key"' not in basic
    assert 'id="quest-client-id"' in basic
    assert 'id="quest-bot-id"' in advanced
    assert 'id="quest-user-id"' in advanced
    assert 'id="quest-api-key"' in advanced
    assert "<details" in html
    assert 'type="password"' in advanced


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
        "console.log",
        "system_prompt",
        "begin_dialogs",
        "private-tool",
        "plugin_mimo_stt_api_key",
        "plugin_mimo_stt_api_base",
        "plugin_mimo_stt_model",
    ):
        assert forbidden not in combined

    assert "http://" not in combined.lower()
    assert "https://" not in combined.lower()


def test_operator_page_supports_explicit_quest_persona_conversion_workflow() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    for element_id in (
        "persona-workflow-tabs",
        "persona-mode-live",
        "persona-mode-import",
        "persona-mode-independent",
        "active-persona-name",
        "persona-converter-provider",
        "save-persona-converter-provider",
        "persona-import-source",
        "persona-profile-name",
        "persona-profile-aliases",
        "persona-admin-requirements",
        "persona-source-prompt",
        "quest-persona-prompt",
        "persona-profile-list",
        "new-persona-profile-button",
        "persona-conversion-report",
        "persona-conversion-progress",
        "cancel-persona-conversion-button",
        "persona-unresolved-warning",
        "convert-persona-button",
        "save-persona-profile-button",
        "activate-persona-profile-button",
    ):
        assert f'id="{element_id}"' in html

    assert 'apiGet("pairing/persona-library")' in js
    assert 'endpoint: "pairing/persona-converter-settings"' in js
    assert '"pairing/persona-conversion-start"' in js
    assert '"pairing/persona-conversion-status"' in js
    assert '"pairing/persona-conversion-cancel"' in js
    assert '"pairing/persona-convert"' not in js
    assert 'apiPost("pairing/persona-profile-open"' in js
    assert 'apiPost("pairing/persona-profile-save"' in js
    assert 'apiPost("pairing/persona-profile-activate"' in js
    assert 'apiPost("pairing/persona-profile-delete"' in js
    assert "source_type: sourceType" in js
    assert "source_persona_id:" in js
    assert "source_prompt:" in js
    assert "admin_requirements:" in js
    assert "conversion_report:" in js
    assert "draft_token: personaConversionDraftToken" in js
    assert "invalidatePersonaDraft" in js
    assert "await loadPersonaProfiles()" in js
    assert "实时人格来源已保存并启用" in js
    assert 'event.key === "ArrowRight"' in js
    assert "button.tabIndex = selected ? 0 : -1" in js
    assert "profiles:[" not in js
    assert "innerHTML" not in js
    assert "textContent" in js
    assert "人格已保存，但没有自动启用" in js
    assert "人格已保存，尚未启用" in js
    assert "PERSONA_CONVERSION_POLL_MS = 1000" in js
    # 转换阶段文案已随阶段码由后端 stage_label 下发（core/diagnostic_labels.py）。
    assert "job?.stage_label || job?.stage" in js
    assert "正在等待转换模型首个流块" in labels
    assert "转换模型已开始响应" in labels
    assert "转换模型正在持续生成" in labels
    assert "转换预览完成，后台任务用时" in js
    assert "人格已保存，并已立即更新当前启用的人格" in js
    assert "后端封存" in html
    assert "原人格由后端读取并封存" in html
    assert "保存并启用实时人格" in html
    assert 'maxlength="24000"' in html
    assert 'maxlength="12000"' in html
    assert 'role="tablist"' in html
    assert 'aria-label="人格来源方式"' in html
    assert ".persona-prompt-columns" in css
    assert ".conversion-report-grid" in css
    assert ".persona-profile-row.active" in css
    assert "window.sessionStorage" in js
    assert "PERSONA_CONVERSION_JOB_STORAGE_KEY" in js
    assert "restorePersonaConversionContext" in js
    assert "restorePersonaConversionEditor" in js
    assert "profile_id: document.getElementById" in js
    assert "source_persona_id: personaWorkflowMode" in js
    assert 'error.code === "conversion_job_not_found"' in js
    assert "job?.error?.message" in js
    assert "persona.convert.source.started" in labels
    assert "persona.convert.model.started" in labels
    assert "persona.convert.model.first_chunk" in labels
    assert "persona.convert.model.streaming" in labels
    assert "persona.convert.validation.started" in labels
    assert "persona.convert.draft.created" in labels
    assert "persona.convert.progress" in js
    assert "personaConversionJobSnapshot" in js
    assert "setPersonaConversionLocked(true)" in js
    assert "data-conversion-was-disabled" in js
    assert ".persona-panel.conversion-locked" in css
    assert "隐藏推理内容" in html


def test_operator_page_settings_panels_grouped_into_category_tabs() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")

    # 页面级分类标签栏：运行 / 设备与绑定 / 对话与模型 / 人格。
    assert 'id="settings-tabs"' in html
    assert 'aria-label="设置分类"' in html
    assert html.count('role="tablist"') == 2  # 页面级 + 人格工作区子 tab
    for tab_id, group in (
        ("settings-tab-runtime", "runtime"),
        ("settings-tab-devices", "devices"),
        ("settings-tab-dialogue", "dialogue"),
        ("settings-tab-persona", "persona"),
    ):
        assert f'id="{tab_id}"' in html
        assert f'data-settings-tab="{group}"' in html
        assert f'aria-controls="settings-group-{group}"' in html
        assert f'id="settings-group-{group}"' in html
        assert f'data-settings-group="{group}"' in html
        assert f'aria-labelledby="{tab_id}"' in html

    # 默认激活「运行」，其余分组初始 hidden。
    assert 'data-settings-group="runtime">' in html
    assert 'data-settings-group="runtime" hidden' not in html
    assert 'data-settings-group="devices" hidden>' in html
    assert 'data-settings-group="dialogue" hidden>' in html
    assert 'data-settings-group="persona" hidden>' in html
    assert 'id="settings-tab-runtime" class="segment active"' in html
    assert 'aria-selected="true"' in html

    # 各分组收纳的面板（按面板标题 id 判定归属）。
    group_positions = {
        group: html.index(f'id="settings-group-{group}"')
        for group in ("dialogue", "persona", "devices", "runtime")
    }
    ordered = sorted(group_positions, key=group_positions.get)
    slices = {}
    for index, group in enumerate(ordered):
        end = (
            group_positions[ordered[index + 1]]
            if index + 1 < len(ordered)
            else html.index('class="boundary-note"')
        )
        slices[group] = html[group_positions[group] : end]
    for title_id in (
        "model-title",
        "fast-action-title",
        "stt-provider-title",
        "platform-title",
        "quest-chain-title",
    ):
        assert f'aria-labelledby="{title_id}"' in slices["dialogue"]
    assert 'aria-labelledby="persona-title"' in slices["persona"]
    assert 'aria-labelledby="quest-identity-title"' in slices["devices"]
    assert 'aria-labelledby="identity-title"' in slices["devices"]
    assert 'aria-labelledby="diagnostics-title"' in slices["runtime"]
    # 人格工作区内部子 tab 保持原样（两级 tab 是预期形态）。
    assert 'id="persona-workflow-tabs"' in slices["persona"]

    # 前端绑定：点击切换 + roving 方向键导航，默认激活运行。
    assert "function setActiveSettingsGroup(group)" in js
    assert 'setActiveSettingsGroup("runtime")' in js
    assert "[data-settings-tab]" in js
    assert "[data-settings-group]" in js
    assert "panel.hidden = panel.dataset.settingsGroup !== group" in js
    assert "tab.tabIndex = selected ? 0 : -1" in js

    assert ".settings-tabs" in css
    assert ".settings-group" in css


def test_operator_diagnostics_panel_exposes_runtime_switches() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    # 三个诊断开关控件 + 保存按钮 + 状态行，收纳在诊断面板内。
    panel_start = html.index('class="setting-panel diagnostics-panel"')
    panel_end = html.index("</section>", panel_start)
    panel = html[panel_start:panel_end]
    for element_id in (
        "diagnostic-log-enabled",
        "diagnostic-plugin-timing-enabled",
        "diagnostic-platform-log-enabled",
        "save-diagnostics-settings-button",
        "diagnostics-settings-status",
    ):
        assert f'id="{element_id}"' in panel
    assert "开启后立即生效，日志写入数据目录" in panel
    assert 'class="diagnostics-switches"' in panel
    assert ".diagnostics-switches" in css

    # 前端走 saveSection 表驱动范式并注册初始加载。
    assert 'apiGet("pairing/diagnostics-settings")' in js
    assert 'endpoint: "pairing/diagnostics-settings"' in js
    assert "function renderDiagnosticsSettings(settings)" in js
    assert "async function saveDiagnosticsSettings()" in js
    assert "load: loadDiagnosticsSettings" in js
    assert 'diagnostics: ["diagnostics-settings-status", "诊断开关读取失败，可单独重试。"]' in js
    assert 'getElementById("save-diagnostics-settings-button")' in js
    assert "diagnostic_log_enabled:" in js
    assert "diagnostic_plugin_timing_enabled:" in js
    assert "diagnostic_platform_log_enabled:" in js
    assert "response.diagnostics_settings" in js

    # 保存事件的诊断标签已下沉到后端标签表。
    assert '"diagnostics.settings_updated"' in labels


def test_operator_page_exposes_integration_panel_and_direct_fallback_switch() -> (
    None
):
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    css = (PAGE_ROOT / "style.css").read_text(encoding="utf-8")
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    # ① 集成状态只读面板（审计 C-6）：位于「运行」标签、诊断面板之前。
    runtime_start = html.index('data-settings-group="runtime"')
    assert 'aria-labelledby="integrations-title"' in html[runtime_start:]
    panel_start = html.index('class="setting-panel integrations-panel"')
    panel_end = html.index("</section>", panel_start)
    panel = html[panel_start:panel_end]
    assert panel_start < html.index('class="setting-panel diagnostics-panel"')
    assert 'id="integrations-title"' in panel
    assert 'id="integrations-list"' in panel
    assert "只读展示" in panel
    assert "不含身份标识与密钥" in panel

    # 前端渲染：复用 .status-badge，label 优先、码值兜底，随服务状态轮询刷新。
    assert "function renderIntegrations(integrations)" in js
    assert "renderIntegrations(serviceState.integrations)" in js
    assert 'getElementById("integrations-list")' in js
    assert '"status-badge " + badgeClass' in js
    assert "integration?.status_label || integration?.status" in js
    assert "integration?.reason_label || integration?.reason" in js
    assert "integration?.label || integration?.name" in js
    assert ".integrations-list" in css
    assert ".status-badge.degraded" in css

    # 集成名中文映射由服务端下发（core/diagnostic_labels.py）。
    assert "def integration_label(" in labels
    assert "def integration_status_label(" in labels
    for name, label in (
        ("identity", "身份授权"),
        ("quest_enriched_pipeline", "临专属链路"),
        ("knowledge", "全局知识"),
        ("environment", "环境感知"),
        ("voice_audio_output", "“声”语音"),
        ("relationship", "“情”关系"),
        ("runtime", "运行时诊断"),
    ):
        assert f'"{name}": "{label}"' in labels

    # ② 直连回退开关（审计 C-9）：挂在「临专属链路」面板内，随保存提交。
    chain_start = html.index('aria-labelledby="quest-chain-title"')
    chain_end = html.index("</section>", chain_start)
    chain_panel = html[chain_start:chain_end]
    assert 'id="quest-chain-allow-fallback"' in chain_panel
    assert "链路故障时自动回退直管模型，保证对话不中断；关闭则直接报错" in chain_panel
    assert 'getElementById("quest-chain-allow-fallback")' in js
    assert "questChainSettings.allow_direct_provider_fallback === true" in js
    assert "allow_direct_provider_fallback: Boolean(" in js
    assert 'endpoint: "pairing/quest-chain-settings"' in js


def test_operator_diagnostics_refreshes_live_without_concurrent_requests() -> None:
    html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
    js = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
    labels = (PLUGIN_ROOT / "core" / "diagnostic_labels.py").read_text(
        encoding="utf-8"
    )

    assert "DIAGNOSTICS_REFRESH_MS = 1000" in js
    assert "if (diagnosticsRefreshInFlight) return diagnosticsRefreshInFlight;" in js
    assert "loadDiagnostics({ silent: true })" in js
    assert 'document.addEventListener("visibilitychange"' in js
    assert 'window.addEventListener("pagehide", clearPageTimers)' in js
    assert 'window.addEventListener("pageshow", handlePageVisibilityChange)' in js
    assert "if (document.hidden || !bridgeReady) return;" in js
    assert "startServiceRefresh()" in js
    assert "每秒自动刷新" in html
    assert "DIAGNOSTIC_AUTO_SCROLL_STORAGE_KEY" in js
    assert "setDiagnosticAutoScroll" in js
    assert "if (diagnosticAutoScroll)" in js
    for event_name in (
        "avatar.action.eventbus_outcome",
        "avatar.action.main_delivery_parallel",
        "avatar.action.reply_wait_for_arbitration",
        "avatar.action.arbitration_winner",
        "avatar.intent.emitted",
        "avatar.intent.dropped",
        "audio.upload.completed",
    ):
        assert f'"{event_name}"' in labels
    assert "event.eventbus_tool_called" in js
