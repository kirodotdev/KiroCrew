const { readFileSync: readSource } = require("./source-with-owners");
"use strict";

// The trackpad's real finger-down / finger-up, read off Chromium's
// gesture-scroll input events and relayed to the dashboard as `begin` / `end`.
// The page's swipe reader releases a drag on `end` instead of guessing a gap.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const {
  armScrollTouch,
  readNaturalScrolling,
  scrollTouchPhase,
  SCROLL_DIRECTION_CHANNEL,
  SCROLL_TOUCH_CHANNEL,
} = require("../scroll-touch");

function makeFakeWc() {
  const handlers = {};
  const sent = [];
  const wc = {
    on(event, fn) { handlers[event] = fn; },
    send(channel, ...args) { sent.push([channel, ...args]); },
  };
  const input = (type) => handlers["input-event"]({}, { type });
  return { wc, sent, handlers, input };
}

describe("scrollTouchPhase", () => {
  it("maps the gesture-scroll pair and nothing else", () => {
    assert.equal(scrollTouchPhase({ type: "gestureScrollBegin" }), "begin");
    assert.equal(scrollTouchPhase({ type: "gestureScrollEnd" }), "end");
    for (const type of ["gestureScrollUpdate", "mouseWheel", "mouseDown", "gestureFlingStart", ""]) {
      assert.equal(scrollTouchPhase({ type }), null, type);
    }
    assert.equal(scrollTouchPhase(null), null);
    assert.equal(scrollTouchPhase("gestureScrollBegin"), null);
  });
});

