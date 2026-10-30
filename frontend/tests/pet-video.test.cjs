const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function fixture({hidden=false, reduced=false, alpha=0}={}) {
  const videos = [], classes = new Set();
  const timers = new Map(); let now = 0, sequence = 0, observe;
  const preference = Object.assign(new EventTarget(), {matches:reduced});
  const document = Object.assign(new EventTarget(), {hidden:false});
  const art = {append(){}};
  const root = {dataset:{state:"idle"}, querySelector:s => s === ".pet-art" ? art : {hidden},
    classList:{add:c=>classes.add(c), remove:c=>classes.delete(c)}};
  document.createElement = tag => {
    if (tag === "canvas") return {getContext:()=>({drawImage(){},getImageData:x=>({data:[0,0,0,x ? 255:alpha]})})};
    const video = Object.assign(new EventTarget(), {paused:true, currentTime:0, setAttribute(){}, removeAttribute(){}, load(){},
      pause(){this.paused=true;}, remove(){this.removed=true;},
      play(){this.paused=false;this.dispatchEvent(new Event("playing"));return Promise.resolve();}});
    videos.push(video);
    return video;
  };
  const context = vm.createContext({document, window:{matchMedia:()=>preference},
    setTimeout:(fn,delay)=>{const id=++sequence;timers.set(id,{fn,at:now+delay});return id;},
    clearTimeout:id=>timers.delete(id),
    IntersectionObserver:class { constructor(fn){observe=fn;} observe(){} }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../pet-video.js"),"utf8"), context);
  const media = context.mountPetVideo(root);
  return {media, videos, preference, document, classes, root,
    tick(ms){now+=ms;for(const [id,t] of timers)if(t.at<=now){timers.delete(id);t.fn();}},
    inView(value){observe([{isIntersecting:value}]);},
    time(seconds){const v=videos.at(-1);v.currentTime=seconds;v.dispatchEvent(new Event("timeupdate"));},
    cycle(){this.time(3);},
    ready:()=>videos.at(-1).dispatchEvent(new Event("loadeddata"))};
}

test("only current clip loads; SVG remains until transparent frame is playing", () => {
  const f=fixture();
  assert.equal(f.videos.length,1); assert.match(f.videos[0].src,/idle-v2/);
  assert.equal(f.classes.has("pet-video-active"),false);
  f.ready(); assert.equal(f.classes.has("pet-video-active"),true);
  assert.equal(f.videos[0].muted,true); assert.equal(f.videos[0].loop,true);
});

test("pat outlives old 1.8 second gesture, then returns to current idle state", () => {
  const f=fixture(); f.ready();
  assert.ok(f.media.react("pat")>10000); f.ready();
  const pat=f.videos.at(-1); assert.equal(pat.loop,false);
  pat.dispatchEvent(new Event("ended"));
  assert.match(f.videos.at(-1).src,/idle-v2/);
});

test("search interrupts pat and stale ready, error, ended events cannot replace it", () => {
  const f=fixture(); f.media.react("pat"); const old=f.videos.at(-1);
  f.media.setState("searching"); f.ready(); const search=f.videos.at(-1);
  for (const event of ["loadeddata","error","ended","playing"]) old.dispatchEvent(new Event(event));
  f.media.react("pat");
  assert.equal(f.videos.at(-1),search); assert.match(search.src,/search-v2/);
  assert.equal(f.classes.has("pet-video-active"),true);
  f.media.setState("done"); assert.equal(search.removed,true);
  assert.equal(f.classes.has("pet-video-active"),false);
});

test("a question starts search immediately and keeps the same loop across all working phases", () => {
  const f=fixture(); f.ready(); f.media.react("pat");
  f.media.setState("thinking"); f.ready();
  const search=f.videos.at(-1), count=f.videos.length;
  assert.match(search.src,/search-v2/); assert.equal(search.loop,true);
  for(const phase of ["searching","composing"]) {
    f.media.setState(phase); f.media.react("pat");
    assert.equal(f.videos.at(-1),search); assert.equal(f.videos.length,count);
    assert.equal(search.paused,false);
  }
  f.media.setState("idle"); // Cancellation returns to idle, not a completed or pending pat.
  assert.equal(search.removed,true); assert.match(f.videos.at(-1).src,/idle-v2/);
});

test("hide and background pause playback; latest state resumes when visible", () => {
  const f=fixture(); f.ready(); const old=f.videos[0];
  f.media.setVisible(false); assert.equal(old.paused,true);
  f.media.setState("searching"); assert.equal(f.videos.length,1);
  f.media.setVisible(true); f.ready(); const search=f.videos.at(-1);
  assert.match(search.src,/search-v2/);
  f.document.hidden=true; f.document.dispatchEvent(new Event("visibilitychange")); assert.equal(search.paused,true);
  f.document.hidden=false; f.document.dispatchEvent(new Event("visibilitychange")); assert.equal(search.paused,false);
  const hidden=fixture({hidden:true}); assert.equal(hidden.videos.length,0);
});

test("reduced motion stops video and unsupported alpha keeps SVG", () => {
  const f=fixture({reduced:true}); assert.equal(f.videos.length,0);
  f.preference.matches=false; f.preference.dispatchEvent(new Event("change")); f.ready();
  f.preference.matches=true; f.preference.dispatchEvent(new Event("change"));
  assert.equal(f.videos[0].removed,true); assert.equal(f.classes.has("pet-video-active"),false);
  const opaque=fixture({alpha:255}); opaque.ready(); opaque.media.setState("searching");
  assert.equal(opaque.videos.length,1); assert.equal(opaque.classes.has("pet-video-active"),false);
});

test("media failure falls back without retry loop; another clip can still play", () => {
  const f=fixture(); f.videos[0].dispatchEvent(new Event("error"));
  f.media.react(null); assert.equal(f.videos.length,1);
  f.media.setState("searching"); f.ready();
  assert.equal(f.classes.has("pet-video-active"),true);
});

test("three seconds of idle playback precede each full performance; shuffled bags cover all six", () => {
  const f=fixture(), chosen=[]; f.ready();
  for(let i=0;i<18;i++) {
    const base=f.videos.at(-1); assert.match(base.src,/\/idle-v2/);
    f.time(2.99); f.tick(60000); // Wall time alone cannot queue animations.
    assert.equal(f.videos.at(-1),base);
    f.cycle(); f.ready();
    const variant=f.videos.at(-1); assert.equal(variant.loop,false);
    chosen.push(variant.src);
    f.time(3); f.tick(60000); assert.equal(f.videos.at(-1),variant);
    variant.dispatchEvent(new Event("ended")); f.ready();
  }
  for(let i=0;i<18;i+=6) assert.equal(new Set(chosen.slice(i,i+6)).size,6);
  for(let i=1;i<chosen.length;i++) assert.notEqual(chosen[i],chosen[i-1]);
});

test("question and pat interrupt idle performances; obsolete media events cannot resume them", () => {
  for(const interrupt of ["thinking","pat"]) {
    const f=fixture(); f.ready(); f.cycle(); const old=f.videos.at(-1);
    if(interrupt==="pat") f.media.react("pat"); else f.media.setState(interrupt);
    f.ready(); const active=f.videos.at(-1);
    assert.match(active.src,interrupt==="pat"?/pat-v2/:/search-v2/);
    for(const type of ["timeupdate","loadeddata","error","ended","playing"]) old.dispatchEvent(new Event(type));
    f.tick(60000); assert.equal(f.videos.at(-1),active);
    if(interrupt==="pat") active.dispatchEvent(new Event("ended")); else f.media.setState("idle");
    f.ready(); assert.match(f.videos.at(-1).src,/\/idle-v2/);
    f.time(2.99); assert.match(f.videos.at(-1).src,/\/idle-v2/);
  }
});

test("hidden, offscreen and reduced-motion pets discard performances and resume with a fresh base", () => {
  for(const mode of ["hide","background","offscreen","reduced"]) {
    const f=fixture(); f.ready(); f.cycle(); f.ready(); const old=f.videos.at(-1);
    const toggle=value=>{
      if(mode==="hide") f.media.setVisible(!value);
      else if(mode==="offscreen") f.inView(!value);
      else if(mode==="reduced") {f.preference.matches=value;f.preference.dispatchEvent(new Event("change"));}
      else {f.document.hidden=value;f.document.dispatchEvent(new Event("visibilitychange"));}
    };
    toggle(true); const count=f.videos.length;
    f.tick(300000); old.dispatchEvent(new Event("ended"));
    assert.equal(f.videos.length,count); assert.equal(old.removed,true);
    toggle(false); f.ready(); assert.match(f.videos.at(-1).src,/\/idle-v2/);
    assert.equal(f.videos.at(-1).currentTime,0);
  }
});

test("drag blocks every video until release and cannot accumulate idle playback", () => {
  const f=fixture(); f.ready(); f.cycle(); f.ready(); const old=f.videos.at(-1);
  f.media.setDragging(true); assert.equal(old.removed,true);
  const count=f.videos.length; f.media.react(null); f.tick(60000);
  old.dispatchEvent(new Event("ended")); assert.equal(f.videos.length,count);
  f.media.setState("thinking"); assert.equal(f.videos.length,count);
  f.media.setDragging(false); f.ready(); assert.match(f.videos.at(-1).src,/search-v2/);
});

test("done returns to visual idle after feedback without changing request state; error/waiting never do", () => {
  const f=fixture(); f.ready(); f.root.dataset.state="done"; f.media.setState("done");
  assert.equal(f.classes.has("pet-video-active"),false);
  f.tick(1199); assert.equal(f.videos.length,1);
  f.tick(1); f.ready(); assert.match(f.videos.at(-1).src,/\/idle-v2/);
  f.cycle(); assert.equal(f.root.dataset.state,"done");
  for(const state of ["error","waiting"]) {
    f.media.setState("done"); f.media.setState(state); const count=f.videos.length;
    f.tick(60000); assert.equal(f.videos.length,count);
    assert.equal(f.classes.has("pet-video-active"),false);
  }
  f.media.setState("done"); f.media.setState("thinking"); f.ready(); const search=f.videos.at(-1);
  f.tick(60000); assert.equal(f.videos.at(-1),search);
});

test("broken variants are skipped for this page; all failures leave the base loop usable", () => {
  const f=fixture(), broken=new Set(); f.ready();
  for(let i=0;i<6;i++) {
    f.cycle(); const variant=f.videos.at(-1);
    assert.equal(broken.has(variant.src),false); broken.add(variant.src);
    variant.dispatchEvent(new Event("error")); f.ready();
    assert.match(f.videos.at(-1).src,/\/idle-v2/);
  }
  const base=f.videos.at(-1); f.cycle(); f.cycle();
  assert.equal(f.videos.at(-1),base); assert.equal(base.paused,false);
});
