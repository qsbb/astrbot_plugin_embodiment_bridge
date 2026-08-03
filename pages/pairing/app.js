const STORAGE_KEY = "quest-avatar-pairing-form-v1";
let bridge = null;
let pairing = null;
let countdownTimer = null;
let statusTimer = null;
let statusFailCount = 0;
let statusInFlight = false;

function readStoredForm() {
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (_error) {
    return {};
  }
}

function storeNonSecretForm() {
  const value = {
    public_url: document.getElementById("public-url").value.trim(),
    port: document.getElementById("port").value.trim(),
    client_id: document.getElementById("client-id").value.trim(),
    user_id: document.getElementById("user-id").value.trim(),
    bot_id: document.getElementById("bot-id").value.trim(),
    group_id: document.getElementById("group-id").value.trim(),
    relationship_profile_id: document.getElementById("relationship-profile-id").value.trim(),
    ttl_seconds: document.getElementById("ttl").value
  };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch (_error) {
    // Pairing still works when the iframe blocks storage.
  }
}

function restoreForm() {
  const stored = readStoredForm();
  const fields = {
    "public-url": stored.public_url,
    port: stored.port,
    "client-id": stored.client_id,
    "user-id": stored.user_id,
    "bot-id": stored.bot_id,
    "group-id": stored.group_id,
    "relationship-profile-id": stored.relationship_profile_id,
    ttl: stored.ttl_seconds
  };
  Object.entries(fields).forEach(([id, value]) => {
    if (typeof value === "string" && value) document.getElementById(id).value = value;
  });
}

async function resolveBridge(timeout = 3000) {
  if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
  if (typeof window.waitForAstrBotBridge === "function") return window.waitForAstrBotBridge(timeout);
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
    throw new Error(data.message || data.detail || data.error || data?.data?.code || "请求失败");
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
  const message = `页面启动失败：${error?.message || error}`;
  const node = document.getElementById("startup-error");
  node.textContent = message;
  node.hidden = false;
  setRuntimeState("error", "Bridge 页面不可用");
}

function setButtonBusy(button, busy, busyText) {
  if (busy) {
    if (button.getAttribute("aria-busy") === "true") return false;
    button.dataset.idleText = button.textContent;
    button.textContent = busyText;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    return true;
  }
  button.textContent = button.dataset.idleText || button.textContent;
  button.setAttribute("aria-busy", "false");
  button.disabled = !pairing || pairing.state !== "waiting";
  return true;
}

function normalizePublicUrl() {
  const node = document.getElementById("public-url");
  const value = node.value.trim();
  if (value && !value.includes("://")) node.value = `https://${value}`;
}

function requestPayload() {
  const portValue = document.getElementById("port").value.trim();
  return {
    protocol_version: "1.0",
    public_url: document.getElementById("public-url").value.trim(),
    port: portValue ? Number(portValue) : null,
    astrbot_api_key: document.getElementById("astrbot-api-key").value,
    client_id: document.getElementById("client-id").value.trim(),
    user_id: document.getElementById("user-id").value.trim(),
    bot_id: document.getElementById("bot-id").value.trim(),
    group_id: document.getElementById("group-id").value.trim(),
    relationship_profile_id: document.getElementById("relationship-profile-id").value.trim(),
    ttl_seconds: Number(document.getElementById("ttl").value)
  };
}

