"use strict";

// relay-crew:open takes a valid endpoint ID only, and only from the local
// dashboard: the opener behind it sends this machine's local secret.
//
// A small harness of its own, so this channel's test doesn't grow
// ipc-registrar.test.js. It fakes only what registerShell needs.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");

const MODULE_PATH = path.join(__dirname, "..", "ipc-registrar.js");

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

function harness({ opener, gatewayLocal = true, portOwner = "kirocrew" }) {
  const handlers = new Map();
  const probes = [];
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
      windowForWebContents: () => ({ fake: "window" }),
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => gatewayLocal },
      menu: { buildApplicationMenu: () => {} },
    },
    gateway: { probePrimaryPortOwner: async () => { probes.push(true); return portOwner; } },
    backendUrl: "http://localhost:5476",
    port: 5476,
    relayCrewOpenerOverride: opener,
  });
  registrar.registerShell();
  return { open: handlers.get("relay-crew:open"), probes };
}

const event = (url = "http://localhost:5476/system/services") => ({
  sender: { id: "dashboard-sender" },
  senderFrame: { url },
});
const spy = (calls) => ({ open: async (x) => { calls.push(x); return { ok: false, error: "CREW_OFFLINE" }; } });

const REJECTIONS = [
  ["wrong origin", {}, "https://remote.example/chat"],
  ["remote window", { gatewayLocal: false }, undefined],
  ["foreign port owner", { portOwner: "foreign" }, undefined],
  ["unknown port owner", { portOwner: "unknown" }, undefined],
];

test("relay-crew:open checks the endpoint ID, then the local-dashboard gate", async () => {
  const id = "a1b2c3d4e5f6g7h8i9j0k1";
  const calls = [];
  const h = harness({ opener: spy(calls) });
  for (const bad of ["", "UPPERCASEUPPERCASEUPPER", `${id}.evil`, "a".repeat(64), 42, null, [id]]) {
    assert.deepEqual(await h.open(event(), bad), { ok: false, error: "INVALID_ENDPOINT" }, String(bad));
  }
  assert.equal(h.probes.length, 0);
  assert.deepEqual(await h.open(event(), id), { ok: false, error: "CREW_OFFLINE" });
  assert.deepEqual(calls, [id]);
  for (const [label, options, url] of REJECTIONS) {
    const refusedCalls = [];
    const r = harness({ ...options, opener: spy(refusedCalls) });
    assert.deepEqual(await r.open(event(url), id), { ok: false, error: "UNAVAILABLE" }, label);
    assert.deepEqual(refusedCalls, [], label);
  }
});
