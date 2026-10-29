/* 囤囤：the server owns answers and evidence; animation only reflects request state. */
const PetHelpers = {
  safeLink(value) {
    if (/^#(?:model|insight)\/\d+$/.test(value || "")) return value;
    try {
      const url = new URL(value);
      if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) return url.href;
    } catch (_) { /* Invalid evidence links remain plain text. */ }
    return null;
  },
  phase(value) {
    return ({thinking:"正在想怎么查…", searching:"正在翻资料…", composing:"正在整理回答…", done:"整理好了", failed:"这次遇到一点问题", cancelled:"已停止", interrupted:"上次处理已中断"})[value] || "有什么想了解的？";
  },
  accepts(event, version, clientId) { return event.version === version && event.request_id === clientId; }
};
if (typeof module !== "undefined") module.exports = PetHelpers;

if (typeof document !== "undefined") (() => {
  "use strict";
  const asset = "/static/assets/pet/tuntun-cool.svg";
  const root = document.createElement("aside");
  root.className = "pet-root";
  root.dataset.state = "idle";
  root.setAttribute("aria-label", "囤囤 AI 助手");
  root.innerHTML = `<div class="pet-dock"><button type="button" class="pet-launcher" aria-label="双击囤囤摸摸头，可拖动" aria-describedby="pet-move-help">
    <span class="pet-hint">双击摸摸头 · 拖动搬家</span><span class="pet-art"><span class="pet-vector"><img src="${asset}" alt="" width="120" height="120" draggable="false"></span></span></button>
    <div class="pet-controls"><button type="button" class="pet-chat" aria-label="打开囤囤 AI 助手" aria-expanded="false" aria-controls="pet-panel">聊一聊 <span aria-hidden="true">↗</span></button>
      <button type="button" class="pet-more" aria-label="囤囤互动菜单" aria-expanded="false" aria-controls="pet-menu">···</button></div></div>
    <span id="pet-move-help" class="pet-sr-only">双击或快速轻点两下摸头，Enter 或空格也可摸头。拖动移动，方向键微调位置，Home 复位。右键或更多按钮打开动作菜单。</span>
    <div class="pet-bubble" role="status" hidden></div>
    <div id="pet-menu" class="pet-menu" role="group" aria-label="囤囤互动" hidden><div class="pet-menu-title">和囤囤玩一会儿</div>
      <button type="button" data-pet-action="pat">摸摸头 <span>♡</span></button>
      <button type="button" data-pet-action="wave">打个招呼 <span>✧</span></button>
      <button type="button" data-pet-action="hop">跳一跳 <span>↑</span></button>
      <button type="button" data-pet-action="chat">聊一聊 <span>↗</span></button>
      <button type="button" data-pet-action="reset">回到右下角 <span>↘</span></button>
      <button type="button" data-pet-action="hide">暂时隐藏 <span>−</span></button></div>
    <section id="pet-panel" class="pet-panel" role="dialog" aria-labelledby="pet-title" hidden>
      <div class="pet-header" title="拖动标题栏移动助手"><img src="${asset}" alt="" width="45" height="45" draggable="false"><div class="pet-heading"><strong id="pet-title">囤囤 <span class="pet-grip" aria-hidden="true">⠿</span></strong><span>你的 AI 情报助手</span></div>
        <button class="pet-icon" type="button" data-action="clear" title="清空对话" aria-label="清空对话">↺</button>
        <button class="pet-icon" type="button" data-action="hide" title="隐藏助手，可从页脚唤回" aria-label="隐藏助手">−</button>
        <button class="pet-icon" type="button" data-action="close" title="收起" aria-label="收起对话">×</button></div>
      <div class="pet-messages" role="log" aria-label="对话记录" aria-live="polite" aria-relevant="additions text">
        <div class="pet-welcome"><img src="${asset}" alt="抱着资料夹的囤囤" width="124" height="124"><h2>把想了解的，交给囤囤。</h2>
          <p>翻翻新闻，找找模型。<br>重要结论，都带着出处。</p>
          <div class="pet-suggestions">
            <button type="button">最近 OpenAI 发布了哪些产品？<span aria-hidden="true">↗</span></button>
            <button type="button">推荐 30B 左右的长文本开源模型<span aria-hidden="true">↗</span></button>
            <button type="button">最近有哪些值得关注的 AI 工程进展？<span aria-hidden="true">↗</span></button>
          </div></div></div>
      <div class="pet-status" role="status"></div>
      <form class="pet-composer"><label class="pet-sr-only" for="pet-input">向囤囤提问</label>
        <textarea id="pet-input" rows="2" maxlength="2000" placeholder="问问新闻、模型，或聊两句…"></textarea>
        <button type="submit" class="pet-send" aria-label="发送问题" title="发送问题">↑</button>
        <button type="button" class="pet-stop" data-action="stop" hidden>停止</button></form>
      <div class="pet-footnote"><span class="pet-quota">点击后连接</span><span>记录保留 24 小时 · 可清空</span></div>
    </section>`;
  document.body.append(root);
  const $ = selector => root.querySelector(selector);
  const panel = $(".pet-panel"), launcher = $(".pet-launcher"), log = $(".pet-messages"), input = $("textarea"), status = $(".pet-status");
  const dock = $(".pet-dock"), chat = $(".pet-chat");
  let session = null, busy = false, active = null, generation = 0, loading = null, controller = null;
  const rows = new Map();
  let initiallyHidden = false;
  try { initiallyHidden = localStorage.getItem("tracker.pet.hidden") === "true"; } catch (_) { /* storage optional */ }
  dock.hidden = initiallyHidden;
  const media = mountPetVideo(root);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  function setState(value) {
    root.dataset.state = value;
    media.setState(value);
    $(".pet-hint").textContent = busy ? PetHelpers.phase(value) : "双击摸摸头 · 拖动搬家";
  }
  function setBusy(value) {
    busy = value;
    $(".pet-send").hidden = value;
    $(".pet-stop").hidden = !value;
    input.disabled = value;
    $(".pet-send").disabled = !session || !session.enabled || !session.configured;
    $(".pet-stop").disabled = !active;
  }
  function quota() {
    if (!session) return;
    $(".pet-quota").textContent = session.enabled && session.configured ? `今日剩余 ${session.remaining} / ${session.daily_limit} 次` : "助手暂未开放";
    if (!busy) setBusy(false);
  }
  function adoptSession(data) {
    if (session && (session.csrf !== data.csrf || session.version !== data.version)) {
      generation++; controller?.abort(); active = null; setBusy(false);
      rows.forEach(row => row.remove()); rows.clear(); $(".pet-welcome").hidden = true;
    }
    session = data;
    $(".pet-welcome").hidden = rows.size > 0 || data.history.length > 0;
    quota();
  }
  function link(title, url, className) {
    const safe = PetHelpers.safeLink(url);
    const element = node(safe ? "a" : "span", title, className);
    if (safe) {
      element.href = safe;
      if (safe.startsWith("#")) element.onclick = close;
      else { element.target = "_blank"; element.rel = "noopener noreferrer"; element.referrerPolicy = "no-referrer"; }
    }
    return element;
  }
  function renderAnswer(target, answer) {
    target.replaceChildren();
    const numbers = new Map((answer.sources || []).map((source, i) => [source.id, i + 1]));
    for (const block of answer.blocks || []) {
      const p = node("p", block.text);
      for (const id of block.citation_ids || []) {
        const source = answer.sources?.find(item => item.id === id);
        if (source) p.append(link(`[${numbers.get(id)}]`, source.url, "pet-citation"));
      }
      target.append(p);
    }
    if (answer.cards?.length) {
      const list = node("div", null, "pet-results");
      for (const card of answer.cards) {
        const a = link(card.title, card.url, "pet-result");
        a.prepend(node("span", card.kind === "model" ? "模型" : "新闻", "pet-result-type"));
        a.append(node("span", "↗", "pet-result-arrow"));
        list.append(a);
      }
      target.append(list);
    }
    if (answer.limitations?.length) target.append(node("p", answer.limitations.join("\n"), "pet-limitation"));
    if (answer.sources?.length) {
      const details = node("details", null, "pet-sources");
      details.append(node("summary", `${answer.sources.length} 处参考资料`));
      answer.sources.forEach((source, i) => details.append(link(`${i + 1}. ${source.title}`, source.url)));
      target.append(details);
    }
    if (answer.follow_up_question) target.append(node("p", answer.follow_up_question, "pet-followup"));
  }
  function renderRequest(event) {
    $(".pet-welcome").hidden = true;
    let row = rows.get(event.request_id);
    if (!row) {
      row = node("div", null, "pet-turn");
      row.append(node("div", event.message, "pet-user"), node("div", "囤囤", "pet-speaker"), node("div", null, "pet-answer"));
      rows.set(event.request_id, row);
      log.append(row);
    }
    const answer = row.querySelector(".pet-answer");
    if (event.answer) renderAnswer(answer, event.answer);
    else answer.textContent = event.error || PetHelpers.phase(event.phase);
    answer.classList.toggle("pet-pending", event.status === "running");
    log.scrollTop = log.scrollHeight;
  }
  async function api(path, options = {}) {
    const response = await fetch("/api/assistant" + path, {credentials:"same-origin", cache:"no-store", ...options,
      headers:{"Content-Type":"application/json", ...(session ? {"X-CSRF-Token":session.csrf} : {}), ...options.headers}});
    if (!response.ok) {
      let detail;
      try { const data = await response.json(); detail = typeof data.detail === "string" ? data.detail : null; } catch (_) { /* no body */ }
      throw new Error(detail || `连接暂不可用（${response.status}），请稍后重试。`);
    }
    return response;
  }
  async function connect() {
    if (loading) return loading;
    const gen = generation;
    loading = (async () => {
      status.textContent = "正在连接囤囤…";
      try {
        const data = await (await api("/session")).json();
        if (gen !== generation) return;
        adoptSession(data);
        for (const request of data.history) renderRequest(request);
        quota();
        status.textContent = !data.enabled ? "囤囤暂时休息中。" : !data.configured ? "助手尚未配置模型服务。" : "";
        const pending = data.history.find(request => request.status === "running");
        if (pending && !busy) {
          active = pending.id;
          setBusy(true);
          const recoveryGeneration = generation;
          recover(pending.id, pending.request_id, recoveryGeneration).catch(error => { if (recoveryGeneration === generation) status.textContent = error.message; })
            .finally(() => { if (recoveryGeneration === generation) { active = null; setBusy(false); } });
        }
      } catch (error) { status.textContent = error.message; setState("error"); }
      finally { loading = null; }
    })();
    return loading;
  }
  function receive(event, clientId, gen) {
    if (gen !== generation || !PetHelpers.accepts(event, session.version, clientId)) return false;
    active = event.id;
    $(".pet-stop").disabled = false;
    renderRequest(event);
    const state = event.status === "running" ? event.phase : event.status === "done" ?
      (event.answer?.follow_up_question ? "waiting" : "done") : event.status === "cancelled" ? "idle" : "error";
    setState(state);
    status.textContent = event.status === "running" ? PetHelpers.phase(event.phase) : "";
    return event.status !== "running";
  }
  async function recover(identifier, clientId, gen) {
    for (let i = 0; i < 100 && gen === generation; i++) {
      const event = await (await api("/requests/" + identifier)).json();
      if (receive(event, clientId, gen)) return;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (gen === generation) throw new Error("连接中断，请收起并重新打开囤囤查看结果。不会重复生成。");
  }
  async function send(text) {
    if (busy) return;
    if (!session) await connect();
    if (!session?.enabled || !session?.configured || busy) return;
    text = text.trim();
    if (!text) return;
    const gen = generation, clientId = crypto.randomUUID();
    const body = JSON.stringify({request_id:clientId, version:session.version, message:text});
    active = null;
    setBusy(true); setState("thinking"); input.value = "";
    renderRequest({request_id:clientId, message:text, status:"running", phase:"thinking"});
    controller = new AbortController();
    try {
      const response = await api("/messages", {method:"POST", body, signal:controller.signal});
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "", complete = false;
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), {stream:!chunk.done});
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const part = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          if (part.startsWith("data: ")) complete = receive(JSON.parse(part.slice(6)), clientId, gen) || complete;
        }
        if (chunk.done) break;
      }
      if (!complete && active && gen === generation) await recover(active, clientId, gen);
      else if (!complete && gen === generation) throw new Error("连接已中断，重新打开后可查看处理状态。");
    } catch (error) {
      if (gen !== generation || error.name === "AbortError") return;
      if (active) {
        try { await recover(active, clientId, gen); }
        catch (recoveryError) { status.textContent = recoveryError.message; setState("error"); }
      } else {
        status.textContent = error.message; setState("error");
        const target = rows.get(clientId)?.querySelector(".pet-answer");
        if (target) { target.textContent = error.message; target.classList.remove("pet-pending"); }
      }
    } finally {
      if (gen === generation) {
        active = null; controller = null; setBusy(false);
        try { adoptSession(await (await api("/session")).json()); } catch (_) { /* keep last known quota */ }
        if (!panel.hidden) input.focus();
      }
    }
  }
  function close() { panel.hidden = true; chat.setAttribute("aria-expanded", "false"); interaction.layout(); chat.focus(); }
  async function open() {
    dock.hidden = false; panel.hidden = false; chat.setAttribute("aria-expanded", "true"); interaction.layout();
    media.setVisible(true);
    try { localStorage.removeItem("tracker.pet.hidden"); } catch (_) { /* storage optional */ }
    if (!busy) { setState("wave"); setTimeout(() => { if (!busy && root.dataset.state === "wave") setState("idle"); }, 900); }
    await connect(); if (!panel.hidden) input.focus();
  }
  function hide() {
    interaction.suspend(); close(); dock.hidden = true;
    media.setVisible(false);
    try { localStorage.setItem("tracker.pet.hidden", "true"); } catch (_) { /* storage optional */ }
    document.getElementById("pet-reopen")?.focus();
  }
  const interaction = mountPetInteraction(root, {open, hide, onReaction:action => media.react(action), onDragging:value => media.setDragging(value)});
  chat.onclick = () => panel.hidden ? open() : close();
  $("[data-action=close]").onclick = close;
  $("[data-action=hide]").onclick = hide;
  $("[data-action=clear]").onclick = async () => {
    const button = $("[data-action=clear]"); button.disabled = true;
    try {
      if (session) {
        const result = await (await api("/conversation", {method:"DELETE"})).json();
        generation++; controller?.abort(); active = null; session = {...session, ...result};
      }
      rows.forEach(row => row.remove()); rows.clear(); $(".pet-welcome").hidden = false;
      status.textContent = "对话已清空，已使用的额度保留。"; setBusy(false); setState("idle"); quota(); input.focus();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("[data-action=stop]").onclick = async () => {
    if (!active) return;
    try { await api(`/requests/${active}/cancel`, {method:"POST"}); }
    catch (error) { status.textContent = error.message; }
  };
  $("form").onsubmit = event => { event.preventDefault(); send(input.value); };
  input.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(input.value); }
  });
  root.addEventListener("keydown", event => { if (event.key === "Escape" && !panel.hidden) { event.preventDefault(); close(); } });
  root.querySelectorAll(".pet-suggestions button").forEach(button => button.onclick = () => send(button.firstChild.textContent));
  document.getElementById("pet-reopen")?.addEventListener("click", event => { event.preventDefault(); open(); });
  document.addEventListener("visibilitychange", () => root.classList.toggle("pet-paused", document.hidden));
  setBusy(false);

  // Inline only the trusted, local SVG; namespace every ID so page artwork cannot collide.
  fetch(asset).then(response => { if (!response.ok) throw new Error("art unavailable"); return response.text(); }).then(text => {
    const svg = new DOMParser().parseFromString(text, "image/svg+xml").documentElement;
    if (svg.nodeName !== "svg") return;
    svg.querySelectorAll("script,foreignObject").forEach(element => element.remove());
    const ids = new Map([...svg.querySelectorAll("[id]")].map(element => [element.id, "pet-art-" + element.id]));
    for (const element of [svg, ...svg.querySelectorAll("*")]) {
      if (element.id) { element.dataset.part = element.id; element.id = ids.get(element.id); }
      for (const attr of [...element.attributes]) {
        if (/^on/i.test(attr.name)) { element.removeAttribute(attr.name); continue; }
        let value = attr.value;
        for (const [id, renamed] of ids) value = value.replaceAll(`url(#${id})`, `url(#${renamed})`);
        if (attr.name.endsWith("href") && value.startsWith("#")) value = "#" + (ids.get(value.slice(1)) || value.slice(1));
        if (attr.name !== "id") element.setAttribute(attr.name, value);
      }
    }
    svg.setAttribute("aria-hidden", "true"); svg.removeAttribute("aria-labelledby");
    $(".pet-vector").replaceChildren(document.importNode(svg, true));
  }).catch(() => { /* Static <img> remains a usable launcher if animation cannot load. */ });
})();
