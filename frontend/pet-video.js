/* Only received clips are published here. Other states keep the existing SVG. */
const PetClips = {
  idle: {src:"/static/assets/pet/animations/idle-v2.webm", loop:true},
  searching: {src:"/static/assets/pet/animations/search-v2.webm", loop:true},
  pat: {src:"/static/assets/pet/animations/pat-v2.webm", loop:false},
  idle_guard: {src:"/static/assets/pet/animations/idle_guard-v2.webm", loop:false},
  idle_drum: {src:"/static/assets/pet/animations/idle_drum-v2.webm", loop:false},
  idle_listen: {src:"/static/assets/pet/animations/idle_listen-v2.webm", loop:false},
  idle_sneeze: {src:"/static/assets/pet/animations/idle_sneeze-v2.webm", loop:false},
  peek: {src:"/static/assets/pet/animations/peek-v2.webm", loop:false},
  nap: {src:"/static/assets/pet/animations/nap-v2.webm", loop:false}
};
const PetIdleVariants = ["idle_guard", "idle_drum", "idle_listen", "idle_sneeze", "peek", "nap"];

function mountPetVideo(root) {
  const art = root.querySelector(".pet-art");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const failed = new Set();
  let state = root.dataset.state, action = null, current = null;
  let variant = null, bag = [], lastVariant = null, dragging = false, settled = false, doneTimer;
  let visible = !root.querySelector(".pet-dock").hidden, inView = true, alphaSupported = true;
  const working = () => ["thinking", "searching", "composing"].includes(state);
  const idle = () => state === "idle" || (state === "done" && settled);
  const available = () => visible && inView && !document.hidden && !reduced.matches && alphaSupported;
  function nextIdle() {
    bag = bag.filter(id => !failed.has(id));
    if (!bag.length) {
      bag = PetIdleVariants.filter(id => !failed.has(id));
      for (let i = bag.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [bag[i], bag[j]] = [bag[j], bag[i]];
      }
      // Each bag contains every usable clip once; avoid repeats across bags too.
      if (bag.length > 1 && bag[0] === lastVariant) [bag[0], bag[1]] = [bag[1], bag[0]];
    }
    const id = bag.shift() || null;
    if (id) lastVariant = id;
    return id;
  }
  function resetIdle() {
    variant = null;
    if (current?.id === "idle") current.video.currentTime = 0;
  }
  function fail(entry) {
    if (current !== entry) return;
    failed.add(entry.id); stop();
    if (variant === entry.id) { variant = null; sync(); }
  }
  function stop() {
    if (current) {
      const video = current.video;
      current = null;
      video.pause(); video.removeAttribute("src"); video.load(); video.remove();
    }
    root.classList.remove("pet-video-active");
  }
  function sync() {
    if (!available()) {
      const wasVariant = variant !== null;
      resetIdle();
      if (wasVariant || reduced.matches || !alphaSupported) stop();
      else current?.video.pause();
      return;
    }
    if (dragging) { stop(); return; }
    const id = working() ? "searching" : action || variant || (idle() ? "idle" : state);
    const clip = PetClips[id];
    if (!clip || failed.has(id) || !alphaSupported || reduced.matches) { stop(); return; }
    if (current?.id === id) {
      if (current.ready && current.video.paused) play(current);
      return;
    }
    stop();
    const video = document.createElement("video");
    const entry = {id, video, ready:false};
    current = entry;
    video.className = "pet-video";
    video.muted = true; video.playsInline = true; video.loop = clip.loop;
    video.preload = "auto"; video.setAttribute("aria-hidden", "true");
    video.addEventListener("loadeddata", () => {
      if (current !== entry) return;
      // Test decoded pixels: codec support alone does not guarantee transparency.
      try {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 32;
        const context = canvas.getContext("2d", {willReadFrequently:true});
        context.drawImage(video, 0, 0, 32, 32);
        if (context.getImageData(0, 0, 1, 1).data[3] !== 0 ||
            context.getImageData(16, 16, 1, 1).data[3] < 200) {
          alphaSupported = false; stop(); return;
        }
      } catch (_) { fail(entry); return; }
      entry.ready = true;
      sync();
    });
    video.addEventListener("playing", () => {
      const reveal = () => {
        if (current === entry && entry.ready) root.classList.add("pet-video-active");
      };
      if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(reveal);
      else reveal();
    });
    video.addEventListener("error", () => fail(entry));
    video.addEventListener("timeupdate", () => {
      if (current !== entry || id !== "idle" || !entry.ready || video.paused ||
          !available() || !idle() || action || dragging) return;
      // Three seconds of visible base playback, then one full performance.
      if (video.currentTime >= 3) { variant = nextIdle(); sync(); }
    });
    video.addEventListener("ended", () => {
      if (current !== entry || clip.loop) return;
      action = null; variant = null; sync();
    });
    art.append(video);
    video.src = clip.src;
    video.load();
  }
  function play(entry) {
    entry.video.play().catch(error => {
      if (error.name !== "AbortError") fail(entry);
    });
  }
  document.addEventListener("visibilitychange", sync);
  reduced.addEventListener("change", sync);
  if (typeof IntersectionObserver !== "undefined") {
    new IntersectionObserver(entries => { inView = entries[0].isIntersecting; sync(); }).observe(art);
  }
  sync();
  return {
    setState(value) {
      if (state !== value) {
        clearTimeout(doneTimer); settled = false;
        state = value; action = null; resetIdle(); sync();
        if (state === "done") doneTimer = setTimeout(() => {
          settled = true; sync(); // Visual idle only; do not alter the answer/request state.
        }, 1200);
      }
    },
    react(value) {
      resetIdle();
      action = working() ? null : value;
      // A repeated pat is a fresh interaction, even before the last one ends.
      if (action === "pat" && current?.id === "pat") stop();
      sync();
      return action === "pat" && !reduced.matches && alphaSupported && !failed.has("pat") ? 10300 : 1800;
    },
    setVisible(value) { visible = value; sync(); },
    setDragging(value) { dragging = value; resetIdle(); sync(); }
  };
}
if (typeof module !== "undefined") module.exports = {mountPetVideo};
