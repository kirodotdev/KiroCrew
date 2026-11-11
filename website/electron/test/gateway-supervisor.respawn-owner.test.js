"use strict";

// One owner for an in-flight gateway respawn. Liveness recovery, the failure
// dialog's retries and the recovery after a failed update install all respawn
// through it, so they cannot start two launches at once, and none of them
// starts a backend under an update install. The supervisor cases run the real
// supervisor on a Windows harness: netstat answers with no owner, the bind
// check runs against a real free loopback port, and the backend probe can be
// held open so a launch stays inside startGateway's own async probes.

const { EventEmitter } = require("node:events");
const net = require("node:net");
const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness, fakeDialogWindows, fakeTimers } = require("./gateway-supervisor-test-helpers");
const { createRespawnOwner, RESPAWN_SUPERSEDED } = require("../runtime/gateway/respawn-owner");

// ---------------------------------------------------------------------------
// The owner on its own.
// ---------------------------------------------------------------------------

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function ownerHarness() {
  const state = { epoch: 0, installing: false, launches: [] };
  const { respawnGateway } = createRespawnOwner({
    startGateway: ({ isStale }) => {
      const launch = { ...deferred(), isStale };
      state.launches.push(launch);
      return launch.promise;
    },
    getInstallEpoch: () => state.epoch,
    isInstallingUpdate: () => state.installing,
  });
  return { state, respawnGateway };
}

test("a second respawn asked for while one is in flight joins it", async () => {
  const { state, respawnGateway } = ownerHarness();
  const first = respawnGateway();
  const second = respawnGateway();
  assert.strictEqual(state.launches.length, 1, "one launch for both callers");
  state.launches[0].resolve(true);
  assert.deepStrictEqual(await Promise.all([first, second]), [true, true]);

  // Settled, so the next respawn is a launch of its own.
  const third = respawnGateway();
  assert.strictEqual(state.launches.length, 2);
  state.launches[1].resolve(false);
  assert.strictEqual(await third, false);
});

test("no launch starts while an update install is in flight", async () => {
  const { state, respawnGateway } = ownerHarness();
  state.installing = true;
  assert.strictEqual(await respawnGateway(), RESPAWN_SUPERSEDED);
  assert.strictEqual(state.launches.length, 0);
});

test("a launch overtaken by an install is superseded, and a failed install's respawn runs after it", async () => {
  const { state, respawnGateway } = ownerHarness();
  const stale = respawnGateway();
  const { isStale } = state.launches[0];
  assert.strictEqual(isStale(), false);

  // An install is dispatched and fails; its recovery asks for a respawn.
  state.epoch += 1;
  assert.strictEqual(isStale(), true, "the running launch now sees itself as stale");
  const fresh = respawnGateway();
  await flush();
  assert.strictEqual(state.launches.length, 1, "the fresh launch waits for the stale one to settle");

  state.launches[0].resolve(false);
  assert.strictEqual(await stale, RESPAWN_SUPERSEDED, "the stale caller must not connect");
  await flush();
  assert.strictEqual(state.launches.length, 2, "then the fresh launch starts");
  assert.strictEqual(state.launches[1].isStale(), false);
  state.launches[1].resolve(true);
  assert.strictEqual(await fresh, true);
});

// ---------------------------------------------------------------------------
// The three interleavings, on the real supervisor.
// ---------------------------------------------------------------------------

async function freePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ port: 0, host: "127.0.0.1" }, resolve);
  });
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

// Refuses every request at once, or, while held, keeps each one open until
// release() refuses them all. A held request is a launch parked inside its
// first backend probe.
function holdableHttp() {
  const held = [];
  let holding = false;
  const refuse = (request) => request.emit("error", new Error("connection refused"));
  return {
    held,
    hold() { holding = true; },
    release() {
      holding = false;
      for (const request of held.splice(0)) refuse(request);
    },
    get() {
      const request = new EventEmitter();
      request.destroy = () => {};
      if (holding) held.push(request);
      else queueMicrotask(() => refuse(request));
      return request;
    },
  };
}

