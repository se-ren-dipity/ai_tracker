// Curated information views; shared navigation and helpers live in index.html.
async function readJSON(url, options) {
  const response = await apiFetch(API + url, options);
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : response.status === 404 ? "这条情报不存在" : "加载失败，请稍后重试");
  }
  return response.json();
}

async function loadInsightStatus() {
  const data = await readJSON("/api/insights/status");
  const counts = data.counts;
  const pending = (counts.pending || 0) + (counts.failed || 0);
  const progress = data.running ? (pending ? `正在整理，剩余 ${pending} 条` : "正在编排文章…") : pending ? `${pending} 条待整理${counts.failed ? `（${counts.failed} 条上次失败，可重试）` : ""}` : data.unwritten ? `${data.unwritten} 篇文章待编排，可点击更新继续` : data.unprepared_articles ? "有资料等待拆分" : "文章已更新";
  const sources = (data.collection_sources || []).map(source => `${esc(source.name)}：${source.status === "ok" ? "连接正常" : source.status === "cached" ? `刷新失败，使用 ${esc(source.fetched_at ? new Date(source.fetched_at).toLocaleString() : "已有")} 缓存` : "采集失败"}`).join("<br>");
  document.getElementById("insight-status").innerHTML = `<strong>${data.insights} 篇文章</strong> · 已收录 ${data.articles} 份原文 · 合并 ${data.merged} 条重复报道${counts.filtered ? ` · 过滤 ${counts.filtered} 条` : ""}<br>${esc(progress)}${data.last_error ? `<br>${esc(data.last_error)}` : ""}${sources ? `<br>${sources}` : ""}`;
  const button = document.getElementById("organize-button");
  button.disabled = data.running;
  button.textContent = data.running ? "正在更新…" : data.last_error || counts.failed ? "重试采集与整理" : "采集并更新";
  clearTimeout(statusTimer);
  if (data.running && activeView === "manage") statusTimer = setTimeout(async () => {
    try { await loadInsightStatus(); }
    catch { document.getElementById("insight-status").textContent = "更新状态暂时无法读取，请稍后重试。"; }
  }, 8000);
}

async function loadInsightDates() {
  const data = await readJSON("/api/insights/dates");
  const sel = document.getElementById("insight-day");
  const selection = insightReady ? sel.value : data.dates[0] || "";
  sel.innerHTML = '<option value="">全部日期</option>' + data.dates.map(day => `<option value="${esc(day)}">${esc(day)}</option>`).join("");
  sel.value = selection;
  insightReady = true;
}

async function loadInsights(refreshDates = true) {
  const request = ++insightRequest;
  const list = document.getElementById("insight-list");
  const pages = document.getElementById("insight-pages");
  pages.innerHTML = "";
  list.innerHTML = '<div class="loading"><div class="spinner"></div>正在加载文章…</div>';
  try {
    if (refreshDates || !insightReady) await loadInsightDates();
    if (request !== insightRequest) return;
    const params = new URLSearchParams({page: insightPage, page_size: 12});
    for (const [id, key] of [["insight-day","day"],["insight-cat","category"],["insight-q","q"]]) {
      const value = document.getElementById(id).value.trim();
      if (value) params.set(key, value);
    }
    const data = await readJSON("/api/insights?" + params);
    if (request !== insightRequest) return;
    const edition = document.getElementById("edition-date");
    const day = params.get("day");
    edition.textContent = day ? new Date(day + "T12:00:00+08:00").toLocaleDateString("zh-CN", {year:"numeric", month:"long", day:"numeric"}) : "全部日期";
    if (day) edition.setAttribute("datetime", day); else edition.removeAttribute("datetime");
    const count = document.getElementById("insight-count");
    count.hidden = !params.has("q") && !params.has("category") && insightPage === 1 && data.total > 0;
    count.textContent = `${params.has("q") ? "找到 " : ""}${data.total} 篇文章`;
    const featureFirst = insightPage === 1 && !params.has("q") && !params.has("category");
    list.innerHTML = data.items.length ? data.items.map((item, index) => renderStory(item, featureFirst && index === 0)).join("") : '<div class="empty">没有找到文章，试试其他关键词、日期或分类。</div>';
    list.querySelectorAll(".story").forEach((story, index) => bindStoryImage(story, data.items[index]));
    const totalPages = Math.ceil(data.total / 12);
    if (totalPages > 1) {
      pages.innerHTML = `<button class="btn btn-reject" id="insight-prev" ${insightPage === 1 ? "disabled" : ""}>上一页</button><span>${insightPage} / ${totalPages}</span><button class="btn btn-reject" id="insight-next" ${insightPage === totalPages ? "disabled" : ""}>下一页</button>`;
      document.getElementById("insight-prev").onclick = () => { insightPage--; loadInsights(false); window.scrollTo(0,0); };
      document.getElementById("insight-next").onclick = () => { insightPage++; loadInsights(false); window.scrollTo(0,0); };
    }
  } catch (error) {
    if (request === insightRequest) list.innerHTML = `<div class="empty">${esc(error.message)} <button class="btn btn-reject" onclick="loadInsights()">重试</button></div>`;
  }
}

