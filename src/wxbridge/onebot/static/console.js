"use strict";
const $ = (id) => document.getElementById(id);
const sessionKey = "wxbridge-console-admin";
const initialFragment = new URLSearchParams(location.hash.slice(1));
let adminToken = initialFragment.get("admin") || sessionStorage.getItem(sessionKey) || "";
if (initialFragment.has("admin")) history.replaceState(null, "", location.pathname + location.search + (initialFragment.get("view") === "messages" ? "#message-monitor" : ""));
let busy = false;
let lastView = null;
let monitorCursor = 0;
let monitorSession = "";
let monitorTimer = null;
let monitorInFlight = false;
let monitorPaused = false;
let monitorRows = [];
let monitorState = null;
let monitorScope = null;
const clearKey = "wxbridge-monitor-cleared";
try {
  const cleared = JSON.parse(sessionStorage.getItem(clearKey) || "null");
  if (cleared && Number.isSafeInteger(cleared.cursor) && cleared.cursor >= 0 && typeof cleared.session === "string") {
    monitorCursor = cleared.cursor;
    monitorSession = cleared.session;
  }
} catch { sessionStorage.removeItem(clearKey); }

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

function locked() {
  $("unlock").hidden = false;
  $("settings").hidden = true;
  $("message-monitor").hidden = true;
  clearTimeout(monitorTimer);
}

async function api(path, body) {
  const options = {headers: {Authorization: `Bearer ${adminToken}`}, cache: "no-store"};
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`/console/api/${path}`, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem(sessionKey);
      locked();
    }
    throw new Error(result.error || "请求失败，请稍后重试");
  }
  return result;
}

function renderStatus(view) {
  lastView = view;
  const labels = {unconfigured: "等待配置", disconnected: "尚未连接", connecting: "正在连接", connected: "已连接微信", attention: "请检查微信窗口"};
  const label = view.mock && view.phase === "connected" ? "模拟模式已连接" : labels[view.phase];
  $("status-label").textContent = label;
  $("connection-summary").textContent = label;
  $("status-pill").className = `status-pill ${view.phase === "connected" ? "good" : view.error ? "error" : view.phase === "connecting" ? "busy" : ""}`;
  $("environment").textContent = view.mock ? "模拟模式 · 不操作微信" : "本地服务 · 真实微信";
  $("server-address").textContent = location.host;
  $("disconnect").disabled = busy || !view.connected;
  $("save-note").textContent = view.connected ? "配置已生效 · 新消息将通过 OneBot 上报" : "配置保存在本机 · 保存不会发送测试消息";
  $("chat-kind-summary").textContent = view.config?.chat_type === "group" ? "群聊接入" : "私聊接入";
}

function chatMode() {
  const group = $("chat-type").value === "group";
  $("group-fields").hidden = !group;
  $("private-fields").hidden = group;
  $("peer-id").disabled = group;
  $("group-id").required = group;
  $("group-id").disabled = !group;
  $("chat-name-label").textContent = group ? "群聊名称" : "联系人备注名";
  $("chat-name").placeholder = group ? "输入微信中该群的准确名称" : "输入微信中的准确备注名";
  $("chat-name-hint").textContent = group ? "与群标题一致，不填写旁边的成员人数。请使用唯一群名，避免同名。" : "需与当前私聊标题完全一致。请使用唯一备注，避免同名。";
}

function populate(view) {
  const c = view.config;
  $("chat-name").value = c.chat_name;
  $("chat-type").value = c.chat_type || "friend";
  $("group-id").value = c.group_id || "20001";
  $("group-mention-name").value = c.group_mention_name || "";
  $("nickname").value = c.nickname;
  $("self-id").value = c.self_id;
  $("peer-id").value = c.peer_id;
  $("accessibility").checked = c.enable_accessibility;
  $("foreground").checked = c.foreground;
  $("accessibility").disabled = view.mock;
  $("foreground").disabled = view.mock;
  $("reverse-urls").value = c.reverse_ws_urls.join("\n");
  $("http-urls").value = c.http_post_urls.join("\n");
  $("media-base-url").value = c.media_base_url || "";
  $("access-token").value = c.access_token;
  chatMode();
  renderStatus(view);
}

async function load() {
  if (!adminToken) { locked(); return; }
  try {
    const view = await api("config");
    sessionStorage.setItem(sessionKey, adminToken);
    $("unlock").hidden = true;
    $("settings").hidden = false;
    $("message-monitor").hidden = false;
    populate(view);
    if (view.error) notice(view.error, true);
    pollMessages();
    if (location.hash === "#message-monitor") {
      $("message-monitor").scrollIntoView();
      for (const item of document.querySelectorAll(".nav-item")) item.classList.toggle("active", item.getAttribute("href") === "#message-monitor");
    }
  } catch (error) { notice(error.message, true); }
}

