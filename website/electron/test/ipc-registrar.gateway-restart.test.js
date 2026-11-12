"use strict";

// Settings → About's Restart gateway stops and respawns the gateway this app
// runs, so both channels take the local-dashboard gate, a refused sender never
// reaches the supervisor, and the supervisor gets the sender's own window.
//
// A small harness of its own, so these channels' tests don't grow
// ipc-registrar.test.js. It fakes only what registerShell needs.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");

const MODULE_PATH = path.join(__dirname, "..", "ipc-registrar.js");
const OWNER = { fake: "main-window" };

function loadRegistrar() {
  const originalLoad = Module._load;
  Module._load = function loadWithFakes(request, parent, isMain) {
    if (request === "electron") {
      throw new Error("ipc-registrar tests must never load real Electron");
    }
    if (request === "./global-hotkey" && parent?.filename === MODULE_PATH) {
      return {
        DEFAULT_GLOBAL_HOTKEY: "",
        createSummonHandler: () => () => {},
        bindGlobalHotkey: () => ({ accelerator: "", bound: false }),
        unregisterGlobalHotkey: () => {},
        currentGlobalHotkey: () => "",
        setGlobalHotkeyLogger: () => {},
      };
    }
    if (request === "./auto-update" && parent?.filename === MODULE_PATH) {
      return { initAutoUpdate: () => ({}) };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  delete require.cache[require.resolve(MODULE_PATH)];
  try {
    return require(MODULE_PATH).createIpcRegistrar;
  } finally {
    Module._load = originalLoad;
  }
}

function harness({ gatewayLocal = true, portOwner = "kirocrew", can = true, outcome = "restarting" } = {}) {
  const handlers = new Map();
  const calls = [];
  const createIpcRegistrar = loadRegistrar();
  const registrar = createIpcRegistrar({
    electron: {
      app: {},
      dialog: {},
      Notification: class {},
      ipcMain: { handle: (ch, fn) => handlers.set(ch, fn), on: () => {} },
      shell: {},
      webContents: { getAllWebContents: () => [] },
      clipboard: {},
    },
    store: { get: () => undefined, set: () => {} },
    windows: {
      windowForWebContents: () => OWNER,
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => gatewayLocal },
      menu: { buildApplicationMenu: () => {} },
    },
    gateway: {
      probePrimaryPortOwner: async () => portOwner,
      canRestartGateway: (window) => { calls.push(["can", window]); return can; },
      restartGateway: (window) => { calls.push(["restart", window]); return outcome; },
    },
    backendUrl: "http://localhost:5476",
    port: 5476,
  });
  registrar.registerShell();
  return { can: handlers.get("gateway:can-restart"), restart: handlers.get("gateway:restart"), calls };
}

const event = (url = "http://localhost:5476/settings/about") => ({
  sender: { id: "dashboard-sender" },
  senderFrame: { url },
});

const REJECTIONS = [
  ["wrong origin", {}, "https://remote.example/settings"],
  ["remote window", { gatewayLocal: false }, undefined],
  ["foreign port owner", { portOwner: "foreign" }, undefined],
  ["unknown port owner", { portOwner: "unknown" }, undefined],
];

test("both channels are refused outside the local dashboard and never reach the supervisor", async () => {
  for (const [label, options, url] of REJECTIONS) {
    const h = harness(options);
    assert.equal(await h.can(event(url)), false, label);
    await assert.rejects(() => h.restart(event(url)), {
      message: "gateway:restart is restricted to the local dashboard",
    }, label);
    assert.deepEqual(h.calls, [], `${label}: a rejected sender must not reach the supervisor`);
  }
});

test("both channels pass the sender's own window to the supervisor and return its answer", async () => {
  for (const [can, outcome] of [[true, "restarting"], [false, "unavailable"]]) {
    const h = harness({ can, outcome });
    assert.equal(await h.can(event()), can);
    assert.equal(await h.restart(event()), outcome);
    assert.deepEqual(h.calls, [["can", OWNER], ["restart", OWNER]]);
  }
});

test("gateway:can-restart is a strict boolean", async () => {
  assert.equal(await harness({ can: "yes" }).can(event()), false);
});
