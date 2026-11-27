"use strict";

// relay-crew:open takes a valid endpoint ID only, and only from the local
// dashboard: the opener behind it sends this machine's local secret.
//
// A small harness of its own, so this channel's test doesn't grow
// ipc-registrar.test.js. It fakes only what registerShell needs.

const { test } = require("node:test");
const { EventEmitter } = require("node:events");
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

function harness({ opener, gatewayLocal = true, portOwner = "kirocrew", main = null }) {
  const handlers = new Map();
  const probes = [];
  const raised = [];
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
      // The main window's dashboard maps to it, a relay view to no window (it
      // is no window's `_mcView`), and any other sender to another window.
      windowForWebContents: (wc) => {
        if (main && wc === main._mcView.webContents) return main;
        return wc && wc.id === "relay-view" ? null : { fake: "window" };
      },
      getMainWindow: () => main,
      showMainWindow: (o) => { raised.push(o); return true; },
      security: { configureSession: () => {}, isGatewayLocalForWindow: () => gatewayLocal },
      menu: { buildApplicationMenu: () => {} },
    },
    gateway: { probePrimaryPortOwner: async () => { probes.push(true); return portOwner; } },
    backendUrl: "http://localhost:5476",
    port: 5476,
    relayCrewOpenerOverride: opener,
  });
  registrar.registerShell();
  return {
    open: handlers.get("relay-crew:open"),
    closeAll: handlers.get("relay-crew:close-all"),
    openPane: handlers.get("relay-crew:open-pane"),
    prewarmPane: handlers.get("relay-crew:prewarm-pane"),
    paneBounds: handlers.get("relay-crew:pane-bounds"),
    paneOverlay: handlers.get("relay-crew:pane-overlay"),
    paneInactive: handlers.get("relay-crew:pane-inactive"),
    paneClose: handlers.get("relay-crew:pane-close"),
    openRequests: handlers.get("relay-crew:open-requests"),
    probes,
    raised,
    built,
  };
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

// The pane channels: open-pane passes the local-dashboard gate and then the
// main-window gate; the steering channels take only the main window's top
// frame on its own origin, and only from the dashboard view that opened the pane.
const ID = "a1b2c3d4e5f6g7h8i9j0k1";
const DASHBOARD_URL = "http://localhost:5476/system/services";

function mainWindow({ url = DASHBOARD_URL, width = 1000, height = 800 } = {}) {
  const frame = { url };
  const webContents = { id: "main-dashboard", mainFrame: frame };
  return {
    _mcBackendUrl: "http://localhost:5476",
    _mcView: { webContents },
    getContentBounds: () => ({ x: 0, y: 0, width, height }),
  };
}
const from = (wc, frame = wc.mainFrame) => ({ sender: wc, senderFrame: frame });
const otherWindow = () => { const frame = { url: DASHBOARD_URL }; return { id: "other-dashboard", mainFrame: frame }; };
const relayView = () => { const frame = { url: `https://${ID}.relay.example.test/` }; return { id: "relay-view", mainFrame: frame }; };

function paneOpener(dashboard) {
  const calls = [];
  const steer = [];
  const handle = {
    setBounds: (r) => steer.push(["bounds", r]),
    setOverlay: (v) => steer.push(["overlay", v]),
    setInactive: (v) => steer.push(["inactive", v]),
    close: () => steer.push(["close"]),
  };
  return {
    calls,
    steer,
    open: async (id, pane) => { calls.push([id, pane]); return { ok: true }; },
    paneFor: (id) => (dashboard && id === ID ? { handle, dashboard } : null),
  };
}

