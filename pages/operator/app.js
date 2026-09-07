let bridge = null;
let operatorSettings = null;
let fastActionSettings = null;
let sttSettings = null;
let personaSettings = null;
let platformSettings = null;
let questIdentitySettings = null;
let questChainSettings = null;
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
  node.classList.toggle("warning", kind === "warning");
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

// 表驱动保存助手：统一 busy 态 / 错误提示 / finally 复位，各分区只提供
// endpoint、payload 与差异化回调（协议字段名保持既有形态，见各 payload()）。
async function saveSection({
  button,
  busyText,
  endpoint,
  payload,
  okToast,
  errorToast,
  onOk,
  onError,
  onFinally,
}) {
  if (!setButtonBusy(button, true, busyText ?? button.textContent)) return;
  try {
    const response = await apiPost(endpoint, payload());
    if (onOk) await onOk(response);
    const message = typeof okToast === "function" ? okToast(response) : okToast;
    if (message) toast(message);
  } catch (error) {
    if (onError) {
      onError(error);
    } else {
      const prefix = typeof errorToast === "function"
        ? errorToast(error)
        : errorToast ?? "保存失败：";
      toast(prefix + error.message, true);
    }
  } finally {
    setButtonBusy(button, false);
    if (onFinally) onFinally();
  }
}

