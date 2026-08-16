let bridge = null;
let operatorSettings = null;
let fastActionSettings = null;
let sttSettings = null;
let personaSettings = null;
let platformSettings = null;
let questIdentitySettings = null;
let serviceState = null;
let serviceRefreshInFlight = null;
let personaProfiles = null;
let personaWorkflowMode = "live";
let personaConversionReport = null;
let personaConversionDraftToken = "";
let personaDraftRequiresConversion = false;
let personaOpenedConverterPromptVersion = "";
let bridgeReady = false;
let eventsBound = false;
let serviceRefreshTimer = null;
let diagnosticsRefreshTimer = null;
let diagnosticsRefreshInFlight = null;
let personaConversionJobId = "";
let personaConversionJobSnapshot = null;
let personaConversionPollTimer = null;
let personaConversionPollInFlight = null;
let initialDataPromise = null;
const PAGE_REQUEST_TIMEOUT_MS = 10000;
const PERSONA_CONVERSION_POLL_MS = 1000;
const DIAGNOSTICS_REFRESH_MS = 1000;
const PERSONA_CONVERSION_JOB_STORAGE_KEY = "quest-avatar-bridge.persona-conversion-job";
const DIAGNOSTIC_AUTO_SCROLL_STORAGE_KEY = "quest-avatar-bridge.diagnostic-auto-scroll";
let diagnosticAutoScroll = readDiagnosticAutoScrollPreference();

function readDiagnosticAutoScrollPreference() {
  try {
    const stored = window.localStorage.getItem(DIAGNOSTIC_AUTO_SCROLL_STORAGE_KEY);
    return stored === null ? true : stored === "true";
  } catch (_error) {
    return true;
  }
}

function setDiagnosticAutoScroll(enabled) {
  diagnosticAutoScroll = enabled === true;
  try {
    window.localStorage.setItem(
      DIAGNOSTIC_AUTO_SCROLL_STORAGE_KEY,
      String(diagnosticAutoScroll),
    );
  } catch (_error) {
    // Browser storage is optional; the page remains usable in private mode.
  }
  const container = document.getElementById("diagnostics-events");
  if (diagnosticAutoScroll && container) {
    container.scrollTop = container.scrollHeight;
  }
}

async function resolveBridge(timeout = 8000) {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  if (typeof window.waitForAstrBotBridge === "function") {
    return window.waitForAstrBotBridge(timeout);
  }
  const started = Date.now();
  while (Date.now() - started < timeout) {
    await new Promise((resolve) => window.setTimeout(resolve, 50));
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  }
  throw new Error("请从 AstrBot 插件管理页面打开此页面");
}

function parseResponse(value) {
  const data = typeof value === "string" ? JSON.parse(value) : value;
  if (data?.success === false || data?.status === "error") {
    const error = new Error(
      data.message ||
      data.detail ||
      (typeof data.error === "string" ? data.error : "") ||
      data?.data?.code ||
      "请求失败"
    );
    error.code = String(data?.data?.code || data?.code || "");
    throw error;
  }
  return data;
}

function setRuntimeState(kind, label) {
  const node = document.querySelector(".runtime-state");
  node.classList.toggle("ready", kind === "ready");
  node.classList.toggle("error", kind === "error");
  document.getElementById("runtime-label").textContent = label;
}

function toast(message, error = false) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("visible");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("visible"), 2800);
}

function setButtonBusy(button, busy, busyText) {
  if (busy) {
    if (button.getAttribute("aria-busy") === "true") return false;
    button.dataset.idleText = button.textContent.trim();
    button.textContent = busyText;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    return true;
  }
  button.textContent = button.dataset.idleText || button.textContent;
  button.disabled = false;
  button.setAttribute("aria-busy", "false");
  return true;
}

function serviceReasonLabel(reason) {
  const labels = {
    ready: "服务运行正常",
    service_disabled: "服务已由管理员关闭",
    disabled: "内置 8520 监听尚未启用",
    not_started: "监听器尚未启动",
    bind_failed: "监听端口绑定失败",
    start_failed: "监听器启动失败",
    invalid_enabled: "监听开关配置无效",
    invalid_bind_host: "监听地址配置无效",
    invalid_port: "监听端口配置无效",
    invalid_upstream_url: "AstrBot 回环上游配置无效",
    listener_unavailable: "内置监听器不可用",
    pairing_listener_public_url_missing: "服务已运行，但尚未配置客户端可达地址"
  };
  return labels[String(reason || "")] || "服务状态需要检查";
}

function renderCapability(name, available, enabled) {
  const item = document.querySelector(`[data-capability="${name}"]`);
  if (!item) return;
  const active = available === true;
  item.classList.toggle("available", active && enabled);
  item.classList.toggle("standby", active && !enabled);
  item.classList.toggle("unavailable", !active);
  item.querySelector("strong").textContent = active
    ? enabled ? "可用" : "已配置"
    : "不可用";
}

function renderServiceStatus(service) {
  serviceState = service || {};
  const enabled = serviceState.enabled === true;
  const status = String(serviceState.status || "degraded");
  const badge = document.getElementById("service-status-badge");
  const statusLabels = {
    running: "运行中",
    stopped: "已关闭",
    degraded: "需检查"
  };
  badge.textContent = statusLabels[status] || "未知";
  badge.className = "status-badge " + status;

  document.getElementById("service-summary").textContent =
    serviceReasonLabel(serviceState.reason);
  const listener = serviceState.listener || {};
  const listenerConfigured = listener.configured === true;
  const bindHost = String(listener.bind_host || "");
  const port = Number(listener.port || 0);
  let listenerText = "内置监听：未配置";
  if (listenerConfigured && bindHost && port) {
    listenerText = `内置监听：${bindHost}:${port}`;
    if (listener.ready !== true) listenerText += "（当前未监听）";
  }
  document.getElementById("listener-address").textContent = listenerText;
  const portInput = document.getElementById("listener-port");
  if (document.activeElement !== portInput && port > 0) {
    portInput.value = String(port);
  }
  portInput.disabled = serviceState.config_writable !== true;
  document.getElementById("save-listener-port-button").disabled =
    serviceState.config_writable !== true;

  const sessions = serviceState.sessions || {};
  document.getElementById("active-session-count").textContent =
    String(Number(sessions.active_sessions || 0));
  document.getElementById("attached-stream-count").textContent =
    String(Number(sessions.attached_streams || 0));
  document.getElementById("queued-event-count").textContent =
    String(Number(sessions.queued_events || 0));

  const capabilities = serviceState.capabilities || {};
  ["dialogue", "eventbus", "identity_configured", "stt", "tts", "avatar_actions"]
    .forEach((name) => renderCapability(name, capabilities[name], enabled));

  const control = document.getElementById("service-control-button");
  control.dataset.nextEnabled = String(!enabled);
  control.textContent = enabled ? "关闭服务" : "启动服务";
  control.classList.toggle("danger", enabled);
  control.classList.toggle("primary", !enabled);
  control.disabled = serviceState.config_writable !== true;

  if (status === "running") setRuntimeState("ready", "服务运行中");
  else if (status === "stopped") setRuntimeState("error", "服务已关闭");
  else setRuntimeState("warning", "服务需要检查");
}

async function loadServiceStatus({ silent = false } = {}) {
  if (serviceRefreshInFlight) return serviceRefreshInFlight;
  const button = document.getElementById("refresh-service-button");
  serviceRefreshInFlight = (async () => {
    if (!silent) setButtonBusy(button, true, "刷新中…");
    try {
      const response = await apiGet("pairing/service-status");
      renderServiceStatus(response.service);
      return true;
    } catch (error) {
      setRuntimeState("error", "服务状态读取失败");
      if (!silent) toast("读取服务状态失败：" + error.message, true);
      return false;
    } finally {
      if (!silent) setButtonBusy(button, false);
    }
  })();
  try {
    return await serviceRefreshInFlight;
  } finally {
    serviceRefreshInFlight = null;
  }
}

