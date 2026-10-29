let adminSession = null, adminTimer = null, adminDestination = "manage";
const adminLabels = {queued:"排队中", running:"执行中", retry:"等待重试", done:"已完成", failed:"失败",
  blocked:"等待配置", cached:"使用缓存", disabled:"已停用", ok:"连接正常", idle:"空闲",
  stopped:"已停止", not_started:"尚未启动", disabled_no_api_key:"等待 API 配置"};
const adminTime = value => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "尚无记录";
function adminEvent(action) {
  if (action === "POST /api/insights/refresh") return "提交采集与整理任务";
  if (action === "POST /api/admin/logout") return "退出管理";
  if (action === "POST /api/pending/review") return "提交模型复核任务";
  if (action === "POST /api/pending/enrich") return "提交已收录模型补查任务";
  return action.replace(/^POST \/api\/admin\/runs\/(\d+)\/retry$/, "重试采集任务 #$1")
    .replace(/^POST \/api\/pending\/(\d+)\/approve$/, "确认模型草稿 #$1 入库")
    .replace(/^POST \/api\/pending\/(\d+)\/reject$/, "丢弃模型草稿 #$1")
    .replace(/^POST \/api\/pending\/changes\/(\d+)\/accept$/, "采用字段更正 #$1")
    .replace(/^POST \/api\/pending\/changes\/(\d+)\/dismiss$/, "保留现有字段，忽略更正 #$1");
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers);
  if (options.method && !["GET", "HEAD"].includes(options.method.toUpperCase()) && adminSession?.csrf)
    headers.set("X-CSRF-Token", adminSession.csrf);
  const response = await fetch(url, {...options, headers});
  if (response.status === 401 && ["manage", "pending"].includes(activeView)) {
    adminSession = null;
    route();
  }
  return response;
}

async function ensureAdmin(destination) {
  adminDestination = destination;
  try {
    const response = await fetch("/api/admin/session");
    if (!response.ok) throw new Error("无法读取登录状态，请稍后重试。");
    adminSession = await response.json();
    document.getElementById("admin-logout").hidden = !adminSession.authenticated;
    document.getElementById("admin-login-message").textContent = adminSession.configured ? "" :
      "请先在服务器运行 python -m backend.admin_auth 设置密码，并重启网页服务。";
    return adminSession.authenticated;
  } catch (error) {
    document.getElementById("admin-login-message").textContent = error.message;
    return false;
  }
}

async function adminAction(url, options) {
  const response = await apiFetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "操作失败，请稍后重试");
  return data;
}

