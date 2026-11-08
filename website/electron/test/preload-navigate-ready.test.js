"use strict";

// preload.js's `onNavigate` bridge, loaded for real against a fake `electron`:
// it acks `navigate-ready` to the main process once the `navigate` listener is
// registered, and only once per document however often the SPA re-subscribes.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const Module = require("module");

function loadPreload() {
  const log = [];
  const exposed = {};
  const fakeElectron = {
    contextBridge: { exposeInMainWorld: (key, api) => { exposed[key] = api; } },
    ipcRenderer: {
      send: (channel) => log.push(["send", channel]),
      invoke: () => Promise.resolve(),
      on: (channel) => log.push(["on", channel]),
      once: () => {},
      removeListener: (channel) => log.push(["off", channel]),
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
  return { api: exposed.electronAPI, log };
}

test("onNavigate acks navigate-ready after registering its listener", () => {
  const { api, log } = loadPreload();
  assert.deepEqual(log.filter(([, c]) => c === "navigate-ready"), [], "nothing is acked before a subscriber");
  api.onNavigate(() => {});
  const on = log.findIndex(([kind, c]) => kind === "on" && c === "navigate");
  const ack = log.findIndex(([kind, c]) => kind === "send" && c === "navigate-ready");
  assert.ok(on >= 0, "the navigate listener is registered");
  assert.ok(ack > on, "the ack follows the registration");
});

test("re-subscribing in the same document does not ack again", () => {
  const { api, log } = loadPreload();
  const stop = api.onNavigate(() => {});
  stop();
  api.onNavigate(() => {});
  api.onNavigate(() => {});
  assert.equal(log.filter(([kind, c]) => kind === "send" && c === "navigate-ready").length, 1);
});

test("a fresh document (preload re-run) acks again", () => {
  loadPreload().api.onNavigate(() => {});
  const { api, log } = loadPreload();
  api.onNavigate(() => {});
  assert.equal(log.filter(([kind, c]) => kind === "send" && c === "navigate-ready").length, 1);
});
