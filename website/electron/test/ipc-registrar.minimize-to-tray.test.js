"use strict";

// minimize-to-tray:get/set answer only the main window's own dashboard, the
// same check close-action:* and tray-icon:* take, so the row still works on a
// main window connected to a remote gateway. The refusal sweep over every
// main-window setting channel is in ipc-registrar.test.js.
//
// A small harness of its own, so these channels' tests don't grow
// ipc-registrar.test.js. It fakes only what registerShell needs.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");

const MODULE_PATH = path.join(__dirname, "..", "ipc-registrar.js");
const BACKEND = "http://localhost:5476";

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

// `sender` is the window the event's WebContents belongs to.
function harness({ sender = "main" } = {}) {
  const handlers = new Map();
  const calls = [];
  const main = { fake: "main-window", _mcBackendUrl: BACKEND };
  const other = { fake: "connection-window", _mcBackendUrl: "http://localhost:6100" };
  const state = { on: false, offered: true, canEnable: true };
  const createIpcRegistrar = loadRegistrar();
  createIpcRegistrar({
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
      getMainWindow: () => main,
      windowForWebContents: () => (sender === "main" ? main : other),
      // A main window on a configured remote host: the local-dashboard gate
      // would refuse it, this channel must not.
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => false },
      menu: { buildApplicationMenu: () => {} },
      minimizeToTray: {
        get: () => { calls.push(["get"]); return { ...state }; },
        set: (on) => { calls.push(["set", on]); state.on = on === true; return { ...state }; },
      },
    },
    gateway: { probePrimaryPortOwner: async () => "foreign" },
    backendUrl: BACKEND,
    port: 5476,
  }).registerShell();
  return { get: handlers.get("minimize-to-tray:get"), set: handlers.get("minimize-to-tray:set"), calls };
}

function topFrameEvent(url = `${BACKEND}/settings/display/view`) {
  const frame = { url };
  return { sender: { id: "main-view", mainFrame: frame }, senderFrame: frame };
}

test("both channels answer the main window's dashboard on a remote gateway", async () => {
  const h = harness();
  assert.deepEqual(await h.get(topFrameEvent()), { on: false, offered: true, canEnable: true });
  assert.deepEqual(await h.set(topFrameEvent(), true), { on: true, offered: true, canEnable: true });
  assert.deepEqual(h.calls, [["get"], ["set", true]]);
});

test("both channels refuse another window and never reach the setting", async () => {
  const h = harness({ sender: "other" });
  for (const [name, call] of [["minimize-to-tray:get", () => h.get(topFrameEvent())], ["minimize-to-tray:set", () => h.set(topFrameEvent(), true)]]) {
    await assert.rejects(call, { message: `${name} is restricted to the main window's dashboard` });
  }
  assert.deepEqual(h.calls, []);
});
