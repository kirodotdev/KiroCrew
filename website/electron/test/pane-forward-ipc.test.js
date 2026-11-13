"use strict";

// remote-crew:forward-pane-ports, registered by ipc-registrar.js. In
// client-only mode the launch port's dashboard is a crew's gateway on another
// machine, and each pane it frames loads a port forwarded on THAT machine. The
// managed keeper carries those ports, asked by the launch port's dashboard
// only, and a window's ports are released when it is destroyed.
//
// The registrar's full fake runtime lives in ipc-registrar.test.js; this file
// needs only the channel, so everything but ipcMain is an inert stand-in.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const Module = require("node:module");

const MODULE_PATH = path.join(__dirname, "..", "ipc-registrar.js");
const CHANNEL = "remote-crew:forward-pane-ports";
const REFUSED = { message: `${CHANNEL} is restricted to the launch port's dashboard` };

/** Any property, any call: answers with another stand-in, never a thenable. */
function inert() {
  const fn = function () { return inert(); };
  return new Proxy(fn, {
    get: (_target, key) => (key === "then" || typeof key === "symbol" ? undefined : inert()),
  });
}

function loadRegistrar() {
  const originalLoad = Module._load;
  Module._load = function loadWithFakes(request, parent, isMain) {
    if (request === "electron") throw new Error("these tests must never load real Electron");
    if ((request === "./global-hotkey" || request === "./auto-update") && parent?.filename === MODULE_PATH) {
      return inert();
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

function harness() {
  const handlers = new Map();
  const calls = [];
  const gateway = {
    forwardPanePorts: (owner, ports) => {
      calls.push(["forward", owner, ports]);
      return Promise.resolve({ 7778: "up" });
    },
    releasePaneForwards: (owner) => calls.push(["release", owner]),
  };
  const createIpcRegistrar = loadRegistrar();
  const registrar = createIpcRegistrar({
    electron: {
      ipcMain: { handle: (channel, fn) => handlers.set(channel, fn), on: () => {} },
      app: inert(),
      dialog: inert(),
      Notification: inert(),
      shell: inert(),
      webContents: inert(),
      clipboard: inert(),
    },
    store: inert(),
    windows: inert(),
    gateway,
    backendUrl: "http://localhost:5476",
    port: 5476,
    detectWsl: async () => ({}),
  });
  registrar.registerShell();
  return { handler: handlers.get(CHANNEL), calls };
}

function launchPortEvent(url = "http://localhost:5476/") {
  const frame = { url };
  const sender = new EventEmitter();
  sender.id = 41;
  sender.mainFrame = frame;
  return { sender, senderFrame: frame };
}

test("the launch port's dashboard has its pane ports forwarded, and released when it closes", async () => {
  const { handler, calls } = harness();
  const event = launchPortEvent();
  assert.deepEqual(await handler(event, [7778]), { 7778: "up" });
  await handler(event, [7778, 7779]);
  assert.deepEqual(calls, [["forward", 41, [7778]], ["forward", 41, [7778, 7779]]]);
  event.sender.emit("destroyed");
  assert.deepEqual(calls.at(-1), ["release", 41]);
  assert.equal(event.sender.listenerCount("destroyed"), 0, "one release hook per window");
});

test("pane forwards are refused to a subframe or another origin", async () => {
  for (const event of [
    { ...launchPortEvent(), senderFrame: { url: "http://localhost:5476/" } }, // not the top frame
    launchPortEvent("http://localhost:7778/"), // a pane's own origin
    launchPortEvent("http://localhost:6100/"), // another port's window
    launchPortEvent("about:blank"),
  ]) {
    const { handler, calls } = harness();
    await assert.rejects(() => handler(event, [7778]), REFUSED);
    assert.deepEqual(calls, []);
  }
});