// 同构下拉渲染：占位项 + 可选项 + “已配置但不可用”兜底项 + 选中与禁用。
function fillSelect(select, {
  items,
  selected = "",
  emptyLabel,
  itemLabel = (item) => String(item?.id || ""),
  itemValue = (item) => String(item?.id || ""),
  missingLabel = (id) => "已配置但当前不可用 · " + id,
  disabled = false,
}) {
  select.replaceChildren(new Option(emptyLabel, ""));
  items.forEach((item) => {
    const value = itemValue(item);
    if (!value) return;
    select.add(new Option(itemLabel(item), value));
  });
  if (selected && !items.some((item) => itemValue(item) === selected)) {
    select.add(new Option(missingLabel(selected), selected));
  }
  select.value = selected;
  select.disabled = disabled;
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
  badge.textContent = serviceState.status_label || "未知";
  badge.className = "status-badge " + status;

  document.getElementById("service-summary").textContent =
    serviceState.reason_label || "服务状态需要检查";
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
  ["dialogue", "bridge", "identity_configured", "stt", "tts", "avatar_actions"]
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
  if (!providers.length) {
    select.replaceChildren(new Option("没有可用的决策 / 回退 Provider", ""));
    select.disabled = true;
  } else {
    fillSelect(select, {
      items: providers,
      selected: operatorSettings.selected_id || "",
      emptyLabel: "请选择决策 / 回退模型",
      itemLabel: providerLabel,
      missingLabel: (id) => "已配置但不可用 · " + id,
      disabled: operatorSettings.config_writable !== true,
    });
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

// ---------------------------------------------------------------------------
// Quest 链路模式（双模）：主链路 / 临独立链路 / 自动回退，按模式条件显隐设置区。
// ---------------------------------------------------------------------------

function renderQuestChainSettings(settings) {
  questChainSettings = settings || {};
  const button = document.getElementById("save-quest-chain-button");
  const status = document.getElementById("quest-chain-status");
  if (!button || !status) return;
  const writable = questChainSettings.config_writable === true;
  const perHook = document.getElementById("quest-chain-per-hook");
  const totalHook = document.getElementById("quest-chain-total-hook");
  const llmTimeout = document.getElementById("quest-chain-llm-timeout");
  const cacheTtl = document.getElementById("quest-chain-cache-ttl");
  const excluded = document.getElementById("quest-chain-excluded");
  if (perHook) perHook.value = questChainSettings.per_hook_budget_seconds ?? 6.0;
  if (totalHook) totalHook.value = questChainSettings.total_hook_budget_seconds ?? 10.0;
  if (llmTimeout) llmTimeout.value = questChainSettings.llm_timeout_seconds ?? 30.0;
  if (cacheTtl) cacheTtl.value = questChainSettings.memory_cache_ttl_seconds ?? 30.0;
  if (excluded) excluded.value = questChainSettings.excluded_plugins || "";
  [perHook, totalHook, llmTimeout, cacheTtl, excluded].forEach((input) => {
    if (input) input.disabled = !writable;
  });
  button.disabled = !writable;
  if (!writable) {
    status.textContent = "当前 AstrBot 配置对象不支持安全保存。";
    return;
  }
  status.textContent =
    questChainSettings.bridge_available === true
      ? "临专属链路：就绪"
      : "临专属链路不可用：" + (questChainSettings.bridge_availability_reason || "未知原因");
}

async function loadQuestChainSettings() {
  const response = await apiGet("pairing/quest-chain-settings");
  renderQuestChainSettings(response.quest_chain);
}

async function saveQuestChainSettings() {
  const button = document.getElementById("save-quest-chain-button");
  if (!button || button.disabled) return;
  await saveSection({
    button,
    // 保持既有交互：busy 期间不替换按钮文案。
    endpoint: "pairing/quest-chain-settings",
    payload: () => {
      const numberValue = (id) => {
        const node = document.getElementById(id);
        const value = node ? Number(node.value) : NaN;
        return Number.isFinite(value) ? value : null;
      };
      return {
        per_hook_budget_seconds: numberValue("quest-chain-per-hook"),
        total_hook_budget_seconds: numberValue("quest-chain-total-hook"),
        llm_timeout_seconds: numberValue("quest-chain-llm-timeout"),
        memory_cache_ttl_seconds: numberValue("quest-chain-cache-ttl"),
        excluded_plugins: (document.getElementById("quest-chain-excluded") || {}).value || ""
      };
    },
    okToast: "已保存：临专属链路参数",
    onOk: (response) => renderQuestChainSettings(response.quest_chain),
    onError: (error) => toast(error.message || "保存链路模式失败", true),
    onFinally: () => {
      button.disabled = questChainSettings?.config_writable !== true;
    },
  });
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
  fillSelect(select, {
    items: providers,
    selected,
    emptyLabel: "请选择快速动作模型",
    itemLabel: providerLabel,
    disabled: !writable || !providers.length,
  });
  const effectiveTimeout = Number(fastActionSettings.effective_timeout_seconds);
  const timeoutValue = fastActionTimeoutInputValue(fastActionSettings);
  if (document.activeElement !== timeoutInput) timeoutInput.value = String(timeoutValue);
  timeoutInput.disabled = !writable;
  timeoutHelp.textContent = Number.isFinite(effectiveTimeout)
    ? `有效超时：${effectiveTimeout.toFixed(1)} 秒 · 策略：${String(fastActionSettings.timeout_policy_revision || "v3")}` +
      (fastActionSettings.timeout_migrated === true ? " · 旧4秒策略已安全迁移到6秒" : "")
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

function fastActionTimeoutInputValue(settings) {
  const configuredTimeout = Number(settings?.configured_timeout_seconds);
  const effectiveTimeout = Number(settings?.effective_timeout_seconds);
  if (settings?.timeout_migrated === true && Number.isFinite(effectiveTimeout)) {
    return effectiveTimeout;
  }
  return Number.isFinite(configuredTimeout)
    ? configuredTimeout
    : Number.isFinite(effectiveTimeout) ? effectiveTimeout : 6;
}

async function saveFastActionSettings() {
  const button = document.getElementById("save-fast-action-button");
  const enabled = document.getElementById("fast-action-enabled").checked;
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/fast-action-settings",
    payload: () => {
      const providerId = document.getElementById("fast-action-provider-id").value;
      const timeoutSeconds = Number(document.getElementById("fast-action-timeout-seconds").value);
      return {
        enabled,
        provider_id: providerId,
        timeout_seconds: timeoutSeconds
      };
    },
    okToast: () => enabled
      ? "异步快速动作已启用"
      : "异步快速动作已关闭，动作将走主回复链路",
    errorToast: "快速动作设置保存失败：",
    onOk: (response) => renderFastActionSettings(response.fast_action),
    onFinally: () => renderFastActionSettings(fastActionSettings || {}),
  });
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
  fillSelect(select, {
    items: providers,
    selected,
    emptyLabel: "关闭 Quest 语音识别",
    itemLabel: sttProviderLabel,
    disabled: !writable,
  });
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
  fillSelect(select, {
    items: platforms,
    selected,
    emptyLabel: "不使用正式消息平台",
    itemLabel: (item) => {
      const id = String(item?.id || "");
      const displayName = String(item?.display_name || item?.adapter_type || id);
      const adapterType = String(item?.adapter_type || "");
      return displayName === adapterType
        ? displayName + " · " + id
        : displayName + " · " + adapterType + " · " + id;
    },
    missingLabel: (id) => "已配置但不可用 · " + id,
    disabled: platformSettings.config_writable !== true || !platforms.length,
  });
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
  const selectedPersona = String(personaSettings.persona_selected
    ? personaSettings.astrbot_persona_id || ""
    : "");
  fillSelect(personaSelect, {
    items: personas,
    selected: selectedPersona,
    emptyLabel: "AstrBot 明确默认人格",
    itemLabel: (item) => String(item.id || ""),
    itemValue: (item) => String(item.id || ""),
    missingLabel: () => "已选择但不可用",
    disabled: !writable || sourceMode !== "astrbot",
  });

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
  fillSelect(select, {
    items: providers,
    selected,
    emptyLabel: "请选择转换模型",
    itemLabel: (provider) => {
      const id = String(provider?.id || "");
      const model = String(provider?.model || "未标注模型");
      const adapter = String(provider?.adapter_type || "未知适配器");
      return `${model} · ${adapter} · ${id}`;
    },
    missingLabel: () => "已配置但当前不可用",
    disabled: catalog.config_writable === false || providers.length === 0,
  });
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
  const personaId = (persona) => String(persona?.id || persona?.persona_id || "");
  fillSelect(select, {
    items: sources,
    selected: current && sources.some((persona) => personaId(persona) === current)
      ? current
      : "",
    emptyLabel: "请选择 AstrBot 来源人格",
    itemLabel: (persona) => {
      const id = personaId(persona);
      const name = String(persona?.display_name || persona?.name || id);
      return name === id ? id : `${name} · ${id}`;
    },
    itemValue: personaId,
    disabled: catalog.config_writable === false || sources.length === 0,
  });
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
  if (!providerId) return;
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/persona-converter-settings",
    payload: () => ({ persona_converter_provider_id: providerId }),
    onOk: (response) => {
      if (response.library) renderPersonaProfiles(response.library);
      else if (personaProfiles) {
        personaProfiles.persona_converter_provider_id = providerId;
        personaProfiles.converter_provider_id = providerId;
        personaProfiles.converter_selected_available = true;
      }
    },
    okToast: "人格转换模型已保存",
    errorToast: "转换模型保存失败：",
    onFinally: () => {
      button.disabled = !document.getElementById("persona-converter-provider").value ||
        document.getElementById("persona-converter-provider").value ===
          String(personaProfiles?.persona_converter_provider_id || "");
      updatePersonaEditorActions();
    },
  });
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
    // 首个轮询返回前的本地占位；后续快照一律以后端 label 字段为准。
    status_label: "排队中",
    stage_label: "任务已受理",
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

