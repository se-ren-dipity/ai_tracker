const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function setup() {
  const calls = [], elements = new Map();
  const context = vm.createContext({Headers, activeView:"manage", fetch:async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200, json:async () => ({configured:true, authenticated:true, csrf:"session-csrf"})};
  }, document:{getElementById(id) {
    if (!elements.has(id)) elements.set(id, {addEventListener() {}});
    return elements.get(id);
  }}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../admin.js"), "utf8"), context);
  return {context, calls};
}

test("writes use the authenticated CSRF token and keep JSON content type", async () => {
  const {context, calls} = setup();
  assert.equal(await context.ensureAdmin("manage"), true);
  await context.apiFetch("/api/admin/sources/id", {method:"PATCH", headers:{"Content-Type":"application/json"}});
  assert.equal(calls[1].options.headers.get("X-CSRF-Token"), "session-csrf");
  assert.equal(calls[1].options.headers.get("Content-Type"), "application/json");
  await context.apiFetch("/api/models");
  assert.equal(calls[2].options.headers.has("X-CSRF-Token"), false);
});

test("an unavailable session fails closed and explains the login failure", async () => {
  const {context} = setup();
  context.fetch = async () => { throw new Error("connection failed"); };
  assert.equal(await context.ensureAdmin("pending"), false);
  assert.equal(context.document.getElementById("admin-login-message").textContent, "connection failed");
});

test("all served JavaScript parses, including inline navigation", () => {
  const html = fs.readFileSync(path.join(__dirname, "../index.html"), "utf8");
  for (const [, script] of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(script);
  for (const name of ["admin", "insights", "models"]) new vm.Script(fs.readFileSync(path.join(__dirname, `../${name}.js`), "utf8"));
});