function formatRemaining(seconds) {
  const remaining = Math.max(0, Math.ceil(seconds));
  return `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
}

function renderState(state) {
  if (!pairing) return;
  const label = document.getElementById("pairing-status");
  const knownStates = new Set(["waiting", "consumed", "expired", "revoked"]);
  const normalizedState = knownStates.has(state) ? state : "unknown";
  const labels = {
    waiting: "等待 Quest 兑换",
    consumed: "Quest 已完成绑定",
    expired: "配对码已过期",
    revoked: "配对已撤销",
    unknown: "配对状态暂时无法识别，请重新生成配对码"
  };
  pairing.state = normalizedState;
  label.textContent = labels[normalizedState];
  label.className = normalizedState;
  const active = normalizedState === "waiting";
  document.getElementById("copy-code").disabled = !active;
  document.getElementById("revoke").disabled = !active;
  if (normalizedState === "consumed") {
    const clientId = document.getElementById("client-id").value.trim() || "Quest";
    document.getElementById("recent-binding").textContent = `${clientId} · 已连接`;
    toast("Quest 已获取配置并完成一次性兑换");
  }
  if (!active) stopPolling();
}

function updateCountdown() {
  if (!pairing) return;
  const remaining = pairing.expires_at - Date.now() / 1000;
  document.getElementById("countdown").textContent = formatRemaining(remaining);
  if (remaining <= 0 && pairing.state === "waiting") renderState("expired");
}

function showPairing(result) {
  pairing = result;
  document.getElementById("pairing-empty").hidden = true;
  document.getElementById("pairing-result").hidden = false;
  document.getElementById("qr-image").src = result.qr_svg_data_uri;
  document.getElementById("short-code").textContent = `${result.short_code.slice(0, 3)} ${result.short_code.slice(3)}`;
  document.getElementById("exchange-url").textContent = result.exchange_url;
  renderState(result.state || "waiting");
  updateCountdown();
  window.clearInterval(countdownTimer);
  countdownTimer = window.setInterval(updateCountdown, 250);
  startPolling();
}

async function refreshStatus() {
  if (!pairing || pairing.state !== "waiting") return;
  const response = await apiPost("pairing/status", { pairing_id: pairing.pairing_id });
  const current = response.pairing;
  pairing.expires_at = current.expires_at;
  if (current.state !== pairing.state) renderState(current.state);
}

function startPolling() {
  window.clearInterval(statusTimer);
  statusTimer = window.setInterval(() => {
    if (statusInFlight || document.hidden) return;
    statusInFlight = true;
    refreshStatus()
      .then(() => {
        statusFailCount = 0;
      })
      .catch((error) => {
        statusFailCount += 1;
        if (statusFailCount === 3) toast(`状态读取连续失败：${error.message}（仍在自动重试）`, true);
      })
      .finally(() => {
        statusInFlight = false;
      });
  }, 1800);
}

function stopPolling() {
  window.clearInterval(statusTimer);
  statusTimer = null;
}

async function createPairing(event) {
  event.preventDefault();
  normalizePublicUrl();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = document.getElementById("generate-button");
  button.disabled = true;
  button.textContent = "正在生成…";
  try {
    storeNonSecretForm();
    const response = await apiPost("pairing/create", requestPayload());
    document.getElementById("astrbot-api-key").value = "";
    showPairing(response.pairing);
    toast("一次性配对码已生成");
  } catch (error) {
    toast(`生成失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "生成配对";
  }
}

async function revokePairing() {
  if (!pairing || pairing.state !== "waiting") return;
  const button = document.getElementById("revoke");
  if (!setButtonBusy(button, true, "正在撤销…")) return;
  try {
    const response = await apiPost("pairing/revoke", { pairing_id: pairing.pairing_id });
    renderState(response.pairing.state);
    toast("配对已撤销");
  } catch (error) {
    toast(`撤销失败：${error.message}`, true);
  } finally {
    setButtonBusy(button, false);
  }
}

async function copyCode() {
  if (!pairing || pairing.state !== "waiting") return;
  const button = document.getElementById("copy-code");
  if (!setButtonBusy(button, true, "正在复制…")) return;
  try {
    await navigator.clipboard.writeText(pairing.short_code);
    toast("配对码已复制");
  } catch (_error) {
    window.prompt("复制 6 位配对码", pairing.short_code);
  } finally {
    setButtonBusy(button, false);
  }
}

async function loadOverview() {
  const overview = await apiGet("pairing/overview");
  if (!overview.bridge_key_configured) {
    setRuntimeState("error", "Bridge 长期密钥尚未配置");
    document.getElementById("generate-button").disabled = true;
    document.getElementById("form-hint").textContent = "请先在插件配置中设置至少 32 字符的 Bridge API Key。";
    return;
  }
  setRuntimeState("ready", "Bridge 配对服务已就绪");
  if (overview.trusted_client_id) document.getElementById("client-id").value = overview.trusted_client_id;
  document.getElementById("trusted-platform").textContent = overview.trusted_platform_id || "未配置（受保护关系上下文将关闭）";
}

function bindEvents() {
  document.getElementById("pairing-form").addEventListener("submit", createPairing);
  document.getElementById("public-url").addEventListener("blur", normalizePublicUrl);
  document.getElementById("copy-code").addEventListener("click", copyCode);
  document.getElementById("revoke").addEventListener("click", revokePairing);
}

async function init() {
  restoreForm();
  bridge = await resolveBridge();
  if (typeof bridge.ready !== "function") throw new Error("Bridge ready() 不可用");
  await bridge.ready();
  bindEvents();
  await loadOverview();
}

window.addEventListener("pagehide", () => {
  stopPolling();
  window.clearInterval(countdownTimer);
});

init().catch(showStartupError);
