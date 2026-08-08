let bridge = null;
let operatorSettings = null;
let personaSettings = null;
let platformSettings = null;

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
  button.setAttribute("aria-busy", "false");
  return true;
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
    toast(personId ? "关系自然人已保存" : "已清除关系自然人绑定");
  } catch (error) {
    toast("自然人保存失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
    button.disabled = document.getElementById(
      "relationship-person-select"
    ).disabled;
  }
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
      disabled: "未启用",
      unavailable: "暂不可用"
    };
    const status = String(diagnostics.status || "unavailable");
    document.getElementById("diagnostics-status").textContent =
      `状态：${statusLabels[status] || status} · 事件：${events.length} 条`;
    document.getElementById("diagnostics-events").textContent = JSON.stringify(
      events,
      null,
      2
    );
  } catch (error) {
    document.getElementById("diagnostics-status").textContent =
      "诊断日志暂不可用";
    document.getElementById("diagnostics-events").textContent = "[]";
    toast("读取诊断日志失败：" + error.message, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function bindEvents() {
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
    loadOperatorSettings(),
    loadPlatformSettings(),
    loadPersonaSettings()
  ]);
  setRuntimeState("ready", "角色设置已就绪");
}

init().catch(showStartupError);
