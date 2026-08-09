let bridge = null;
let operatorSettings = null;
let personaSettings = null;
let platformSettings = null;
let questIdentitySettings = null;
let serviceState = null;
let serviceRefreshInFlight = false;

async function resolveBridge(timeout = 3000) {
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
    throw new Error(
      data.message || data.detail || data.error || data?.data?.code || "请求失败"
    );
  }
  return data;
}

async function apiGet(name) {
  if (!bridge) throw new Error("页面 Bridge 尚未初始化");
  return parseResponse(await bridge.apiGet(name));
}

async function apiPost(name, payload) {
  if (!bridge) throw new Error("页面 Bridge 尚未初始化");
  return parseResponse(await bridge.apiPost(name, payload));
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

function showStartupError(error) {
  const message = "页面启动失败：" + (error?.message || error);
  const node = document.getElementById("startup-error");
  node.textContent = message;
  node.hidden = false;
  setRuntimeState("error", "角色设置不可用");
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
    pairing_listener_public_url_missing: "服务已运行，但尚未配置 Quest 可达地址"
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
  if (serviceRefreshInFlight) return;
  serviceRefreshInFlight = true;
  const button = document.getElementById("refresh-service-button");
  if (!silent) setButtonBusy(button, true, "刷新中…");
  try {
    const response = await apiGet("pairing/service-status");
    renderServiceStatus(response.service);
  } catch (error) {
    setRuntimeState("error", "服务状态读取失败");
    if (!silent) toast("读取服务状态失败：" + error.message, true);
  } finally {
    serviceRefreshInFlight = false;
    if (!silent) setButtonBusy(button, false);
  }
}

async function toggleService() {
  const button = document.getElementById("service-control-button");
  const enabled = button.dataset.nextEnabled === "true";
  if (!enabled && !window.confirm("关闭服务会断开当前 Quest 会话，确定继续吗？")) {
    return;
  }
  if (!setButtonBusy(button, true, enabled ? "启动中…" : "关闭中…")) return;
  try {
    const response = await apiPost("pairing/service-control", { enabled });
    renderServiceStatus(response.service);
    toast(enabled ? "Quest Bridge 服务已启动" : "Quest Bridge 服务已关闭");
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
  if (active > 0 && !window.confirm("修改端口会断开当前 Quest 会话，确定继续吗？")) {
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
    select.add(new Option("没有可用的 Chat Completion Provider", ""));
    select.disabled = true;
  } else {
    select.add(new Option("请选择聊天模型", ""));
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
    status.textContent = "当前 AstrBot 配置对象不支持异步保存。";
  } else if (operatorSettings.status === "selected_missing") {
    status.textContent = "已配置模型当前不可用，请重新选择。";
  } else if (operatorSettings.selected_available) {
    status.textContent = "当前模型：" + operatorSettings.selected_id;
  } else {
    status.textContent = "尚未选择聊天模型，实时对话不可用。";
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
    : "当前 AstrBot 配置对象不支持异步保存";
  document.getElementById("save-persona-button").disabled = !writable;
}

async function loadOperatorSettings() {
  const response = await apiGet("pairing/operator-settings");
  renderOperatorSettings(response.settings);
}

async function loadPlatformSettings() {
  const response = await apiGet("pairing/platform-settings");
  renderPlatformSettings(response.platform);
}

async function loadPersonaSettings() {
  const response = await apiGet("pairing/persona-settings");
  renderPersonaSettings(response.persona);
}

function renderQuestIdentitySettings(identity) {
  questIdentitySettings = identity || {};
  const writable = questIdentitySettings.config_writable === true;
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
      : "请填写 Quest 专用 API Key";
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
  document.getElementById("quest-identity-status").textContent = writable
    ? source + (missing.length ? `；待补充：${missing.join("、")}` : "；身份配置完整") + validationText
    : "当前 AstrBot 配置对象不支持异步保存";
  document.getElementById("save-quest-identity-button").disabled = !writable;
}

async function loadQuestIdentitySettings() {
  const response = await apiGet("pairing/quest-identity-settings");
  renderQuestIdentitySettings(response.identity);
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
    toast("人格来源已保存并立即生效");
  } catch (error) {
    toast("角色身份保存失败：" + error.message, true);
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
    toast("聊天模型已保存并立即生效");
  } catch (error) {
    toast("模型保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = !document.getElementById("chat-provider-id").value;
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
  select.replaceChildren(new Option("不绑定自然人", ""));

  if (catalog?.status !== "ok") {
    if (selected) select.add(new Option("已选择 · " + selected, selected));
    select.value = selected;
    select.disabled = true;
    saveButton.disabled = true;
    status.textContent = identityUnavailableMessage(catalog?.status);
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
    : "“情”中尚无可选自然人。";
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
      : "已清除关系绑定；Quest 服务端身份保持不变");
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
    local_quest_identity_mismatch: "Quest 客户端、平台、Bot 或主人用户与本地绑定不一致",
    invalid_user_id: "Quest 用户 ID 无效或仍是占位值",
    missing_user_id: "Quest 用户 ID 缺失",
    invalid_bot_id: "Quest Bot ID 无效",
    missing_bot_id: "Quest Bot ID 缺失",
    client_id_mismatch: "Quest 客户端 ID 与服务端配置不一致",
    trusted_platform_not_configured: "尚未配置可进入 EventBus 的 AstrBot 平台",
    trusted_platform_unavailable: "已配置的 AstrBot 平台当前不可用",
    astrbot_event_api_unavailable: "当前 AstrBot 不提供消息事件接口",
    astrbot_message_pipeline_unavailable: "AstrBot 消息链路不可用",
    astrbot_pipeline_timeout: "AstrBot 消息链处理超时",
    astrbot_pipeline_empty_reply: "AstrBot 消息链没有返回可用内容",
    stt_empty: "没有识别到有效语音",
    stt_unavailable: "语音识别服务未配置",
    stt_failed: "语音识别失败",
    llm_failed: "模型生成失败",
    tts_failed: "语音合成失败，文字回复仍可能可用",
    audio_upload_backpressure: "音频上传速度跟不上录音",
    audio_http_request_failed: "音频上传请求失败",
    turn_failed: "对话生成失败",
    interaction_failed: "触碰交互决策失败",
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
    eventbus: "AstrBot/EventBus",
    llm: "模型生成",
    tts: "语音合成",
    reply: "回复交付",
    turn: "对话轮次",
    audio_playback: "音频播放",
    interrupt: "打断"
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
    "llm.completed": "模型生成完成",
    "llm.error": "模型生成失败",
    "tts.completed": "语音合成完成",
    "tts.error": "语音合成失败",
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
    closed: "已关闭"
  };
  return labels[String(value || "")] || String(value || "状态未知");
}

function diagnosticMeta(event) {
  const parts = [];
  if (Number.isFinite(event.http_status)) parts.push(`HTTP ${event.http_status}`);
  if (Number.isFinite(event.duration_ms)) parts.push(`${Math.round(event.duration_ms)} ms`);
  if (Number.isFinite(event.chunks)) parts.push(`${event.chunks} 块`);
  if (Number.isFinite(event.bytes)) parts.push(`${event.bytes} 字节`);
  if (Number.isFinite(event.event_count)) parts.push(`${event.event_count} 个事件`);
  if (event.authorized === true) parts.push("身份已授权");
  if (event.authorized === false) parts.push("身份未授权");
  return parts.join(" · ");
}

function renderDiagnosticEvents(events) {
  const container = document.getElementById("diagnostics-events");
  container.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "diagnostics-empty";
    empty.textContent = "尚无诊断事件。发起一次 Quest 连接或对话后再刷新。";
    container.append(empty);
    return;
  }
  events.slice(-40).forEach((event) => {
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
  container.scrollTop = container.scrollHeight;
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

async function loadDiagnostics() {
  const button = document.getElementById("load-diagnostics");
  if (!setButtonBusy(button, true, "读取中……")) return;
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
      `状态：${statusLabels[status] || status} · 事件：${events.length} 条`;
    const rootCause = diagnostics.root_cause || {};
    document.getElementById("diagnostics-root-cause").textContent = rootCause.code
      ? `当前根因：${diagnosticStageLabel(rootCause.stage)} · ${diagnosticReasonLabel(rootCause.code)}`
      : "当前根因：未发现明确的失败事件";
    renderDiagnosticSummary(events);
    renderDiagnosticEvents(events);
  } catch (error) {
    document.getElementById("diagnostics-status").textContent =
      "诊断日志暂不可用";
    document.getElementById("diagnostics-root-cause").textContent =
      "当前根因：诊断接口读取失败";
    renderDiagnosticSummary([]);
    renderDiagnosticEvents([]);
    toast("读取诊断日志失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
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
  document
    .getElementById("load-identity-candidates")
    .addEventListener("click", loadIdentityCandidates);
  document
    .getElementById("save-identity-button")
    .addEventListener("click", saveIdentitySelection);
  document
    .getElementById("load-diagnostics")
    .addEventListener("click", loadDiagnostics);
}

async function init() {
  bridge = await resolveBridge();
  if (typeof bridge.ready !== "function") throw new Error("Bridge ready() 不可用");
  await bridge.ready();
  bindEvents();
  await Promise.all([
    loadServiceStatus(),
    loadOperatorSettings(),
    loadPlatformSettings(),
    loadPersonaSettings(),
    loadQuestIdentitySettings()
  ]);
  window.setInterval(() => loadServiceStatus({ silent: true }), 10000);
}

init().catch(showStartupError);