function imageSource(image) {
  if (!image) return "";
  if (image.cached_url !== undefined) {
    return /^\/api\/articles\/[1-9]\d*\/images\/[a-f0-9]{64}$/.test(image.cached_url) ? image.cached_url : "";
  }
  return /^https?:\/\//i.test(image.url) ? image.url : "";
}

function storyCoverImages(item) {
  return (item.cover_images || (item.cover_image ? [item.cover_image] : []))
    .filter(image => imageSource(image));
}

function bindStoryImage(story, item) {
  const img = story.querySelector(".story-image img");
  if (!img) return;
  const covers = storyCoverImages(item);
  let index = 0;
  const tryNextImage = () => {
    const next = covers[++index];
    if (next) {
      img.alt = next.alt || item.title;
      img.src = imageSource(next);
    } else {
      img.removeEventListener("error", tryNextImage);
      story.classList.add("no-image");
      story.querySelector(".story-image").remove();
    }
  };
  img.addEventListener("error", tryNextImage);
  if (img.complete && !img.naturalWidth) tryNextImage();
}

function renderStory(item, featured) {
  const cover = storyCoverImages(item)[0];
  const text = item.body_paragraphs?.[0] || item.summary || "";
  const minutes = Math.max(1, Math.ceil((item.body_paragraphs?.join("") || text).length / 450));
  return `<article class="story${featured ? " story-featured" : ""}${cover ? "" : " no-image"}">
    <div class="story-copy">
      <div class="story-meta"><span class="story-category">${catLabel(item.category)}</span><span>${fmtDate(item.published)}</span><span>${minutes} 分钟阅读</span></div>
      <h2 class="story-title"><a href="#insight/${item.id}">${esc(item.title)}</a></h2>
      <p class="story-summary">${esc(text)}</p>
      ${featured ? `<a class="story-read" href="#insight/${item.id}">阅读全文 <span aria-hidden="true">→</span></a>` : ""}
    </div>
    ${cover ? `<a class="story-image" href="#insight/${item.id}" aria-label="阅读：${esc(item.title)}" tabindex="-1"><img src="${esc(imageSource(cover))}" alt="${esc(cover.alt || item.title)}" loading="${featured ? "eager" : "lazy"}" decoding="async" referrerpolicy="no-referrer"></a>` : ""}
  </article>`;
}

function sourceHref(source) {
  return `#article/${source.article_id}${/^article-section-\d+$/.test(source.section_id || "") ? "/" + source.section_id : ""}`;
}

