const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const item = {id: 1, name: "Model", type: "LLM", notes: "完整说明 <原文>"};
const response = (total, items = [item]) => ({ok: true, json: async () => ({total, items})});
const tick = () => new Promise(setImmediate);

function setup(fetch = async () => response(61)) {
  const elements = new Map(), calls = [], timers = new Map();
  let ready, timerId = 0;
  function el(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", innerHTML: "", textContent: "", handlers: {},
      tagName: ["m-type", "m-org", "m-lic", "m-lang"].includes(id) ? "SELECT" : "INPUT",
      dataset: {sort: "name"},
      addEventListener(event, callback) { this.handlers[event] = callback; },
    });
    return elements.get(id);
  }
  const context = vm.createContext({URLSearchParams,
    window: {addEventListener() {}, scrollTo() {}},
    setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
    clearTimeout(id) { timers.delete(id); },
    document: {getElementById: el, querySelectorAll: () => [el("sort")],
      addEventListener(event, callback) { ready = callback; }},
    apiFetch(url) { calls.push(new URL(url, "http://test")); return fetch(url); },
  });
  vm.runInContext(script, context);
  vm.runInContext('activeView = "models"', context);
  context.route = () => {};
  ready();
  return {context, calls, el, timers};
}

test("requests 20 models per page, retains page on return, and disables boundary controls", async () => {
  const {context, calls, el} = setup(async url => response(61, Array(url.includes("page=4&") ? 1 : 20).fill(item)));
  await context.loadModels();
  assert.equal(calls[0].searchParams.get("page_size"), "20");
  assert.equal(calls[0].searchParams.get("sort"), "updated_at");
  assert.match(el("m-count").textContent, /第 1–20 个/);
  assert.match(el("m-pages-top").innerHTML, /disabled>上一页/);
  await context.loadModels(4);
  await context.loadModels();
  assert.equal(calls.at(-1).searchParams.get("page"), "4");
  assert.match(el("m-pages-bottom").innerHTML, /disabled>下一页/);
  assert.match(el("mt-body").innerHTML, /完整说明 &lt;原文&gt;/);
});

test("filters and sorting reset to page one and keep current query parameters", async () => {
  const {context, calls, el, timers} = setup();
  await context.loadModels(3);
  el("m-type").value = "LLM";
  el("m-type").handlers.change();
  await tick();
  assert.equal(calls.at(-1).searchParams.get("page"), "1");
  assert.equal(calls.at(-1).searchParams.get("type"), "LLM");
  await context.loadModels(3);
  el("m-q").value = "长文本";
  el("m-q").handlers.input();
  for (const callback of [...timers.values()]) callback();
  await tick();
  assert.equal(calls.at(-1).searchParams.get("page"), "1");
  assert.equal(calls.at(-1).searchParams.get("q"), "长文本");
  await context.loadModels(3);
  el("sort").handlers.click();
  await tick();
  assert.equal(calls.at(-1).searchParams.get("page"), "1");
  assert.equal(calls.at(-1).searchParams.get("sort"), "name");
  assert.equal(el("m-sort").value, "name");
  el("m-sort").handlers.change({target: {value: "release_date"}});
  await tick();
  assert.equal(calls.at(-1).searchParams.get("sort"), "release_date");
  assert.equal(calls.at(-1).searchParams.get("page"), "1");
});

test("a later result or error from an old request cannot replace the current page", async () => {
  for (const fail of [false, true]) {
    const pending = [];
    const {context, el} = setup(() => new Promise((resolve, reject) => pending.push({resolve, reject})));
    const old = context.loadModels(1), latest = context.loadModels(2);
    pending[1].resolve(response(61, [{...item, name: "New page"}]));
    await latest;
    if (fail) pending[0].reject(new Error("Old failure"));
    else pending[0].resolve(response(61));
    await old;
    assert.match(el("mt-body").innerHTML, /New page/);
    assert.match(el("m-pages-top").innerHTML, /第 2 \/ 4 页/);
  }
});

test("an out-of-range page recovers to the last valid page", async () => {
  const {context, calls, el} = setup(url => Promise.resolve(response(21, url.includes("page=4&") ? [] : [item])));
  await context.loadModels(4);
  assert.equal(calls.at(-1).searchParams.get("page"), "2");
  assert.match(el("m-count").textContent, /第 21–21 个/);
});

test("empty results and failed requests clear pagination and give useful feedback", async () => {
  const {context, el} = setup(async () => response(0, []));
  await context.loadModels();
  assert.match(el("mt-body").innerHTML, /没有匹配的模型/);
  assert.equal(el("m-pages-top").innerHTML, "");
  context.apiFetch = async () => ({ok: false});
  await context.loadModels();
  assert.match(el("mt-body").innerHTML, /加载失败/);
  assert.match(el("mt-body").innerHTML, /重试/);
  assert.equal(el("m-pages-bottom").innerHTML, "");
});