describe("armScrollTouch", () => {
  it("sends each phase change on its channel", () => {
    const { wc, sent, input } = makeFakeWc();
    assert.equal(typeof armScrollTouch(wc, { platform: "darwin" }), "function");
    input("gestureScrollBegin");
    input("gestureScrollUpdate");
    input("mouseWheel");
    input("gestureScrollEnd");
    assert.deepEqual(sent, [[SCROLL_TOUCH_CHANNEL, "begin"], [SCROLL_TOUCH_CHANNEL, "end"]]);
  });

  it("sends a repeated phase once", () => {
    const { wc, sent, input } = makeFakeWc();
    armScrollTouch(wc, { platform: "darwin" });
    input("gestureScrollEnd");
    input("gestureScrollBegin");
    input("gestureScrollBegin");
    input("gestureScrollEnd");
    input("gestureScrollEnd");
    assert.deepEqual(sent.map(([, p]) => p), ["begin", "end"]);
  });

  it("arms nothing off macOS", () => {
    for (const platform of ["win32", "linux"]) {
      const { wc, handlers } = makeFakeWc();
      assert.equal(armScrollTouch(wc, { platform }), null, platform);
      assert.equal(handlers["input-event"], undefined, platform);
    }
  });

  it("sends nothing once the window is torn down, and survives a throwing send", () => {
    let alive = true;
    const { wc, sent, input } = makeFakeWc();
    armScrollTouch(wc, { platform: "darwin", isAlive: () => alive });
    alive = false;
    input("gestureScrollBegin");
    assert.deepEqual(sent, []);
    alive = true;
    wc.send = () => { throw new Error("destroyed"); };
    assert.doesNotThrow(() => input("gestureScrollEnd"));
  });

  it("is armed on the dashboard webContents and packaged", () => {
    const lifecycle = readSource(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
    assert.match(lifecycle, /armScrollTouch\(view\.webContents,/);
    const pkg = JSON.parse(readSource(path.join(__dirname, "..", "package.json"), "utf8"));
    assert.ok(pkg.build.files.includes("scroll-touch.js"), "scroll-touch.js ships in the app bundle");
  });
});

describe("preload onScrollTouch", () => {
  function loadPreload() {
    const listeners = new Map();
    const exposed = {};
    const fakeElectron = {
      contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
      ipcRenderer: {
        send: () => {},
        invoke: () => Promise.resolve(),
        sendSync: () => false,
        on: (channel, fn) => {
          if (!listeners.has(channel)) listeners.set(channel, new Set());
          listeners.get(channel).add(fn);
        },
        once: () => {},
        removeListener: (channel, fn) => listeners.get(channel)?.delete(fn),
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
    const emit = (value) => { for (const fn of [...(listeners.get(SCROLL_TOUCH_CHANNEL) || [])]) fn({}, value); };
    return { api: exposed.electronAPI, emit, listeners };
  }

  it("passes the two words through and drops anything else", () => {
    const { api, emit } = loadPreload();
    const got = [];
    const stop = api.onScrollTouch((p) => got.push(p));
    emit("begin");
    emit("bogus");
    emit({ phase: "end" });
    emit("end");
    assert.deepEqual(got, ["begin", "end"]);
    stop();
    emit("begin");
    assert.deepEqual(got, ["begin", "end"], "an unsubscribed caller hears nothing");
  });

  it("caches the Natural scrolling report, keeping only booleans", () => {
    const { api, listeners } = loadPreload();
    const tell = (v) => { for (const fn of [...(listeners.get(SCROLL_DIRECTION_CHANNEL) || [])]) fn({}, v); };
    assert.equal(api.getNaturalScrolling(), null, "unknown until main says");
    tell(false);
    assert.equal(api.getNaturalScrolling(), false);
    tell("true");
    tell(null);
    assert.equal(api.getNaturalScrolling(), false, "a non-boolean is dropped");
    tell(true);
    assert.equal(api.getNaturalScrolling(), true);
  });
});

describe("readNaturalScrolling", () => {
  const prefs = (value) => ({ getUserDefault: (key, type) => {
    assert.equal(key, "com.apple.swipescrolldirection");
    assert.equal(type, "string");
    if (value instanceof Error) throw value;
    return value;
  } });

  it("reads the stored boolean, and an absent key as the macOS default (on)", () => {
    // NSUserDefaults stringForKey gives "0" / "1" for a stored boolean, and
    // nil (Electron: "") when the user never changed the setting.
    assert.equal(readNaturalScrolling(prefs("0")), false);
    assert.equal(readNaturalScrolling(prefs("1")), true);
    assert.equal(readNaturalScrolling(prefs("")), true);
  });

  it("is unknown for anything else, or when the read fails", () => {
    for (const v of ["yes", undefined, null, 0, false]) assert.equal(readNaturalScrolling(prefs(v)), null, String(v));
    assert.equal(readNaturalScrolling(prefs(new Error("no"))), null);
    assert.equal(readNaturalScrolling(undefined), null);
  });
});

describe("armScrollTouch: Natural scrolling", () => {
  it("tells every page load, and again at a gesture's begin only when it changed", () => {
    const { wc, sent, input, handlers } = makeFakeWc();
    let natural = true;
    armScrollTouch(wc, { platform: "darwin", readNatural: () => natural });
    handlers["did-finish-load"]();
    input("gestureScrollBegin");
    input("gestureScrollEnd");
    natural = false;
    input("gestureScrollBegin");
    input("gestureScrollEnd");
    handlers["did-finish-load"]();
    assert.deepEqual(sent, [
      [SCROLL_DIRECTION_CHANNEL, true],
      [SCROLL_TOUCH_CHANNEL, "begin"],
      [SCROLL_TOUCH_CHANNEL, "end"],
      // Before the begin, so the page reads the first wheel right.
      [SCROLL_DIRECTION_CHANNEL, false],
      [SCROLL_TOUCH_CHANNEL, "begin"],
      [SCROLL_TOUCH_CHANNEL, "end"],
      // A new page's preload starts empty.
      [SCROLL_DIRECTION_CHANNEL, false],
    ]);
  });

  it("sends nothing when unknown or without a reader", () => {
    const a = makeFakeWc();
    armScrollTouch(a.wc, { platform: "darwin", readNatural: () => null });
    a.handlers["did-finish-load"]();
    a.input("gestureScrollBegin");
    assert.deepEqual(a.sent, [[SCROLL_TOUCH_CHANNEL, "begin"]]);
    const b = makeFakeWc();
    armScrollTouch(b.wc, { platform: "darwin" });
    b.handlers["did-finish-load"]();
    assert.deepEqual(b.sent, []);
  });

  it("is wired to systemPreferences on the dashboard webContents", () => {
    const lifecycle = readSource(path.join(__dirname, "..", "window-lifecycle.js"), "utf8");
    assert.match(lifecycle, /readNatural: \(\) => readNaturalScrolling\(systemPreferences\)/);
  });
});
