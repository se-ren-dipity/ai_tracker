const assert = require("node:assert/strict");
const test = require("node:test");
const motion = require("../pet-interaction.js");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

test("restored positions reject malformed storage and adapt to smaller viewports", () => {
  for (const value of [null, "bad", "null", '{}', '{"x":-1,"y":0}', '{"x":0,"y":2}', '{"x":"1","y":0}']) assert.equal(motion.saved(value), null);
  const saved = motion.saved('{"x":1,"y":0.5}');
  for (const view of [{x:0,y:0,w:1200,h:800}, {x:0,y:0,w:390,h:667}, {x:20,y:90,w:320,h:300}]) {
    const size = {w:78,h:103}, point = motion.restore(saved,size,view);
    assert.ok(point.x + size.w <= view.x + view.w - 12);
    assert.deepEqual(motion.normalize(point,size,view), saved);
  }
});

test("pet and panel stay visible without overlap at corners and after group dragging", () => {
  for (const view of [{x:0,y:0,w:1440,h:900},{x:0,y:0,w:1024,h:600},{x:0,y:0,w:390,h:667},{x:0,y:70,w:320,h:320}]) {
    const size = {w:view.w < 600 ? 78:120,h:view.w < 600 ? 103:145};
    const panel = {w:Math.min(408,view.w-24),h:Math.min(650,view.h-size.h-36)};
    for (const rx of [0,.25,.5,.75,1]) for (const ry of [0,.25,.5,.75,1]) {
      const result = motion.arrange(motion.restore({x:rx,y:ry},size,view),size,panel,view);
      const b = motion.bounds(size,view,result.panel);
      for (const point of [result.point, motion.constrain({x:-999,y:-999},b), motion.constrain({x:9999,y:9999},b)]) {
        for (const box of [{x:point.x,y:point.y,...size},{x:point.x+result.panel.x,y:point.y+result.panel.y,...panel}]) {
          assert.ok(box.x >= view.x+12 && box.y >= view.y+12, JSON.stringify({view,box}));
          assert.ok(box.x+box.w <= view.x+view.w-12 && box.y+box.h <= view.y+view.h-12, JSON.stringify({view,box}));
        }
      }
      const q = result.panel;
      assert.ok(q.x+q.w <= 0 || q.x >= size.w || q.y+q.h <= 0 || q.y >= size.h);
    }
  }
});

test("small pointer jitter stays a click; movement in any direction starts a drag", () => {
  const start = {x:100,y:100};
  assert.equal(motion.dragged(start,{clientX:103,clientY:104}),false);
  for (const [x,y] of [[106,100],[94,100],[100,106],[104,105]]) assert.equal(motion.dragged(start,{clientX:x,clientY:y}),true);
});

