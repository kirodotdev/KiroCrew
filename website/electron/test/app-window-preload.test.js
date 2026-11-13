/**
 * app-window-preload — the whole surface a hosted app window's page gets.
 *
 * The bridge is enumerated, not a pass-through: a channel it does not name is a
 * channel the page cannot reach. Its identity comes from the host's own
 * `--kc-app-window=` argument and is informational only (the host resolves
 * every message by sender; see app-window-host.test.js).
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

function loadPreload(argv) {
  const exposed = {};
  const sent = [];
  const listeners = {};
  const electron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send: (channel, ...args) => sent.push([channel, ...args]),
      on: (channel, fn) => { (listeners[channel] = listeners[channel] || []).push(fn); },
      removeListener: (channel, fn) => {
        listeners[channel] = (listeners[channel] || []).filter((f) => f !== fn);
      },
    },
  };
  const modPath = path.join(__dirname, "..", "app-window-preload.js");
  delete require.cache[require.resolve(modPath)];
  const origLoad = Module._load;
  const origArgv = process.argv;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return electron;
    return origLoad(request, parent, isMain);
  };
  process.argv = argv;
  try {
    require(modPath);
  } finally {
    Module._load = origLoad;
    process.argv = origArgv;
  }
  return { exposed, sent, listeners };
}

test("the bridge exposes exactly the host calls, under one global", () => {
  const { exposed } = loadPreload(["electron", "--kc-app-window=pets/pet/overlay/1"]);
  assert.deepStrictEqual(Object.keys(exposed), ["kirocrewDesktopWindow"]);
  const bridge = exposed.kirocrewDesktopWindow;
  assert.ok(Object.isFrozen(bridge));
  assert.deepStrictEqual(Object.keys(bridge).sort(), [
    "app", "close", "kind", "level", "onShortcut", "open", "ready", "setFocusable", "setHitRegions", "window",
  ]);
  assert.deepStrictEqual(
    { app: bridge.app, window: bridge.window, kind: bridge.kind, level: bridge.level },
    { app: "pets", window: "pet", kind: "overlay", level: 1 },
  );
});

test("each call sends its own channel with coerced arguments", () => {
  const { exposed, sent } = loadPreload(["--kc-app-window=pets/pet/overlay/1"]);
  const bridge = exposed.kirocrewDesktopWindow;
  bridge.ready();
  bridge.close();
  bridge.open({ toString: () => "panel" });
  bridge.setFocusable("yes");
  bridge.setHitRegions([{ x: "1", y: 2, width: 3, height: 4, secret: "x" }]);
  bridge.setHitRegions("everything");
  assert.deepStrictEqual(sent, [
    ["app-window:ready"],
    ["app-window:close"],
    ["app-window:open", "panel"],
    ["app-window:set-focusable", false],
    ["app-window:set-hit-regions", [{ x: 1, y: 2, width: 3, height: 4 }]],
    ["app-window:set-hit-regions", []],
  ]);
});

test("onShortcut delivers ids and unsubscribes", () => {
  const { exposed, listeners } = loadPreload(["--kc-app-window=pets/panel/panel/1"]);
  const got = [];
  const off = exposed.kirocrewDesktopWindow.onShortcut((id) => got.push(id));
  const [listener] = listeners["app-window:shortcut"];
  listener({}, "toggle");
  listener({}, { not: "an id" });
  off();
  assert.deepStrictEqual(got, ["toggle"]);
  assert.deepStrictEqual(listeners["app-window:shortcut"], []);
  assert.strictEqual(typeof exposed.kirocrewDesktopWindow.onShortcut(null), "function");
});

test("a window the host did not tag reports no identity", () => {
  const { exposed } = loadPreload(["electron"]);
  const bridge = exposed.kirocrewDesktopWindow;
  assert.deepStrictEqual(
    { app: bridge.app, window: bridge.window, level: bridge.level },
    { app: "", window: "", level: 0 },
  );
});
