const assert = require("node:assert/strict");
const test = require("node:test");
const pet = require("../pet.js");

test("evidence links reject script URLs and accept actual detail routes", () => {
  for (const value of ["javascript:alert(1)", "data:text/html,test", "#manage", "https://name:secret@example.com"]) assert.equal(pet.safeLink(value), null);
  assert.equal(pet.safeLink("#model/42"), "#model/42");
  assert.equal(pet.safeLink("https://openai.com/"), "https://openai.com/");
});
test("old request or cleared conversation cannot overwrite new state", () => {
  assert.equal(pet.accepts({version:1,request_id:"old"},2,"new"),false);
  assert.equal(pet.accepts({version:2,request_id:"old"},2,"new"),false);
  assert.equal(pet.accepts({version:2,request_id:"new"},2,"new"),true);
});
test("interruption is not displayed as success", () => {
  assert.match(pet.phase("interrupted"), /中断/);
  assert.match(pet.phase("cancelled"), /停止/);
});
