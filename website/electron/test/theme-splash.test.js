"use strict";

// The theme pack's boot-splash art: what the dashboard hands over is what the
// next loading.html paint is given, and anything else leaves the stock splash.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { fileURLToPath } = require("node:url");

const { SPLASH_MAX_BYTES, saveThemeSplash, themeSplashUrl } = require("../theme-splash");

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const SVG = new TextEncoder().encode("<svg xmlns='http://www.w3.org/2000/svg'/>");

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-theme-splash-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("a pack's art is kept and named by its type, replacing the previous one", (t) => {
  const dir = tempDir(t);
  saveThemeSplash(dir, { type: "image/png", bytes: PNG });
  const pngUrl = themeSplashUrl(dir);
  assert.equal(path.basename(fileURLToPath(pngUrl)), "theme-splash.png");
  assert.deepEqual(new Uint8Array(fs.readFileSync(fileURLToPath(pngUrl))), PNG);

  saveThemeSplash(dir, { type: "image/svg+xml", bytes: SVG });
  assert.equal(path.basename(fileURLToPath(themeSplashUrl(dir))), "theme-splash.svg");
  assert.deepEqual(fs.readdirSync(dir), ["theme-splash.svg"]);
});

test("art whose pack asks for a cover fit is kept under the cover name, and only then", (t) => {
  const dir = tempDir(t);
  for (const [image, name] of [
    [{ type: "image/png", bytes: PNG, fit: "cover" }, "theme-splash-cover.png"],
    [{ type: "image/svg+xml", bytes: SVG, fit: "cover" }, "theme-splash-cover.svg"],
    [{ type: "image/png", bytes: PNG }, "theme-splash.png"],
    [{ type: "image/png", bytes: PNG, fit: "contain" }, "theme-splash.png"],
    [{ type: "image/png", bytes: PNG, fit: "stretch" }, "theme-splash.png"],
    [{ type: "image/svg+xml", bytes: SVG, fit: "cover" }, "theme-splash-cover.svg"],
  ]) {
    saveThemeSplash(dir, image);
    assert.equal(path.basename(fileURLToPath(themeSplashUrl(dir))), name, JSON.stringify(image.fit));
    assert.deepEqual(fs.readdirSync(dir), [name]);
  }
  saveThemeSplash(dir, null);
  assert.deepEqual(fs.readdirSync(dir), []);
});

test("a theme without art, or art the pack validator would refuse, restores the stock splash", (t) => {
  const dir = tempDir(t);
  for (const image of [
    null,
    { type: "image/gif", bytes: PNG },
    { type: "image/png", bytes: new Uint8Array(0) },
    { type: "image/png", bytes: new Uint8Array(SPLASH_MAX_BYTES + 1) },
    { type: "image/png", bytes: "not bytes" },
  ]) {
    saveThemeSplash(dir, { type: "image/png", bytes: PNG });
    saveThemeSplash(dir, image);
    assert.equal(themeSplashUrl(dir), "", JSON.stringify(image && image.type));
    assert.deepEqual(fs.readdirSync(dir), []);
  }
});

test("loading.html shows only the stored theme-splash file, and keeps the stock art for anything else", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "loading.html"), "utf8");
  const fn = html.match(/function themeSplashSrc\(raw\) \{[\s\S]*?\n    \}/);
  assert.ok(fn, "loading.html must define themeSplashSrc");
  assert.match(html, /artSrc = themeSplashSrc\(new URLSearchParams\(location\.search\)\.get\("splash"\)/);
  const context = { URL };
  require("node:vm").runInNewContext(`${fn[0]}; this.themeSplashSrc = themeSplashSrc;`, context);
  const { themeSplashSrc } = context;

  for (const ok of [
    "file:///home/u/.config/KiroCrew/theme-splash.png",
    "file:///C:/Users/u/AppData/Roaming/KiroCrew/theme-splash.svg",
    "file:///home/u/.config/KiroCrew/theme-splash-cover.png",
    "file:///C:/Users/u/AppData/Roaming/KiroCrew/theme-splash-cover.svg",
  ]) assert.equal(themeSplashSrc(ok), ok);
  for (const bad of [
    "",
    "not a url",
    "javascript:alert(1)//theme-splash.png",
    "data:image/svg+xml,<svg/>#/theme-splash.svg",
    "https://example.com/theme-splash.png",
    "file:///home/u/other.png",
    "file://server/share/theme-splash.svg",
    "file:////server/share/theme-splash.svg",
    "file:///home/u/theme-splash.png.html",
    "file:///home/u/theme-splash.png?x=1",
    "file:///home/u/theme-splash.svg#x",
    "file:///home/u/theme-splash-fill.png",
    "file:///home/u/theme-splash-cover.gif",
    "https://example.com/theme-splash-cover.png",
  ]) assert.equal(themeSplashSrc(bad), "", bad);
});