function setBusy(value) {
  busy = value;
  for (const id of ["save-connect", "save-only", "disconnect"]) $(id).disabled = value;
  $("save-connect").textContent = value ? "正在处理，请稍候…" : "保存并连接微信 ↗";
  if (!value && lastView) renderStatus(lastView);
}

function urls(id) { return $(id).value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean); }

async function save(connect) {
  if (busy || !$("settings").reportValidity()) return;
  setBusy(true);
  notice(connect ? "正在保存配置并连接微信，请让目标会话保持前台…" : "正在保存配置…");
  try {
    const view = await api("config", {
      chat_name: $("chat-name").value.trim(), nickname: $("nickname").value.trim(),
      self_id: $("self-id").value, peer_id: $("peer-id").value,
      chat_type: $("chat-type").value, group_id: $("group-id").value,
      group_mention_name: $("group-mention-name").value.trim(),
      enable_accessibility: $("accessibility").checked, foreground: $("foreground").checked,
      reverse_ws_urls: urls("reverse-urls"), http_post_urls: urls("http-urls"),
      media_base_url: $("media-base-url").value.trim(), connect,
    });
    populate(view);
    if (view.error) notice(`配置已保存。连接未完成：${view.error}`, true);
    else notice(connect ? (view.mock ? "模拟微信已连接，可以验证 OneBot 接入。" : "微信已连接。文字、图片、文件与语音转写将交给 OneBot；保存附件和转写时需微信在前台。") : "配置已保存，尚未连接微信。准备好目标会话后，点击「保存并连接微信」。");
  } catch (error) { notice(error.message, true); }
  finally { setBusy(false); }
}

$("unlock-form").addEventListener("submit", (event) => { event.preventDefault(); adminToken = $("admin-token").value.trim(); load(); });
$("settings").addEventListener("submit", (event) => { event.preventDefault(); save(true); });
$("chat-type").addEventListener("change", chatMode);
$("save-only").addEventListener("click", () => save(false));
$("disconnect").addEventListener("click", async () => {
  if (busy) return;
  setBusy(true);
  try { renderStatus(await api("disconnect", {})); notice("微信连接已断开，配置已保留。"); }
  catch (error) { notice(error.message, true); }
  finally { setBusy(false); }
});
$("toggle-token").addEventListener("click", () => {
  const show = $("access-token").type === "password";
  $("access-token").type = show ? "text" : "password";
  $("toggle-token").textContent = show ? "隐藏" : "显示";
});
$("copy-token").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("access-token").value); notice("OneBot 访问令牌已复制。"); }
  catch { $("access-token").type = "text"; $("access-token").select(); notice("请复制已选中的访问令牌。"); }
});
setInterval(async () => {
  if (busy || !adminToken || $("settings").hidden) return;
  try {
    const view = await api("config");
    renderStatus(view);
    if (view.error) notice(view.error, true);
  } catch (error) { notice(error.message, true); }
}, 3000);
load();

function updateCounts() {
  $("received-count").textContent = monitorRows.filter((row) => row.direction === "received").length;
  $("sent-count").textContent = monitorRows.filter((row) => row.direction === "sent").length;
  $("message-count").textContent = monitorRows.length;
  $("monitor-empty").hidden = monitorRows.length > 0;
}

function clearDisplay() {
  monitorRows = [];
  $("message-list").replaceChildren();
  updateCounts();
}

function monitorStatus(data) {
  monitorState = data;
  $("monitor-chat").textContent = data.chat_name ? `当前${data.chat_type === "group" ? "群聊" : "私聊"}：${data.chat_name}` : "请先在上方配置并连接一个微信会话。";
  const label = monitorPaused ? "已暂停显示" : !data.connected ? "等待连接微信" : data.ready ? "正在监听" : "等待微信消息区可见";
  $("monitor-state").textContent = label;
  $("monitor-state").className = `monitor-state ${!monitorPaused && data.ready ? "listening" : ""}`;
  $("toggle-monitor").textContent = monitorPaused ? "继续监听" : "暂停显示";
  if (!monitorPaused && data.error) {
    $("monitor-hint").textContent = `${data.error}。可将微信与浏览器并排；最小化或遮挡后可能漏报。`;
  } else if (monitorPaused) {
    $("monitor-hint").textContent = "网页显示已暂停，OneBot 仍在运行。继续时读取缓冲的新消息，最多保留 200 条。";
  } else if (data.dropped) {
    $("monitor-hint").textContent = `缓冲区已更新，较早的 ${data.dropped} 条未显示。当前最多显示最近 200 条新消息。`;
  } else {
    $("monitor-hint").textContent = "每秒更新 · 仅显示连接后新消息 · 暂停或清空只影响网页显示，不停止 OneBot";
  }
}

