"use strict";

// preload.js's `watchCursorAway` bridge, loaded for real against a fake
// `electron`: several callers in one renderer share the main process's single
// per-window watch, so only the LAST unsubscribe may disarm it.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

function loadPreload() {
  const sent = [];
  const listeners = new Set();
  const exposed = {};
  const fakeElectron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send: (channel, ...args) => sent.push([channel, ...args]),
      invoke: () => Promise.resolve(),
      on: (channel, fn) => { if (channel === "focus-mode:cursor-away") listeners.add(fn); },
      once: () => {},
      removeListener: (channel, fn) => { if (channel === "focus-mode:cursor-away") listeners.delete(fn); },
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
  const watches = () => sent.filter(([c]) => c === "focus-mode-watch-cursor").map(([, on]) => on);
  const report = (away) => { for (const fn of [...listeners]) fn({}, away); };
  return { api: exposed.electronAPI, watches, report, listeners };
}

test("one caller unsubscribing does not disarm another's pending watch", () => {
  const { api, watches, report } = loadPreload();
  const topBar = [];
  const rail = [];
  const stopTop = api.watchCursorAway((away) => topBar.push(away));
  const stopRail = api.watchCursorAway((away) => rail.push(away));
  assert.deepEqual(watches(), [true, true]);

  stopTop();
  // The rail is still waiting, so the main-process watch must stay armed.
  assert.deepEqual(watches(), [true, true]);

  report(true);
  assert.deepEqual(rail, [true]);
  assert.deepEqual(topBar, [], "an unsubscribed caller hears nothing");

  stopRail();
  assert.deepEqual(watches(), [true, true, false], "the last unsubscribe disarms");
});

test("a repeated unsubscribe is a no-op and cannot disarm a live caller", () => {
  const { api, watches, listeners } = loadPreload();
  const stopA = api.watchCursorAway(() => {});
  api.watchCursorAway(() => {});
  stopA();
  stopA();
  assert.deepEqual(watches(), [true, true]);
  assert.equal(listeners.size, 1);
});
