// Model reading pages; operational controls stay in content management.
let modelDetailRequest = 0;
const modelFieldLabels = {params_b:"参数量", context_length:"上下文", license:"许可证", organization:"开发机构", languages:"支持语言", modality_in:"输入", modality_out:"输出", release_date:"发布日期", announced_date:"公布日期", available_date:"开放日期", weight_access:"模型权重", api_availability:"API 服务"};
const modelValueLabels = {open:"开放权重", closed:"闭源", available:"已开放", preview:"预览开放", unavailable:"尚未开放", discontinued:"已停止服务", text:"文本", image:"图像", audio:"音频", video:"视频", zh:"中文", en:"英语"};
function modelValue(field, value, precision) {
  if (value == null || value === "unknown" || value === "" || Array.isArray(value) && !value.length) return "暂无资料";
  if (field === "params_b") return `${Number(value).toLocaleString("zh-CN", {maximumFractionDigits:2})}B`;
  if (field === "context_length") return `${value >= 1000000 ? Number((value / 1000000).toFixed(2)) + "M" : value >= 1000 ? Number((value / 1000).toFixed(2)) + "K" : value} tokens`;
  if (field.endsWith("_date")) return precision === "month" ? String(value).slice(0,7).replace("-","/") : fmtDate(value);
  if (Array.isArray(value)) return value.map(v => modelValueLabels[v] || v).join("、");
  return modelValueLabels[value] || String(value);
}

async function loadModelDetail(id) {
  const request = ++modelDetailRequest;
  const root = document.getElementById("model-detail");
  root.innerHTML = '<div class="loading"><div class="spinner"></div>正在读取模型…</div>';
  try {
    const response = await apiFetch(`${API}/api/models/${id}`);
    if (!response.ok) throw new Error(response.status === 404 ? "没有找到这个模型" : "模型暂时无法加载，请稍后重试");
    const model = await response.json();
    if (request !== modelDetailRequest || activeView !== "model") return;
    document.title = `${model.name} · 模型库 · AI 情报`;
    const value = field => {
      const empty = model[field] == null || model[field] === "unknown" || Array.isArray(model[field]) && !model[field].length;
      if (empty && model.field_states?.[field] === "undisclosed") return "未公开";
      if (empty && model.field_states?.[field] === "not_applicable") return "不适用";
      return modelValue(field, model[field]);
    };
    const dateLabel = model.announced_date ? `公布于 ${fmtDate(model.announced_date)}` : model.release_date ? (model.release_precision === "month" ? model.release_date.slice(0,7).replace("-","/") : fmtDate(model.release_date)) : "";
    const stories = model.articles || [];
    root.innerHTML = `<article class="model-profile">
      <a class="reader-back" href="#models">← 返回模型库</a>
      <header class="model-intro">
        <p class="model-eyebrow">${esc(model.organization || "AI 模型")}<span> / </span>${esc(model.type === "Unknown" ? "模型档案" : model.type)}${dateLabel ? `<span> / </span>${esc(dateLabel)}` : ""}</p>
        <h1>${esc(model.name)}</h1>
        ${model.notes ? `<p class="model-lead">${esc(model.notes)}</p>` : ""}
        <dl class="model-highlights">${["params_b","context_length","weight_access"].map(field => `<div><dt>${modelFieldLabels[field]}</dt><dd>${esc(value(field))}</dd></div>`).join("")}</dl>
      </header>
      <div class="model-columns">
        <div class="model-main">
          <section class="model-section" aria-labelledby="model-spec-title">
            <h2 id="model-spec-title">能力与规格</h2>
            <dl class="model-specs">${["modality_in","modality_out","languages","license","api_availability","announced_date","available_date"].filter(field => !field.endsWith("_date") || model[field]).map(field => `<div><dt>${modelFieldLabels[field]}</dt><dd>${esc(value(field))}</dd></div>`).join("")}</dl>
          </section>
          <section class="model-section" aria-labelledby="model-stories-title">
            <div class="model-section-title"><h2 id="model-stories-title">相关报道</h2><span>${stories.length ? stories.length + " 篇" : ""}</span></div>
            ${stories.length ? stories.map(story => `<article class="model-story">
              <div><p class="story-meta">${esc(catLabel(story.category))}<span>${fmtDate(story.published)}</span></p>
              <h3><a href="#insight/${story.id}">${esc(story.title)}</a></h3>
              <p class="model-story-summary">${esc(story.summary || "")}</p></div>
              ${imageSource(story.cover_image) ? `<a class="model-story-image" href="#insight/${story.id}" tabindex="-1" aria-hidden="true"><img src="${esc(imageSource(story.cover_image))}" alt="" loading="lazy" referrerpolicy="no-referrer"></a>` : ""}
            </article>`).join("") : '<p class="model-muted">关于这个模型的后续报道，会汇集在这里。</p>'}
          </section>
          ${model.events?.length ? `<section class="model-section"><h2>模型进展</h2><ol class="model-updates">${model.events.map(event => `<li><time>${fmtDate(event.date)}</time><p>${esc(event.summary)}</p>${event.sources.filter(source => /^https?:\/\//i.test(source.url)).map(source => `<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>`).join(" · ")}</li>`).join("")}</ol></section>` : ""}
        </div>
        <aside class="model-aside">
          <section class="model-section"><h2>使用与资料</h2>
            ${(model.links || []).length ? model.links.map(link => `<a class="model-resource" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}<span aria-hidden="true">↗</span></a>`).join("") : '<p class="model-muted">暂无公开资料入口。</p>'}
          </section>
          ${model.updates?.length ? `<section class="model-section"><h2>资料更新</h2><ol class="model-updates">${model.updates.slice(0,6).map(update => `<li><time>${fmtDate(update.date)}</time><p>${esc(update.label)} · ${esc(modelValue(update.field, update.value, update.precision))}</p></li>`).join("")}</ol></section>` : ""}
        </aside>
      </div>
      <footer class="model-footnote">${model.last_checked_at ? `资料截至 ${fmtDate(model.last_checked_at)}` : ""}${model.references?.length ? `<details><summary>参考资料</summary>${model.references.map(url => `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(new URL(url).hostname)} ↗</a>`).join("")}</details>` : ""}</footer>
    </article>`;
    root.querySelectorAll(".model-story-image img").forEach(img => {
      const hide = () => img.closest(".model-story-image")?.remove();
      img.addEventListener("error", hide, {once:true});
      if (img.complete && !img.naturalWidth) hide();
    });
  } catch (error) {
    if (request === modelDetailRequest && activeView === "model") root.innerHTML = `<a class="reader-back" href="#models">← 返回模型库</a><div class="empty">${esc(error.message)}</div>`;
  }
}

