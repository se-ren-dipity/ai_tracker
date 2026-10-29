/* Browser-only gestures; never send questions or change the agent's request state. */
const PetMotion = {
  clamp(value, min, max) { return Math.min(Math.max(value, min), Math.max(min, max)); },
  saved(value) {
    try {
      const p = JSON.parse(value);
      return p && [p.x, p.y].every(n => Number.isFinite(n) && n >= 0 && n <= 1) ? p : null;
    } catch (_) { return null; }
  },
  bounds(size, view, panel = null) {
    const left = Math.min(0, panel?.x || 0), top = Math.min(0, panel?.y || 0);
    const right = Math.max(size.w, panel ? panel.x + panel.w : 0);
    const bottom = Math.max(size.h, panel ? panel.y + panel.h : 0);
    return {minX:view.x + 12 - left, maxX:view.x + view.w - 12 - right,
      minY:view.y + 12 - top, maxY:view.y + view.h - 12 - bottom};
  },
  constrain(point, bounds) {
    return {x:this.clamp(point.x, bounds.minX, bounds.maxX), y:this.clamp(point.y, bounds.minY, bounds.maxY)};
  },
  restore(saved, size, view) {
    const b = this.bounds(size, view);
    return {x:b.minX + (saved?.x ?? 1) * Math.max(0, b.maxX - b.minX),
      y:b.minY + (saved?.y ?? 1) * Math.max(0, b.maxY - b.minY)};
  },
  normalize(point, size, view) {
    const b = this.bounds(size, view);
    return {x:this.clamp((point.x - b.minX) / Math.max(1, b.maxX - b.minX), 0, 1),
      y:this.clamp((point.y - b.minY) / Math.max(1, b.maxY - b.minY), 0, 1)};
  },
  arrange(point, size, panel, view) {
    let p = this.constrain(point, this.bounds(size, view)), x, y;
    const gap = 12;
    if (p.x - view.x >= panel.w + gap + 12) x = -panel.w - gap;
    else if (view.x + view.w - p.x - size.w >= panel.w + gap + 12) x = size.w + gap;
    if (x !== undefined) y = this.clamp(p.y + size.h - panel.h, view.y + 12, view.y + view.h - panel.h - 12) - p.y;
    else {
      x = this.clamp(p.x + size.w / 2 - panel.w / 2, view.x + 12, view.x + view.w - panel.w - 12) - p.x;
      if (p.y - view.y >= panel.h + gap + 12 || p.y - view.y > view.y + view.h - p.y - size.h) {
        p.y = Math.max(p.y, view.y + panel.h + gap + 12); y = -panel.h - gap;
      } else { p.y = Math.min(p.y, view.y + view.h - size.h - panel.h - gap - 12); y = size.h + gap; }
    }
    const offset = {...panel, x, y};
    return {point:this.constrain(p, this.bounds(size, view, offset)), panel:offset};
  },
  dragged(start, event) { return Math.hypot(event.clientX - start.x, event.clientY - start.y) >= 6; }
};
if (typeof module !== "undefined") module.exports = PetMotion;