function gestures() {
  class Element extends EventTarget {
    constructor(w=120,h=145) { super(); Object.assign(this,{style:{},dataset:{},hidden:false,offsetWidth:w,offsetHeight:h,capture:null,attributes:{}}); }
    classList = {add(){},remove(){},toggle(){}};
    setPointerCapture(id) { this.capture=id; }
    hasPointerCapture(id) { return this.capture===id; }
    releasePointerCapture() { this.capture=null; }
    setAttribute(key,value) { this.attributes[key]=value; }
    contains(target) { return target===this; }
    closest() { return null; }
    focus() {}
    querySelector() { return new Element(); }
  }
  const root=new Element(), elements=new Map();
  for (const name of ["launcher","dock","panel","header","menu","more","bubble"]) elements.set(name,new Element());
  const get = name => elements.get(name);
  get("panel").hidden=true; get("menu").hidden=true;
  root.querySelector=selector=>get(selector.slice(5));
  root.dataset.state="searching";
  const window=new EventTarget(), document=new EventTarget(), storage=new Map(), timers=new Map();
  Object.assign(window,{innerHeight:800}); document.documentElement={clientWidth:1200};
  let now=1000, sequence=0, opens=0;
  const context=vm.createContext({window,document,performance:{now:()=>now},
    localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    setTimeout:(fn,delay)=>{const id=++sequence;timers.set(id,{fn,at:now+delay});return id;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../pet-interaction.js"),"utf8"),context);
  const reactions=[], dragging=[];
  context.mountPetInteraction(root,{open:()=>opens++,hide(){},onReaction:action=>{reactions.push(action);return 1800;},onDragging:value=>dragging.push(value)});
  const emit=(target,type,props={})=>{
    const event=new Event(type,{cancelable:true});
    Object.assign(event,{button:0,pointerId:1,isPrimary:true,pointerType:"mouse",clientX:100,clientY:100,detail:1,...props});
    target.dispatchEvent(event);
  };
  const tap=(props={})=>{emit(get("launcher"),"pointerdown",props);emit(get("launcher"),"pointerup",props);emit(get("launcher"),"click",props);};
  return {root,get,emit,tap,reactions,dragging,storage,window,opens:()=>opens,tick(ms){now+=ms;for(const [id,t] of timers)if(t.at<=now){timers.delete(id);t.fn();}}};
}

test("drag suppresses release-click, persists once, and leaves live request state intact", () => {
  const g=gestures(), pet=g.get("launcher");
  g.emit(pet,"pointerdown"); g.emit(pet,"pointermove",{clientX:40,clientY:40}); g.emit(pet,"pointerup");
  const position=g.storage.get("tracker.pet.position");
  assert.ok(position); assert.equal(pet.capture,null);
  assert.equal(g.root.dataset.gesture,"land");
  g.emit(pet,"click"); assert.equal(g.root.dataset.gesture,"land");
  g.tick(500); g.tap(); assert.equal(g.root.dataset.gesture,"land");
  g.tick(100); g.tap(); assert.equal(g.root.dataset.gesture,"pat");
  assert.equal(g.root.dataset.state,"searching"); assert.equal(g.opens(),0);
  assert.equal(g.storage.get("tracker.pet.position"),position);
  assert.deepEqual(g.dragging,[true,false]);
});

test("mouse double-click and touch double-tap trigger one pat; a single tap does not interrupt it", () => {
  for (const pointerType of ["mouse","touch"]) {
    const g=gestures();
    g.tap({pointerType}); assert.equal(g.root.dataset.gesture,undefined);
    assert.deepEqual(g.dragging,[]);
    g.tick(150); g.tap({pointerType});
    assert.equal(g.root.dataset.gesture,"pat");
    assert.equal(g.reactions.filter(a=>a==="pat").length,1);
    const count=g.reactions.length;
    g.tick(100); g.tap({pointerType});
    assert.equal(g.reactions.length,count); // Third click neither restarts nor clears the video.
    g.emit(g.get("launcher"),"dblclick",{detail:2,pointerType});
    assert.equal(g.reactions.length,count); // Browser's extra event must not double-fire.
  }
});

test("slow or far-apart taps and mixed pointer types do not form a pair", () => {
  for (const [delay,second] of [[401,{}],[100,{clientX:130}],[100,{pointerType:"touch"}]]) {
    const g=gestures(); g.tap(); g.tick(delay); g.tap(second);
    assert.equal(g.root.dataset.gesture,undefined);
  }
});

test("a drag, long press or lost focus clears the previous tap", () => {
  for (const interruption of ["drag","longpress","blur"]) {
    const g=gestures(), pet=g.get("launcher"); g.tap();
    if(interruption==="blur") g.window.dispatchEvent(new Event("blur"));
    else {
      g.emit(pet,"pointerdown",{pointerType:interruption==="longpress"?"touch":"mouse"});
      if(interruption==="drag") g.emit(pet,"pointermove",{clientX:70});
      else g.tick(500);
      g.emit(pet,"pointerup");
      g.tick(401);
    }
    g.tap(); assert.notEqual(g.root.dataset.gesture,"pat");
    g.tick(100); g.tap(); assert.equal(g.root.dataset.gesture,"pat");
  }
});

test("keyboard activation remains accessible without two presses", () => {
  const g=gestures(); g.emit(g.get("launcher"),"click",{detail:0});
  assert.equal(g.root.dataset.gesture,"pat"); assert.equal(g.opens(),0);
});

test("touch long press opens actions; moving first cancels it", () => {
  const g=gestures(), pet=g.get("launcher");
  g.emit(pet,"pointerdown",{pointerType:"touch"}); g.tick(500);
  assert.equal(g.get("menu").hidden,false); assert.equal(pet.capture,null);
  g.tick(1500);
  g.emit(pet,"pointerup"); g.emit(pet,"click"); assert.equal(g.root.dataset.gesture,undefined);
  g.emit(pet,"pointerdown",{pointerType:"touch"}); g.emit(pet,"pointermove",{clientX:80}); g.tick(500);
  assert.equal(g.get("menu").hidden,true); g.emit(pet,"pointerup");
});

test("cancel and lost capture release dragging; a second pointer cannot hijack it", () => {
  const g=gestures(), pet=g.get("launcher");
  for (const terminal of ["pointercancel","lostpointercapture"]) {
    g.emit(pet,"pointerdown"); g.emit(pet,"pointermove",{clientX:50});
    const before=g.root.style.left;
    g.emit(pet,"pointermove",{pointerId:2,clientX:0}); assert.equal(g.root.style.left,before);
    g.emit(pet,terminal); g.emit(pet,"pointermove",{clientX:0});
    assert.equal(g.root.style.left,before); assert.equal(pet.capture,null);
    assert.equal(g.dragging.at(-1),false);
    g.tick(500);
  }
  g.emit(pet,"pointerdown"); g.window.dispatchEvent(new Event("blur")); assert.equal(pet.capture,null);
});