async function toggleService() {
  const button = document.getElementById("service-control-button");
  const enabled = button.dataset.nextEnabled === "true";
  if (!enabled && !window.confirm("关闭服务会断开当前具身会话，确定继续吗？")) {
    return;
  }
  if (!setButtonBusy(button, true, enabled ? "启动中…" : "关闭中…")) return;
  try {
    const response = await apiPost("pairing/service-control", { enabled });
    renderServiceStatus(response.service);
    toast(enabled ? "具身桥接服务已启动" : "具身桥接服务已关闭");
  } catch (error) {
    toast((enabled ? "启动" : "关闭") + "服务失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    if (serviceState) renderServiceStatus(serviceState);
  }
}

async function saveListenerPort() {
  const button = document.getElementById("save-listener-port-button");
  const input = document.getElementById("listener-port");
  const port = Number(input.value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    toast("监听端口必须在 1024 到 65535 之间", true);
    return;
  }
  const active = Number(serviceState?.sessions?.active_sessions || 0);
  if (active > 0 && !window.confirm("修改端口会断开当前具身会话，确定继续吗？")) {
    return;
  }
  if (!setButtonBusy(button, true, "应用中…")) return;
  try {
    const response = await apiPost("pairing/listener-port", { port });
    renderServiceStatus(response.service);
    toast(response.service?.status === "running"
      ? `监听端口已切换为 ${port}；Docker 部署请确认宿主机映射相同端口`
      : `端口已保存为 ${port}，请检查监听状态和 Docker 端口映射`);
  } catch (error) {
    toast("监听端口保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    if (serviceState) renderServiceStatus(serviceState);
  }
}

function providerLabel(provider) {
  const id = String(provider?.id || "");
  const model = String(provider?.model || "");
  return model ? id + " · " + model : id;
}

function renderOperatorSettings(settings) {
  operatorSettings = settings || {};
  const select = document.getElementById("chat-provider-id");
  const providers = Array.isArray(operatorSettings.providers)
    ? operatorSettings.providers
    : [];
  select.replaceChildren();
  if (!providers.length) {
    select.add(new Option("没有可用的决策 / 回退 Provider", ""));
    select.disabled = true;
  } else {
    select.add(new Option("请选择决策 / 回退模型", ""));
    providers.forEach((provider) => {
      select.add(new Option(providerLabel(provider), provider.id));
    });
    if (
      operatorSettings.selected_id &&
      !providers.some((provider) => provider.id === operatorSettings.selected_id)
    ) {
      select.add(
        new Option(
          "已配置但不可用 · " + operatorSettings.selected_id,
          operatorSettings.selected_id
        )
      );
    }
    select.value = operatorSettings.selected_id || "";
    select.disabled = operatorSettings.config_writable !== true;
  }

  const status = document.getElementById("model-status");
  if (operatorSettings.config_writable !== true) {
    status.textContent = "当前 AstrBot 配置对象不支持安全保存。";
  } else if (operatorSettings.status === "selected_missing") {
    status.textContent = "已配置模型当前不可用，请重新选择。";
  } else if (operatorSettings.selected_available) {
    status.textContent = "当前模型：" + operatorSettings.selected_id;
  } else {
    status.textContent = "尚未选择决策 / 回退模型；EventBus 基础对话仍可使用 AstrBot 默认模型。";
  }
  document.getElementById("save-model-button").disabled =
    select.disabled || !select.value;
  renderDialogueMode(settings.dialogue_mode || {});

  const selectedPerson = String(operatorSettings.relationship_person_id || "");
  const personSelect = document.getElementById("relationship-person-select");
  if (
    selectedPerson &&
    ![...personSelect.options].some((option) => option.value === selectedPerson)
  ) {
    personSelect.add(new Option("已选择 · " + selectedPerson, selectedPerson));
  }
  personSelect.value = selectedPerson;
}

function renderDialogueMode(mode) {
  const direct = mode.direct_mode === true;
  const checkbox = document.getElementById("quest-direct-dialogue-mode");
  const button = document.getElementById("save-dialogue-mode-button");
  const status = document.getElementById("quest-dialogue-mode-status");
  if (!checkbox || !button || !status) return;
  checkbox.checked = direct;
  checkbox.disabled = operatorSettings.config_writable !== true;
  button.disabled = checkbox.disabled || !String(operatorSettings.selected_id || "");
  status.textContent = checkbox.disabled
    ? "当前 AstrBot 配置对象不支持安全保存。"
    : direct
      ? "已启用：不需要 Bot/User，不进入 EventBus。"
      : "未启用：正式 EventBus 模式需要服务端身份绑定。";
  if (!operatorSettings.selected_available) {
    status.textContent = "请先选择一个聊天 Provider；基础模式也需要模型。";
  }
}

function renderFastActionSettings(settings) {
  fastActionSettings = settings || {};
  const enabled = fastActionSettings.enabled !== false;
  const writable = fastActionSettings.config_writable === true;
  const providers = Array.isArray(fastActionSettings.providers)
    ? fastActionSettings.providers
    : [];
  const selected = String(fastActionSettings.selected_id || "");
  const checkbox = document.getElementById("fast-action-enabled");
  const select = document.getElementById("fast-action-provider-id");
  const button = document.getElementById("save-fast-action-button");
  const status = document.getElementById("fast-action-status");
  const timeoutInput = document.getElementById("fast-action-timeout-seconds");
  const timeoutHelp = document.getElementById("fast-action-timeout-help");

  checkbox.checked = enabled;
  checkbox.disabled = !writable;
  select.replaceChildren(new Option("请选择快速动作模型", ""));
  providers.forEach((provider) => {
    const id = String(provider?.id || "");
    if (id) select.add(new Option(providerLabel(provider), id));
  });
  if (selected && !providers.some((provider) => String(provider?.id || "") === selected)) {
    select.add(new Option("已配置但当前不可用 · " + selected, selected));
  }
  select.value = selected;
  select.disabled = !writable || !providers.length;
  const configuredTimeout = Number(fastActionSettings.configured_timeout_seconds);
  const effectiveTimeout = Number(fastActionSettings.effective_timeout_seconds);
  const timeoutValue = Number.isFinite(configuredTimeout)
    ? configuredTimeout
    : Number.isFinite(effectiveTimeout) ? effectiveTimeout : 6;
  if (document.activeElement !== timeoutInput) timeoutInput.value = String(timeoutValue);
  timeoutInput.disabled = !writable;
  timeoutHelp.textContent = Number.isFinite(effectiveTimeout)
    ? `有效超时：${effectiveTimeout.toFixed(1)} 秒 · 策略：${String(fastActionSettings.timeout_policy_revision || "v2")}` +
      (fastActionSettings.timeout_migrated === true ? " · 旧4秒默认已安全迁移，未覆盖显式设置" : "")
    : "有效超时：读取中";
  button.disabled = !writable || (enabled && !select.value);

  const messages = {
    ready: "快速动作模型已就绪，动作与主回复会并行处理。",
    disabled: "快速动作已关闭；动作继续由 AstrBot 主回复链路处理。",
    provider_not_configured: "功能默认开启，请选择一个响应较快的 Provider。",
    selected_missing: "已选快速模型当前不可用；动作会回退主回复链路，不会自动换模型。",
    llm_api_unavailable: "当前 AstrBot 版本未提供快速模型调用接口。"
  };
  const reason = String(
    fastActionSettings.availability_reason || fastActionSettings.status || ""
  );
  status.textContent = writable
    ? messages[reason] || (enabled
      ? "快速动作状态未知，普通回复链路不受影响。"
      : messages.disabled)
    : "当前 AstrBot 配置对象不支持安全保存。";
}

async function saveFastActionSettings() {
  const button = document.getElementById("save-fast-action-button");
  if (!setButtonBusy(button, true, "正在保存…")) return;
  try {
    const enabled = document.getElementById("fast-action-enabled").checked;
    const providerId = document.getElementById("fast-action-provider-id").value;
    const timeoutSeconds = Number(document.getElementById("fast-action-timeout-seconds").value);
    const response = await apiPost("pairing/fast-action-settings", {
      enabled,
      provider_id: providerId,
      timeout_seconds: timeoutSeconds
    });
    renderFastActionSettings(response.fast_action);
    toast(enabled
      ? "异步快速动作已启用"
      : "异步快速动作已关闭，动作将走主回复链路");
  } catch (error) {
    toast("快速动作设置保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    renderFastActionSettings(fastActionSettings || {});
  }
}

async function saveDialogueMode() {
  const button = document.getElementById("save-dialogue-mode-button");
  if (!setButtonBusy(button, true, "保存中…")) return;
  try {
    const response = await apiPost("pairing/operator-settings", {
      chat_provider_id: document.getElementById("chat-provider-id").value,
      direct_mode: document.getElementById("quest-direct-dialogue-mode").checked
    });
    renderDialogueMode(response.settings?.dialogue_mode || {});
    toast(response.settings?.dialogue_mode?.direct_mode
      ? "已启用基础对话模式；不进入 EventBus"
      : "已切回正式 EventBus 模式");
    await loadQuestIdentitySettings();
  } catch (error) {
    toast("模式保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function sttProviderLabel(provider) {
  const id = String(provider?.id || "");
  const model = String(provider?.model || "");
  const adapterType = String(provider?.adapter_type || "");
  const providerType = String(provider?.provider_type || "");
  return [model, adapterType, providerType, id].filter(Boolean).join(" · ");
}

function renderSttSettings(settings) {
  sttSettings = settings || {};
  const select = document.getElementById("stt-provider-id");
  const button = document.getElementById("save-stt-button");
  const status = document.getElementById("stt-status");
  const providers = Array.isArray(sttSettings.providers)
    ? sttSettings.providers.map((provider) => ({
      id: String(provider?.id || ""),
      model: String(provider?.model || ""),
      adapter_type: String(provider?.adapter_type || ""),
      provider_type: String(provider?.provider_type || "")
    })).filter((provider) => provider.id)
    : [];
  const selected = String(sttSettings.selected_id || "");
  const writable = sttSettings.config_writable === true;
  select.replaceChildren(new Option("关闭 Quest 语音识别", ""));
  providers.forEach((provider) => {
    select.add(new Option(sttProviderLabel(provider), provider.id));
  });
  if (selected && !providers.some((provider) => provider.id === selected)) {
    select.add(new Option("已配置但当前不可用 · " + selected, selected));
  }
  select.value = selected;
  select.disabled = !writable;
  button.disabled = !writable;

  const messages = {
    ready: "所选 STT Provider 已就绪。",
    selected_missing: "所选 STT Provider 已删除、禁用或尚未实例化；不会自动切换其他模型。",
    legacy_default_ready: "正在兼容旧版默认 STT 设置；请保存一个明确的 STT Provider。",
    legacy_default_missing: "旧版默认 STT 当前不可用；请重新选择正式 STT Provider。",
    legacy_private_mimo_disabled: "旧版插件私有 MiMo 配置已停用；请改选 AstrBot 正式 STT Provider。",
    disabled: "Quest 语音识别已关闭；文本对话不受影响。",
    adapter_unavailable: "当前 Bridge 没有可用的 STT 适配器；文本对话不受影响。",
    closed: "语音识别适配器已关闭。"
  };
  status.textContent = writable
    ? messages[String(sttSettings.status || "")] || "语音识别状态未知，请刷新后重试。"
    : "当前 AstrBot 配置对象不支持安全保存。";
}

function renderPlatformSettings(platform) {
  platformSettings = platform || {};
  const select = document.getElementById("trusted-platform-id");
  const button = document.getElementById("save-platform-button");
  const status = document.getElementById("platform-status");
  const selected = String(platformSettings.trusted_platform_id || "");
  const platforms = Array.isArray(platformSettings.platforms)
    ? platformSettings.platforms
    : [];
  select.replaceChildren(new Option("不使用正式消息平台", ""));
  platforms.forEach((item) => {
    const id = String(item?.id || "");
    const displayName = String(item?.display_name || item?.adapter_type || id);
    const adapterType = String(item?.adapter_type || "");
    const label = displayName === adapterType
      ? displayName + " · " + id
      : displayName + " · " + adapterType + " · " + id;
    select.add(new Option(label, id));
  });
  if (selected && !platforms.some((item) => String(item?.id || "") === selected)) {
    select.add(new Option("已配置但不可用 · " + selected, selected));
  }
  select.value = selected;
  select.disabled = platformSettings.config_writable !== true || !platforms.length;
  button.disabled = select.disabled;

  const messages = {
    ready: "\u5df2\u8fde\u63a5\u8be5\u5e73\u53f0\uff0c\u666e\u901a\u5bf9\u8bdd\u53ef\u8fdb\u5165 AstrBot EventBus\u3002",
    trusted_platform_not_configured: "\u5c1a\u672a\u914d\u7f6e\u53ef\u4fe1\u5e73\u53f0\uff0c\u666e\u901a\u5bf9\u8bdd\u6682\u4e0d\u53ef\u7528\u3002\u8bf7\u4fdd\u5b58\u5df2\u542f\u7528\u7684 AstrBot \u5e73\u53f0\u5b9e\u4f8b ID\u3002",
    astrbot_event_api_unavailable: "\u5f53\u524d AstrBot \u7248\u672c\u4e0d\u63d0\u4f9b EventBus \u5e73\u53f0\u63a5\u53e3\u3002",
    trusted_platform_unavailable: "\u5df2\u914d\u7f6e\u7684\u5e73\u53f0\u5f53\u524d\u4e0d\u5b58\u5728\u6216\u672a\u542f\u7528\u3002",
    disabled: "AstrBot \u6b63\u5f0f\u6d88\u606f\u94fe\u8def\u5df2\u5173\u95ed\u3002"
  };
  status.textContent = platformSettings.config_writable === true
    ? platforms.length
      ? messages[platformSettings.availability_reason] || "\u5e73\u53f0\u72b6\u6001\u672a\u77e5\u3002"
      : "\u6ca1\u6709\u5df2\u52a0\u8f7d\u7684 AstrBot \u5e73\u53f0\uff0c\u666e\u901a\u5bf9\u8bdd\u6682\u4e0d\u53ef\u7528\u3002"
    : "\u5f53\u524d AstrBot \u914d\u7f6e\u5bf9\u8c61\u4e0d\u652f\u6301\u5f02\u6b65\u4fdd\u5b58\u3002";
}

function renderPersonaSettings(persona) {
  personaSettings = persona || {};
  const writable = personaSettings.config_writable === true;
  const sourceMode = personaSettings.source_mode === "manual_override"
    ? "manual_override"
    : "astrbot";
  const sourceSelect = document.getElementById("persona-source-mode");
  sourceSelect.value = sourceMode;
  sourceSelect.disabled = !writable;

  const personaSelect = document.getElementById("astrbot-persona-id");
  const personas = Array.isArray(personaSettings.personas)
    ? personaSettings.personas
    : [];
  personaSelect.replaceChildren(new Option("AstrBot 明确默认人格", ""));
  personas.forEach((item) => {
    personaSelect.add(new Option(String(item.id || ""), String(item.id || "")));
  });
  const selectedPersona = String(personaSettings.persona_selected
    ? personaSettings.astrbot_persona_id || ""
    : "");
  if (
    selectedPersona &&
    !personas.some((item) => String(item.id || "") === selectedPersona)
  ) {
    personaSelect.add(new Option("已选择但不可用", selectedPersona));
  }
  personaSelect.value = selectedPersona;
  personaSelect.disabled = !writable || sourceMode !== "astrbot";

  const fields = {
    "character-name": personaSettings.character_name,
    "character-self-reference": personaSettings.character_self_reference,
    "character-self-description": personaSettings.character_self_description,
    "character-user-relationship": personaSettings.character_user_relationship
  };
  Object.entries(fields).forEach(([id, value]) => {
    const input = document.getElementById(id);
    input.value = String(value || "");
    input.disabled = !writable || sourceMode !== "manual_override";
  });
  document.getElementById("astrbot-persona-fields").hidden =
    sourceMode !== "astrbot";
  document.getElementById("manual-persona-fields").hidden =
    sourceMode !== "manual_override";
  const status = document.getElementById("persona-status");
  const statusMessages = {
    ready: sourceMode === "manual_override"
      ? "手动兼容身份已启用"
      : personaSettings.source === "astrbot_selected"
        ? "正在继承管理员选择的 AstrBot 人格"
        : "正在继承 AstrBot 明确默认人格",
    selected_missing: "所选人格已删除或失效；当前安全回退通用 MR 身份，不会自动换人格",
    default_missing: "AstrBot 默认人格不可用；当前安全回退通用 MR 身份",
    timeout: "AstrBot 人格读取超时；当前安全回退通用 MR 身份",
    unavailable: "AstrBot 人格接口当前不可用；当前安全回退通用 MR 身份",
    configuration_invalid: "已保存的人格 ID 无效；当前安全回退通用 MR 身份",
    not_checked: "人格尚未完成读取"
  };
  status.textContent = writable
    ? statusMessages[personaSettings.status] || "当前使用通用 MR 身份"
    : "当前 AstrBot 配置对象不支持安全保存";
  document.getElementById("save-persona-button").disabled = !writable;
}

async function loadOperatorSettings() {
  const response = await apiGet("pairing/operator-settings");
  renderOperatorSettings(response.settings);
  return true;
}

async function loadFastActionSettings() {
  const response = await apiGet("pairing/fast-action-settings");
  renderFastActionSettings(response.fast_action);
  return true;
}

async function loadSttSettings() {
  const response = await apiGet("pairing/stt-settings");
  renderSttSettings(response.stt);
  return true;
}

async function loadPlatformSettings() {
  const response = await apiGet("pairing/platform-settings");
  renderPlatformSettings(response.platform);
  return true;
}

async function loadPersonaSettings() {
  const response = await apiGet("pairing/persona-settings");
  renderPersonaSettings(response.persona);
  return true;
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function personaProfileId(profile) {
  return String(profile?.profile_id || profile?.id || "");
}

function personaProfileName(profile) {
  return String(profile?.display_name || profile?.name || "未命名人格");
}

function populatePersonaConverterProviders(catalog) {
  const select = document.getElementById("persona-converter-provider");
  const providers = safeArray(catalog.providers);
  const selected = String(catalog.persona_converter_provider_id || "");
  select.replaceChildren(new Option("请选择转换模型", ""));
  providers.forEach((provider) => {
    const id = String(provider?.id || "");
    if (!id) return;
    const model = String(provider?.model || "未标注模型");
    const adapter = String(provider?.adapter_type || "未知适配器");
    select.add(new Option(`${model} · ${adapter} · ${id}`, id));
  });
  if (selected && !providers.some((provider) => String(provider?.id || "") === selected)) {
    select.add(new Option("已配置但当前不可用", selected));
  }
  select.value = selected;
  select.disabled = catalog.config_writable === false || providers.length === 0;
  document.getElementById("save-persona-converter-provider").disabled =
    select.disabled || !select.value || select.value === selected;
}

function populatePersonaImportSources(catalog) {
  const select = document.getElementById("persona-import-source");
  const sources = safeArray(catalog.astrbot_personas).length
    ? safeArray(catalog.astrbot_personas)
    : safeArray(catalog.source_personas).length
      ? safeArray(catalog.source_personas)
      : safeArray(personaSettings?.personas);
  const current = select.value;
  select.replaceChildren(new Option("请选择 AstrBot 来源人格", ""));
  sources.forEach((persona) => {
    const id = String(persona?.id || persona?.persona_id || "");
    if (!id) return;
    const name = String(persona?.display_name || persona?.name || id);
    select.add(new Option(name === id ? id : `${name} · ${id}`, id));
  });
  if (current && sources.some((persona) =>
    String(persona?.id || persona?.persona_id || "") === current
  )) select.value = current;
  select.disabled = catalog.config_writable === false || sources.length === 0;
}

function appendReportItems(listId, values) {
  const list = document.getElementById(listId);
  list.replaceChildren();
  safeArray(values).forEach((value) => {
    const item = document.createElement("li");
    item.textContent = String(value || "");
    if (item.textContent) list.append(item);
  });
  if (!list.childElementCount) {
    const item = document.createElement("li");
    item.textContent = "无";
    item.className = "muted";
    list.append(item);
  }
}

function setDisabledWhenIdle(buttonId, disabled) {
  const button = document.getElementById(buttonId);
  if (button.getAttribute("aria-busy") !== "true") button.disabled = disabled;
}

function invalidatePersonaDraft(message, forceConversion = false) {
  const hadDraft = Boolean(personaConversionDraftToken);
  personaConversionDraftToken = "";
  personaDraftRequiresConversion =
    personaDraftRequiresConversion || forceConversion || hadDraft;
  if (personaDraftRequiresConversion && message) {
    document.getElementById("persona-profile-status").textContent = message;
  }
  updatePersonaEditorActions();
}

function renderPersonaConversionReport(report, version = "") {
  personaConversionReport = report && typeof report === "object" ? report : null;
  const panel = document.getElementById("persona-conversion-report");
  panel.hidden = !personaConversionReport;
  if (!personaConversionReport) return;
  document.getElementById("persona-conversion-version").textContent =
    version ? `规则 ${String(version)}` : "";
  appendReportItems("persona-report-preserved", personaConversionReport.preserved);
  appendReportItems("persona-report-adapted", personaConversionReport.adapted);
  appendReportItems("persona-report-removed", personaConversionReport.removed);
  const unresolved = safeArray(personaConversionReport.unresolved_questions);
  appendReportItems("persona-report-unresolved", unresolved);
  document.getElementById("persona-unresolved-warning").hidden = unresolved.length === 0;
}

function updatePersonaEditorActions() {
  const converter = document.getElementById("persona-converter-provider").value;
  const configuredConverter = String(
    personaProfiles?.persona_converter_provider_id ||
    personaProfiles?.converter_provider_id ||
    ""
  );
  const converterReady = Boolean(
    converter &&
    converter === configuredConverter &&
    personaProfiles?.converter_selected_available !== false &&
    safeArray(personaProfiles?.providers).some((provider) =>
      String(provider?.id || "") === converter
    )
  );
  const sourceReady = personaWorkflowMode === "import"
    ? Boolean(document.getElementById("persona-import-source").value)
    : Boolean(document.getElementById("persona-source-prompt").value.trim());
  setDisabledWhenIdle(
    "convert-persona-button",
    !converterReady || !sourceReady || personaProfiles?.config_writable === false
  );
  const canSave = Boolean(
    document.getElementById("persona-profile-name").value.trim() &&
    (personaConversionDraftToken ||
      document.getElementById("persona-source-prompt").value.trim()) &&
    document.getElementById("quest-persona-prompt").value.trim()
  );
  const newAstrBotProfileNeedsDraft = Boolean(
    personaWorkflowMode === "import" &&
    !document.getElementById("persona-profile-id").value &&
    !personaConversionDraftToken
  );
  setDisabledWhenIdle(
    "save-persona-profile-button",
    !canSave ||
      newAstrBotProfileNeedsDraft ||
      personaDraftRequiresConversion ||
      personaProfiles?.config_writable === false
  );
  const profileId = document.getElementById("persona-profile-id").value;
  setDisabledWhenIdle(
    "activate-persona-profile-button",
    !profileId ||
      profileId === String(personaProfiles?.active_quest_persona_id || "") ||
      personaProfiles?.config_writable === false
  );
}

function setPersonaWorkflowMode(mode) {
  personaWorkflowMode = ["live", "import", "independent"].includes(mode)
    ? mode
    : "live";
  document.querySelectorAll("[data-persona-workflow-mode]").forEach((button) => {
    const selected = button.dataset.personaWorkflowMode === personaWorkflowMode;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  document.getElementById("persona-live-workflow").hidden =
    personaWorkflowMode !== "live";
  document.getElementById("persona-editor-workflow").hidden =
    personaWorkflowMode === "live";
  document.getElementById("persona-editor-workflow").setAttribute(
    "aria-labelledby",
    personaWorkflowMode === "independent"
      ? "persona-mode-independent"
      : "persona-mode-import"
  );
  const importing = personaWorkflowMode === "import";
  document.getElementById("persona-import-source-fields").hidden = !importing;
  const sourcePrompt = document.getElementById("persona-source-prompt");
  sourcePrompt.readOnly = importing;
  sourcePrompt.placeholder = importing
    ? "AstrBot 来源正文由后端封存；保存后显式打开人格即可查看"
    : "写入原始人格或人物设定，再由转换模型生成具身版本";
  document.getElementById("persona-source-visibility").textContent = importing
    ? "后端封存"
    : "可编辑";
  document.getElementById("convert-persona-button").textContent = importing
    ? "导入并转换"
    : "转换为临人格";
  updatePersonaEditorActions();
}

function clearPersonaProfileEditor() {
  personaConversionDraftToken = "";
  personaDraftRequiresConversion = false;
  personaOpenedConverterPromptVersion = "";
  document.getElementById("persona-profile-id").value = "";
  document.getElementById("persona-profile-name").value = "";
  document.getElementById("persona-profile-aliases").value = "";
  document.getElementById("persona-import-source").value = "";
  document.getElementById("persona-source-prompt").value = "";
  document.getElementById("persona-admin-requirements").value = "";
  document.getElementById("quest-persona-prompt").value = "";
  renderPersonaConversionReport(null);
  document.getElementById("persona-profile-status").textContent =
    "这是未保存的草稿；保存后仍需单独点击启用。";
  updatePersonaEditorActions();
}

function renderPersonaProfileEditor(profile) {
  personaConversionDraftToken = "";
  personaDraftRequiresConversion = false;
  personaOpenedConverterPromptVersion = String(
    profile?.converter_prompt_version || profile?.prompt_version || ""
  );
  const id = personaProfileId(profile);
  const sourceType = String(profile?.source_type || profile?.source_kind || "manual") === "astrbot"
    ? "astrbot"
    : "manual";
  document.getElementById("persona-profile-id").value = id;
  document.getElementById("persona-profile-name").value = personaProfileName(profile);
  document.getElementById("persona-profile-aliases").value =
    safeArray(profile?.aliases).join("，");
  document.getElementById("persona-import-source").value =
    String(profile?.source_persona_id || "");
  document.getElementById("persona-source-prompt").value =
    String(profile?.source_prompt || profile?.source_snapshot || "");
  document.getElementById("persona-admin-requirements").value =
    String(profile?.admin_requirements || "");
  document.getElementById("quest-persona-prompt").value =
    String(profile?.quest_persona_prompt || "");
  renderPersonaConversionReport(
    profile?.conversion_report,
    personaOpenedConverterPromptVersion
  );
  setPersonaWorkflowMode(sourceType === "astrbot" ? "import" : "independent");
  const active = id === String(personaProfiles?.active_quest_persona_id || "");
  document.getElementById("persona-profile-status").textContent = active
    ? personaProfiles?.active_available === false
      ? "此人格已配置为当前人格，但文件尚不可用；请修正并保存后重新启用。"
      : "此人格当前已启用。修改后请先保存；保存不会自动重新启用。"
    : "已打开保存的人格；修改后需要保存，启用是独立操作。";
  updatePersonaEditorActions();
}

async function openPersonaProfile(profile, trigger = null) {
  const profileId = personaProfileId(profile);
  if (!profileId || trigger?.getAttribute("aria-busy") === "true") return false;
  if (trigger) {
    trigger.disabled = true;
    trigger.setAttribute("aria-busy", "true");
  }
  try {
    const response = await apiPost("pairing/persona-profile-open", {
      profile_id: profileId
    });
    const fullProfile = response.profile || response.persona_profile;
    if (!fullProfile || personaProfileId(fullProfile) !== profileId) {
      throw new Error("人格文件响应不完整");
    }
    renderPersonaProfileEditor({ ...profile, ...fullProfile });
    return true;
  } catch (error) {
    toast("读取人格文件失败：" + error.message, true);
    return false;
  } finally {
    if (trigger?.isConnected) {
      trigger.disabled = false;
      trigger.setAttribute("aria-busy", "false");
    }
  }
}

function renderPersonaProfileList(catalog) {
  const list = document.getElementById("persona-profile-list");
  const profiles = safeArray(catalog.profiles);
  const activeId = String(catalog.active_quest_persona_id || "");
  list.replaceChildren();
  document.getElementById("persona-profile-count").textContent = `${profiles.length} 个`;
  if (!profiles.length) {
    const empty = document.createElement("p");
    empty.className = "persona-empty";
    empty.textContent = "尚未创建独立人格";
    list.append(empty);
    return;
  }
  profiles.forEach((profile) => {
    const id = personaProfileId(profile);
    const row = document.createElement("div");
    row.className = "persona-profile-row";
    row.setAttribute("role", "listitem");
    if (id === activeId) row.classList.add("active");

    const open = document.createElement("button");
    open.type = "button";
    open.className = "persona-profile-open";
    open.setAttribute("aria-label", `打开人格 ${personaProfileName(profile)}`);
    const name = document.createElement("strong");
    name.textContent = personaProfileName(profile);
    const meta = document.createElement("span");
    const source = String(profile?.source_type || profile?.source_kind || "manual") === "astrbot"
      ? "AstrBot 转换"
      : "独立创建";
    meta.textContent = id === activeId
      ? `${source} · ${catalog.active_available === false ? "当前不可用" : "当前启用"}`
      : source;
    open.append(name, meta);
    open.addEventListener("click", () => openPersonaProfile(profile, open));

    const actions = document.createElement("div");
    actions.className = "persona-profile-row-actions";
    const reconvert = document.createElement("button");
    reconvert.type = "button";
    reconvert.className = "icon-text-button";
    reconvert.textContent = "重转";
    reconvert.setAttribute("aria-label", `重新转换人格 ${personaProfileName(profile)}`);
    reconvert.addEventListener("click", async () => {
      if (await openPersonaProfile(profile, reconvert)) await convertPersona();
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon-text-button danger-quiet";
    remove.textContent = "删除";
    const isActive = id === activeId;
    remove.setAttribute(
      "aria-label",
      isActive
        ? `人格 ${personaProfileName(profile)} 当前已启用，不能删除`
        : `删除人格 ${personaProfileName(profile)}`
    );
    remove.disabled = isActive;
    if (isActive) remove.title = "请先启用另一个人格";
    remove.addEventListener("click", () => deletePersonaProfile(profile, remove));
    actions.append(reconvert, remove);
    row.append(open, actions);
    list.append(row);
  });
}

function renderPersonaProfiles(catalog) {
  personaProfiles = catalog && typeof catalog === "object" ? catalog : {};
  if (!personaProfiles.active_quest_persona_id && personaProfiles.active_profile_id) {
    personaProfiles.active_quest_persona_id = personaProfiles.active_profile_id;
  }
  if (!personaProfiles.persona_converter_provider_id && personaProfiles.converter_provider_id) {
    personaProfiles.persona_converter_provider_id = personaProfiles.converter_provider_id;
  }
  populatePersonaConverterProviders(personaProfiles);
  populatePersonaImportSources(personaProfiles);
  renderPersonaProfileList(personaProfiles);
  const active = safeArray(personaProfiles.profiles).find((profile) =>
    personaProfileId(profile) === String(personaProfiles.active_quest_persona_id || "")
  );
  const activeId = String(personaProfiles.active_quest_persona_id || "");
  let activeLabel = "实时继承 AstrBot";
  if (activeId && active) {
    activeLabel = personaProfileName(active) +
      (personaProfiles.active_available === false ? "（不可用）" : "");
  } else if (activeId) {
    activeLabel = "已配置人格不可用";
  }
  document.getElementById("active-persona-name").textContent = activeLabel;
  updatePersonaEditorActions();
}

async function loadPersonaProfiles() {
  try {
    const response = await apiGet("pairing/persona-library");
    const catalog = response.library || response.persona_profiles || response.catalog || response;
    renderPersonaProfiles(catalog);
    return true;
  } catch (error) {
    document.getElementById("active-persona-name").textContent = "独立人格不可用";
    document.getElementById("persona-profile-status").textContent =
      "读取临人格失败：" + error.message;
    toast("读取临人格失败：" + error.message, true);
    return false;
  }
}

async function savePersonaConverterProvider() {
  const button = document.getElementById("save-persona-converter-provider");
  const providerId = document.getElementById("persona-converter-provider").value;
  if (!providerId || !setButtonBusy(button, true, "正在保存…")) return;
  try {
    const response = await apiPost("pairing/persona-converter-settings", {
      persona_converter_provider_id: providerId
    });
    if (response.library) renderPersonaProfiles(response.library);
    else if (personaProfiles) {
      personaProfiles.persona_converter_provider_id = providerId;
      personaProfiles.converter_provider_id = providerId;
      personaProfiles.converter_selected_available = true;
    }
    toast("人格转换模型已保存");
  } catch (error) {
    toast("转换模型保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = !document.getElementById("persona-converter-provider").value ||
      document.getElementById("persona-converter-provider").value ===
        String(personaProfiles?.persona_converter_provider_id || "");
    updatePersonaEditorActions();
  }
}

function applyPersonaConversionResult(response) {
  const result = response.profile || response.draft || response.conversion || response;
  personaConversionDraftToken = String(
    response.draft_token || result.draft_token || ""
  );
  personaDraftRequiresConversion = false;
  const sourcePrompt = result.source_prompt ?? result.source_snapshot ?? response.source_prompt;
  if (sourcePrompt !== undefined) {
    document.getElementById("persona-source-prompt").value = String(sourcePrompt || "");
  }
  const converted = result.quest_persona_prompt ?? response.quest_persona_prompt;
  document.getElementById("quest-persona-prompt").value = String(converted || "");
  if (!document.getElementById("persona-profile-name").value.trim()) {
    document.getElementById("persona-profile-name").value =
      String(result.display_name || response.display_name || "");
  }
  const aliases = safeArray(result.aliases).length
    ? safeArray(result.aliases)
    : safeArray(response.aliases);
  if (aliases.length) {
    document.getElementById("persona-profile-aliases").value = aliases.join("，");
  }
  const report = result.conversion_report || response.conversion_report || null;
  renderPersonaConversionReport(
    report,
    result.converter_prompt_version || response.converter_prompt_version || ""
  );
  document.getElementById("persona-profile-status").textContent =
    "转换完成，当前仍是未保存草稿。请检查内容与待确认项后再保存。";
  updatePersonaEditorActions();
}

function currentPersonaEditorProfile() {
  const sourceType = personaWorkflowMode === "import" ? "astrbot" : "manual";
  return {
    profile_id: document.getElementById("persona-profile-id").value,
    display_name: document.getElementById("persona-profile-name").value,
    aliases: document.getElementById("persona-profile-aliases").value
      .split(/[,，\n]/)
      .map((value) => value.trim())
      .filter((value, index, values) => value && values.indexOf(value) === index),
    source_kind: sourceType,
    source_persona_id: sourceType === "astrbot"
      ? document.getElementById("persona-import-source").value
      : "",
    source_snapshot: document.getElementById("persona-source-prompt").value,
    admin_requirements: document.getElementById("persona-admin-requirements").value,
    quest_persona_prompt: document.getElementById("quest-persona-prompt").value,
    conversion_report: personaConversionReport || {}
  };
}

async function convertPersona() {
  const button = document.getElementById("convert-persona-button");
  if (button.disabled || !setButtonBusy(button, true, "正在转换…")) return;
  const progress = document.getElementById("persona-conversion-progress");
  progress.textContent = "正在创建后台转换任务……";
  progress.hidden = false;
  personaConversionJobSnapshot = {
    status: "queued",
    stage: "accepted",
    elapsed_ms: 0
  };
  setPersonaConversionLocked(true);
  const sourceType = personaWorkflowMode === "import" ? "astrbot" : "manual";
  const requiresConversionOnFailure = Boolean(
    personaConversionDraftToken ||
    sourceType === "astrbot" ||
    (document.getElementById("persona-profile-id").value &&
      personaOpenedConverterPromptVersion !== "manual")
  );
  personaConversionDraftToken = "";
  personaDraftRequiresConversion = requiresConversionOnFailure;
  if (sourceType === "astrbot") {
    document.getElementById("persona-source-prompt").value = "";
  }
  try {
    const response = await apiPost("pairing/persona-conversion-start", {
      source_type: sourceType,
      source_persona_id: sourceType === "astrbot"
        ? document.getElementById("persona-import-source").value
        : "",
      source_prompt: sourceType === "manual"
        ? document.getElementById("persona-source-prompt").value
        : "",
      display_name: document.getElementById("persona-profile-name").value,
      admin_requirements: document.getElementById("persona-admin-requirements").value
    });
    const job = response.job;
    if (!job?.job_id) throw new Error("后台转换任务响应不完整");
    personaConversionJobId = String(job.job_id);
    storePersonaConversionJobId(personaConversionJobId);
    renderPersonaConversionJob(job);
    schedulePersonaConversionPoll(0);
  } catch (error) {
    personaConversionJobSnapshot = null;
    setPersonaConversionLocked(false);
    progress.textContent = `转换任务创建失败：${error.message}`;
    document.getElementById("persona-profile-status").textContent =
      "转换任务创建失败：" + error.message;
    toast("转换任务创建失败：" + error.message, true);
    setButtonBusy(button, false);
    updatePersonaEditorActions();
  }
}

function storePersonaConversionJobId(jobId) {
  try {
    if (jobId) {
      window.sessionStorage.setItem(
        PERSONA_CONVERSION_JOB_STORAGE_KEY,
        JSON.stringify({
          job_id: jobId,
          mode: personaWorkflowMode,
          profile_id: document.getElementById("persona-profile-id").value,
          source_persona_id: personaWorkflowMode === "import"
            ? document.getElementById("persona-import-source").value
            : ""
        })
      );
    } else {
      window.sessionStorage.removeItem(PERSONA_CONVERSION_JOB_STORAGE_KEY);
    }
  } catch (_error) {
    // sessionStorage can be unavailable in restricted embedded-page contexts.
  }
}

function restorePersonaConversionContext() {
  try {
    const raw = String(
      window.sessionStorage.getItem(PERSONA_CONVERSION_JOB_STORAGE_KEY) || ""
    ).trim();
    if (!raw) return null;
    if (raw.startsWith("pcj_")) return { job_id: raw };
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || !String(value.job_id || "")) {
      return null;
    }
    return {
      job_id: String(value.job_id),
      mode: ["import", "independent"].includes(String(value.mode))
        ? String(value.mode)
        : "import",
      profile_id: String(value.profile_id || ""),
      source_persona_id: String(value.source_persona_id || "")
    };
  } catch (_error) {
    return null;
  }
}

function restorePersonaConversionEditor(context) {
  if (!context) return;
  setPersonaWorkflowMode(context.mode || "import");
  document.getElementById("persona-profile-id").value = context.profile_id || "";
  if (context.mode === "import" && context.source_persona_id) {
    document.getElementById("persona-import-source").value =
      context.source_persona_id;
  }
}

function personaConversionStageLabel(stage) {
  const labels = {
    accepted: "任务已受理",
    source_lookup: "正在读取来源人格",
    source_ready: "来源人格读取完成",
    provider_wait: "正在等待转换模型首个流块",
    provider_first_chunk: "转换模型已开始响应",
    provider_streaming: "转换模型正在持续生成",
    provider_response: "模型生成已返回",
    response_validation: "正在校验转换结果结构",
    response_validated: "转换结果结构校验完成",
    preview_ready: "转换预览已就绪",
    failed: "转换失败",
    cancelled: "转换已取消"
  };
  return labels[String(stage || "")] || "正在处理转换任务";
}

function isPersonaConversionJobFinished(status) {
  return ["completed", "failed", "cancelled"].includes(String(status || ""));
}

function personaConversionErrorMessage(job) {
  const code = job?.error_code || job?.error?.code;
  return String(
    job?.error_message ||
    job?.error?.message ||
    (code ? diagnosticReasonLabel(code) : "") ||
    "后台转换任务失败"
  );
}

function setPersonaConversionLocked(locked) {
  const panel = document.querySelector(".persona-panel");
  if (!panel) return;
  panel.classList.toggle("conversion-locked", locked);
  panel.setAttribute("aria-busy", String(locked));
  panel.querySelectorAll(
    "#persona-workflow-tabs button, " +
    "#persona-editor-workflow button, " +
    "#persona-editor-workflow input, " +
    "#persona-editor-workflow select, " +
    "#persona-editor-workflow textarea"
  ).forEach((control) => {
    if (control.id === "cancel-persona-conversion-button") return;
    if (locked) {
      if (!control.hasAttribute("data-conversion-was-disabled")) {
        control.dataset.conversionWasDisabled = String(Boolean(control.disabled));
      }
      control.disabled = true;
      return;
    }
    if (control.hasAttribute("data-conversion-was-disabled")) {
      control.disabled = control.dataset.conversionWasDisabled === "true";
      control.removeAttribute("data-conversion-was-disabled");
    }
  });
}

function renderPersonaConversionJob(job) {
  const status = String(job?.status || "queued");
  const elapsedMs = Number(job?.elapsed_ms);
  const elapsedSeconds = (
    Number.isFinite(elapsedMs) ? Math.max(0, elapsedMs) / 1000 : 0
  ).toFixed(1);
  const progress = document.getElementById("persona-conversion-progress");
  const convertButton = document.getElementById("convert-persona-button");
  const cancelButton = document.getElementById("cancel-persona-conversion-button");
  const stage = personaConversionStageLabel(job?.stage);
  personaConversionJobSnapshot = {
    status,
    stage: String(job?.stage || "accepted"),
    elapsed_ms: Number.isFinite(elapsedMs) ? Math.max(0, elapsedMs) : 0
  };
  progress.hidden = false;
  progress.dataset.status = status;
  progress.textContent = `${stage}，后台任务已用时 ${elapsedSeconds} 秒。`;

  if (!isPersonaConversionJobFinished(status)) {
    setPersonaConversionLocked(true);
    setButtonBusy(convertButton, true, "转换进行中…");
    cancelButton.hidden = false;
    cancelButton.disabled = false;
    return;
  }

  personaConversionJobId = "";
  personaConversionJobSnapshot = null;
  storePersonaConversionJobId("");
  setPersonaConversionLocked(false);
  if (personaConversionPollTimer !== null) {
    window.clearTimeout(personaConversionPollTimer);
    personaConversionPollTimer = null;
  }
  cancelButton.hidden = true;
  setButtonBusy(convertButton, false);

  if (status === "completed") {
    if (!job.result || typeof job.result !== "object") {
      progress.dataset.status = "failed";
      progress.textContent = `转换任务完成，但结果响应不完整；后台任务用时 ${elapsedSeconds} 秒。`;
      personaDraftRequiresConversion = true;
      toast("人格转换结果响应不完整", true);
    } else {
      applyPersonaConversionResult(job.result);
      progress.textContent = `转换预览完成，后台任务用时 ${elapsedSeconds} 秒；尚未保存或启用。`;
      toast("人格转换完成，请确认后保存");
    }
  } else if (status === "cancelled") {
    progress.textContent = `转换已取消，后台任务用时 ${elapsedSeconds} 秒。`;
    document.getElementById("persona-profile-status").textContent = "人格转换已取消。";
  } else {
    const message = personaConversionErrorMessage(job);
    progress.textContent = `转换失败，后台任务用时 ${elapsedSeconds} 秒：${message}`;
    document.getElementById("persona-profile-status").textContent =
      "转换失败：" + message;
    toast("人格转换失败：" + message, true);
  }
  updatePersonaEditorActions();
  loadDiagnostics({ silent: true });
}

function schedulePersonaConversionPoll(delay = PERSONA_CONVERSION_POLL_MS) {
  if (!personaConversionJobId || personaConversionPollTimer !== null) return;
  personaConversionPollTimer = window.setTimeout(() => {
    personaConversionPollTimer = null;
    refreshPersonaConversionJob();
  }, delay);
}

async function refreshPersonaConversionJob() {
  if (!personaConversionJobId || personaConversionPollInFlight) {
    return personaConversionPollInFlight;
  }
  const jobId = personaConversionJobId;
  personaConversionPollInFlight = (async () => {
    try {
      const response = await apiPost("pairing/persona-conversion-status", {
        job_id: jobId
      });
      const job = response.job;
      if (!job?.job_id || String(job.job_id) !== jobId) {
        throw new Error("后台转换任务状态响应不完整");
      }
      if (personaConversionJobId !== jobId) return null;
      renderPersonaConversionJob(job);
      return job;
    } catch (error) {
      const progress = document.getElementById("persona-conversion-progress");
      progress.hidden = false;
      if (error.code === "conversion_job_not_found") {
        personaConversionJobId = "";
        personaConversionJobSnapshot = null;
        setPersonaConversionLocked(false);
        storePersonaConversionJobId("");
        progress.dataset.status = "failed";
        progress.textContent = "上次转换任务不存在或已经过期，请重新发起转换。";
        document.getElementById("cancel-persona-conversion-button").hidden = true;
        setButtonBusy(document.getElementById("convert-persona-button"), false);
        updatePersonaEditorActions();
        return null;
      }
      progress.dataset.status = "retrying";
      progress.textContent = `任务状态读取失败，将自动重试：${error.message}`;
      return null;
    }
  })();
  try {
    return await personaConversionPollInFlight;
  } finally {
    personaConversionPollInFlight = null;
    if (personaConversionJobId === jobId) schedulePersonaConversionPoll();
  }
}

async function cancelPersonaConversion() {
  if (!personaConversionJobId) return;
  const button = document.getElementById("cancel-persona-conversion-button");
  if (!setButtonBusy(button, true, "正在取消…")) return;
  try {
    const response = await apiPost("pairing/persona-conversion-cancel", {
      job_id: personaConversionJobId
    });
    if (response.job) renderPersonaConversionJob(response.job);
    else schedulePersonaConversionPoll(0);
  } catch (error) {
    toast("取消转换失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.hidden = !personaConversionJobId;
  }
}

async function savePersonaProfile() {
  const button = document.getElementById("save-persona-profile-button");
  if (button.disabled || !setButtonBusy(button, true, "正在保存…")) return;
  const sourceType = personaWorkflowMode === "import" ? "astrbot" : "manual";
  const profileId = document.getElementById("persona-profile-id").value;
  const wasActive = Boolean(
    profileId && profileId === String(personaProfiles?.active_quest_persona_id || "")
  );
  const editorSnapshot = currentPersonaEditorProfile();
  try {
    const response = await apiPost("pairing/persona-profile-save", {
      profile_id: profileId,
      draft_token: personaConversionDraftToken,
      display_name: document.getElementById("persona-profile-name").value,
      aliases: editorSnapshot.aliases,
      source_type: sourceType,
      source_persona_id: sourceType === "astrbot"
        ? document.getElementById("persona-import-source").value
        : "",
      source_prompt: document.getElementById("persona-source-prompt").value,
      quest_persona_prompt: document.getElementById("quest-persona-prompt").value,
      conversion_report: personaConversionReport || {}
    });
    const saved = response.profile || response.saved_profile || {};
    personaConversionDraftToken = "";
    const savedId = personaProfileId(saved) || String(response.profile_id || profileId);
    if (savedId) document.getElementById("persona-profile-id").value = savedId;
    await loadPersonaProfiles();
    const current = safeArray(personaProfiles?.profiles).find((profile) =>
      personaProfileId(profile) === savedId
    );
    renderPersonaProfileEditor({
      ...editorSnapshot,
      ...(current || {}),
      ...saved,
      profile_id: savedId
    });
    document.getElementById("persona-profile-status").textContent = wasActive
      ? "人格已保存，并已立即更新当前启用的人格。"
      : "人格已保存，但没有自动启用。确认无误后可单独启用。";
    toast(wasActive ? "人格已保存并立即更新" : "人格已保存，尚未启用");
  } catch (error) {
    toast("人格保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    updatePersonaEditorActions();
  }
}

async function activatePersonaProfile() {
  const button = document.getElementById("activate-persona-profile-button");
  const profileId = document.getElementById("persona-profile-id").value;
  if (!profileId || !setButtonBusy(button, true, "正在启用…")) return;
  const editorSnapshot = currentPersonaEditorProfile();
  try {
    await apiPost("pairing/persona-profile-activate", { profile_id: profileId });
    await loadPersonaProfiles();
    const current = safeArray(personaProfiles?.profiles).find((profile) =>
      personaProfileId(profile) === profileId
    );
    renderPersonaProfileEditor({
      ...editorSnapshot,
      ...(current || {}),
      profile_id: profileId
    });
    toast("临人格已启用，只影响经过“临”的对话");
  } catch (error) {
    toast("人格启用失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    updatePersonaEditorActions();
  }
}

async function deletePersonaProfile(profile, button) {
  const id = personaProfileId(profile);
  if (!id || !window.confirm(`确定删除“${personaProfileName(profile)}”吗？此操作不可撤销。`)) {
    return;
  }
  if (!setButtonBusy(button, true, "删除中")) return;
  try {
    await apiPost("pairing/persona-profile-delete", { profile_id: id });
    if (document.getElementById("persona-profile-id").value === id) {
      clearPersonaProfileEditor();
    }
    await loadPersonaProfiles();
    toast("人格已删除");
  } catch (error) {
    toast("人格删除失败：" + error.message, true);
  } finally {
    if (button.isConnected) setButtonBusy(button, false);
  }
}

function renderQuestIdentitySettings(identity) {
  questIdentitySettings = identity || {};
  const writable = questIdentitySettings.config_writable === true;
  const advanced = document.getElementById("quest-identity-advanced");
  const badge = document.getElementById("quest-identity-badge");
  const basicStatus = document.getElementById("quest-identity-basic-status");
  document.getElementById("quest-client-id").value =
    String(questIdentitySettings.client_id || "quest-living-room");
  document.getElementById("quest-bot-id").value =
    String(questIdentitySettings.bot_id || "");
  document.getElementById("quest-user-id").value =
    String(questIdentitySettings.user_id || "");
  document.getElementById("quest-bot-id").placeholder =
    questIdentitySettings.bot_id_configured ? "已配置，可留空保持" : "请输入 Bot ID";
  document.getElementById("quest-user-id").placeholder =
    questIdentitySettings.user_id_configured ? "已配置，可留空保持" : "请输入用户 ID";
  document.getElementById("quest-api-key").value = "";
  document.getElementById("quest-api-key").placeholder =
    questIdentitySettings.astrbot_auth_configured
      ? "已配置，可留空并重新验证"
      : "请填写具身客户端专用 API Key";
  ["quest-client-id", "quest-bot-id", "quest-user-id", "quest-api-key"]
    .forEach((id) => { document.getElementById(id).disabled = !writable; });

  const control = questIdentitySettings.control_plane || {};
  let source = "未安装“序”，由“临”本地精确绑定";
  if (control.source === "identity_guardian") {
    source = `由“序”统一管理 · ${Number(control.owner_count || 0)} 位主人 · ` +
      `${Number(control.quest_binding_count || 0)} 个 Quest 绑定`;
    if (control.status !== "ready") {
      const reasons = {
        identity_control_plane_incompatible: "请升级“序”后再保存",
        identity_control_plane_timeout: "“序”响应超时",
        identity_control_plane_error: "“序”控制面读取失败",
        plugin_disabled: "“序”已停用",
        guard_stopped: "“序”已暂停"
      };
      source += `；${reasons[control.reason] || "统一身份控制面当前不可用"}`;
    }
  }
  const missing = [];
  if (!questIdentitySettings.astrbot_auth_configured) missing.push("AstrBot API Key");
  if (!questIdentitySettings.bridge_auth_configured) missing.push("Bridge Key 将在保存时自动生成");
  if (!questIdentitySettings.client_id) missing.push("客户端 ID");
  if (!questIdentitySettings.platform_id) missing.push("平台实例");
  if (!questIdentitySettings.bot_id_configured) missing.push("Bot ID");
  if (!questIdentitySettings.user_id_configured) missing.push("主人用户 ID");
  if (questIdentitySettings.identity_source === "relationship") {
    source += "；当前 Bot/User 由自然人映射管理，改为主人身份时需明确填写两项";
  }
  const validation = questIdentitySettings.binding_validation;
  const validationText = validation?.authorized === true ? "；保存后授权校验通过" : "";
  const ready = questIdentitySettings.status === "ready";
  badge.textContent = ready ? "已绑定" : "待完成";
  badge.classList.toggle("ready", ready);
  badge.classList.toggle("loading", !ready);
  basicStatus.textContent = !writable
    ? "当前配置对象不支持安全保存"
    : ready
      ? (questIdentitySettings.identity_source === "relationship"
        ? "已绑定；Bot/User 由“序”根据自然人映射管理"
        : "已绑定；Quest 可使用快速绑定码连接")
      : "尚未完成基础绑定，请展开高级身份设置补充首次验证材料";
  document.getElementById("quest-identity-status").textContent = writable
    ? source + (missing.length ? `；待补充：${missing.join("、")}` : "；身份配置完整") + validationText
    : "当前 AstrBot 配置对象不支持安全保存";
  document.getElementById("save-quest-identity-button").disabled = !writable;
  if (!ready && missing.length && advanced) advanced.open = true;
}

async function loadQuestIdentitySettings() {
  const response = await apiGet("pairing/quest-identity-settings");
  renderQuestIdentitySettings(response.identity);
  return true;
}

async function saveQuestIdentitySettings() {
  const button = document.getElementById("save-quest-identity-button");
  if (!setButtonBusy(button, true, "正在保存并验证…")) return;
  const apiKeyInput = document.getElementById("quest-api-key");
  const apiKey = apiKeyInput.value;
  try {
    const response = await apiPost("pairing/quest-identity-settings", {
      client_id: document.getElementById("quest-client-id").value,
      platform_id: document.getElementById("trusted-platform-id").value,
      bot_id: document.getElementById("quest-bot-id").value,
      user_id: document.getElementById("quest-user-id").value,
      api_key: apiKey
    });
    renderQuestIdentitySettings(response.identity);
    await loadPlatformSettings();
    toast(response.identity.control_plane?.source === "identity_guardian"
      ? "Quest 身份已保存到“序”并验证"
      : "Quest 身份已保存到“临”的本地精确绑定");
  } catch (error) {
    toast("Quest 身份保存失败：" + error.message, true);
  } finally {
    apiKeyInput.value = "";
    setButtonBusy(button, false);
    button.disabled = questIdentitySettings?.config_writable !== true;
  }
}

async function savePersonaSettings() {
  const button = document.getElementById("save-persona-button");
  if (!setButtonBusy(button, true, "正在保存…")) return;
  let sourceSaved = false;
  try {
    const response = await apiPost("pairing/persona-settings", {
      persona_source_mode: document.getElementById("persona-source-mode").value,
      astrbot_persona_id: document.getElementById("astrbot-persona-id").value,
      character_name: document.getElementById("character-name").value,
      character_self_reference: document.getElementById(
        "character-self-reference"
      ).value,
      character_self_description: document.getElementById(
        "character-self-description"
      ).value,
      character_user_relationship: document.getElementById(
        "character-user-relationship"
      ).value
    });
    renderPersonaSettings(response.persona);
    sourceSaved = true;
    await loadPersonaProfiles();
    toast("实时人格来源已保存并启用");
  } catch (error) {
    toast(
      sourceSaved
        ? "实时人格来源已启用，但状态刷新失败：" + error.message
        : "角色身份保存失败：" + error.message,
      true
    );
  } finally {
    setButtonBusy(button, false);
    button.disabled = personaSettings?.config_writable !== true;
  }
}

async function saveModelSelection() {
  const button = document.getElementById("save-model-button");
  const selected = document.getElementById("chat-provider-id").value;
  if (!selected || !setButtonBusy(button, true, "正在保存…")) return;
  try {
    const response = await apiPost("pairing/operator-settings", {
      chat_provider_id: selected
    });
    renderOperatorSettings(response.settings);
    toast("临直连与交互决策模型已保存并立即生效");
  } catch (error) {
    toast("模型保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = !document.getElementById("chat-provider-id").value;
  }
}

async function saveSttSettings() {
  const button = document.getElementById("save-stt-button");
  if (!setButtonBusy(button, true, "正在保存…")) return;
  try {
    const response = await apiPost("pairing/stt-settings", {
      provider_id: document.getElementById("stt-provider-id").value
    });
    renderSttSettings(response.stt);
    toast(response.stt?.selected_id
      ? "语音识别 Provider 已保存并立即生效"
      : "Quest 语音识别已关闭");
  } catch (error) {
    toast("语音识别保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = sttSettings?.config_writable !== true;
  }
}

async function savePlatformSettings() {
  const button = document.getElementById("save-platform-button");
  if (!setButtonBusy(button, true, "\u6b63\u5728\u4fdd\u5b58\u2026")) return;
  try {
    const response = await apiPost("pairing/platform-settings", {
      trusted_platform_id: document.getElementById("trusted-platform-id").value
    });
    renderPlatformSettings(response.platform);
    toast("\u5e73\u53f0\u5df2\u4fdd\u5b58\u5e76\u7acb\u5373\u751f\u6548");
  } catch (error) {
    toast("\u5e73\u53f0\u4fdd\u5b58\u5931\u8d25\uff1a" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = document.getElementById("trusted-platform-id").disabled;
  }
}

function identityUnavailableMessage(status) {
  const messages = {
    provider_unavailable: "未检测到“情”插件。",
    contract_unavailable: "“情”当前版本未提供候选读取契约。",
    timeout: "读取“情”自然人候选超时。",
    error: "“情”读取自然人候选失败。",
    invalid_response: "“情”返回了不兼容的候选数据。"
  };
  return messages[status] || "自然人候选当前不可用。";
}

function renderIdentityCandidates(catalog) {
  const select = document.getElementById("relationship-person-select");
  const saveButton = document.getElementById("save-identity-button");
  const status = document.getElementById("identity-status");
  const candidates = Array.isArray(catalog?.candidates) ? catalog.candidates : [];
  const selected = String(operatorSettings?.relationship_person_id || "");
  select.replaceChildren(new Option("不使用“情”的关系上下文", ""));

  if (catalog?.status !== "ok") {
    if (selected) select.add(new Option("已选择 · " + selected, selected));
    select.value = selected;
    select.disabled = false;
    saveButton.disabled = false;
    status.textContent = selected
      ? identityUnavailableMessage(catalog?.status) + " 当前选择无法验证；可切换为空值并关闭关系上下文。"
      : identityUnavailableMessage(catalog?.status) + " 可保持留空，基础对话不受影响。";
    return;
  }

  candidates.forEach((candidate) => {
    const count = Number(candidate.account_count || 0);
    select.add(
      new Option(
        candidate.display_name +
          " · " +
          candidate.person_id +
          " · " +
          count +
          " 个账号",
        candidate.person_id
      )
    );
  });
  if (selected && !candidates.some((candidate) => candidate.person_id === selected)) {
    select.add(new Option("已选择但已不可用 · " + selected, selected));
  }
  select.value = selected;
  select.disabled = false;
  saveButton.disabled = false;
  status.textContent = candidates.length
    ? "已读取 " + candidates.length + " 个自然人，只包含管理员标签。"
    : "“情”中尚无可选自然人；可保持留空，基础对话不受影响。";
}

async function loadIdentityCandidates() {
  const button = document.getElementById("load-identity-candidates");
  if (!setButtonBusy(button, true, "正在读取…")) return;
  try {
    const response = await apiGet("pairing/identity-candidates");
    renderIdentityCandidates(response.identity_catalog);
    if (response.identity_catalog?.status === "ok") {
      toast("已从“情”读取自然人候选");
    }
  } catch (error) {
    renderIdentityCandidates({ status: "error", candidates: [] });
    toast("读取自然人失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = false;
  }
}

async function saveIdentitySelection() {
  const button = document.getElementById("save-identity-button");
  const personId = document.getElementById("relationship-person-select").value;
  if (!setButtonBusy(button, true, "正在保存…")) return;
  try {
    const response = await apiPost("pairing/identity-selection", {
      person_id: personId
    });
    renderOperatorSettings(response.settings);
    toast(personId
      ? "自然人与正式消息身份已同步"
      : "已关闭“情”的关系上下文；Quest 基础对话身份保持不变");
  } catch (error) {
    toast("自然人保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = document.getElementById(
      "relationship-person-select"
    ).disabled;
  }
}

function diagnosticReasonLabel(code) {
  const labels = {
    owner_not_configured: "“序”尚未为这组 Quest 原始身份配置主人",
    quest_identity_not_allowlisted: "Quest 原始身份不在“序”的允许列表",
    local_identity_not_configured: "“临”的本地 Quest 身份尚未配置完整",
    local_api_principal_mismatch: "Quest 使用的 AstrBot API Key 与本地绑定不一致",
    local_quest_identity_mismatch: "具身客户端、平台、Bot 或主人用户与本地绑定不一致",
    invalid_user_id: "Quest 用户 ID 无效或仍是占位值",
    missing_user_id: "Quest 用户 ID 缺失",
    invalid_bot_id: "Quest Bot ID 无效",
    missing_bot_id: "Quest Bot ID 缺失",
    client_id_mismatch: "具身客户端 ID 与服务端配置不一致",
    trusted_platform_not_configured: "尚未配置可进入 EventBus 的 AstrBot 平台",
    trusted_platform_unavailable: "已配置的 AstrBot 平台当前不可用",
    astrbot_event_api_unavailable: "当前 AstrBot 不提供消息事件接口",
    astrbot_message_pipeline_unavailable: "AstrBot 消息链路不可用",
    astrbot_pipeline_timeout: "AstrBot 消息链处理超时",
    astrbot_pipeline_empty_reply: "AstrBot 消息链没有返回可用内容",
    astrbot_pipeline_event_stopped: "AstrBot 消息事件已由插件接管，但未留下可用正文",
    astrbot_pipeline_not_woken: "AstrBot 消息事件未通过唤醒规则",
    astrbot_pipeline_reply_capture_empty: "AstrBot 已执行发送，但回复捕获为空",
    stt_empty: "没有识别到有效语音",
    stt_unavailable: "语音识别服务未配置",
    stt_failed: "语音识别失败",
    llm_failed: "模型生成失败",
    tts_failed: "语音合成失败，文字回复仍可能可用",
    audio_upload_backpressure: "音频上传速度跟不上录音",
    audio_http_request_failed: "音频上传请求失败",
    turn_failed: "对话生成失败",
    interaction_failed: "触碰交互决策失败",
    fast_action_disabled: "异步快速动作已关闭",
    fast_action_provider_not_configured: "尚未选择快速动作模型",
    fast_action_selected_missing: "已选快速动作模型当前不可用",
    fast_action_provider_catalog_unavailable: "快速动作模型目录当前不可用",
    fast_action_timeout: "快速动作模型等待超时，已使用本地说话动作兜底",
    fast_action_invalid_output: "快速动作模型返回了不符合动作协议的内容",
    fast_action_failed: "快速动作决策失败，普通回复不受影响",
    fast_action_enabled: "已由独立快速动作模型处理",
    fast_action_selected: "快速动作已先于主回复选定",
    reply_path_selected: "主回复链路已先选定动作",
    conversion_timeout: "人格转换模型等待超时",
    conversion_first_chunk_timeout: "人格转换模型首个流块等待超时",
    conversion_stream_idle_timeout: "人格转换模型输出流长时间无新数据",
    conversion_stream_unsupported: "所选模型 Provider 不支持流式人格转换",
    conversion_response_too_large: "人格转换模型返回内容过大",
    conversion_provider_failed: "人格转换模型调用失败",
    conversion_response_invalid: "人格转换结果无法解析",
    conversion_schema_invalid: "人格转换结果结构不符合要求",
    conversion_schema_unsupported: "人格转换结果版本不受支持",
    persona_conversion_failed: "人格转换任务失败",
    conversion_job_in_progress: "已有其他人格转换正在运行",
    conversion_job_not_found: "人格转换任务不存在或已经过期",
    response_first_event_timeout: "后端接收后没有返回首个事件",
    response_event_stall_timeout: "后端事件流在回复结束前停滞",
    ready: "链路就绪"
  };
  return labels[String(code || "")] || String(code || "未发现明确错误码");
}

function diagnosticStageLabel(value) {
  const labels = {
    configuration: "配置",
    authorization: "身份授权",
    identity: "身份授权",
    session: "会话",
    health: "健康检查",
    sse: "实时事件",
    transport: "HTTP 传输",
    audio_input: "音频上传",
    audio_upload: "音频上传",
    microphone: "麦克风",
    stt: "语音识别",
    message_pipeline: "AstrBot/EventBus",
    action: "角色动作",
    eventbus: "AstrBot/EventBus",
    llm: "模型生成",
    tts: "语音合成",
    reply: "回复交付",
    turn: "对话轮次",
    audio_playback: "音频播放",
    interrupt: "打断",
    persona: "人格转换",
    persona_conversion: "人格转换",
    persona_source: "人格来源读取",
    persona_model: "人格模型生成",
    persona_validation: "人格结构校验"
  };
  return labels[String(value || "")] || String(value || "运行链路");
}

function diagnosticEventLabel(value) {
  const labels = {
    "session.authorization": "完成身份授权检查",
    "session.authorization_error": "身份授权检查异常",
    "session.started": "会话已建立",
    "sse.connected": "SSE 已连接",
    "sse.disconnected": "SSE 已断开",
    "turn.accepted": "后端已接收轮次",
    "audio.received": "音频上传完成",
    "stt.started": "开始语音识别",
    "stt.completed": "语音识别完成",
    "stt.error": "语音识别失败",
    "message_pipeline.selected": "选择回复链路",
    "message_pipeline.started": "进入 AstrBot EventBus",
    "message_pipeline.completed": "AstrBot EventBus 返回",
    "message_pipeline.blocked": "AstrBot EventBus 被阻止",
    "message_pipeline.fallback": "消息链路发生降级",
    "message_pipeline.stopped_after_fast_action": "EventBus 已完成动作轮但没有正文",
    "fast_action.started": "快速动作模型开始判断",
    "fast_action.provider_resolved": "快速动作模型已定位",
    "fast_action.request_queued": "快速动作请求已发出",
    "fast_action.first_chunk": "快速动作模型首个流块已到达",
    "fast_action.provider_completed": "快速动作模型生成完成",
    "fast_action.parsed": "快速动作结果解析完成",
    "fast_action.parsed_no_action": "快速动作模型决定本轮不做动作",
    "fast_action.parse_invalid": "快速动作结果格式无效",
    "fast_action.timeout": "快速动作模型等待超时",
    "fast_action.provider_error": "快速动作模型调用失败",
    "fast_action.provider_unavailable": "快速动作模型当前不可用",
    "fast_action.explicit_selected": "明确动作命令已立即选定",
    "fast_action.explicit_rejected": "不安全或歧义动作命令已拒绝",
    "fast_action.completed": "快速动作判断完成",
    "fast_action.skipped": "快速动作已回退或跳过",
    "fast_action.error": "快速动作判断失败",
    "fast_action.settings_updated": "快速动作设置已更新",
    "avatar.action.eventbus_outcome": "EventBus 动作工具结果",
    "avatar.action.main_delivery_parallel": "正文与动作并行交付",
    "avatar.action.reply_wait_for_arbitration": "回复结束前等待动作仲裁",
    "avatar.action.arbitration_winner": "动作仲裁胜者已确定",
    "avatar.intent.emitted": "动作意图已下发",
    "avatar.intent.dropped": "动作意图未下发",
    "audio.upload.completed": "音频上传汇总完成",
    "avatar.action.tool_skipped": "主回复动作工具已切换",
    "avatar.intent.skipped": "重复动作意图已抑制",
    "llm.completed": "模型生成完成",
    "llm.error": "模型生成失败",
    "tts.completed": "语音合成完成",
    "tts.error": "语音合成失败",
    "persona.convert.started": "开始转换具身人格",
    "persona.convert.completed": "具身人格预览转换完成",
    "persona.convert.failed": "具身人格转换失败",
    "persona.convert.job.queued": "人格转换后台任务已排队",
    "persona.convert.job.cancelled": "人格转换后台任务已取消",
    "persona.convert.cancelled": "人格转换后台任务已取消",
    "persona.convert.source.started": "开始读取人格来源",
    "persona.convert.source.completed": "人格来源读取完成",
    "persona.convert.model.started": "转换模型开始生成",
    "persona.convert.model.first_chunk": "转换模型首个流块已到达",
    "persona.convert.model.streaming": "转换模型正在持续生成",
    "persona.convert.model.completed": "转换模型生成完成",
    "persona.convert.validation.started": "开始校验转换结果结构",
    "persona.convert.validation.completed": "转换结果结构校验完成",
    "persona.convert.draft.created": "转换预览草稿已就绪",
    "persona.convert.progress": "人格转换任务实时进度",
    "persona.save.started": "开始保存具身人格",
    "persona.save.completed": "具身人格文件保存完成",
    "persona.save.failed": "具身人格保存失败",
    "persona.activate.started": "开始切换当前具身人格",
    "persona.activate.completed": "当前具身人格切换完成",
    "persona.activate.failed": "具身人格切换失败",
    "persona.overlay.injected": "具身人格已注入 Quest 对话",
    "persona.overlay.skipped": "本轮未注入具身人格",
    "reply.completed": "回复交付完成",
    "reply.failed": "回复交付失败",
    "http.health": "健康检查完成",
    "http.error": "HTTP 请求失败"
  };
  return labels[String(value || "")] || String(value || "诊断事件");
}

function diagnosticStatusLabel(value) {
  const labels = {
    ok: "正常",
    ready: "就绪",
    authorized: "已授权",
    connected: "已连接",
    completed: "完成",
    processing: "处理中",
    uploading: "上传中",
    awaiting_audio: "等待音频",
    limited: "受限",
    unavailable: "不可用",
    fallback: "已降级",
    blocked: "已阻止",
    error: "错误",
    failed: "失败",
    timeout: "超时",
    disconnected: "已断开",
    cancelled: "已取消",
    no_action: "无需动作",
    selected: "已选择",
    superseded: "已由更早结果接管",
    closed: "已关闭"
  };
  return labels[String(value || "")] || String(value || "状态未知");
}

function diagnosticMeta(event) {
  const parts = [];
  if (
    String(event.event || "").startsWith("persona.convert.") &&
    event.phase
  ) {
    parts.push(`阶段：${personaConversionStageLabel(event.phase)}`);
  }
  if (Number.isFinite(event.http_status)) parts.push(`HTTP ${event.http_status}`);
  if (Number.isFinite(event.duration_ms)) parts.push(`${Math.round(event.duration_ms)} ms`);
  if (Number.isFinite(event.chunks)) parts.push(`${event.chunks} 块`);
  if (Number.isFinite(event.bytes)) parts.push(`${event.bytes} 字节`);
  if (Number.isFinite(event.event_count)) parts.push(`${event.event_count} 个事件`);
  if (event.operation) parts.push(`动作：${String(event.operation)}`);
  if (event.action_source) parts.push(`来源：${String(event.action_source)}`);
  if (event.method) parts.push(`方式：${String(event.method)}`);
  if (event.catalog_status) parts.push(`目录：${String(event.catalog_status)}`);
  if (event.eventbus_tool_called === true) parts.push("EventBus 工具已调用");
  if (event.eventbus_tool_called === false) parts.push("EventBus 工具未调用");
  if (event.authorized === true) parts.push("身份已授权");
  if (event.authorized === false) parts.push("身份未授权");
  return parts.join(" · ");
}

function renderDiagnosticEvents(events) {
  const container = document.getElementById("diagnostics-events");
  const previousScrollTop = Number(container.scrollTop || 0);
  const previousScrollHeight = Number(container.scrollHeight || 0);
  const previousClientHeight = Number(container.clientHeight || 0);
  const previousDistanceFromBottom = Math.max(
    0,
    previousScrollHeight - previousScrollTop - previousClientHeight,
  );
  container.replaceChildren();
  const restoreScrollPosition = () => {
    if (diagnosticAutoScroll) {
      container.scrollTop = container.scrollHeight;
      return;
    }
    const nextMax = Math.max(
      0,
      Number(container.scrollHeight || 0) - Number(container.clientHeight || 0),
    );
    container.scrollTop = previousDistanceFromBottom <= 24
      ? Math.max(0, nextMax - previousDistanceFromBottom)
      : Math.min(previousScrollTop, nextMax);
  };
  if (!events.length && !personaConversionJobSnapshot) {
    const empty = document.createElement("p");
    empty.className = "diagnostics-empty";
    empty.textContent = "尚无诊断事件。发起一次 Quest 连接或对话后再刷新。";
    container.append(empty);
    restoreScrollPosition();
    return;
  }
  const timeline = events.slice(-39);
  if (personaConversionJobSnapshot) {
    const snapshot = personaConversionJobSnapshot;
    timeline.push({
      timestamp: new Date().toISOString(),
      event: "persona.convert.progress",
      component: "persona",
      status: isPersonaConversionJobFinished(snapshot.status)
        ? snapshot.status
        : "processing",
      phase: snapshot.stage,
      duration_ms: snapshot.elapsed_ms
    });
  }
  timeline.forEach((event) => {
    const item = document.createElement("div");
    const status = String(event.status || "");
    item.className = `diagnostic-line status-${status || "unknown"}`;
    const reason = event.reason_code || event.code;
    const timestamp = event.timestamp
      ? new Date(event.timestamp).toLocaleTimeString("zh-CN", { hour12: false })
      : "--:--:--";
    const parts = [
      `${timestamp} [${diagnosticStageLabel(event.component)}] ${diagnosticStatusLabel(status)}`,
      diagnosticEventLabel(event.event),
      reason ? diagnosticReasonLabel(reason) : "",
      diagnosticMeta(event)
    ].filter(Boolean);
    item.textContent = parts.join(" · ");
    container.append(item);
  });
  restoreScrollPosition();
}

function renderDiagnosticSummary(events) {
  const summary = document.getElementById("diagnostics-summary");
  const latestHttp = events.slice().reverse().find((event) =>
    Number.isFinite(event.http_status));
  const latestInput = events.slice().reverse().find((event) =>
    Number.isFinite(event.chunks) || Number.isFinite(event.bytes));
  const durations = {};
  events.forEach((event) => {
    if (Number.isFinite(event.duration_ms)) {
      durations[String(event.component || "runtime")] = Math.round(event.duration_ms);
    }
  });
  const durationText = Object.entries(durations).slice(-5)
    .map(([stage, value]) => `${diagnosticStageLabel(stage)} ${value}ms`)
    .join(" · ") || "暂无耗时记录";
  summary.replaceChildren();
  [
    `链路：${serviceState?.status === "running" ? "服务运行中" : "服务需要检查"} · ${events.length} 个事件` +
      (latestHttp ? ` · HTTP ${latestHttp.http_status}` : ""),
    latestInput
      ? `输入：${Number(latestInput.chunks || 0)}块/${Number(latestInput.bytes || 0)}B`
      : "输入：暂无音频块记录",
    `耗时：${durationText}`
  ].forEach((value) => {
    const line = document.createElement("p");
    line.textContent = value;
    summary.append(line);
  });
}

async function loadDiagnostics({ silent = false } = {}) {
  if (diagnosticsRefreshInFlight) return diagnosticsRefreshInFlight;
  const button = document.getElementById("load-diagnostics");
  diagnosticsRefreshInFlight = (async () => {
    if (!silent && !setButtonBusy(button, true, "读取中……")) return false;
    try {
      const response = await apiGet("pairing/diagnostics");
      const diagnostics = response.diagnostics || {};
      const events = Array.isArray(diagnostics.events) ? diagnostics.events : [];
      const statusLabels = {
        ready: "可用",
        memory_only: "内存诊断可用（文件日志未启用）",
        disabled: "未启用",
        unavailable: "暂不可用"
      };
      const status = String(diagnostics.status || "unavailable");
      document.getElementById("diagnostics-status").textContent =
        `实时刷新 · 状态：${statusLabels[status] || status} · 事件：${events.length} 条`;
      const rootCause = diagnostics.root_cause || {};
      document.getElementById("diagnostics-root-cause").textContent = rootCause.code
        ? `当前根因：${diagnosticStageLabel(rootCause.stage)} · ${diagnosticReasonLabel(rootCause.code)}`
        : "当前根因：未发现明确的失败事件";
      renderDiagnosticSummary(events);
      renderDiagnosticEvents(events);
      return true;
    } catch (error) {
      if (!silent) {
        document.getElementById("diagnostics-status").textContent =
          "诊断日志暂不可用";
        document.getElementById("diagnostics-root-cause").textContent =
          "当前根因：诊断接口读取失败";
        toast("读取诊断日志失败：" + error.message, true);
      }
      return false;
    } finally {
      if (!silent) setButtonBusy(button, false);
    }
  })();
  try {
    return await diagnosticsRefreshInFlight;
  } finally {
    diagnosticsRefreshInFlight = null;
  }
}

function stopDiagnosticsRefresh() {
  if (diagnosticsRefreshTimer !== null) {
    window.clearTimeout(diagnosticsRefreshTimer);
    diagnosticsRefreshTimer = null;
  }
}

function scheduleDiagnosticsRefresh(delay = DIAGNOSTICS_REFRESH_MS) {
  stopDiagnosticsRefresh();
  if (document.hidden || !bridgeReady) return;
  diagnosticsRefreshTimer = window.setTimeout(async () => {
    diagnosticsRefreshTimer = null;
    await loadDiagnostics({ silent: true });
    scheduleDiagnosticsRefresh();
  }, delay);
}

function handlePageVisibilityChange() {
  if (document.hidden) {
    stopDiagnosticsRefresh();
    return;
  }
  scheduleDiagnosticsRefresh(0);
  startServiceRefresh();
  if (personaConversionJobId && personaConversionPollTimer === null) {
    schedulePersonaConversionPoll(0);
  }
}

function startServiceRefresh() {
  if (serviceRefreshTimer !== null || !bridgeReady || document.hidden) return;
  serviceRefreshTimer = window.setInterval(
    () => loadServiceStatus({ silent: true }),
    10000
  );
}

function clearPageTimers() {
  stopDiagnosticsRefresh();
  if (serviceRefreshTimer !== null) {
    window.clearInterval(serviceRefreshTimer);
    serviceRefreshTimer = null;
  }
  if (personaConversionPollTimer !== null) {
    window.clearTimeout(personaConversionPollTimer);
    personaConversionPollTimer = null;
  }
}

function bindEvents() {
  document
    .getElementById("refresh-service-button")
    .addEventListener("click", () => loadServiceStatus());
  document
    .getElementById("service-control-button")
    .addEventListener("click", toggleService);
  document
    .getElementById("save-listener-port-button")
    .addEventListener("click", saveListenerPort);
  document.getElementById("chat-provider-id").addEventListener("change", (event) => {
    document.getElementById("save-model-button").disabled =
      !event.currentTarget.value;
  });
  document
    .getElementById("save-model-button")
    .addEventListener("click", saveModelSelection);
  document
    .getElementById("fast-action-enabled")
    .addEventListener("change", () => {
      const enabled = document.getElementById("fast-action-enabled").checked;
      document.getElementById("save-fast-action-button").disabled =
        fastActionSettings?.config_writable !== true ||
        (enabled && !document.getElementById("fast-action-provider-id").value);
    });
  document
    .getElementById("fast-action-provider-id")
    .addEventListener("change", () => {
      const enabled = document.getElementById("fast-action-enabled").checked;
      document.getElementById("save-fast-action-button").disabled =
        fastActionSettings?.config_writable !== true ||
        (enabled && !document.getElementById("fast-action-provider-id").value);
    });
  document
    .getElementById("save-fast-action-button")
    .addEventListener("click", saveFastActionSettings);
  document
    .getElementById("save-dialogue-mode-button")
    .addEventListener("click", saveDialogueMode);
  document
    .getElementById("save-stt-button")
    .addEventListener("click", saveSttSettings);
  document
    .getElementById("save-platform-button")
    .addEventListener("click", savePlatformSettings);
  document
    .getElementById("save-persona-button")
    .addEventListener("click", savePersonaSettings);
  document
    .getElementById("save-quest-identity-button")
    .addEventListener("click", saveQuestIdentitySettings);
  document
    .getElementById("persona-source-mode")
    .addEventListener("change", () => {
      renderPersonaSettings({
        ...personaSettings,
        source_mode: document.getElementById("persona-source-mode").value
      });
    });
  document.querySelectorAll("[data-persona-workflow-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextMode = button.dataset.personaWorkflowMode;
      if (nextMode !== personaWorkflowMode) {
        clearPersonaProfileEditor();
      }
      setPersonaWorkflowMode(nextMode);
    });
    button.addEventListener("keydown", (event) => {
      const tabs = Array.from(
        document.querySelectorAll("[data-persona-workflow-mode]")
      );
      const current = tabs.indexOf(event.currentTarget);
      let target = -1;
      if (event.key === "ArrowRight") target = (current + 1) % tabs.length;
      if (event.key === "ArrowLeft") target = (current - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") target = 0;
      if (event.key === "End") target = tabs.length - 1;
      if (target < 0) return;
      event.preventDefault();
      tabs[target].click();
      tabs[target].focus();
    });
  });
  document
    .getElementById("persona-converter-provider")
    .addEventListener("change", () => {
      invalidatePersonaDraft("转换模型已改变，请重新转换后再保存。", false);
      document.getElementById("save-persona-converter-provider").disabled =
        !document.getElementById("persona-converter-provider").value ||
        document.getElementById("persona-converter-provider").value ===
          String(personaProfiles?.persona_converter_provider_id || "");
      updatePersonaEditorActions();
    });
  document
    .getElementById("save-persona-converter-provider")
    .addEventListener("click", savePersonaConverterProvider);
  document
    .getElementById("new-persona-profile-button")
    .addEventListener("click", () => {
      clearPersonaProfileEditor();
      setPersonaWorkflowMode("independent");
      document.getElementById("persona-profile-name").focus();
    });
  document
    .getElementById("persona-import-source")
    .addEventListener("change", () => {
      document.getElementById("persona-source-prompt").value = "";
      invalidatePersonaDraft("来源人格已改变，请重新转换后再保存。", true);
    });
  document
    .getElementById("persona-source-prompt")
    .addEventListener("input", () => {
      const convertedProfile = Boolean(
        document.getElementById("persona-profile-id").value &&
        personaOpenedConverterPromptVersion &&
        personaOpenedConverterPromptVersion !== "manual"
      );
      invalidatePersonaDraft(
        "来源正文已改变，请重新转换后再保存。",
        convertedProfile
      );
    });
  document
    .getElementById("persona-admin-requirements")
    .addEventListener("input", () => {
      invalidatePersonaDraft("转换补充要求已改变，请重新转换后再保存。", false);
    });
  ["persona-profile-name", "persona-profile-aliases", "quest-persona-prompt"]
    .forEach((id) => {
      document.getElementById(id).addEventListener("input", updatePersonaEditorActions);
    });
  document
    .getElementById("convert-persona-button")
    .addEventListener("click", convertPersona);
  document
    .getElementById("cancel-persona-conversion-button")
    .addEventListener("click", cancelPersonaConversion);
  document
    .getElementById("save-persona-profile-button")
    .addEventListener("click", savePersonaProfile);
  document
    .getElementById("activate-persona-profile-button")
    .addEventListener("click", activatePersonaProfile);
  document
    .getElementById("load-identity-candidates")
    .addEventListener("click", loadIdentityCandidates);
  document
    .getElementById("save-identity-button")
    .addEventListener("click", saveIdentitySelection);
  document
    .getElementById("load-diagnostics")
    .addEventListener("click", () => loadDiagnostics());
  const autoScroll = document.getElementById("diagnostics-auto-scroll");
  autoScroll.checked = diagnosticAutoScroll;
  autoScroll.addEventListener("change", () => {
    setDiagnosticAutoScroll(autoScroll.checked);
  });
  document.addEventListener("visibilitychange", handlePageVisibilityChange);
  window.addEventListener("pagehide", clearPageTimers);
  window.addEventListener("pageshow", handlePageVisibilityChange);
}

// Keep the page interactive while the parent Dashboard context is still loading.
// AstrBot's bridge.ready() intentionally waits for that context and can otherwise
// leave every handler unbound when the page is opened directly or restored from cache.
function withBridgeTimeout(promise, timeout, message) {
  let timer = null;
  const timeoutPromise = new Promise((_, reject) => {
    timer = window.setTimeout(() => reject(new Error(message)), timeout);
  });
  return Promise.race([Promise.resolve(promise), timeoutPromise]).finally(() => {
    if (timer !== null) window.clearTimeout(timer);
  });
}

async function apiGet(name) {
  if (!bridge || !bridgeReady) throw new Error("页面 Bridge 尚未连接");
  return parseResponse(
    await withBridgeTimeout(bridge.apiGet(name), 10000, "页面 Bridge 请求超时")
  );
}

async function apiPost(
  name,
  payload,
  { timeout = PAGE_REQUEST_TIMEOUT_MS, timeoutMessage = "页面 Bridge 请求超时" } = {}
) {
  if (!bridge || !bridgeReady) throw new Error("页面 Bridge 尚未连接");
  return parseResponse(
    await withBridgeTimeout(bridge.apiPost(name, payload), timeout, timeoutMessage)
  );
}

function clearStartupError() {
  const node = document.getElementById("startup-error");
  node.hidden = true;
  node.replaceChildren();
}

const INITIAL_DATA_SECTIONS = [
  { key: "service", label: "服务状态", load: () => loadServiceStatus() },
  { key: "operator", label: "聊天模型", load: loadOperatorSettings },
  { key: "fast-action", label: "快速动作", load: loadFastActionSettings },
  { key: "stt", label: "语音识别", load: loadSttSettings },
  { key: "platform", label: "正式消息链路", load: loadPlatformSettings },
  { key: "persona", label: "实时人格", load: loadPersonaSettings },
  { key: "persona-library", label: "具身人格库", load: loadPersonaProfiles },
  { key: "quest-identity", label: "Quest 身份", load: loadQuestIdentitySettings }
];

function markInitialSectionFailed(key) {
  const messages = {
    operator: ["model-status", "聊天模型读取失败，可单独重试。"],
    "fast-action": ["fast-action-status", "快速动作设置读取失败，可单独重试。"],
    stt: ["stt-status", "语音识别设置读取失败，可单独重试。"],
    platform: ["platform-status", "正式消息链路读取失败，可单独重试。"],
    persona: ["persona-status", "实时人格读取失败，可单独重试。"],
    "quest-identity": ["quest-identity-status", "Quest 身份读取失败，可单独重试。"]
  };
  const target = messages[key];
  if (target) document.getElementById(target[0]).textContent = target[1];
}

function showInitialDataError(failedSections) {
  const node = document.getElementById("startup-error");
  node.replaceChildren();
  const message = document.createElement("span");
  message.textContent =
    "页面已连接，但部分设置读取失败：" +
    failedSections.map((section) => section.label).join("、");
  const retry = document.createElement("button");
  retry.type = "button";
  retry.id = "retry-initial-data-button";
  retry.className = "primary startup-retry";
  retry.textContent = "重试失败区域";
  retry.addEventListener("click", () =>
    loadInitialData(failedSections.map((section) => section.key))
  );
  node.append(message, retry);
  node.hidden = false;
  setRuntimeState("warning", "页面已连接，部分设置未加载");
}

async function loadInitialData(sectionKeys = null) {
  if (initialDataPromise) return initialDataPromise;
  const requested = Array.isArray(sectionKeys) ? new Set(sectionKeys) : null;
  const sections = requested
    ? INITIAL_DATA_SECTIONS.filter((section) => requested.has(section.key))
    : INITIAL_DATA_SECTIONS;
  clearStartupError();
  initialDataPromise = (async () => {
    const results = await Promise.allSettled(
      sections.map(async (section) => {
        const loaded = await section.load();
        if (loaded === false) throw new Error(section.key);
        return section.key;
      })
    );
    const failedSections = sections.filter(
      (_section, index) => results[index].status === "rejected"
    );
    failedSections.forEach((section) => markInitialSectionFailed(section.key));
    if (failedSections.length) {
      showInitialDataError(failedSections);
      return false;
    }
    clearStartupError();
    if (serviceState) renderServiceStatus(serviceState);
    else setRuntimeState("ready", "页面 Bridge 已连接");
    return true;
  })();
  try {
    return await initialDataPromise;
  } finally {
    initialDataPromise = null;
  }
}

function showStartupError(error) {
  const node = document.getElementById("startup-error");
  node.replaceChildren();
  const message = document.createElement("span");
  message.textContent = "页面连接失败：" + (error?.message || error);
  const retry = document.createElement("button");
  retry.type = "button";
  retry.id = "retry-startup-button";
  retry.className = "primary startup-retry";
  retry.textContent = "重试连接";
  retry.addEventListener("click", initializeBridgeAndData);
  node.append(message, retry);
  node.hidden = false;
  setRuntimeState("error", "页面 Bridge 不可用");
}

let bridgeInitPromise = null;

async function initializeBridgeAndData() {
  if (bridgeInitPromise) return bridgeInitPromise;
  bridgeInitPromise = (async () => {
    bridgeReady = false;
    clearStartupError();
    setRuntimeState("warning", "正在连接页面 Bridge…");
    bridge = await resolveBridge(8000);
    if (typeof bridge.ready !== "function") {
      throw new Error("页面 Bridge ready() 不可用");
    }
    await withBridgeTimeout(
      bridge.ready(),
      8000,
      "等待 AstrBot 页面上下文超时，请从插件管理页面重新打开"
    );
    bridgeReady = true;
    clearStartupError();
    setPersonaWorkflowMode(personaWorkflowMode);
  })();
  try {
    await bridgeInitPromise;
  } catch (error) {
    bridgeReady = false;
    showStartupError(error);
    return false;
  } finally {
    bridgeInitPromise = null;
  }
  await loadInitialData();
  if (!personaConversionJobId) {
    const context = restorePersonaConversionContext();
    personaConversionJobId = String(context?.job_id || "");
    if (personaConversionJobId) restorePersonaConversionEditor(context);
  }
  if (personaConversionJobId) {
    const convertButton = document.getElementById("convert-persona-button");
    setPersonaConversionLocked(true);
    setButtonBusy(convertButton, true, "恢复转换任务…");
    document.getElementById("cancel-persona-conversion-button").hidden = false;
    schedulePersonaConversionPoll(0);
  }
  scheduleDiagnosticsRefresh(0);
  startServiceRefresh();
  return true;
}

async function init() {
  if (!eventsBound) {
    bindEvents();
    eventsBound = true;
  }
  await initializeBridgeAndData();
}

init();