async function loadAdminOverview() {
  clearTimeout(adminTimer);
  try {
    const data = await adminAction("/api/admin/overview");
    if (activeView !== "manage") return;
    const root = document.getElementById("admin-overview");
    const expanded = new Set([...root.querySelectorAll("details[open]")].map(node => node.id));
    const counts = data.model_tasks.counts;
    root.innerHTML = `<section class="admin-section"><h2>后台进程</h2>
      <p>${esc(data.schedule)}${data.analysis_configured ? "" : " · 未配置 DeepSeek，原始资料仍会采集"}</p>
      <div class="admin-grid">${data.workers.map(worker => `<div class="admin-card"><strong>${worker.role === "collector" ? "采集与整理" : "模型补查"}</strong>
        <span class="admin-state ${worker.online ? "" : "is-warning"}">${worker.online ? "在线" : "离线"} · ${esc(adminLabels[worker.state] || worker.state)}</span>
        <small>最近心跳：${esc(adminTime(worker.last_seen))}</small></div>`).join("")}</div>
      ${data.workers.some(worker => !worker.online) ? '<p class="admin-warning">有后台进程离线。已排队任务会保留，请检查服务器上的对应服务。</p>' : ""}
      <p>模型补查：${counts.queued} 项排队 · ${counts.running} 项执行中 · ${counts.retry} 项等待重试 · ${counts.blocked || 0} 项等待服务恢复 · ${counts.failed} 项失败 · ${counts.paused || 0} 项自动任务已暂停。<a href="#pending">前往审核与重试 →</a></p>
      ${data.research_service?.status === "blocked" ? `<p class="admin-warning">${esc(data.research_service.diagnosis)} 下次检测：${esc(adminTime(data.research_service.next_retry_at))}</p><button class="btn btn-reject" id="research-retry">重新检测补查服务</button>` : ""}</section>
      ${data.model_coverage ? `<section class="admin-section"><h2>模型发布收录对账 <small>${esc(data.model_coverage.day)}</small></h2>
        <p>${data.model_coverage.checked} / ${data.model_coverage.sources} 条来源已核对 · ${data.model_coverage.unresolved_sources} 条来源待处理 · ${data.model_coverage.pending_candidates} 个模型待确认。新模型先建基础档案，未知字段留空；已有模型只融合新事实。默认不自动联网复查。</p>
        ${data.model_coverage.items.map(item => `<details class="admin-run" id="coverage-${item.source_item_id}" ${expanded.has(`coverage-${item.source_item_id}`) ? "open" : ""}>
          <summary>${esc(item.title)} · ${esc(item.source)}${item.section ? " / " + esc(item.section) : ""}</summary>
          <p>${esc(item.reason || "尚未对账")}${item.needs_review ? " · 需要核对身份" : ""}</p>
          <ul>${item.candidates.map(candidate => `<li>${esc(candidate.name)} · ${candidate.model_id ? `<a href="#model/${Number(candidate.model_id)}">已入库 #${Number(candidate.model_id)}</a>` : `<a href="#pending">${esc(candidate.decision === "rejected" ? "已排除" : "待确认")}</a>`}${candidate.reason ? " · " + esc(candidate.reason) : ""}</li>`).join("")}</ul>
        </details>`).join("")}</section>` : ""}
      <section class="admin-section"><h2>采集源</h2><p>停用后跳过尚未开始的采集，保留历史资料；正在进行的请求会完成。重新启用后可点击“采集并更新”。</p>
        ${data.sources.map(source => `<div class="admin-source"><div><strong>${esc(source.name)}</strong><span class="admin-state">${source.enabled ? esc(adminLabels[source.status] || "等待首次采集") : "已停用"}</span>
          <small>最近成功：${esc(adminTime(source.fetched_at))}${source.error ? " · " + esc(source.error) : ""}</small>
          ${source.diagnosis ? `<small>${esc(source.diagnosis)}${source.next_retry_at ? " 下次尝试：" + esc(adminTime(source.next_retry_at)) : ""}</small>` : ""}</div>
          <button class="btn btn-reject" data-source="${esc(source.key)}" data-enabled="${!source.enabled}" aria-pressed="${source.enabled}">${source.enabled ? "停用" : "启用"}</button></div>`).join("")}</section>
      <section class="admin-section"><h2>采集任务 <small>最近 30 次</small></h2><p>失败步骤间隔 30 分钟重试，最多自动尝试 3 次；成功采集的来源跳过，已完成的分析结果继续复用。</p>
        ${data.runs.length ? data.runs.map(run => `<details class="admin-run" id="run-${run.id}" ${expanded.has(`run-${run.id}`) ? "open" : ""}>
          <summary>#${run.id} · ${run.key.startsWith("daily:") ? "每日更新" : "手动更新"} · ${esc(adminTime(run.requested_at))}<span>${esc(adminLabels[run.status])} · 第 ${run.attempts} 次</span></summary>
          ${run.status === "retry" ? `<p>下次尝试：${esc(adminTime(run.next_run_at))}</p>` : ""}
          ${run.error ? `<p class="admin-warning">${esc(run.error)}</p>` : ""}
          <ul>${Object.values(run.stages).map(stage => `<li><strong>${esc(stage.label)}</strong> · ${esc(adminLabels[stage.status] || stage.status)}${stage.detail ? " · " + esc(stage.detail) : ""}</li>`).join("")}</ul>
          ${["retry", "failed", "blocked"].includes(run.status) ? `<button class="btn btn-reject" data-retry="${run.id}">重试未完成步骤</button>` : ""}</details>`).join("") : "<p>暂无任务记录。可以立即采集，也可以等待每天 10 点自动运行。</p>"}</section>
      <details class="admin-section" id="admin-events" ${expanded.has("admin-events") ? "open" : ""}><summary>最近管理操作</summary>
        <ul>${data.events.map(event => `<li>${esc(adminTime(event.at))} · ${esc(adminEvent(event.action))}</li>`).join("")}</ul></details>`;
    root.querySelectorAll("[data-source]").forEach(button => button.onclick = () => performAdmin(button,
      "/api/admin/sources/" + button.dataset.source, {method:"PATCH", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({enabled:button.dataset.enabled === "true"})}, "采集源设置已保存。"));
    root.querySelectorAll("[data-retry]").forEach(button => button.onclick = () => performAdmin(button,
      `/api/admin/runs/${button.dataset.retry}/retry`, {method:"POST"}, "已加入重试队列。"));
    const researchRetry = root.querySelector("#research-retry");
    if (researchRetry) researchRetry.onclick = () => performAdmin(researchRetry, "/api/admin/research/retry", {method:"POST"}, "已允许后台重新检测补查服务。");
    loadAssistantAdmin();
  } catch (error) {
    document.getElementById("admin-message").textContent = error.message;
  } finally {
    if (activeView === "manage") adminTimer = setTimeout(loadAdminOverview, 8000);
  }
}

