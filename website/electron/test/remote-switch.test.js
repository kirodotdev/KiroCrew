"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  PROMPT_SAVE_LABEL,
  PROMPT_TITLE,
  STOP_FAILED_MESSAGE,
  SWITCHING_STATUS,
  createRemoteSwitch,
} = require("../runtime/gateway/remote-switch");

const flush = () => new Promise((resolve) => setImmediate(resolve));

function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get: (key, fallback) => (Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback),
    set: (key, value) => { data[key] = value; },
  };
}

function fakeWindow() {
  const loads = [];
  return {
    loads,
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    webContents: { loadFile: (...args) => loads.push(args) },
  };
}

const CREW = { host: "devbox.example.com", binPath: "", remotePort: "", remotePath: "", manageTunnel: true };

/**
 * The owner with every dependency recorded. `forms` is the sequence of answers
 * the crew form gives, one per opening.
 */
function harness({
  store = fakeStore({ runLocalGateway: true, remoteHosts: {} }),
  port = 5476,
  window = fakeWindow(),
  main = undefined,
  ownership = "spawned",
  child = { exitCode: null },
  forms = [CREW],
  freed = true,
  isWindows = false,
  quitting = false,
} = {}) {
  const events = [];
  const state = {
    ownership,
    child,
    monitor: { stop: () => events.push("monitor.stop") },
    switching: false,
    runLocalGateway: true,
    startFailure: { code: 0 },
  };
  const prompts = [];
  const dialogs = [];
  const owner = createRemoteSwitch({
    store,
    port,
    backendUrl: `http://localhost:${port}`,
    dialog: {
      showMessageBox: async (parent, options) => { dialogs.push(options); events.push(`dialog:${options.title}`); },
    },
    path,
    dirname: "/virtual/electron",
    isWindows,
    log: () => {},
    mainWindow: () => (main === undefined ? window : main),
    quitting: () => quitting,
    promptRemoteCrew: async (parent, promptPort, draft, labels) => {
      prompts.push({ parent, promptPort, draft, labels });
      events.push("prompt");
      return forms.shift() ?? null;
    },
    splashQuery: () => ({ primary: "1" }),
    currentThemeAccent: () => "#8E48FF",
    sendStatus: (message) => events.push(`status:${message}`),
    getGatewayProcess: () => state.child,
    getGatewayOwnership: () => state.ownership,
    setGatewayOwnership: (value) => { state.ownership = value; },
    getLivenessMonitor: () => state.monitor,
    setLivenessMonitor: (value) => { state.monitor = value; },
    setSwitchingToRemote: (value) => { state.switching = value; events.push(`switching:${value}`); },
    setRunLocalGateway: (value) => { state.runLocalGateway = value; },
    setGatewayStartFailure: (value) => { state.startFailure = value; },
    stopGatewayGracefully: async () => {
      // A crew must not be on the port while our own gateway still holds it.
      events.push(`stop:crew=${store.get("remoteHosts", {})[String(port)]?.host || ""}`);
      state.child = null;
    },
    waitForPortFree: async () => { events.push("waitForPortFree"); return freed; },
    startGateway: async () => { events.push(`startGateway:local=${state.runLocalGateway}`); },
    showLoadingThenConnect: async (target, url) => { events.push(`connect:${url}`); },
  });
  return { owner, store, window, state, events, prompts, dialogs };
}

test("offered only to the main window on a gateway this app spawned and still runs", () => {
  const h = harness();
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true);

  for (const [label, options] of [
    ["connection window", { main: fakeWindow() }],
    ["adopted gateway", { ownership: "reused-local" }],
    ["service gateway", { ownership: "reused-service" }],
    ["external holder", { ownership: "none" }],
    ["no child", { child: null }],
    ["exited child", { child: { exitCode: 1 } }],
    ["quitting", { quitting: true }],
    ["unselectable port", { port: 80 }],
    ["crew already on the port", {
      store: fakeStore({ remoteHosts: { 5476: { host: "elsewhere" } } }),
    }],
  ]) {
    const c = harness(options);
    assert.equal(c.owner.canSwitchToRemoteCrew(c.window), false, label);
  }
  const destroyed = harness();
  destroyed.window.destroyed = true;
  assert.equal(destroyed.owner.canSwitchToRemoteCrew(destroyed.window), false, "destroyed window");
});

