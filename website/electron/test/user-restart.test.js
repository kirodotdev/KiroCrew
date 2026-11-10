"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { RESTARTING_STATUS, createUserRestart } = require("../runtime/gateway/user-restart");

const flush = () => new Promise((resolve) => setImmediate(resolve));
const SUPERSEDED = "superseded";
const BACKEND = "http://localhost:5476";

function fakeWindow(url = `${BACKEND}/settings/about?x=1#top`) {
  return {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    webContents: { getURL: () => url },
  };
}

function harness({
  window = fakeWindow(),
  main = undefined,
  ownership = "spawned",
  child = { exitCode: null },
  monitor = true,
  installing = false,
  quitting = false,
  respawn = true,
  failures = {},
} = {}) {
  const events = [];
  const logs = [];
  const state = {
    child,
    monitor: monitor ? { stop: () => events.push("monitor.stop") } : null,
    restarting: false,
    startFailure: { code: 0 },
    quitting,
  };
  const owner = createUserRestart({
    backendUrl: BACKEND,
    log: (message) => logs.push(message),
    mainWindow: () => (main === undefined ? window : main),
    quitting: () => state.quitting,
    isInstallingUpdate: () => installing,
    paintLoadingScreen: (target) => events.push(target === window ? "splash" : "splash:other"),
    sendStatus: (message) => events.push(`status:${message}`),
    getGatewayProcess: () => state.child,
    getGatewayOwnership: () => ownership,
    getLivenessMonitor: () => state.monitor,
    setLivenessMonitor: (value) => { state.monitor = value; },
    setRestartingGateway: (value) => { state.restarting = value; events.push(`restarting:${value}`); },
    setGatewayStartFailure: (value) => { state.startFailure = value; },
    stopGatewayGracefully: async () => {
      // The child's exit must be read as intended while the stop runs.
      events.push(`stop:restarting=${state.restarting}`);
      state.child = null;
      if (failures.stop) throw failures.stop;
    },
    respawnSuperseded: SUPERSEDED,
    respawnGateway: async () => {
      events.push("respawn");
      return respawn;
    },
    showLoadingThenConnect: async (target, url, options) => {
      events.push(`connect:${url}:${options.initialPath}`);
    },
  });
  return { owner, window, state, events, logs };
}

test("offered only to the main window, connected to a gateway this app spawned and still runs", () => {
  const h = harness();
  assert.equal(h.owner.canRestartGateway(h.window), true);

  for (const [label, options] of [
    ["connection window", { main: fakeWindow() }],
    ["adopted gateway", { ownership: "reused-local" }],
    ["service gateway", { ownership: "reused-service" }],
    ["remote crew or external holder", { ownership: "none" }],
    ["no child", { child: null }],
    ["exited child", { child: { exitCode: 0 } }],
    ["connect or recovery in flight (no armed monitor)", { monitor: false }],
    ["update install in flight", { installing: true }],
    ["quitting", { quitting: true }],
  ]) {
    const refused = harness(options);
    assert.equal(refused.owner.canRestartGateway(refused.window), false, label);
    assert.equal(refused.owner.restartGateway(refused.window), "unavailable", label);
    assert.deepEqual(refused.events, [], `${label}: nothing is stopped`);
  }
  const destroyed = harness();
  destroyed.window.destroyed = true;
  assert.equal(destroyed.owner.canRestartGateway(destroyed.window), false, "destroyed window");
  assert.equal(destroyed.owner.canRestartGateway(null), false, "no window");
});

test("stops the child with liveness disarmed, then respawns and reconnects to the same page", async () => {
  const h = harness();
  assert.equal(h.owner.restartGateway(h.window), "restarting");
  // A second click while the first restart runs is refused, not stacked.
  assert.equal(h.owner.canRestartGateway(h.window), false);
  assert.equal(h.owner.restartGateway(h.window), "unavailable");
  await flush();

  assert.deepEqual(h.events, [
    "monitor.stop",
    "restarting:true",
    "splash",
    `status:${RESTARTING_STATUS}`,
    "stop:restarting=true",
    "restarting:false",
    "respawn",
    `connect:${BACKEND}:/settings/about?x=1#top`,
  ]);
  assert.equal(h.state.monitor, null);
  assert.equal(h.state.startFailure, null, "the stopped child's exit is not this launch's failure");
});

test("a page on another origin is not carried into the reconnect", async () => {
  const h = harness({ window: fakeWindow("file:///virtual/electron/loading.html") });
  h.owner.restartGateway(h.window);
  await flush();
  assert.ok(h.events.includes(`connect:${BACKEND}:`));
});

test("a failed stop still reconnects instead of leaving the splash up", async () => {
  const h = harness({ failures: { stop: new Error("boom") } });
  h.owner.restartGateway(h.window);
  await flush();
  assert.deepEqual(h.events.slice(-3), ["restarting:false", "respawn", `connect:${BACKEND}:/settings/about?x=1#top`]);
  assert.ok(h.logs.some((line) => line.includes("stopping the gateway failed: boom")));
});

test("an update install that took over the respawn is not followed by a connect", async () => {
  const h = harness({ respawn: SUPERSEDED });
  h.owner.restartGateway(h.window);
  await flush();
  assert.equal(h.events.at(-1), "respawn");
});

test("a window closed during the stop still gets its gateway back, without a connect", async () => {
  const h = harness();
  h.owner.restartGateway(h.window);
  h.window.destroyed = true;
  await flush();
  assert.equal(h.events.at(-1), "respawn");
});

test("quitting during the stop starts nothing", async () => {
  const h = harness();
  h.owner.restartGateway(h.window);
  h.state.quitting = true;
  await flush();
  assert.ok(!h.events.includes("respawn"));
  assert.equal(h.state.restarting, false);
});

test("a finished restart can be asked for again", async () => {
  const h = harness();
  h.owner.restartGateway(h.window);
  await flush();
  h.state.child = { exitCode: null };
  h.state.monitor = { stop: () => {} };
  assert.equal(h.owner.canRestartGateway(h.window), true);
});