function appendMessage(message) {
  const index = message.entry_id ? monitorRows.findIndex((item) => item.entry_id === message.entry_id) : -1;
  const row = document.createElement("article");
  row.className = `message-row ${message.direction === "sent" ? "sent" : "received"}`;
  row.dataset.direction = message.direction;
  const icon = document.createElement("span");
  icon.className = "message-direction";
  icon.textContent = message.direction === "sent" ? "↗" : "↙";
  const content = document.createElement("div");
  content.className = "message-content";
  const header = document.createElement("div");
  header.className = "message-meta";
  const sender = document.createElement("strong");
  sender.textContent = message.direction === "sent" ? `自己 · ${message.sender}` : message.sender;
  const time = document.createElement("time");
  time.dateTime = new Date(message.time * 1000).toISOString();
  time.textContent = new Intl.DateTimeFormat("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "Asia/Shanghai"}).format(message.time * 1000);
  const id = document.createElement("span");
  id.className = "message-id";
  id.textContent = message.message_id ? `#${message.message_id}` : "";
  header.append(sender, time, id);
  if (message.kind && message.kind !== "text") {
    const kind = document.createElement("span");
    kind.className = "message-kind";
    kind.textContent = {image: "图片", file: "文件", voice: "语音"}[message.kind] || message.kind;
    header.prepend(kind);
  }
  const text = document.createElement("p");
  text.textContent = message.text || {image: "[图片]", file: "[文件]", voice: "[语音]"}[message.kind] || "";
  content.append(header, text);
  if (message.forward_status) {
    const status = document.createElement("small");
    status.className = `media-status ${message.forward_status}`;
    const labels = {pending: "等待处理", processing: "正在处理", waiting: "等待微信前台", forwarded: "已交给 OneBot", sent: "已发出", failed: "上报失败", display_only: "仅显示"};
    status.textContent = `${labels[message.forward_status] || message.forward_status} · ${message.detail || ""}`;
    content.append(status);
  }
  row.append(icon, content);
  if (index >= 0) {
    $("message-list").children[index].replaceWith(row);
    monitorRows[index] = message;
  } else {
    $("message-list").append(row);
    monitorRows.push(message);
  }
  while (monitorRows.length > 200) { monitorRows.shift(); $("message-list").firstElementChild.remove(); }
}

async function pollMessages() {
  clearTimeout(monitorTimer);
  if (monitorPaused || monitorInFlight || !adminToken || $("message-monitor").hidden) return;
  monitorInFlight = true;
  try {
    const params = new URLSearchParams({after: String(monitorCursor), limit: "100"});
    if (monitorSession) params.set("session", monitorSession);
    const data = await api(`messages?${params}`);
    if (monitorPaused || $("message-monitor").hidden) return;
    if (data.reset || (monitorScope !== null && monitorScope !== data.chat_name)) clearDisplay();
    monitorScope = data.chat_name;
    monitorSession = data.session;
    for (const message of data.messages) appendMessage(message);
    monitorCursor = data.cursor;
    updateCounts();
    monitorStatus(data);
    if (data.messages.length && $("auto-scroll").checked) $("message-list").scrollTop = $("message-list").scrollHeight;
  } catch (error) {
    $("monitor-state").textContent = "连接中断，正在重试";
    $("monitor-state").className = "monitor-state";
    $("monitor-hint").textContent = error.message;
  } finally {
    monitorInFlight = false;
    if (!monitorPaused && !$("message-monitor").hidden) monitorTimer = setTimeout(pollMessages, 1000);
  }
}

$("toggle-monitor").addEventListener("click", () => {
  monitorPaused = !monitorPaused;
  clearTimeout(monitorTimer);
  if (monitorState) monitorStatus(monitorState);
  else $("toggle-monitor").textContent = monitorPaused ? "继续监听" : "暂停显示";
  if (!monitorPaused) pollMessages();
});
$("clear-messages").addEventListener("click", () => {
  clearDisplay();
  sessionStorage.setItem(clearKey, JSON.stringify({session: monitorSession, cursor: monitorCursor}));
});
for (const link of document.querySelectorAll(".nav-item")) {
  link.addEventListener("click", () => {
    for (const item of document.querySelectorAll(".nav-item")) item.classList.toggle("active", item === link);
  });
}
window.addEventListener("hashchange", () => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (!fragment.has("admin")) return;
  adminToken = fragment.get("admin") || "";
  history.replaceState(null, "", location.pathname + location.search + (fragment.get("view") === "messages" ? "#message-monitor" : ""));
  load();
});