function mountPetInteraction(root, {open, hide, onReaction = () => 1800, onDragging = () => {}}) {
  const launcher = root.querySelector(".pet-launcher"), dock = root.querySelector(".pet-dock");
  const panel = root.querySelector(".pet-panel"), header = root.querySelector(".pet-header");
  const menu = root.querySelector(".pet-menu"), more = root.querySelector(".pet-more"), bubble = root.querySelector(".pet-bubble");
  const key = "tracker.pet.position";
  let normalized = null, point, offset = null, drag = null, longPress, reaction, lastTap = null, suppressUntil = 0, heldMenu = false;
  try { normalized = PetMotion.saved(localStorage.getItem(key)); } catch (_) { /* Position persistence is optional. */ }
  const viewport = () => ({x:window.visualViewport?.offsetLeft || 0, y:window.visualViewport?.offsetTop || 0,
    w:window.visualViewport?.width || document.documentElement.clientWidth, h:window.visualViewport?.height || window.innerHeight});
  const size = () => ({w:dock.offsetWidth, h:dock.offsetHeight});
  function remember(persist = false) {
    normalized = PetMotion.normalize(point, size(), viewport());
    if (persist) try { localStorage.setItem(key, JSON.stringify(normalized)); } catch (_) { /* storage optional */ }
  }
  function draw() {
    root.style.left = `${point.x}px`; root.style.top = `${point.y}px`;
    root.style.right = "auto"; root.style.bottom = "auto";
    root.classList.toggle("pet-at-top", point.y < viewport().y + 64);
    root.classList.toggle("pet-at-left", point.x < viewport().x + 150);
  }
  function placeMenu() {
    if (menu.hidden) return;
    const v = viewport();
    menu.style.maxHeight = `${Math.max(0, v.h - 24)}px`;
    menu.style.left = `${PetMotion.clamp(point.x + size().w - menu.offsetWidth, v.x + 12, v.x + v.w - menu.offsetWidth - 12) - point.x}px`;
    menu.style.top = `${PetMotion.clamp(point.y - menu.offsetHeight - 10, v.y + 12, v.y + v.h - menu.offsetHeight - 12) - point.y}px`;
  }
  function layout(resized = false) {
    if (dock.hidden) return;
    const v = viewport(), s = size();
    if (!point || resized) point = PetMotion.restore(normalized, s, v);
    point = PetMotion.constrain(point, PetMotion.bounds(s, v));
    offset = null;
    if (!panel.hidden) {
      panel.style.width = `${Math.min(408, Math.max(0, v.w - 24))}px`;
      panel.style.height = `${Math.max(0, Math.min(650, v.h - s.h - 36))}px`;
      const arranged = PetMotion.arrange(point, s, {w:panel.offsetWidth, h:panel.offsetHeight}, v);
      point = arranged.point; offset = arranged.panel;
      panel.style.left = `${offset.x}px`; panel.style.top = `${offset.y}px`;
      panel.style.right = "auto"; panel.style.bottom = "auto";
    }
    draw(); placeMenu(); remember();
  }
  function closeMenu(focus = false) {
    const wasOpen = !menu.hidden;
    menu.hidden = true; more.setAttribute("aria-expanded", "false");
    if (focus && wasOpen) more.focus();
  }
  function showMenu() {
    lastTap = null;
    menu.hidden = false; more.setAttribute("aria-expanded", "true");
    placeMenu(); menu.querySelector("button").focus();
  }
  function clearReaction() {
    clearTimeout(reaction); delete root.dataset.gesture; bubble.hidden = true;
    onReaction(null);
  }
  function react(action, message) {
    lastTap = null;
    clearReaction();
    // Restart the CSS action without changing data-state (the live request owns that).
    void launcher.offsetWidth;
    root.dataset.gesture = action; bubble.textContent = message; bubble.hidden = false;
    reaction = setTimeout(clearReaction, onReaction(action));
  }
  function endDrag(cancelled = false) {
    clearTimeout(longPress);
    if (!drag) return;
    const previous = drag; drag = null;
    root.classList.remove("pet-dragging");
    if (previous.moved) onDragging(false);
    if (previous.target.hasPointerCapture(previous.id)) previous.target.releasePointerCapture(previous.id);
    if (cancelled || previous.moved) { lastTap = null; suppressUntil = performance.now() + 400; }
    if (previous.moved) {
      remember(true);
      if (!cancelled) react("land", "好，就在这里陪你。");
    }
  }
  function start(event) {
    if (event.button !== 0 || event.isPrimary === false || drag ||
        (event.currentTarget === header && event.target.closest("button"))) return;
    closeMenu();
    heldMenu = false;
    const target = event.currentTarget;
    if (target !== launcher) lastTap = null;
    drag = {id:event.pointerId, target, x:event.clientX, y:event.clientY, origin:{...point}, moved:false,
      bounds:PetMotion.bounds(size(), viewport(), offset)};
    target.setPointerCapture(event.pointerId);
    if (event.pointerType === "touch" && target === launcher) longPress = setTimeout(() => {
      if (!drag || drag.moved) return;
      heldMenu = true; endDrag(true); showMenu();
    }, 500);
  }
  function move(event) {
    if (!drag || event.pointerId !== drag.id) return;
    if (!drag.moved && !PetMotion.dragged(drag, event)) return;
    if (!drag.moved) { lastTap = null; onDragging(true); clearReaction(); }
    drag.moved = true; clearTimeout(longPress); event.preventDefault();
    root.classList.add("pet-dragging");
    point = PetMotion.constrain({x:drag.origin.x + event.clientX - drag.x, y:drag.origin.y + event.clientY - drag.y}, drag.bounds);
    draw();
  }
  for (const target of [launcher, header]) {
    target.addEventListener("pointerdown", start);
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", event => { if (drag?.id === event.pointerId) endDrag(); });
    target.addEventListener("pointercancel", event => { if (drag?.id === event.pointerId) endDrag(true); });
    target.addEventListener("lostpointercapture", event => { if (drag?.id === event.pointerId) endDrag(true); });
    target.addEventListener("dragstart", event => event.preventDefault());
  }
  launcher.addEventListener("click", event => {
    // Keyboard/assistive activation stays a single action; pointer taps form pairs.
    if (!event.detail) { react("pat", "在呢，资料夹也准备好了。"); return; }
    if (heldMenu || performance.now() < suppressUntil) { lastTap = null; return; }
    const tap = {at:performance.now(), x:event.clientX, y:event.clientY, type:event.pointerType || "mouse"};
    if (lastTap && tap.at - lastTap.at <= 400 && tap.type === lastTap.type &&
        Math.hypot(tap.x - lastTap.x, tap.y - lastTap.y) <= 24) {
      react("pat", "在呢，资料夹也准备好了。");
    } else lastTap = tap;
  });
  launcher.addEventListener("contextmenu", event => { event.preventDefault(); endDrag(true); showMenu(); });
  more.addEventListener("click", () => menu.hidden ? showMenu() : closeMenu(true));
  function reset() {
    endDrag(true); normalized = null; point = null; layout(); remember(true);
    react("wave", "回到熟悉的小角落啦。");
  }
  menu.addEventListener("click", event => {
    const action = event.target.closest("[data-pet-action]")?.dataset.petAction;
    if (!action) return;
    closeMenu(true);
    if (action === "chat") open();
    else if (action === "hide") hide();
    else if (action === "reset") reset();
    else react(action, {pat:"摸摸头，继续陪你翻资料。", wave:"你好呀，今天想了解什么？", hop:"活动一下，精神满满！"}[action]);
  });
  launcher.addEventListener("keydown", event => {
    const delta = {ArrowLeft:[-1,0], ArrowRight:[1,0], ArrowUp:[0,-1], ArrowDown:[0,1]}[event.key];
    if (delta) {
      event.preventDefault(); closeMenu();
      const step = event.shiftKey ? 40 : 12;
      point = PetMotion.constrain({x:point.x + delta[0] * step, y:point.y + delta[1] * step}, PetMotion.bounds(size(), viewport(), offset));
      draw(); remember(true);
    } else if (event.key === "Home") { event.preventDefault(); reset(); }
    else if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) { event.preventDefault(); showMenu(); }
  });
  root.addEventListener("keydown", event => {
    if (event.key === "Escape" && (drag || !menu.hidden)) {
      event.preventDefault(); event.stopImmediatePropagation(); endDrag(true); closeMenu(true);
    }
  }, true);
  document.addEventListener("pointerdown", event => {
    if (!launcher.contains(event.target)) lastTap = null;
    if (!menu.contains(event.target) && !more.contains(event.target)) closeMenu();
  });
  document.addEventListener("focusin", event => { if (!root.contains(event.target)) closeMenu(); });
  function suspend() { lastTap = null; endDrag(true); closeMenu(); clearReaction(); }
  window.addEventListener("blur", suspend);
  document.addEventListener("visibilitychange", () => { if (document.hidden) suspend(); });
  const resize = () => { endDrag(true); layout(true); };
  window.addEventListener("resize", resize);
  window.visualViewport?.addEventListener("resize", resize);
  window.visualViewport?.addEventListener("scroll", resize);
  layout();
  return {layout, suspend};
}