function isPersonaConversionJobFinished(status) {
  return ["completed", "failed", "cancelled"].includes(String(status || ""));
}

function personaConversionErrorMessage(job) {
  const code = job?.error_code || job?.error?.code;
  return String(
    job?.error_message ||
    job?.error?.message ||
    (code ? job?.error?.label || String(code) : "") ||
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
  const stage = String(
    job?.stage_label || job?.stage || "正在处理转换任务"
  );
  personaConversionJobSnapshot = {
    status,
    stage: String(job?.stage || "accepted"),
    status_label: String(job?.status_label || ""),
    stage_label: String(job?.stage_label || ""),
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

let qpButtonReady = false;

const QUICK_PAIRING_REASONS = {
  bridge_key_missing: "Bridge 长期密钥尚未配置",
  quick_pairing_defaults_missing: "快速绑定服务端配置尚未完成（先完成上方基础绑定）",
  pairing_listener_public_url_missing: "快速绑定公开入口尚未配置（需配置内置监听器公开 URL）",
  pairing_bootstrap_unavailable: "快速绑定交换入口不可用"
};

/* ── 快速绑定弹窗（页内完成，不跳转：Dashboard 禁止窗口打开）── */
let qpPairing = null;
let qpCountdownTimer = null;
let qpStatusTimer = null;
let qpStatusInFlight = false;

function qpStopTimers() {
  window.clearInterval(qpCountdownTimer);
  window.clearInterval(qpStatusTimer);
  qpCountdownTimer = null;
  qpStatusTimer = null;
}

function qpSetBusy(busy, text) {
  const button = document.getElementById("open-quick-pairing-button");
  if (busy) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = text || "正在生成…";
  } else {
    button.setAttribute("aria-busy", "false");
    button.textContent = "生成配对二维码 / 6 位配对码";
    button.disabled = qpButtonReady !== true;
  }
}

function qpFormatRemaining(seconds) {
  const remaining = Math.max(0, Math.ceil(seconds));
  return `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(
    remaining % 60
  ).padStart(2, "0")}`;
}

const QP_STATE_LABELS = {
  waiting: "等待客户端兑换",
  consumed: "客户端已完成绑定",
  expired: "配对码已过期",
  revoked: "配对已撤销",
  unknown: "状态暂时无法识别，请重新生成"
};

function qpRenderState(state) {
  if (!qpPairing) return;
  const known = new Set(["waiting", "consumed", "expired", "revoked"]);
  const normalized = known.has(state) ? state : "unknown";
  qpPairing.state = normalized;
  const label = document.getElementById("qp-status");
  label.textContent = QP_STATE_LABELS[normalized];
  const active = normalized === "waiting";
  document.getElementById("qp-copy").disabled = !active;
  document.getElementById("qp-revoke").disabled = !active;
  if (normalized === "consumed") toast("客户端已获取配置并完成绑定");
  if (!active) qpStopTimers();
}

function qpUpdateCountdown() {
  if (!qpPairing) return;
  const remaining = qpPairing.expires_at - Date.now() / 1000;
  document.getElementById("qp-countdown").textContent =
    qpFormatRemaining(remaining);
  if (remaining <= 0 && qpPairing.state === "waiting") qpRenderState("expired");
}

async function qpRefreshStatus() {
  if (!qpPairing || qpPairing.state !== "waiting") return;
  const response = await apiPost("pairing/status", {
    pairing_id: qpPairing.pairing_id
  });
  const current = response.pairing;
  qpPairing.expires_at = current.expires_at;
  if (current.state !== qpPairing.state) qpRenderState(current.state);
}

function qpStartTimers() {
  qpStopTimers();
  qpCountdownTimer = window.setInterval(qpUpdateCountdown, 250);
  qpStatusTimer = window.setInterval(() => {
    if (qpStatusInFlight || document.hidden) return;
    qpStatusInFlight = true;
    qpRefreshStatus().catch(() => {}).finally(() => {
      qpStatusInFlight = false;
    });
  }, 1800);
}

async function qpCreate() {
  const empty = document.getElementById("qp-empty");
  const result = document.getElementById("qp-result");
  empty.hidden = false;
  document.getElementById("qp-empty-text").textContent = "正在生成一次性配对码…";
  result.hidden = true;
  qpStopTimers();
  qpPairing = null;
  try {
    const response = await apiPost("pairing/create", {
      protocol_version: "1.0"
    });
    qpPairing = response.pairing;
    document.getElementById("qp-qr").src = qpPairing.qr_svg_data_uri;
    const code = qpPairing.short_code;
    document.getElementById("qp-short-code").textContent = `${code.slice(0, 3)} ${code.slice(3)}`;
    empty.hidden = true;
    result.hidden = false;
    qpRenderState(qpPairing.state || "waiting");
    qpUpdateCountdown();
    qpStartTimers();
  } catch (error) {
    document.getElementById("qp-empty-text").textContent =
      `生成失败：${error.message}（可关闭后重试）`;
  }
}

function qpClose() {
  document.getElementById("quick-pairing-modal").hidden = true;
  qpStopTimers();
}

async function qpCopy() {
  if (!qpPairing || qpPairing.state !== "waiting") return;
  try {
    await navigator.clipboard.writeText(qpPairing.short_code);
    toast("配对码已复制");
  } catch (_error) {
    window.prompt("复制 6 位配对码", qpPairing.short_code);
  }
}

async function qpRevoke() {
  if (!qpPairing || qpPairing.state !== "waiting") return;
  try {
    const response = await apiPost("pairing/revoke", {
      pairing_id: qpPairing.pairing_id
    });
    qpRenderState(response.pairing.state);
    toast("配对已撤销");
  } catch (error) {
    toast(`撤销失败：${error.message}`, true);
  }
}

function bindQuickPairingModal() {
  document.getElementById("open-quick-pairing-button")
    .addEventListener("click", () => {
      document.getElementById("quick-pairing-modal").hidden = false;
      qpCreate();
    });
  document.getElementById("qp-close").addEventListener("click", qpClose);
  document.getElementById("quick-pairing-modal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) qpClose();
  });
  document.getElementById("qp-copy").addEventListener("click", qpCopy);
  document.getElementById("qp-revoke").addEventListener("click", qpRevoke);
  document.getElementById("qp-regenerate").addEventListener("click", qpCreate);
}

async function loadQuickPairingStatus() {
  const badge = document.getElementById("quick-pairing-badge");
  const status = document.getElementById("quick-pairing-status");
  const button = document.getElementById("open-quick-pairing-button");
  if (!badge || !status || !button) {
    /* index.html 与 app.js 版本错位（浏览器缓存混合）时静默跳过，
       避免把整个区块误报为读取失败；强刷页面即可对齐。 */
    return true;
  }
  try {
    const overview = await apiGet("pairing/overview");
    const ready = overview.quick_pairing_ready === true;
    badge.textContent = ready ? "已就绪" : "未就绪";
    badge.classList.toggle("ready", ready);
    badge.classList.toggle("stopped", !ready);
    badge.classList.remove("loading");
    if (ready) {
      status.textContent = "快速绑定服务已就绪，点击下方按钮生成二维码 / 6 位配对码。";
      qpButtonReady = true;
      button.disabled = false;
    } else {
      const reason = QUICK_PAIRING_REASONS[overview.quick_pairing_reason] ||
        "快速绑定服务当前不可用";
      status.textContent = `快速绑定尚未就绪：${reason}`;
      button.disabled = true;
      qpButtonReady = false;
    }
    return true;
  } catch (error) {
    badge.textContent = "读取失败";
    badge.classList.remove("ready", "loading");
    badge.classList.add("stopped");
    status.textContent = `快速绑定状态读取失败：${error.message}`;
    button.disabled = true;
    qpButtonReady = false;
    return true;
  }
}

async function saveQuestIdentitySettings() {
  const button = document.getElementById("save-quest-identity-button");
  const apiKeyInput = document.getElementById("quest-api-key");
  await saveSection({
    button,
    busyText: "正在保存并验证…",
    endpoint: "pairing/quest-identity-settings",
    payload: () => ({
      client_id: document.getElementById("quest-client-id").value,
      platform_id: document.getElementById("trusted-platform-id").value,
      bot_id: document.getElementById("quest-bot-id").value,
      user_id: document.getElementById("quest-user-id").value,
      api_key: apiKeyInput.value
    }),
    onOk: async (response) => {
      renderQuestIdentitySettings(response.identity);
      await loadPlatformSettings();
    },
    okToast: (response) =>
      response.identity.control_plane?.source === "identity_guardian"
        ? "Quest 身份已保存到“序”并验证"
        : "Quest 身份已保存到“临”的本地精确绑定",
    errorToast: "Quest 身份保存失败：",
    onFinally: () => {
      apiKeyInput.value = "";
      button.disabled = questIdentitySettings?.config_writable !== true;
    },
  });
}

async function savePersonaSettings() {
  const button = document.getElementById("save-persona-button");
  let sourceSaved = false;
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/persona-settings",
    payload: () => ({
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
    }),
    onOk: async (response) => {
      renderPersonaSettings(response.persona);
      sourceSaved = true;
      await loadPersonaProfiles();
    },
    okToast: "实时人格来源已保存并启用",
    onError: (error) => {
      toast(
        sourceSaved
          ? "实时人格来源已启用，但状态刷新失败：" + error.message
          : "角色身份保存失败：" + error.message,
        true
      );
    },
    onFinally: () => {
      button.disabled = personaSettings?.config_writable !== true;
    },
  });
}