async function loadAssistantAdmin() {
  let section = document.getElementById("assistant-admin");
  if (!section) {
    section = document.createElement("details");
    section.id = "assistant-admin";
    section.className = "admin-section";
    document.getElementById("admin-overview").after(section);
  }
  // The overview refreshes every 8 seconds; do not overwrite a form being edited.
  if (section.contains(document.activeElement) || section.dataset.dirty === "true") return;
  try {
    const data = await adminAction("/api/admin/assistant");
    if (activeView !== "manage") return;
    const config = data.settings, usage = data.usage;
    section.innerHTML = `<summary>囤囤 · 访客助手</summary><p>${esc(data.day)} · 已使用 ${usage.count} 次对话 · ${usage.calls} 次模型调用 · ${data.running} 项进行中<br>输入 ${usage.input_tokens} / 输出 ${usage.output_tokens} tokens · 联网 ${usage.web_calls} 次</p>
      <form class="assistant-admin-form"><label><input name="enabled" type="checkbox" ${config.enabled ? "checked" : ""}> 开放助手</label>
      <label><input name="web_enabled" type="checkbox" ${config.web_enabled ? "checked" : ""}> 允许按需联网补查</label>
      <div class="admin-grid">${[["session_limit","访客每日次数",1,500],["site_limit","全站每日次数",1,10000],["ip_limit","IP 每日次数",1,2000],["minute_limit","访客每分钟次数",1,30],["max_concurrent","全站同时处理数",1,10],["daily_calls","每日模型调用上限",1,10000],["input_budget","每日输入 token 预算",12000,100000000],["output_budget","每日输出 token 预算",2000,10000000]].map(([key,label,min,max]) => `<label>${label}<input name="${key}" type="number" min="${min}" max="${max}" required value="${config[key]}"></label>`).join("")}</div>
      <p>额度按北京时间每日重置；清空对话不重置已消耗额度。关闭后不再开始新的模型或联网调用。</p><button class="btn btn-reject" type="submit">保存助手设置</button><span role="status" class="assistant-admin-message"></span></form>`;
    const form = section.querySelector("form");
    form.oninput = () => section.dataset.dirty = "true";
    form.onsubmit = async event => {
      event.preventDefault();
      const button = form.querySelector("button"); button.disabled = true;
      const values = Object.fromEntries([...form.elements].filter(input => input.name).map(input => [input.name, input.type === "checkbox" ? input.checked : Number(input.value)]));
      try {
        await adminAction("/api/admin/assistant", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(values)});
        section.dataset.dirty = "false";
        section.querySelector(".assistant-admin-message").textContent = " 设置已保存。";
      } catch (error) { section.querySelector(".assistant-admin-message").textContent = " " + error.message; }
      finally { button.disabled = false; }
    };
  } catch (error) { section.textContent = "囤囤设置暂不可用：" + error.message; }
}

async function performAdmin(button, url, options, message) {
  button.disabled = true;
  try {
    await adminAction(url, options);
    document.getElementById("admin-message").textContent = message;
    await Promise.all([loadAdminOverview(), loadInsightStatus()]);
  } catch (error) {
    document.getElementById("admin-message").textContent = error.message;
  } finally { button.disabled = false; }
}

document.getElementById("admin-login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.getElementById("admin-password");
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    adminSession = await adminAction("/api/admin/login", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({password:input.value})});
    input.value = "";
    route(adminDestination);
  } catch (error) { document.getElementById("admin-login-message").textContent = error.message; }
  finally { button.disabled = false; }
});
document.getElementById("admin-logout").onclick = async () => {
  try {
    await adminAction("/api/admin/logout", {method:"POST"});
    adminSession = null;
    document.getElementById("pending-list").innerHTML = "";
    document.getElementById("admin-overview").innerHTML = "";
    document.getElementById("assistant-admin")?.remove();
    route("manage");
  } catch (error) { alert(error.message); }
};