test("loading.html fills the window only for the stored cover-fit art", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "loading.html"), "utf8");
  const fn = html.match(/function isCoverSplash\(src\) \{[\s\S]*?\n    \}/);
  assert.ok(fn, "loading.html must define isCoverSplash");
  assert.match(html, /showThemeArt\(art, artStill, artSrc, reduce, isCoverSplash\(artSrc\),/);
  assert.match(html, /#art\.cover, #artStill\.cover \{[^}]*width: 100vw; height: 100vh; object-fit: cover;/);
  const context = {};
  require("node:vm").runInNewContext(`${fn[0]}; this.isCoverSplash = isCoverSplash;`, context);
  const { isCoverSplash } = context;
  assert.equal(isCoverSplash("file:///home/u/.config/KiroCrew/theme-splash-cover.png"), true);
  assert.equal(isCoverSplash("file:///C:/Users/u/AppData/Roaming/KiroCrew/theme-splash-cover.svg"), true);
  assert.equal(isCoverSplash("file:///home/u/.config/KiroCrew/theme-splash.png"), false);
  assert.equal(isCoverSplash("file:///home/u/.config/KiroCrew/theme-splash.svg"), false);
});

test("loading.html holds a pack's splash art on one still frame under reduced motion", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "loading.html"), "utf8");
  const show = html.match(/function showThemeArt\([^)]*\) \{[\s\S]*?\n    \}/);
  const paint = html.match(/function paintStill\([^)]*\) \{[\s\S]*?\n    \}/);
  assert.ok(show && paint, "loading.html must define showThemeArt and paintStill");
  const context = { window: { devicePixelRatio: 1 } };
  require("node:vm").runInNewContext(`${show[0]}; ${paint[0]}; this.showThemeArt = showThemeArt;`, context);
  const { showThemeArt } = context;
  const classes = () => {
    const set = new Set();
    return { set, toggle: (name, on) => (on ? set.add(name) : set.delete(name)) };
  };
  const make = () => {
    const draws = [];
    const still = {
      hidden: true, clientWidth: 720, clientHeight: 400, classList: classes(),
      getContext: () => ({ drawImage: (...a) => draws.push(a) }),
    };
    const art = { hidden: true, naturalWidth: 100, naturalHeight: 50, classList: classes() };
    return { art, still, draws };
  };

  // Reduced motion: the animating <img> is never displayed; one frame is painted.
  let s = make();
  showThemeArt(s.art, s.still, "file:///x/theme-splash.svg", true, false, () => assert.fail("no failure"));
  assert.equal(s.art.src, "file:///x/theme-splash.svg");
  assert.equal(s.art.hidden, true);
  assert.equal(s.still.hidden, true);
  s.art.onload();
  assert.equal(s.art.hidden, true);
  assert.equal(s.still.hidden, false);
  assert.equal(s.draws.length, 1);
  // Contained: the 100x50 art scales to 720x360, centred in the 720x400 box.
  assert.deepEqual(s.draws[0].slice(1), [0, 20, 720, 360]);
  assert.equal(s.still.classList.set.has("cover"), false);

  // Cover fit, reduced motion: the still fills the canvas and crops the sides.
  s = make();
  showThemeArt(s.art, s.still, "file:///x/theme-splash-cover.svg", true, true, () => assert.fail("no failure"));
  assert.equal(s.art.classList.set.has("cover"), true);
  assert.equal(s.still.classList.set.has("cover"), true);
  s.art.onload();
  assert.equal(s.art.hidden, true);
  assert.equal(s.still.hidden, false);
  // 400 / 50 = 8x: 800x400, 40 px cropped off each side of the 720 px canvas.
  assert.deepEqual(s.draws[0].slice(1), [-40, 0, 800, 400]);

  // Motion allowed: the art itself is shown and nothing is painted.
  s = make();
  showThemeArt(s.art, s.still, "file:///x/theme-splash.png", false, false, () => assert.fail("no failure"));
  assert.equal(s.art.hidden, false);
  assert.equal(s.still.hidden, true);
  assert.equal(s.art.onload, undefined);

  // Art that fails to load hides both and hands back to the stock ghosts.
  for (const reduce of [true, false]) {
    s = make();
    let failed = 0;
    showThemeArt(s.art, s.still, "file:///x/theme-splash.png", reduce, false, () => { failed += 1; });
    s.art.onerror();
    assert.equal(s.art.hidden, true);
    assert.equal(s.still.hidden, true);
    assert.equal(failed, 1);
  }
});