function articleText(text, keywords, emphasized) {
  const words = keywords.filter(word => typeof word === "string" && word.trim()).sort((a, b) => b.length - a.length);
  if (!words.length) return esc(text);
  const pattern = new RegExp(`(${words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  return text.split(pattern).map((part, index) => {
    if (!(index % 2) || emphasized.has(part)) return esc(part);
    emphasized.add(part);
    return `<span class="kw">${esc(part)}</span>`;
  }).join("");
}

async function loadInsight(id) {
  const detail = document.getElementById("insight-detail");
  detail.innerHTML = '<div class="loading"><div class="spinner"></div>正在读取文章…</div>';
  try {
    const item = await readJSON("/api/insights/" + id);
    if (activeView !== "insight" || currentInsight !== id) return;
    document.title = item.title + " · AI 情报";
    const paragraphs = item.body_paragraphs?.length ? item.body_paragraphs : [item.summary, item.analysis].filter(Boolean);
    const images = (item.images || []).filter(img => imageSource(img));
    const minutes = Math.max(1, Math.ceil(paragraphs.join("").length / 450));
    const sources = item.sources.filter((source, index, all) => all.findIndex(s => s.source === source.source) === index);
    const emphasized = new Set();
    // Keep pictures between paragraphs, never as a separate gallery below the story.
    const imageSlots = new Map();
    const selectedImages = images.slice(0, Math.max(0, paragraphs.length - 1));
    selectedImages.forEach((img, index) => {
      const slot = index + (paragraphs.length > selectedImages.length + 1 ? 1 : 0);
      imageSlots.set(slot, img);
    });
    detail.innerHTML = `<a class="reader-back" href="#feed">← 返回资讯</a>
      <div class="reader-meta"><span class="cat-pill cat-${esc(item.category)}">${catLabel(item.category)}</span><span>${fmtDate(item.published)}</span><span>约 ${minutes} 分钟阅读</span></div>
      <h1 class="reader-title">${esc(item.title)}</h1>
      <article class="insight-prose" aria-label="文章正文">${paragraphs.map((paragraph, index) => {
        const img = imageSlots.get(index);
        const alt = img && (img.alt || `${item.title} · 配图 ${images.indexOf(img) + 1}`);
        return `<p${index === 0 ? ' class="article-lead"' : ""}>${index === 0 ? esc(paragraph) : articleText(paragraph, item.keywords.slice(0, 4), emphasized)}</p>${img ? `<figure class="article-figure"><button type="button" class="article-image" aria-label="放大：${esc(alt)}"><img src="${esc(imageSource(img))}" alt="${esc(alt)}" loading="lazy" decoding="async" referrerpolicy="no-referrer"></button><figcaption><span>点击放大</span></figcaption></figure>` : ""}`;
      }).join("")}</article>
      ${(item.models || []).length ? `<div class="article-model">${item.models.map(model => renderModelChip(model.id, model.name)).join("")}</div>` : item.related_model_id ? `<div class="article-model">${renderModelChip(item.related_model_id)}</div>` : ""}
      <footer class="article-end">
        ${(item.links || []).length ? `<p class="article-links"><span>延伸阅读</span>${item.links.filter(link => /^https?:\/\//i.test(link.url)).map(link => `<a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)} ↗</a>`).join("")}</p>` : ""}
        <p class="article-credits"><span>参考</span>${sources.map(source => `<a href="${sourceHref(source)}" title="阅读收录原文：${esc(source.title)}">${esc(source.source)}</a>`).join('<span aria-hidden="true">·</span>')}</p>
      </footer>`;
    detail.querySelectorAll(".article-image").forEach(button => {
      const img = button.querySelector("img");
      const hideUnavailable = () => button.closest("figure").remove();
      img.addEventListener("error", hideUnavailable, {once: true});
      if (img.complete && !img.naturalWidth) hideUnavailable();
      button.onclick = () => openArticleImage(img);
    });
  } catch (error) {
    if (activeView === "insight" && currentInsight === id) detail.innerHTML = `<a class="reader-back" href="#feed">← 返回资讯</a><div class="empty">${esc(error.message)}</div>`;
  }
}

function openArticleImage(img) {
  const viewer = document.getElementById("article-image-viewer");
  const large = viewer.querySelector("img");
  large.src = img.src;
  large.alt = img.alt;
  document.getElementById("image-viewer-caption").textContent = img.alt;
  viewer.querySelector(".image-zoom").textContent = "查看原尺寸";
  viewer.querySelector(".image-zoom").setAttribute("aria-pressed", "false");
  viewer.classList.remove("original-size");
  viewer.showModal();
  document.body.classList.add("image-viewer-open");
}

document.addEventListener("DOMContentLoaded", () => {
  const viewer = document.getElementById("article-image-viewer");
  viewer.querySelector(".image-close").onclick = () => viewer.close();
  viewer.querySelector(".image-zoom").onclick = event => {
    const original = viewer.classList.toggle("original-size");
    event.currentTarget.textContent = original ? "适应屏幕" : "查看原尺寸";
    event.currentTarget.setAttribute("aria-pressed", String(original));
  };
  viewer.addEventListener("click", event => { if (event.target === viewer) viewer.close(); });
  viewer.addEventListener("close", () => {
    document.body.classList.remove("image-viewer-open");
    viewer.classList.remove("original-size");
    viewer.querySelector("img").removeAttribute("src");
  });
  window.addEventListener("hashchange", () => { if (viewer.open) viewer.close(); });
  document.querySelectorAll("[data-category]").forEach(button => {
    button.onclick = () => {
      document.getElementById("insight-cat").value = button.dataset.category;
      document.querySelectorAll("[data-category]").forEach(tab => tab.setAttribute("aria-pressed", String(tab === button)));
      clearTimeout(insightTimer);
      insightPage = 1;
      loadInsights(false);
    };
  });
  ["insight-day", "insight-q"].forEach(id => {
    const element = document.getElementById(id);
    element.addEventListener(id === "insight-q" ? "input" : "change", () => {
      clearTimeout(insightTimer);
      insightTimer = setTimeout(() => { insightPage = 1; loadInsights(false); }, id === "insight-q" ? 300 : 0);
    });
  });
  document.getElementById("organize-button").onclick = async () => {
    const button = document.getElementById("organize-button");
    button.disabled = true;
    try { await readJSON("/api/insights/refresh", {method:"POST"}); await loadInsightStatus(); }
    catch (error) { document.getElementById("insight-status").textContent = error.message; button.disabled = false; }
  };
});