async function loadModelChanges() {
  const root = document.getElementById("model-changes");
  try {
    const data = await readJSON("/api/pending/changes");
    if (activeView !== "pending") return;
    root.innerHTML = data.items.length ? `<details class="model-change-list"><summary>${data.items.length} 项资料更正待确认</summary>${data.items.map(change => `<div class="model-change">
      <p><a href="#model/${change.model_id}">${esc(change.model_name)}</a> · ${esc(modelFieldLabels[change.field] || change.field)}</p>
      <p>${esc(modelValue(change.field, change.previous_value))} → <strong>${esc(modelValue(change.field, change.value))}</strong></p>
      ${change.quote ? `<blockquote>${esc(change.quote)}</blockquote>` : ""}
      ${change.url && /^https?:\/\//i.test(change.url) ? `<a href="${esc(change.url)}" target="_blank" rel="noopener noreferrer">查看依据 ↗</a>` : ""}
      <div class="action-row">${change.state === "known" ? `<button class="btn btn-approve" onclick="resolveModelChange(${change.id}, 'accept')">采用更正</button>` : ""}<button class="btn btn-reject" onclick="resolveModelChange(${change.id}, 'dismiss')">保留现值</button></div>
    </div>`).join("")}</details>` : "";
  } catch { root.textContent = "更正建议暂时无法加载。"; }
}

async function resolveModelChange(id, action) {
  try {
    const response = await apiFetch(`${API}/api/pending/changes/${id}/${action}`, {method:"POST"});
    if (!response.ok) throw new Error((await response.json()).detail || "处理失败");
    await loadModelChanges();
  } catch (error) { alert(error.message); }
}