test("relay-crew:open-pane checks the ID, the local-dashboard gate, the main window's top frame, then the rect", async () => {
  const main = mainWindow();
  const wc = main._mcView.webContents;
  const RECT = { x: 0, y: 48, width: 500, height: 400 };
  const VIEWPORT = { width: 500, height: 400 };
  const o = paneOpener(null);
  const h = harness({ opener: o, main });
  for (const bad of ["", `${ID}.evil`, 42, null]) {
    assert.deepEqual(await h.openPane(from(wc), bad, RECT, VIEWPORT), { ok: false, error: "INVALID_ENDPOINT" });
  }
  assert.equal(h.probes.length, 0);
  // The dashboard measures at half the window's DIP width: the rect doubles, then clamps.
  assert.deepEqual(await h.openPane(from(wc), ID, RECT, VIEWPORT), { ok: true });
  assert.deepEqual(o.calls, [[ID, { parent: main, dashboard: main._mcView, bounds: { x: 0, y: 96, width: 1000, height: 704 } }]]);
  for (const bad of [null, "rect", { x: 0, y: 0, width: 0, height: 10 }, { x: NaN, y: 0, width: 10, height: 10 }, { x: 0, y: 900, width: 10, height: 10 }]) {
    const b = paneOpener(null);
    const r = harness({ opener: b, main });
    assert.deepEqual(await r.openPane(from(wc), ID, bad, VIEWPORT), { ok: false, error: "INVALID_BOUNDS" }, String(bad));
    assert.deepEqual(b.calls, [], "no open, so no mint");
  }
  const refusals = [
    ["wrong origin", {}, from(wc, { url: "https://remote.example/chat" })],
    ["remote window", { gatewayLocal: false }, from(wc)],
    ["foreign port owner", { portOwner: "foreign" }, from(wc)],
    ["a local dashboard window that is not the main window", {}, from(otherWindow())],
    ["a subframe of the main window", {}, from(wc, { url: DASHBOARD_URL })],
    ["the relay view", {}, from(relayView())],
    ["no main window", { main: null }, from(wc)],
  ];
  for (const [label, options, ev] of refusals) {
    const b = paneOpener(null);
    const r = harness({ main, ...options, opener: b });
    assert.deepEqual(await r.openPane(ev, ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" }, label);
    assert.deepEqual(b.calls, [], label);
  }
});

test("relay-crew:prewarm-pane loads only from the main window's dashboard, never hands off, and never opens a Crew twice", async () => {
  const main = mainWindow();
  const wc = main._mcView.webContents;
  const RECT = { x: 0, y: 48, width: 500, height: 400 };
  const VIEWPORT = { width: 500, height: 400 };
  const calls = [];
  const opener = { open: async () => ({ ok: true }), prewarm: async (id, pane) => { calls.push([id, pane]); return { ok: true }; } };
  const h = harness({ opener, main });
  assert.deepEqual(await h.prewarmPane(from(wc), `${ID}.evil`, RECT, VIEWPORT), { ok: false, error: "INVALID_ENDPOINT" });
  assert.deepEqual(await h.prewarmPane(from(wc), ID, RECT, VIEWPORT), { ok: true });
  assert.deepEqual(calls, [[ID, { parent: main, dashboard: main._mcView, bounds: { x: 0, y: 96, width: 1000, height: 704 } }]]);
  for (const [label, options, ev] of [
    ["a local dashboard window that is not the main window", {}, from(otherWindow())],
    ["the relay view", {}, from(relayView())],
    ["no main window", { main: null }, from(wc)],
    ["remote window", { gatewayLocal: false }, from(wc)],
  ]) {
    const refused = [];
    const r = harness({ main, ...options, opener: { open: async () => ({ ok: true }), prewarm: async (id) => { refused.push(id); return { ok: true }; } } });
    assert.deepEqual(await r.prewarmPane(ev, ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" }, label);
    assert.deepEqual(refused, [], label);
    assert.deepEqual(r.raised, [], `${label}: nothing came forward`);
  }
});

test("pane bounds, overlay, inactive and close steer the pane only from the dashboard view that opened it", async () => {
  const main = mainWindow();
  const wc = main._mcView.webContents;
  const o = paneOpener(main._mcView);
  const h = harness({ opener: o, main });
  assert.equal(await h.paneBounds(from(wc), ID, { x: 10, y: 20, width: 300, height: 200 }, { width: 1000, height: 800 }), undefined);
  assert.equal(await h.paneOverlay(from(wc), ID, true), undefined);
  assert.equal(await h.paneOverlay(from(wc), ID, false), undefined);
  assert.equal(await h.paneInactive(from(wc), ID, true), undefined);
  assert.equal(await h.paneInactive(from(wc), ID, false), undefined);
  assert.equal(await h.paneClose(from(wc), ID), undefined);
  assert.deepEqual(o.steer, [
    ["bounds", { x: 10, y: 20, width: 300, height: 200 }],
    ["overlay", true],
    ["overlay", false],
    ["inactive", true],
    ["inactive", false],
    ["close"],
  ]);
  for (const active of ["true", 1, null, undefined]) {
    assert.equal(await h.paneOverlay(from(wc), ID, active), undefined);
    assert.equal(await h.paneInactive(from(wc), ID, active), undefined);
  }
  assert.equal(await h.paneInactive(from(wc), "not-an-endpoint", true), undefined);
  assert.equal(await h.paneClose(from(wc), "not-an-endpoint"), undefined);
  assert.equal(o.steer.length, 6, "a bad argument changes nothing");

  const remote = mainWindow({ url: "https://remote.example/chat" });
  const refusals = [
    ["a local dashboard window that is not the main window", main, main._mcView, from(otherWindow())],
    ["a subframe of the main window", main, main._mcView, from(wc, { url: DASHBOARD_URL })],
    ["the relay view", main, main._mcView, from(relayView())],
    ["the main window on another origin", remote, remote._mcView, from(remote._mcView.webContents)],
    ["a pane opened from another dashboard view", main, { webContents: { id: "gone" } }, from(wc)],
  ];
  for (const [label, win, dashboard, ev] of refusals) {
    const b = paneOpener(dashboard);
    const r = harness({ opener: b, main: win });
    await r.paneBounds(ev, ID, { x: 0, y: 0, width: 10, height: 10 }, { width: 1000, height: 800 });
    await r.paneOverlay(ev, ID, true);
    await r.paneInactive(ev, ID, true);
    await r.paneClose(ev, ID);
    assert.deepEqual(b.steer, [], label);
  }
  const never = harness({ opener: null, main });
  assert.equal(await never.paneClose(from(wc), ID), undefined);
  assert.deepEqual(never.built, [], "steering never builds an opener");
});

test("relay-crew:open-pane from another local dashboard window hands only the ID to the listening main dashboard, raised in that call", async () => {
  const main = mainWindow();
  const frame = main._mcView.webContents.mainFrame;
  const wc = Object.assign(new EventEmitter(), { id: "main-dashboard", mainFrame: frame, sent: [] });
  wc.send = (channel, ...args) => wc.sent.push([channel, ...args]);
  wc.isDestroyed = () => false;
  main._mcView.webContents = wc;
  const RECT = { x: 0, y: 48, width: 500, height: 400 };
  const VIEWPORT = { width: 500, height: 400 };
  const o = paneOpener(null);
  const h = harness({ opener: o, main });
  const other = otherWindow();
  assert.deepEqual(await h.openPane(from(other), ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" },
    "nothing listens yet: refused, not held");
  assert.deepEqual(h.raised, []);
  // Only the main window's dashboard can listen.
  await h.openRequests(from(other), true);
  assert.deepEqual(await h.openPane(from(other), ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" });
  await h.openRequests(from(wc), true);
  assert.deepEqual(await h.openPane(from(other), ID, RECT, VIEWPORT), { ok: true, window: true });
  assert.deepEqual(wc.sent, [["relay-crew:open-request", ID]]);
  assert.deepEqual(h.raised, [{ focus: true }]);
  assert.deepEqual(o.calls, [], "the asking window opens no pane itself");
  assert.deepEqual(await h.openPane(from(other, { url: DASHBOARD_URL }), ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" },
    "a subframe of another window is refused");
  // A new document drops it.
  wc.emit("did-navigate", {}, DASHBOARD_URL, 200);
  assert.deepEqual(await h.openPane(from(other), ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" },
    "a reloaded main dashboard is not handed anything until it listens again");
  await h.openRequests(from(wc), true);
  await h.openRequests(from(wc), false);
  assert.deepEqual(await h.openPane(from(other), ID, RECT, VIEWPORT), { ok: false, error: "UNAVAILABLE" }, "unsubscribed");
  assert.equal(wc.sent.length, 1);
  for (let i = 0; i < 20; i += 1) await h.openRequests(from(wc), i % 2 === 0, 100 + i);
  assert.equal(wc.listenerCount("did-navigate"), 1, "one listener however often it resubscribes");
});

test("an unsubscribe that lands after the resubscribe it preceded leaves the main dashboard listening", async () => {
  const main = mainWindow();
  const frame = main._mcView.webContents.mainFrame;
  const wc = Object.assign(new EventEmitter(), { id: "main-dashboard", mainFrame: frame, sent: [] });
  wc.send = (channel, ...args) => wc.sent.push([channel, ...args]);
  wc.isDestroyed = () => false;
  main._mcView.webContents = wc;
  const h = harness({ opener: paneOpener(null), main });
  await h.openRequests(from(wc), true, 1);
  // A remount: unsubscribe 2, subscribe 3; the gate lets 3 land first.
  await h.openRequests(from(wc), true, 3);
  await h.openRequests(from(wc), false, 2);
  assert.deepEqual(await h.openPane(from(otherWindow()), ID, { x: 0, y: 48, width: 500, height: 400 }, { width: 500, height: 400 }),
    { ok: true, window: true });
  // A new document numbers from 1 again.
  wc.emit("did-navigate", {}, DASHBOARD_URL, 200);
  await h.openRequests(from(wc), true, 1);
  assert.equal((await h.openPane(from(otherWindow()), ID, { x: 0, y: 48, width: 500, height: 400 }, { width: 500, height: 400 })).ok, true);
});