function emptyNetstat(file, _args, _options, callback) {
  if (String(file).toLowerCase().includes("netstat")) {
    callback(null, "", "");
    return;
  }
  callback(new Error(`unexpected command: ${file}`));
}

function fakeAppWindow() {
  const window = {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    destroy() { this.destroyed = true; },
    isMinimized: () => false,
    restore() {},
    show() {},
    focus() {},
    loadedFiles: [],
    webContents: {
      isDestroyed: () => false,
      loadFile(file) { window.loadedFiles.push(String(file)); },
      loadURL() {},
      send() {},
    },
  };
  return window;
}

function windowsHarness(port) {
  const http = holdableHttp();
  const dialogs = fakeDialogWindows();
  const mainWindow = fakeAppWindow();
  const timers = fakeTimers();
  const built = harness({
    port,
    mainWindow,
    timers,
    httpMod: http,
    BrowserWindow: dialogs.FakeBrowserWindow,
    execFileFn: emptyNetstat,
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
  });
  return { ...built, http, dialogs, mainWindow, timers };
}

// Let promises run and fire every one-shot timer recovery arms, until nothing
// new is pending. A held backend probe arms no timer, so a launch parked there
// stays parked.
async function settle(timers) {
  for (let round = 0; round < 50; round += 1) {
    for (let i = 0; i < 5; i += 1) await flush();
    if (!timers.pending.length) return;
    for (const timer of timers.pending.splice(0)) timer.fn();
  }
}

// Boot spawns a gateway this app owns, which is what recovery respawns. The
// child is marked exited so the tree kill does not ask the real OS about it.
async function bootOwnGateway(built) {
  assert.strictEqual(await built.supervisor.start(), true);
  built.timers.pending.splice(0);
  for (const call of built.spawnCalls) call.child.exitCode = 1;
}

test("an update install dispatched while recovery is inside startGateway's probes stops the spawn", async () => {
  const built = windowsHarness(await freePort());
  await bootOwnGateway(built);
  const before = built.spawnCalls.length;

  // A failed install hands the window to recovery, which force-stops the free
  // port and starts a gateway. Its first backend probe is held open.
  built.http.hold();
  built.supervisor.onInstallDispatched();
  built.supervisor.onInstallFailed(built.mainWindow);
  await settle(built.timers);
  assert.strictEqual(built.http.held.length, 1, `the respawn is parked in its probe:\n${built.logs.join("\n")}`);

  // A second install is dispatched while that probe is still open.
  built.supervisor.onInstallDispatched();
  built.http.release();
  await settle(built.timers);
  built.mainWindow.destroyed = true;

  assert.strictEqual(built.spawnCalls.length - before, 0, `no gateway races the install:\n${built.logs.join("\n")}`);
  assert.ok(
    built.logs.some((l) => l.includes("an update install was dispatched while this start was in flight")),
    built.logs.join("\n"),
  );
});

// Boot into the failure dialog on a second window: the gateway this app spawned
// exits, and a connect then finds its exit record and opens the dialog.
async function openFailureDialog(built) {
  await bootOwnGateway(built);
  built.spawnCalls[0].child.emit("exit", 1, null);
  const tab = fakeAppWindow();
  const connected = built.supervisor.connect(tab);
  await settle(built.timers);
  assert.strictEqual(built.dialogs.windows.length, 1, `the failure dialog opened:\n${built.logs.join("\n")}`);
  return { tab, connected, dialog: built.dialogs.windows[0] };
}

for (const action of ["retry", "force-retry", "enable-retry", "configure-remote"]) {
  test(`an update install dispatched while the failure dialog is open stops a later ${action}`, async () => {
    const built = windowsHarness(await freePort());
    const { tab, dialog } = await openFailureDialog(built);
    const before = built.spawnCalls.length;

    built.supervisor.onInstallDispatched();
    dialog.click(action);
    await settle(built.timers);

    assert.strictEqual(built.spawnCalls.length - before, 0, `no gateway races the install:\n${built.logs.join("\n")}`);
    assert.ok(
      built.logs.some((l) => l.includes(`${action}: an update install is in flight`)),
      built.logs.join("\n"),
    );
    assert.strictEqual(built.dialogs.windows.length, 2, "the dialog stays, so a Retry after a failed install still works");
    assert.strictEqual(built.store.data.remoteHosts, undefined, "no crew form was opened or saved");
    built.dialogs.windows[1].click("quit");
    await settle(built.timers);
    tab.destroyed = true;
  });
}

