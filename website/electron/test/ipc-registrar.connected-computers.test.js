"use strict";

// connected-computers:confirm opens the native consent for sharing this
// computer, so it answers only the local dashboard and passes on only the
// consent id plus this shell's own gateway.
//
// A small harness of its own, so this channel's test doesn't grow
// ipc-registrar.test.js. It fakes only what registerShell needs.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");

const MODULE_PATH = path.join(__dirname, "..", "ipc-registrar.js");
const BACKEND_URL = "http://localhost:5476";

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

function fakeConnectedComputers() {
  const calls = [];
  return { calls, confirmConsent: async (...args) => { calls.push(args); return { ok: true }; } };
}

function harness({ connectedComputers, gatewayLocal = true, portOwner = "kirocrew", internalSecret = "" }) {
  const handlers = new Map();
  const createIpcRegistrar = loadRegistrar();
  const registrar = createIpcRegistrar({
    electron: {
      app: {},
      dialog: { fake: "dialog" },
      Notification: class {},
      ipcMain: { handle: (ch, fn) => handlers.set(ch, fn), on: () => {} },
      shell: {},
      webContents: { getAllWebContents: () => [] },
      clipboard: {},
    },
    store: { get: () => undefined, set: () => {} },
    windows: {
      windowForWebContents: () => ({ fake: "owner" }),
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => gatewayLocal },
      menu: { buildApplicationMenu: () => {} },
    },
    gateway: { probePrimaryPortOwner: async () => portOwner },
    backendUrl: BACKEND_URL,
    port: 5476,
    connectedComputers,
    // Like main.js's readInternalSecret: the secret belongs to one gateway.
    readInternalSecret: (gatewayUrl) => (gatewayUrl === BACKEND_URL ? internalSecret : ""),
  });
  registrar.registerShell();
  return { confirm: handlers.get("connected-computers:confirm") };
}

const event = (url = `${BACKEND_URL}/system/services`) => ({
  sender: { id: "dashboard-sender" },
  senderFrame: { url },
});

// The ways a window fails the local-dashboard gate.
const NOT_LOCAL_WINDOWS = [
  ["a remote origin", {}, "https://remote.example/chat"],
  ["a remote gateway behind a tunnel", { gatewayLocal: false }, undefined],
  ["a foreign port holder", { portOwner: "foreign" }, undefined],
  ["an unknown port holder", { portOwner: "unknown" }, undefined],
];

test("B13 connected-computers:confirm rejects a sender that is not the local dashboard", async () => {
  for (const [why, opts, url] of NOT_LOCAL_WINDOWS) {
    const cc = fakeConnectedComputers();
    const h = harness({ connectedComputers: cc, ...opts });
    await assert.rejects(
      () => h.confirm(event(url), "c".repeat(32)),
      { message: "connected-computers:confirm is restricted to the local dashboard" },
      why,
    );
    assert.deepEqual(cc.calls, [], `${why}: a rejected sender reaches no native dialog`);
  }
});

test("B13 connected-computers:confirm passes only the consent id and this shell's own gateway", async () => {
  const cc = fakeConnectedComputers();
  const h = harness({ connectedComputers: cc, internalSecret: "s3cret" });
  // Extra arguments (a folder, a tool list) are ignored: only the id crosses.
  const result = await h.confirm(event(), "abc", "/etc", ["x"]);
  assert.deepEqual(result, { ok: true });
  assert.equal(cc.calls.length, 1);
  const [consent, deps] = cc.calls[0];
  assert.equal(consent, "abc");
  assert.equal(deps.backendUrl, BACKEND_URL);
  assert.equal(deps.getSecret(), "s3cret");
  assert.deepEqual(deps.window, { fake: "owner" });
  assert.deepEqual(deps.dialog, { fake: "dialog" });
  assert.equal(typeof deps.fetchFn, "function");
});