async function saveModelSelection() {
  const button = document.getElementById("save-model-button");
  const selected = document.getElementById("chat-provider-id").value;
  if (!selected) return;
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/operator-settings",
    payload: () => ({ chat_provider_id: selected }),
    okToast: "临直连与交互决策模型已保存并立即生效",
    errorToast: "模型保存失败：",
    onOk: (response) => renderOperatorSettings(response.settings),
    onFinally: () => {
      button.disabled = !document.getElementById("chat-provider-id").value;
    },
  });
}

async function saveSttSettings() {
  const button = document.getElementById("save-stt-button");
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/stt-settings",
    payload: () => ({
      provider_id: document.getElementById("stt-provider-id").value
    }),
    okToast: (response) => response.stt?.selected_id
      ? "语音识别 Provider 已保存并立即生效"
      : "Quest 语音识别已关闭",
    errorToast: "语音识别保存失败：",
    onOk: (response) => renderSttSettings(response.stt),
    onFinally: () => {
      button.disabled = sttSettings?.config_writable !== true;
    },
  });
}

async function savePlatformSettings() {
  const button = document.getElementById("save-platform-button");
  await saveSection({
    button,
    busyText: "\u6b63\u5728\u4fdd\u5b58\u2026",
    endpoint: "pairing/platform-settings",
    payload: () => ({
      trusted_platform_id: document.getElementById("trusted-platform-id").value
    }),
    okToast: "\u5e73\u53f0\u5df2\u4fdd\u5b58\u5e76\u7acb\u5373\u751f\u6548",
    errorToast: "\u5e73\u53f0\u4fdd\u5b58\u5931\u8d25\uff1a",
    onOk: (response) => renderPlatformSettings(response.platform),
    onFinally: () => {
      button.disabled = document.getElementById("trusted-platform-id").disabled;
    },
  });
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
  await saveSection({
    button,
    busyText: "正在保存…",
    endpoint: "pairing/identity-selection",
    payload: () => ({ person_id: personId }),
    okToast: () => personId
      ? "自然人与正式消息身份已同步"
      : "已关闭“情”的关系上下文；Quest 基础对话身份保持不变",
    errorToast: "自然人保存失败：",
    onOk: (response) => renderOperatorSettings(response.settings),
    onFinally: () => {
      button.disabled = document.getElementById(
        "relationship-person-select"
      ).disabled;
    },
  });
}

