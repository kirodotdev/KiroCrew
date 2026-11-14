"use strict";

// `crash-reports:get` tells the crash notice when this launch's native file
// logging is off, and carries only the cause code: the log path stays in main.
//
// A small harness of its own, so this channel's tests don't grow
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

function crashReportsGet(options) {
  const handlers = new Map();
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
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => true },
      menu: { buildApplicationMenu: () => {} },
    },
    gateway: { probePrimaryPortOwner: async () => "kirocrew" },
    backendUrl: "http://localhost:5476",
    port: 5476,
    crashScan: () => ({
      crashLogPath: "/logs/crashes.log",
      newCrashes: [{ key: "a" }, { key: "b" }],
      recorded: 2,
    }),
    ...options,
  });
  registrar.registerShell();
  return handlers.get("crash-reports:get")({
    sender: { id: "dashboard-sender" },
    senderFrame: { url: "http://localhost:5476/system/services" },
  });
}

test("crash-reports:get says when native file logging is off, and nothing else of it", async () => {
  const summary = await crashReportsGet({
    nativeLoggingStatus: () => ({ blocked: true, logPath: "/logs/chromium.log", fileLogOffCause: "disk_full" }),
  });

  assert.deepEqual(summary, { newCount: 2, nativeLogOff: "disk_full" });
  assert.equal(JSON.stringify(summary).includes("/logs"), false, "the log path stays in main");
});

test("crash-reports:get adds no cause when native file logging status was not injected", async () => {
  assert.deepEqual(await crashReportsGet({}), { newCount: 2 });
});
