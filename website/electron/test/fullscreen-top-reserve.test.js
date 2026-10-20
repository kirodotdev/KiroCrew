"use strict";

// The menu-bar verdict the shell sends with every fullscreen change. A notched
// built-in display gets a fullscreen window that starts below the menu bar
// strip, so the renderer's band would only be an empty gap; every other
// display, and anything unreadable, keeps the band.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const { menuBarCanOverlap } = require("../fullscreen-top-reserve");

function fakeWin({ fullScreen = true, bounds, destroyed = false } = {}) {
  return {
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    getBounds: () => {
      if (bounds instanceof Error) throw bounds;
      return bounds;
    },
  };
}

function fakeScreen(displayBounds) {
  return { getDisplayMatching: () => ({ bounds: displayBounds }) };
}

describe("menuBarCanOverlap", () => {
  const builtIn = { x: 0, y: 0, width: 1512, height: 982 };

  it("is false on a notched display, where the window starts below the menu bar", () => {
    const win = fakeWin({ bounds: { x: 0, y: 33, width: 1512, height: 949 } });
    assert.equal(menuBarCanOverlap(win, fakeScreen(builtIn)), false);
  });

  it("is true on a display without a notch, where the window starts at the top edge", () => {
    const win = fakeWin({ bounds: { x: 0, y: 0, width: 1512, height: 982 } });
    assert.equal(menuBarCanOverlap(win, fakeScreen(builtIn)), true);
  });

  it("compares against the window's own display, not y=0", () => {
    // External displays arranged above or below the main one have a non-zero
    // origin; a fullscreen window there starts exactly at that origin.
    const above = { x: 0, y: -1080, width: 1920, height: 1080 };
    const below = { x: 0, y: 982, width: 1920, height: 1080 };
    assert.equal(menuBarCanOverlap(fakeWin({ bounds: { ...above } }), fakeScreen(above)), true);
    assert.equal(menuBarCanOverlap(fakeWin({ bounds: { ...below } }), fakeScreen(below)), true);
    assert.equal(
      menuBarCanOverlap(fakeWin({ bounds: { ...below, y: below.y + 33 } }), fakeScreen(below)),
      false,
    );
  });

  it("keeps the band whenever it cannot tell", () => {
    const notched = { x: 0, y: 33, width: 1512, height: 949 };
    const cases = [
      ["not fullscreen", fakeWin({ fullScreen: false, bounds: notched }), fakeScreen(builtIn)],
      ["destroyed window", fakeWin({ destroyed: true, bounds: notched }), fakeScreen(builtIn)],
      ["no window", null, fakeScreen(builtIn)],
      ["no screen module", fakeWin({ bounds: notched }), undefined],
      ["screen without getDisplayMatching", fakeWin({ bounds: notched }), {}],
      ["getBounds throws", fakeWin({ bounds: new Error("gone") }), fakeScreen(builtIn)],
      ["no display", fakeWin({ bounds: notched }), { getDisplayMatching: () => null }],
      ["non-numeric bounds", fakeWin({ bounds: { y: "33" } }), fakeScreen(builtIn)],
    ];
    for (const [label, win, screen] of cases) {
      assert.equal(menuBarCanOverlap(win, screen), true, label);
    }
  });
});

describe("window-lifecycle sends the verdict with every fullscreen change", () => {
  const SOURCE = fs
    .readFileSync(path.join(__dirname, "..", "window-lifecycle.js"), "utf8")
    .replace(/\r\n/g, "\n");

  it("attaches menuBarOverlap from the helper to the fullscreen-changed send", () => {
    const fn = SOURCE.match(/const sendFullScreen = \(\) => \{([\s\S]*?)\n    \};/);
    assert.ok(fn, "could not locate sendFullScreen");
    assert.match(
      fn[1],
      /send\("fullscreen-changed", win\.isFullScreen\(\), \{\s*menuBarOverlap: menuBarCanOverlap\(win, screen\),\s*\}\)/,
    );
  });

  it("re-sends on every settle pass, after that pass's bounds recompute", () => {
    // The window's position is only final once the reflow lands, so a verdict
    // sent only at the event could read pre-transition bounds.
    const helper = SOURCE.match(/const scheduleFullscreenSettle = \(\) => \{([\s\S]*?)\n    \};/);
    assert.ok(helper, "could not locate scheduleFullscreenSettle");
    const body = helper[1];
    const bounds = body.indexOf("setTimeout(updateViewBounds, ms)");
    const send = body.indexOf("setTimeout(sendFullScreen, ms)");
    assert.ok(bounds !== -1 && send !== -1, "each pass must recompute bounds and re-send");
    assert.ok(bounds < send, "bounds first, so the verdict reads the settled window");
  });

  it("ships the helper in the packaged app", () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
    assert.ok(pkg.build.files.includes("fullscreen-top-reserve.js"));
  });
});

describe("preload forwards the verdict", () => {
  function loadPreload() {
    const listeners = [];
    const exposed = {};
    const fakeElectron = {
      contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
      ipcRenderer: {
        send: () => {},
        invoke: () => Promise.resolve(),
        on: (channel, fn) => { if (channel === "fullscreen-changed") listeners.push(fn); },
        once: () => {},
        removeListener: () => {},
        removeAllListeners: () => {},
      },
      webUtils: { getPathForFile: () => "" },
    };
    const file = path.join(__dirname, "..", "preload.js");
    const origLoad = Module._load;
    Module._load = function (request, ...rest) {
      if (request === "electron") return fakeElectron;
      return origLoad.call(this, request, ...rest);
    };
    try {
      delete require.cache[require.resolve(file)];
      require(file);
    } finally {
      Module._load = origLoad;
    }
    return { api: exposed.electronAPI, emit: (...args) => { for (const fn of listeners) fn({}, ...args); } };
  }

  it("turns the band off only on an explicit menuBarOverlap: false", () => {
    const { api, emit } = loadPreload();
    const heard = [];
    api.onFullScreenChanged((fs, overlap) => heard.push([fs, overlap]));
    emit(true, { menuBarOverlap: false });
    emit(true, { menuBarOverlap: true });
    emit(true);
    emit(true, null);
    emit(true, { menuBarOverlap: 0 });
    emit(false, { menuBarOverlap: false });
    assert.deepEqual(heard, [
      [true, false],
      [true, true],
      [true, true],
      [true, true],
      [true, true],
      [false, false],
    ]);
  });
});