// 诊断事件的中文标签（event/stage/status/reason/action/span 等）已下沉到
// 服务端 core/diagnostic_labels.py，由 pairing/diagnostics 投影时以
// `*_label` 加法字段随事件下发；此处只做 `label || 原始码值` 渲染。

function diagnosticMeta(event) {
  const parts = [];
  if (
    String(event.event || "").startsWith("persona.convert.") &&
    event.phase
  ) {
    parts.push(`阶段：${String(event.phase_label || event.phase)}`);
  }
  if (Number.isFinite(event.http_status)) parts.push(`HTTP ${event.http_status}`);
  if (Number.isFinite(event.duration_ms)) parts.push(`${Math.round(event.duration_ms)} ms`);
  if (Number.isFinite(event.chunks)) parts.push(`${event.chunks} 块`);
  if (Number.isFinite(event.bytes)) parts.push(`${event.bytes} 字节`);
  if (Number.isFinite(event.event_count)) parts.push(`${event.event_count} 个事件`);
  if (event.operation) {
    parts.push(`动作：${String(event.operation_label || event.operation)}`);
  }
  if (event.action_source) {
    parts.push(`来源：${String(event.action_source_label || event.action_source)}`);
  }
  if (event.method) parts.push(`方式：${String(event.method)}`);
  if (event.plugin_name) parts.push(`插件：${String(event.plugin_name)}`);
  if (event.hook) parts.push(`Hook：${String(event.hook)}`);
  if (event.plugin_module) parts.push(`模块：${String(event.plugin_module)}`);
  if (Number.isFinite(event.priority)) parts.push(`优先级：${event.priority}`);
  if (event.stopped === true) parts.push("事件已停止");
  if (event.catalog_status) parts.push(`目录：${String(event.catalog_status)}`);
  if (event.eventbus_tool_called === true) parts.push("EventBus 工具已调用");
  if (event.eventbus_tool_called === false) parts.push("EventBus 工具未调用");
  if (event.authorized === true) parts.push("身份已授权");
  if (event.authorized === false) parts.push("身份未授权");
  // Detailed timing spans (bounded integers exposed by the bridge projection).
  if (Number.isFinite(event.wall_ms) && event.wall_ms > 0) {
    parts.push(`耗时 ${Math.round(event.wall_ms)}ms`);
  }
  if (Number.isFinite(event.active_ms) && event.active_ms > 0) {
    parts.push(`活跃 ${Math.round(event.active_ms)}ms`);
  }
  if (Number.isFinite(event.provider_wait_ms) && event.provider_wait_ms > 0) {
    parts.push(`Provider 等待 ${Math.round(event.provider_wait_ms)}ms`);
  }
  if (Number.isFinite(event.provider_total_ms) && event.provider_total_ms > 0) {
    parts.push(`Provider 总 ${Math.round(event.provider_total_ms)}ms`);
  }
  if (Number.isFinite(event.queue_wait_ms) && event.queue_wait_ms > 0) {
    parts.push(`队列等待 ${Math.round(event.queue_wait_ms)}ms`);
  }
  if (Number.isFinite(event.lock_wait_ms) && event.lock_wait_ms > 0) {
    parts.push(`锁等待 ${Math.round(event.lock_wait_ms)}ms`);
  }
  if (Number.isFinite(event.event_loop_lag_ms) && event.event_loop_lag_ms > 0) {
    parts.push(`事件循环积压 ${Math.round(event.event_loop_lag_ms)}ms`);
  }
  if (event.timeout === true) parts.push("超时");
  if (event.fallback === true) parts.push("已回退");
  if (event.cache_hit === true) parts.push("命中缓存");
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
      status_label: snapshot.status_label,
      phase: snapshot.stage,
      phase_label: snapshot.stage_label,
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
    const stageText = String(
      event.span_label || event.component_label ||
      event.span_name || event.component || "运行链路"
    );
    const parts = [
      `${timestamp} [${stageText}] ${event.status_label || "状态未知"}`,
      String(event.event_label || event.event || "诊断事件"),
      reason ? String(event.reason_label || reason) : "",
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
      const stageKey = String(
        event.component_label || event.component || "runtime"
      );
      durations[stageKey] = Math.round(event.duration_ms);
    }
  });
  const durationText = Object.entries(durations).slice(-5)
    .map(([stage, value]) => `${stage} ${value}ms`)
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

