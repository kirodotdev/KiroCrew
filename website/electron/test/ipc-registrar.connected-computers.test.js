"use strict";

// connected-computers:share opens the native consent for sharing this
// computer, so it answers only the local dashboard and passes on only the
// crew id plus this shell's own consent-only host opener.
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
  return { calls, shareFromDashboard: async (...args) => { calls.push(args); return { ok: true }; } };
}

const OPEN_HOST = () => ({ fake: "consent host" });

function harness({
  connectedComputers,
  gatewayLocal = true,
  portOwner = "kirocrew",
  enabled = true,
}) {
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
    connectedComputersEnabled: enabled,
    openConnectedComputersConsentHost: OPEN_HOST,
    // A consent never needs the gateway's secret: hand one over to prove it is unused.
    readInternalSecret: () => "s3cret",
  });
  registrar.registerShell();
  return { share: handlers.get("connected-computers:share"), handlers };
}

test("flag off: no connected-computers channel is registered at all", () => {
  for (const enabled of [false, undefined, "true", 1]) {
    const h = harness({ connectedComputers: fakeConnectedComputers(), enabled });
    assert.equal(h.share, undefined, `enabled=${String(enabled)}`);
    assert.equal(
      [...h.handlers.keys()].some((ch) => ch.startsWith("connected-computers:")),
      false,
    );
  }
});

test("flag on: the channel is the one extra shell handler", () => {
  const off = harness({ connectedComputers: fakeConnectedComputers(), enabled: false });
  const on = harness({ connectedComputers: fakeConnectedComputers(), enabled: true });
  const added = [...on.handlers.keys()].filter((ch) => !off.handlers.has(ch));
  assert.deepEqual(added, ["connected-computers:share"]);
});

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

test("B13 connected-computers:share rejects a sender that is not the local dashboard", async () => {
  for (const [why, opts, url] of NOT_LOCAL_WINDOWS) {
    const cc = fakeConnectedComputers();
    const h = harness({ connectedComputers: cc, ...opts });
    await assert.rejects(
      () => h.share(event(url), "work-crew"),
      { message: "connected-computers:share is restricted to the local dashboard" },
      why,
    );
    assert.deepEqual(cc.calls, [], `${why}: a rejected sender reaches no native dialog`);
  }
});

test("B13 connected-computers:share passes only the crew id and the consent-only host opener", async () => {
  const cc = fakeConnectedComputers();
  const h = harness({ connectedComputers: cc });
  // Extra arguments (a folder, a tool list) are ignored: only the id crosses.
  const result = await h.share(event(), "work-crew", "/etc", ["x"]);
  assert.deepEqual(result, { ok: true });
  assert.equal(cc.calls.length, 1);
  const [instanceId, deps] = cc.calls[0];
  assert.equal(instanceId, "work-crew");
  assert.deepEqual(Object.keys(deps).sort(), ["dialog", "openHost", "window"]);
  assert.equal(deps.openHost, OPEN_HOST);
  assert.deepEqual(deps.window, { fake: "owner" });
  assert.deepEqual(deps.dialog, { fake: "dialog" });
});
