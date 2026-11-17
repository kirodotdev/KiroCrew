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
  // Reading BaseWindow is the first thing building the real opener does.
  const built = [];
  const createIpcRegistrar = loadRegistrar();
  const registrar = createIpcRegistrar({
    electron: {
      get BaseWindow() { built.push(true); return class {}; },
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
  return { open: handlers.get("relay-crew:open"), closeAll: handlers.get("relay-crew:close-all"), probes, built };
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

test("relay-crew:close-all closes with a fixed reason, only from the local dashboard, and ignores arguments", async () => {
  const reasons = [];
  const closer = { open: async () => ({ ok: true }), closeAll: (reason) => { reasons.push(reason); } };
  const h = harness({ opener: closer });
  assert.equal(await h.closeAll(event(), "a1b2c3d4e5f6g7h8i9j0k1", { reason: "anything" }), undefined);
  assert.deepEqual(reasons, ["SIGNED_OUT"]);
  for (const [label, options, url] of REJECTIONS) {
    const refused = [];
    const r = harness({ ...options, opener: { open: async () => ({ ok: true }), closeAll: (x) => refused.push(x) } });
    assert.equal(await r.closeAll(event(url)), undefined, label);
    assert.deepEqual(refused, [], label);
  }
});

test("relay-crew:close-all never builds an opener that was never built", async () => {
  const h = harness({ opener: null });
  assert.equal(await h.closeAll(event()), undefined);
  assert.equal(h.probes.length, 1, "the gate ran");
  assert.deepEqual(h.built, [], "no opener was built");
});