function formatClientMetric(value, unit = "ms", digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "—";
  return `${number.toFixed(digits)}${unit}`;
}

function renderClientPerf(client) {
  const container = document.getElementById("client-perf");
  if (!container) return;
  container.replaceChildren();
  const sessions = client && Array.isArray(client.sessions) ? client.sessions : [];
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "diagnostics-empty";
    empty.textContent = "Quest 客户端未上报（需头盔已连接且开启详细采样）。";
    container.append(empty);
    return;
  }
  const session = sessions[0];
  const perf = session.latest_perf || null;
  const aggregates = session.aggregates || {};
  const lines = [];
  if (perf) {
    lines.push(
      `FPS ${formatClientMetric(perf.fps, "", 0)} · 帧时 p50 ${formatClientMetric(perf.frame_p50_ms)} / p95 ${formatClientMetric(perf.frame_p95_ms)} / max ${formatClientMetric(perf.frame_max_ms)} · 目标 ${formatClientMetric(perf.target_fps, "", 0)}FPS`
    );
    lines.push(
      `合成器丢帧 ${formatClientMetric(perf.compositor_dropped_session, "", 0)} · 物理丢步 ${formatClientMetric(perf.physics_dropped_s, "s", 3)}/${formatClientMetric(perf.physics_dropped_frames, "", 0)} 帧 · 物理 ${formatClientMetric(perf.physics_hz, "Hz", 0)}/${formatClientMetric(perf.physics_substeps, "步", 0)}`
    );
    lines.push(
      `MMD：求解 ${formatClientMetric(perf.mmd_solver_ms)} · 物理 ${formatClientMetric(perf.mmd_physics_ms)} · 骨骼IK ${formatClientMetric(perf.mmd_bone_ik_ms)} · SDEF ${formatClientMetric(perf.mmd_sdef_ms)} · 回写 ${formatClientMetric(perf.mmd_flush_ms)} · 手接触 ${formatClientMetric(perf.hand_contact_ms)}`
    );
    lines.push(
      `XR：CPU ${formatClientMetric(perf.xr_cpu_ms)} · GPU ${formatClientMetric(perf.xr_gpu_ms)} · 利用率 ${formatClientMetric(perf.cpu_util, "%", 0)}/${formatClientMetric(perf.gpu_util, "%", 0)}`
    );
    lines.push(
      `内存 已分配 ${formatClientMetric(perf.mem_alloc_bytes, "B", 0)} · PSS ${formatClientMetric(perf.mem_pss_bytes, "B", 0)} · GC ${formatClientMetric(perf.gc0, "", 0)}/${formatClientMetric(perf.gc1, "", 0)}/${formatClientMetric(perf.gc2, "", 0)} · 热 ${perf.thermal_state || "—"}`
    );
    lines.push(
      `模型 渲染 ${formatClientMetric(perf.model_renderer, "", 0)} · 材质 ${formatClientMetric(perf.model_material, "", 0)} · 顶点 ${formatClientMetric(perf.model_vertex, "", 0)} · 三角 ${formatClientMetric(perf.model_tri, "", 0)} · 骨骼 ${formatClientMetric(perf.model_bone, "", 0)} · 刚体 ${formatClientMetric(perf.model_rigid, "", 0)} · 渲染比例 ${formatClientMetric(perf.render_scale, "", 2)} · 佩戴 ${perf.headset_worn === true ? "是" : perf.headset_worn === false ? "否" : "—"} · 动作 ${perf.active_action || "—"}`
    );
  } else {
    lines.push("尚无性能快照。");
  }
  lines.push(
    `会话 ${session.session_id} · 上报 ${formatClientMetric(aggregates.report_count, "", 0)} 次（性能 ${formatClientMetric(aggregates.perf_count, "", 0)} / 跨度 ${formatClientMetric(aggregates.span_events, "", 0)}）· 拒收 ${formatClientMetric(aggregates.rejected_count, "", 0)} · 平均FPS ${formatClientMetric(aggregates.avg_fps, "", 1)} · 物理丢步峰值 ${formatClientMetric(aggregates.physics_dropped_max_s, "s", 3)} · ${formatClientMetric(aggregates.age_seconds, "s", 0)} 前活跃`
  );
  lines.forEach((value) => {
    const line = document.createElement("p");
    line.textContent = value;
    container.append(line);
  });
}