test("a refused window gets no form", async () => {
  const h = harness({ ownership: "reused-local" });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "unavailable");
  assert.deepEqual(h.events, []);
});

test("a closed form changes nothing", async () => {
  const h = harness({ forms: [null] });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "cancelled");
  await flush();
  assert.deepEqual(h.events, ["prompt"]);
  assert.deepEqual(h.store.data.remoteHosts, {});
  assert.equal(h.store.data.runLocalGateway, true);
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true, "the exit stays available");
});

test("the form opens with the tunnel option on, under first-run wording", async () => {
  const h = harness({ forms: [null] });
  await h.owner.switchToRemoteCrew(h.window);
  assert.equal(h.prompts[0].promptPort, 5476);
  assert.equal(h.prompts[0].draft.manageTunnel, true);
  assert.deepEqual(h.prompts[0].labels, { title: PROMPT_TITLE, saveLabel: PROMPT_SAVE_LABEL });

  const win = harness({ forms: [null], isWindows: true });
  await win.owner.switchToRemoteCrew(win.window);
  assert.equal(win.prompts[0].draft.manageTunnel, false, "Windows has no managed tunnel");
});

test("an invalid field reopens the form on what was typed and stops nothing", async () => {
  const bad = { ...CREW, host: "not a host!" };
  const h = harness({ forms: [bad, null] });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "cancelled");
  await flush();
  assert.deepEqual(h.events, ["prompt", "dialog:Invalid Input", "prompt"]);
  assert.equal(h.prompts[1].draft.host, "not a host!");
  assert.deepEqual(h.store.data.remoteHosts, {});
});

test("a saved form stops the local gateway, then records the crew and connects to it", async () => {
  const h = harness();
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "switching");
  await flush();
  await flush();

  assert.deepEqual(h.events, [
    "prompt",
    "monitor.stop",
    "switching:true",
    `status:${SWITCHING_STATUS}`,
    "stop:crew=",
    "waitForPortFree",
    "switching:false",
    "startGateway:local=false",
    "connect:http://localhost:5476",
  ]);
  assert.equal(h.window.loads.length, 1);
  assert.equal(h.window.loads[0][0], path.join("/virtual/electron", "loading.html"));
  assert.equal(h.store.data.remoteHosts["5476"].host, "devbox.example.com");
  assert.equal(h.store.data.remoteHosts["5476"].manageTunnel, true);
  assert.equal(h.store.data.runLocalGateway, false, "the setting is off for later launches too");
  assert.equal(h.state.runLocalGateway, false);
  assert.equal(h.state.ownership, "none", "nothing on the port is this app's any more");
  assert.equal(h.state.monitor, null);
  assert.equal(h.state.startFailure, null, "the stopped child's exit is not a start failure");
});

test("a port that does not free keeps the local gateway and says so", async () => {
  const h = harness({ freed: false });
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "switching");
  await flush();
  await flush();

  assert.deepEqual(h.store.data.remoteHosts, {}, "no crew is recorded over our own gateway");
  assert.equal(h.store.data.runLocalGateway, true);
  assert.equal(h.dialogs.at(-1).message, STOP_FAILED_MESSAGE);
  assert.deepEqual(h.events.slice(-2), ["startGateway:local=true", "connect:http://localhost:5476"]);
});

test("one switch at a time", async () => {
  let closeForm;
  const h = harness({ forms: [new Promise((resolve) => { closeForm = resolve; })] });
  const first = h.owner.switchToRemoteCrew(h.window);
  await flush();
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), false, "not while the form is open");
  assert.equal(await h.owner.switchToRemoteCrew(h.window), "unavailable");
  closeForm(null);
  assert.equal(await first, "cancelled");
  assert.equal(h.prompts.length, 1, "the second request opened no form");
  assert.equal(h.owner.canSwitchToRemoteCrew(h.window), true);
});

test("a gateway that exits while the form is open is not replaced", async () => {
  let closeForm;
  const h = harness({ forms: [new Promise((resolve) => { closeForm = resolve; })] });
  const pending = h.owner.switchToRemoteCrew(h.window);
  await flush();
  h.state.child = { exitCode: 1 };
  closeForm(CREW);
  assert.equal(await pending, "unavailable");
  await flush();
  assert.deepEqual(h.store.data.remoteHosts, {});
  assert.ok(!h.events.some((e) => e.startsWith("stop:")), "nothing was stopped");
});