test("a dialog Retry overtaken by an install mid-launch spawns nothing and keeps the dialog", async () => {
  const built = windowsHarness(await freePort());
  const { tab, dialog } = await openFailureDialog(built);
  const before = built.spawnCalls.length;

  // The Retry's own launch parks in its first backend probe, and an install
  // is dispatched while it is there.
  built.http.hold();
  dialog.click("retry");
  await settle(built.timers);
  assert.strictEqual(built.http.held.length, 1, `the Retry's launch is parked in its probe:\n${built.logs.join("\n")}`);
  built.supervisor.onInstallDispatched();
  built.http.release();
  await settle(built.timers);

  assert.strictEqual(built.spawnCalls.length - before, 0, `no gateway races the install:\n${built.logs.join("\n")}`);
  assert.ok(
    built.logs.some((l) => l.includes("retry: an update install took over this start")),
    built.logs.join("\n"),
  );
  // A failed install's recovery reconnects only the main window, so this one
  // gets its dialog back rather than a splash nothing will clear.
  assert.strictEqual(built.dialogs.windows.length, 2, "the dialog is shown again");
  built.dialogs.windows[1].click("quit");
  await settle(built.timers);
  tab.destroyed = true;
  built.mainWindow.destroyed = true;
});

test("a dialog Retry while a recovery is already starting a gateway joins it, so one gateway is spawned", async () => {
  const built = windowsHarness(await freePort());
  const { tab, dialog } = await openFailureDialog(built);
  const before = built.spawnCalls.length;

  // A failed install's recovery reaches startGateway and parks in its probe;
  // the slot it fills is still empty, so the dialog cannot see it running.
  built.http.hold();
  built.supervisor.onInstallDispatched();
  built.supervisor.onInstallFailed(built.mainWindow);
  await settle(built.timers);
  assert.strictEqual(built.http.held.length, 1, `recovery is parked in its probe:\n${built.logs.join("\n")}`);

  dialog.click("retry");
  await settle(built.timers);
  assert.strictEqual(built.http.held.length, 1, "the Retry started no probe of its own");
  assert.ok(built.logs.some((l) => l.includes("respawn: a respawn is already in flight; joining it")), built.logs.join("\n"));

  built.http.release();
  await settle(built.timers);
  built.mainWindow.destroyed = true;
  tab.destroyed = true;

  assert.strictEqual(built.spawnCalls.length - before, 1, `exactly one gateway is spawned:\n${built.logs.join("\n")}`);
});

// A spawn that fails with only an `error` event (ENOENT for a launcher that is not
// on PATH: Node emits no `exit` after it) must leave no child behind, or the
// dialog's Retry reads the dead child as running and starts nothing.
test("a dialog Retry after a spawn error with no exit starts a new gateway", async () => {
  const built = windowsHarness(await freePort());
  await bootOwnGateway(built);
  const enoent = Object.assign(new Error("spawn kirocrew ENOENT"), { code: "ENOENT" });
  built.spawnCalls[0].child.emit("error", enoent);

  const tab = fakeAppWindow();
  built.supervisor.connect(tab);
  await settle(built.timers);
  assert.strictEqual(built.dialogs.windows.length, 1, `the failure dialog opened:\n${built.logs.join("\n")}`);
  const before = built.spawnCalls.length;

  built.dialogs.windows[0].click("retry");
  await settle(built.timers);

  assert.strictEqual(built.spawnCalls.length - before, 1, `the Retry spawned a gateway:\n${built.logs.join("\n")}`);
  tab.destroyed = true;
  built.mainWindow.destroyed = true;
});
