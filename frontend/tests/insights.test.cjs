const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const context = vm.createContext({document: {addEventListener() {}}});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../insights.js"), "utf8"), context);

function storyImage(item, cachedFailure = false) {
  const img = new EventTarget();
  Object.assign(img, {src: item.cover_image.url, complete: cachedFailure, naturalWidth: 0});
  const picture = {removed: false, remove() { this.removed = true; }};
  const classes = new Set();
  const story = {
    classList: classes,
    querySelector(selector) { return selector === ".story-image img" ? img : picture; },
  };
  context.bindStoryImage(story, item);
  return {img, picture, classes};
}

const covers = ["first", "second", "third"].map(name => ({url: `https://images.test/${name}.png`, alt: name}));
const item = {title: "News", cover_image: covers[0], cover_images: covers};

test("a failed cover tries the other event images before hiding the preview", () => {
  const {img, picture, classes} = storyImage(item);
  assert.equal(img.src, covers[0].url);
  for (const cover of covers.slice(1)) {
    img.dispatchEvent(new Event("error"));
    assert.equal(img.src, cover.url);
    assert.equal(img.alt, cover.alt);
    assert.equal(picture.removed, false);
    assert.equal(classes.has("no-image"), false);
  }
  img.dispatchEvent(new Event("error"));
  assert.equal(picture.removed, true);
  assert.equal(classes.has("no-image"), true);
});

test("a cached failed image also advances to the next cover", () => {
  const {img, picture} = storyImage(item, true);
  assert.equal(img.src, covers[1].url);
  assert.equal(picture.removed, false);
});

test("a successful cover stays visible", () => {
  const {img, picture, classes} = storyImage(item);
  img.dispatchEvent(new Event("load"));
  assert.equal(img.src, covers[0].url);
  assert.equal(picture.removed, false);
  assert.equal(classes.has("no-image"), false);
});

test("legacy single-cover responses still hide a broken image", () => {
  const {img, picture} = storyImage({cover_image: covers[0]});
  img.dispatchEvent(new Event("error"));
  assert.equal(picture.removed, true);
});

test("text-only stories need no image handler", () => {
  assert.doesNotThrow(() => context.bindStoryImage({querySelector() { return null; }}, {cover_image: null, cover_images: []}));
});

test("unsafe fallback URLs are skipped", () => {
  const {img} = storyImage({...item, cover_images: [covers[0], {url: "javascript:alert(1)"}, covers[2]]});
  img.dispatchEvent(new Event("error"));
  assert.equal(img.src, covers[2].url);
});