function renderUnifiedTimeline(client, serverEvents) {
  const container = document.getElementById("client-timeline");
  if (!container) return;
  container.replaceChildren();
  const sessions = client && Array.isArray(client.sessions) ? client.sessions : [];
  const groups = [];
  sessions.forEach((session) => {
    (Array.isArray(session.events) ? session.events : []).forEach((entry) => {
      if (entry.kind !== "spans" || !Array.isArray(entry.spans) || !entry.spans.length) {
        return;
      }
      groups.push({
        key: entry.trace_id || entry.turn_id || session.session_id,
        joined: Boolean(entry.trace_id),
        offset_ms: Number(entry.offset_ms || 0),
        spans: entry.spans
      });
    });
  });
  groups.sort((a, b) => b.offset_ms - a.offset_ms);
  const latest = groups.slice(0, 3);
  if (!latest.length) {
    const empty = document.createElement("p");
    empty.className = "diagnostics-empty";
    empty.textContent = "尚无客户端 turn 跨度上报。";
    container.append(empty);
    return;
  }
  latest.forEach((group) => {
    const header = document.createElement("p");
    header.className = "diagnostics-timeline-title";
    header.textContent =
      `trace ${group.key}` + (group.joined ? " · 已与服务端对齐" : " · 未对齐服务端");
    container.append(header);
    group.spans
      .slice()
      .sort((a, b) => Number(a.start_offset_ms || 0) - Number(b.start_offset_ms || 0))
      .slice(0, 10)
      .forEach((span) => {
        const line = document.createElement("div");
        line.className = `diagnostic-line status-${String(span.status || "unknown")}`;
        const parts = [
          `[Quest] [${span.component}/${span.stage}] ${span.code || "-"}`,
          `+${Math.round(Number(span.start_offset_ms || 0))}ms`
        ];
        if (Number(span.duration_ms) >= 0) parts.push(`耗时 ${Math.round(span.duration_ms)}ms`);
        if (Number(span.chunks) > 0) parts.push(`${span.chunks} 块`);
        line.textContent = parts.join(" · ");
        container.append(line);
      });
    const matching = serverEvents
      .filter((event) => String(event.trace_id || "") === group.key)
      .slice(-8);
    if (matching.length) {
      const bridgeHeader = document.createElement("p");
      bridgeHeader.textContent = "── 服务端段 ──";
      container.append(bridgeHeader);
      matching.forEach((event) => {
        const line = document.createElement("div");
        line.className = `diagnostic-line status-${String(event.status || "unknown")}`;
        const parts = [
          `[服务端] [${event.span_name || event.component_label || String(event.component || "")}]`
        ];
        if (Number.isFinite(event.start_offset_ms)) {
          parts.push(`+${Math.round(event.start_offset_ms)}ms`);
        }
        if (Number.isFinite(event.wall_ms) && event.wall_ms > 0) {
          parts.push(`耗时 ${Math.round(event.wall_ms)}ms`);
        }
        if (Number.isFinite(event.provider_wait_ms) && event.provider_wait_ms > 0) {
          parts.push(`Provider 等待 ${Math.round(event.provider_wait_ms)}ms`);
        }
        line.textContent = parts.join(" · ");
        container.append(line);
      });
    }
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
      const status = String(diagnostics.status || "unavailable");
      document.getElementById("diagnostics-status").textContent =
        `实时刷新 · 状态：${diagnostics.status_label || status} · 事件：${events.length} 条`;
      const rootCause = diagnostics.root_cause || {};
      document.getElementById("diagnostics-root-cause").textContent = rootCause.code
        ? `当前根因：${rootCause.stage_label || rootCause.stage} · ${rootCause.reason_label || rootCause.code}`
        : "当前根因：未发现明确的失败事件";
      renderDiagnosticSummary(events);
      renderDiagnosticEvents(events);
      renderClientPerf(diagnostics.client);
      renderUnifiedTimeline(diagnostics.client, events);
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
    .getElementById("save-quest-chain-button")
    .addEventListener("click", saveQuestChainSettings);
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
  bindQuickPairingModal();
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
  { key: "quest-chain", label: "对话链路", load: loadQuestChainSettings },
  { key: "fast-action", label: "快速动作", load: loadFastActionSettings },
  { key: "stt", label: "语音识别", load: loadSttSettings },
  { key: "platform", label: "正式消息链路", load: loadPlatformSettings },
  { key: "persona", label: "实时人格", load: loadPersonaSettings },
  { key: "persona-library", label: "具身人格库", load: loadPersonaProfiles },
  { key: "quest-identity", label: "Quest 身份", load: loadQuestIdentitySettings },
  { key: "quick-pairing", label: "快速绑定", load: loadQuickPairingStatus }
];

function markInitialSectionFailed(key) {
  const messages = {
    operator: ["model-status", "聊天模型读取失败，可单独重试。"],
    "quest-chain": ["quest-chain-status", "对话链路设置读取失败，可单独重试。"],
    "fast-action": ["fast-action-status", "快速动作设置读取失败，可单独重试。"],
    stt: ["stt-status", "语音识别设置读取失败，可单独重试。"],
    platform: ["platform-status", "正式消息链路读取失败，可单独重试。"],
    persona: ["persona-status", "实时人格读取失败，可单独重试。"],
    "quest-identity": ["quest-identity-status", "Quest 身份读取失败，可单独重试。"],
    "quick-pairing": ["quick-pairing-status", "快速绑定状态读取失败，可单独重试。"]
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
