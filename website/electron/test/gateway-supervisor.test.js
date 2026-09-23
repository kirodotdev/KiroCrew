"use strict";

const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { test, mock } = require("node:test");
const assert = require("node:assert");

const MODULE_PATH = path.join(__dirname, "..", "gateway-supervisor.js");
const { createGatewaySupervisor } = require(MODULE_PATH);

function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(key, fallback) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
    },
    set(key, value) { data[key] = value; },
  };
}

function rejectingHttp(onGet = () => {}) {
  return {
    get(url) {
      onGet(url);
      const request = new EventEmitter();
      request.destroy = () => {};
      // Defer until production has attached its error listener. No socket, port,
      // timer, or host input is involved.
      queueMicrotask(() => request.emit("error", new Error("connection refused")));
      return request;
    },
  };
}

// An http fake whose answer can change mid-test: `state.status` null refuses the
// connection, a number answers with that status and `state.body`. Requests are
// recorded so a test can prove which endpoint was probed.
function switchableHttp(state) {
  const requests = [];
  return {
    requests,
    get(url, _options, callback) {
      requests.push(url);
      const request = new EventEmitter();
      request.destroy = () => {};
      queueMicrotask(() => {
        if (state.status === null) {
          request.emit("error", new Error("connection refused"));
          return;
        }
        const response = new EventEmitter();
        response.statusCode = state.status;
        response.resume = () => {};
        callback(response);
        response.emit("data", state.body || "");
        response.emit("end");
      });
      return request;
    },
  };
}

// Timers the supervisor schedules, held instead of run so a test can fire the
// one it means (by delay) or prove none is left armed.
function fakeTimers() {
  const pending = [];
  let nextId = 1;
  return {
    pending,
    setTimeoutFn(fn, ms) {
      const id = nextId;
      nextId += 1;
      pending.push({ id, fn, ms });
      return id;
    },
    clearTimeoutFn(id) {
      const index = pending.findIndex((timer) => timer.id === id);
      if (index >= 0) pending.splice(index, 1);
    },
    fire(ms) {
      const index = pending.findIndex((timer) => timer.ms === ms);
      assert.ok(index >= 0, `a ${ms}ms timer is armed`);
      const [timer] = pending.splice(index, 1);
      timer.fn();
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(overrides = {}) {
  const logs = [];
  const warnings = [];
  const errors = [];
  const spawnCalls = [];
  const store = overrides.store || fakeStore();
  const mainWindow = overrides.mainWindow || null;
  const port = overrides.port ?? 5476;
  const processRef = overrides.processRef || {
    platform: "test",
    arch: "x64",
    env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
    resourcesPath: "/virtual/resources",
    kill() { throw new Error("process kill must not run in this harness"); },
  };
  const fsMod = overrides.fsMod || {
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync() {
      const error = new Error("not found");
      error.code = "ENOENT";
      throw error;
    },
    existsSync() { return false; },
    openSync() { return 41; },
    closeSync() {},
    readFileSync() { throw new Error("unexpected filesystem read"); },
  };

  const supervisor = createGatewaySupervisor({
    app: {
      isPackaged: false,
      getVersion: () => "0.6.0",
      quit: () => {},
      focus: () => {},
      ...(overrides.app || {}),
    },
    store,
    BrowserWindow: class {},
    nativeTheme: { shouldUseDarkColors: false },
    dialog: overrides.dialog
      || { showMessageBox: async () => ({ response: 1 }) },
    shell: { showItemInFolder: () => {} },
    ipcMain: { on: () => {}, removeListener: () => {} },
    port,
    backendUrl: overrides.backendUrl || `http://localhost:${port}`,
    home: "/virtual/kirocrew-home",
    getMainWindow: () => mainWindow,
    isQuitting: overrides.isQuitting || (() => false),
    requestQuit: overrides.requestQuit || (() => {}),
    cancelPendingTrayHide: () => {},
    exitImmersiveModes: () => {},
    log: (message) => logs.push(message),
    warn: (message) => {
      logs.push(message);
      warnings.push(message);
    },
    error: (message) => {
      logs.push(message);
      errors.push(message);
    },
    logPath: () => "/virtual/logs/gateway-launch.log",
    fsMod,
    osMod: { homedir: () => "/virtual/home" },
    pathMod: path.posix,
    httpMod: overrides.httpMod || rejectingHttp(),
    spawnFn: (...args) => {
      const child = new EventEmitter();
      child.pid = 1234;
      child.exitCode = null;
      child.killed = false;
      child.kill = () => { child.killed = true; };
      child.unref = () => { child.unrefed = true; };
      const call = [...args];
      call.child = child;
      spawnCalls.push(call);
      return child;
    },
    execFileFn: overrides.execFileFn
      || (() => { throw new Error("execFile must not run in this harness"); }),
    execFileSyncFn: () => { throw new Error("execFileSync must not run in this harness"); },
    setTimeoutFn: overrides.timers ? overrides.timers.setTimeoutFn : undefined,
    clearTimeoutFn: overrides.timers ? overrides.timers.clearTimeoutFn : undefined,
    processRef,
    dirname: "/virtual/electron",
  });

  return { supervisor, store, logs, warnings, errors, spawnCalls, fsMod };
}

test("module has no top-level Electron dependency and its factory accepts fakes", () => {
  const source = fs.readFileSync(MODULE_PATH, "utf8");
  assert.doesNotMatch(
    source,
    /require\(\s*["']electron["']\s*\)/,
    "node:test must be able to load the supervisor without an Electron runtime",
  );

  const { supervisor } = harness();
  assert.deepStrictEqual(Object.keys(supervisor), [
    "start",
    "connect",
    "fetchLocalToken",
    "fetchRemoteToken",
    "entryUrl",
    "probePrimaryPortOwner",
    "stopGracefully",
    "stopOnQuit",
    "onInstallDispatched",
    "onInstallFailed",
  ]);
});

test("probePrimaryPortOwner probes only the injected primary port", async () => {
  const execCalls = [];
  const { supervisor } = harness({
    port: 6123,
    execFileFn(file, args, options, callback) {
      execCalls.push({ file, args, options });
      callback(null, "", "");
    },
  });

  assert.strictEqual(supervisor.probePrimaryPortOwner.length, 0);
  assert.strictEqual(
    await supervisor.probePrimaryPortOwner(65535),
    "none",
  );
  assert.strictEqual(execCalls.length, 1);
  assert.deepStrictEqual(
    execCalls[0].args,
    ["-nP", "-iTCP:6123", "-sTCP:LISTEN", "-t"],
  );
  assert.ok(!execCalls[0].args.some((arg) => String(arg).includes("65535")));
});

test("entryUrl preserves an initial path/query and encodes the token once", () => {
  const { supervisor } = harness();
  const result = new URL(supervisor.entryUrl(
    "http://localhost:5476",
    "/chat?new=1",
    "token with spaces & punctuation?",
  ));

  assert.strictEqual(result.origin, "http://localhost:5476");
  assert.strictEqual(result.pathname, "/chat");
  assert.strictEqual(result.searchParams.get("new"), "1");
  assert.strictEqual(
    result.searchParams.get("token"),
    "token with spaces & punctuation?",
  );
  assert.strictEqual(result.searchParams.getAll("token").length, 1);
});

test("entryUrl omits the token parameter when no token is available", () => {
  const { supervisor } = harness();
  const result = new URL(supervisor.entryUrl("http://localhost:5476", "/settings"));

  assert.strictEqual(result.pathname, "/settings");
  assert.strictEqual(result.searchParams.has("token"), false);
});

test("disabled local gateway does not spawn when the backend is unreachable", async () => {
  let probes = 0;
  const { supervisor, spawnCalls, logs } = harness({
    store: fakeStore({ runLocalGateway: false }),
    httpMod: rejectingHttp(() => { probes += 1; }),
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(probes, 1);
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(
    logs.some((line) => line.includes("local gateway is off — not starting one")),
  );
});

test("AppImage sandbox advice uses the user-facing warning channel", async () => {
  const baseFs = harness().fsMod;
  const { supervisor, logs, warnings } = harness({
    processRef: {
      platform: "linux",
      arch: "x64",
      env: {
        APPIMAGE: "/virtual/Kiro Crew.AppImage",
        KIROCREW_HOME: "/virtual/kirocrew-home",
      },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    fsMod: {
      ...baseFs,
      readFileSync(file) {
        if (file === "/proc/sys/kernel/apparmor_restrict_unprivileged_userns") {
          return "1\n";
        }
        throw new Error("unexpected filesystem read");
      },
    },
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(warnings.length, 2);
  assert.ok(warnings[0].includes("WARN agent sandbox will fail closed"));
  assert.ok(warnings[1].includes("HINT run this in a terminal"));
  assert.ok(logs.includes(warnings[0]));
  assert.ok(logs.includes(warnings[1]));
});

test("spawn errors use the user-facing error channel", async () => {
  const { supervisor, spawnCalls, errors } = harness();

  assert.strictEqual(await supervisor.start(), true);
  const error = Object.assign(new Error("missing executable"), { code: "ENOENT" });
  spawnCalls[0].child.emit("error", error);

  assert.ok(errors.some((line) => line.includes("spawn ERROR code=ENOENT")));
});

test("unexpected child exits are visible but quit exits stay file-only", async () => {
  const unexpected = harness();
  assert.strictEqual(await unexpected.supervisor.start(), true);
  unexpected.spawnCalls[0].child.emit("exit", 1, null);
  assert.ok(
    unexpected.errors.some((line) => line.includes("gateway child exited code=1")),
  );

  const quitting = harness({ isQuitting: () => true });
  assert.strictEqual(await quitting.supervisor.start(), true);
  quitting.spawnCalls[0].child.emit("exit", 0, "SIGTERM");
  assert.strictEqual(quitting.errors.length, 0);
  assert.ok(
    quitting.logs.some((line) => line.includes("gateway child exited code=0 signal=SIGTERM")),
  );
});

test("stopGracefully is a filesystem-free no-op when no child exists", async () => {
  let reads = 0;
  const baseFs = harness().fsMod;
  const { supervisor } = harness({
    fsMod: {
      ...baseFs,
      readFileSync() {
        reads += 1;
        throw new Error("no child means secrets must not be read");
      },
    },
  });

  await supervisor.stopGracefully();
  assert.strictEqual(reads, 0);
});

test("install-failure recovery hook is armed once per dispatch", async () => {
  const destroyedWindow = {
    isDestroyed: () => true,
    webContents: {},
  };
  const { supervisor, logs } = harness({ mainWindow: destroyedWindow });

  // A random updater error before dispatch must not enter gateway recovery.
  supervisor.onInstallFailed(destroyedWindow);
  assert.strictEqual(
    logs.filter((line) => line.includes("restoring gateway")).length,
    0,
  );

  supervisor.onInstallDispatched();
  supervisor.onInstallFailed(destroyedWindow);
  supervisor.onInstallFailed(destroyedWindow);
  // recoverWedgedGateway exits at the destroyed-window guard; one microtask lets
  // its already-resolved promise and attached catch settle deterministically.
  await Promise.resolve();

  assert.strictEqual(
    logs.filter((line) => line.includes("restoring gateway")).length,
    1,
  );
});

// A macOS app whose bundled backend sits at the usual resourcesPath layout. The
// `pruned` flag flips the bundle out from under the supervisor mid-test, the
// way an in-place update does; the fs then reports ENOENT for every bundled
// candidate and findKirocrewBin falls through to the bare PATH name. Whether
// the app's own executable survives is separate (`appExecutableGone`): a swap
// leaves a new one at the same path, a prune takes it too.
const APP_EXEC_PATH = "/virtual/Applications/KiroCrew.app/Contents/MacOS/KiroCrew";
const APP_ARGV = [APP_EXEC_PATH, "--some-flag"];

function staleBundleHarness({ platform = "darwin", appExecutableGone = false } = {}) {
  const state = {
    pruned: false,
    exits: [],
    execProbes: [],
    lockReleases: 0,
    lockRequests: 0,
    statuses: [],
    // What the gateway port answers: silent until a test brings a successor's
    // gateway up.
    http: { status: null, body: "" },
  };
  const fsMod = {
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync(target) {
      if (target === APP_EXEC_PATH) {
        state.execProbes.push(target);
        if (!appExecutableGone) return;
      } else if (!state.pruned && target.includes("backend-dist")) {
        return;
      }
      const error = new Error("not found");
      error.code = "ENOENT";
      throw error;
    },
    existsSync() { return false; },
    openSync() { return 41; },
    closeSync() {},
    readFileSync() { throw new Error("unexpected filesystem read"); },
  };
  const timers = fakeTimers();
  const httpMod = switchableHttp(state.http);
  const built = harness({
    fsMod,
    httpMod,
    timers,
    mainWindow: {
      isDestroyed: () => false,
      webContents: { send: (channel, message) => state.statuses.push(`${channel}:${message}`) },
    },
    processRef: {
      platform,
      arch: "x64",
      execPath: APP_EXEC_PATH,
      argv: APP_ARGV,
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    app: {
      // No `relaunch` on purpose: app.relaunch() cannot report a failed re-exec,
      // so the supervisor must never reach for it on this path.
      releaseSingleInstanceLock() { state.lockReleases += 1; },
      requestSingleInstanceLock() { state.lockRequests += 1; return true; },
      exit(code) { state.exits.push(code); },
    },
  });
  return { ...built, state, timers, requests: httpMod.requests };
}

// The successor spawn the supervisor issues when it decides to restart the app.
function successorCall(spawnCalls) {
  return spawnCalls.find((call) => call[0] === APP_EXEC_PATH);
}

// Drive a stale-bundle harness to the point where a successor copy of the app
// has been exec'd and the supervisor is waiting for its gateway.
async function spawnedSuccessor(built) {
  const { supervisor, spawnCalls, state } = built;
  await supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  spawnCalls[1].child.emit("exit", 75, null);
  const successor = successorCall(spawnCalls);
  assert.ok(successor, "a successor copy of this app is spawned");
  successor.child.emit("spawn");
  await flush();
  assert.deepStrictEqual(state.exits, [], "exec success alone must not exit this instance");
  return successor;
}

const SUCCESSOR_READY_TIMEOUT_MS = 60_000;
const SUCCESSOR_POLL_MS = 500;

const BUNDLED_BIN = "/virtual/resources/backend-dist/kirocrew-backend-x64/bin/kirocrew";

test("a bundled gateway that exits with the stale-asset status is respawned from a fresh probe", async () => {
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness();

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls.length, 1);
  assert.strictEqual(spawnCalls[0][0], BUNDLED_BIN);

  // The update swapped the bundle at the same path: the probe still finds it.
  const first = spawnCalls[0].child;
  first.exitCode = 75;
  first.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.length, 2);
  assert.strictEqual(spawnCalls[1][0], BUNDLED_BIN);
  assert.ok(logs.some((line) => line.includes("stale bundle (exit 75") && line.includes("attempt 1")));
  assert.strictEqual(successorCall(spawnCalls), undefined);
  assert.deepStrictEqual(state.exits, []);
});

test("a second stale exit starts a fresh copy of the app and exits only once its gateway is serving", async () => {
  const built = staleBundleHarness();
  const { spawnCalls, logs, state, timers, requests } = built;

  await built.supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  assert.strictEqual(spawnCalls.length, 2);

  spawnCalls[1].child.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.filter((call) => call[0] !== APP_EXEC_PATH).length, 2,
    "the budget is one backend re-resolve per incident");
  assert.ok(state.execProbes.length >= 1, "the app executable is probed before restarting");
  const successor = successorCall(spawnCalls);
  assert.ok(successor, "a successor copy of this app is spawned");
  assert.deepStrictEqual(successor[1], ["--some-flag"], "the successor gets this instance's arguments");
  assert.deepStrictEqual(successor[2], { detached: true, stdio: "ignore" });
  assert.strictEqual(state.lockReleases, 1, "the single-instance lock is released so the successor can win it");
  assert.ok(state.statuses.includes("status:Restarting Kiro Crew to finish the update…"),
    "the window is told why it is about to go away");
  assert.deepStrictEqual(state.exits, [], "this instance must not exit before the successor is confirmed running");

  // Exec success is not startup: the port is still silent, so this instance
  // keeps waiting and keeps running.
  successor.child.emit("spawn");
  await flush();
  assert.deepStrictEqual(state.exits, [], "the spawn event alone must not exit this instance");
  assert.ok(logs.some((line) => line.includes("waiting for its gateway to answer")));
  assert.ok(requests.some((url) => url.endsWith("/api/ready")), "readiness is probed on /api/ready");
  assert.ok(timers.pending.some((timer) => timer.ms === SUCCESSOR_READY_TIMEOUT_MS), "the wait is bounded");

  // The successor's gateway comes up and answers ready.
  state.http.status = 200;
  state.http.body = JSON.stringify({ ready: true });
  timers.fire(SUCCESSOR_POLL_MS);
  await flush();

  assert.strictEqual(successor.child.unrefed, true);
  assert.deepStrictEqual(state.exits, [0]);
  assert.ok(logs.some((line) => line.includes("is serving on :5476") && line.includes("exiting this instance")));
  assert.deepStrictEqual(timers.pending, [], "the deadline is disarmed once the successor is confirmed");
  assert.ok(state.statuses.filter((entry) => entry === "status:Restarting Kiro Crew to finish the update…").length >= 2,
    "the restart announcement is re-sent on every poll so a splash that loaded late still shows it");
});

test("a successor whose gateway is still booting (503 starting) also counts as alive", async () => {
  const built = staleBundleHarness();
  const { state, timers } = built;
  const successor = await spawnedSuccessor(built);

  state.http.status = 503;
  state.http.body = JSON.stringify({ ready: false });
  timers.fire(SUCCESSOR_POLL_MS);
  await flush();

  assert.deepStrictEqual(state.exits, [0]);
  assert.strictEqual(successor.child.killed, false);
});

test("a successor that exits before its gateway answers leaves this instance running with the failure surfaced", async () => {
  const built = staleBundleHarness();
  const { spawnCalls, logs, state, timers } = built;
  const successor = await spawnedSuccessor(built);

  // The bundle exec'd but crashed during initialization.
  successor.child.emit("exit", 1, null);

  assert.deepStrictEqual(state.exits, [], "a successor that died must not take this instance down");
  assert.strictEqual(state.lockRequests, 1, "the single-instance lock is taken back");
  assert.strictEqual(successor.child.killed, false, "nothing is left to kill");
  assert.ok(logs.some((line) => line.includes("exited (code=1 signal=null) before its gateway answered")));
  assert.strictEqual(spawnCalls.length, 3, "no further respawn: the ordinary failure path owns the outcome now");
  assert.deepStrictEqual(timers.pending, [], "no poll or deadline stays armed after the failure");

  // A gateway answering later (any gateway) must not revive the handoff.
  state.http.status = 200;
  await flush();
  assert.deepStrictEqual(state.exits, []);
});

test("a successor that never serves within the bound is stopped and this instance stays", async () => {
  const built = staleBundleHarness();
  const { logs, state, timers } = built;
  const successor = await spawnedSuccessor(built);

  timers.fire(SUCCESSOR_READY_TIMEOUT_MS);

  assert.deepStrictEqual(state.exits, [], "a timed-out handoff must not exit this instance");
  assert.strictEqual(successor.child.killed, true, "the unconfirmed successor is stopped so one instance remains");
  assert.strictEqual(state.lockRequests, 1, "the single-instance lock is taken back");
  assert.ok(logs.some((line) => line.includes("did not answer on :5476 within 60s")));
  assert.deepStrictEqual(timers.pending, [], "the poll is disarmed with the deadline");

  // A late readiness answer must not exit either.
  state.http.status = 200;
  await flush();
  assert.deepStrictEqual(state.exits, []);
});

test("a pruned bundle re-probes once, then surfaces the failure when the app executable is gone too", async () => {
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness({ appExecutableGone: true });

  await supervisor.start();
  assert.strictEqual(spawnCalls[0][0], BUNDLED_BIN);

  // The versioned directory is gone: the probed binary vanished before exec.
  state.pruned = true;
  const enoent = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
  spawnCalls[0].child.emit("error", enoent);

  // The re-probe found nothing bundled and fell through to the PATH name.
  assert.strictEqual(spawnCalls.length, 2);
  assert.strictEqual(spawnCalls[1][0], "kirocrew");

  spawnCalls[1].child.emit("error", enoent);

  assert.strictEqual(spawnCalls.length, 2, "never try to start a copy of an executable that is already gone");
  assert.strictEqual(state.lockReleases, 0);
  assert.deepStrictEqual(state.exits, [], "a missing app executable must not exit into nothing");
  assert.ok(logs.some((line) => line.includes("cannot relaunch; surfacing the failure instead")));
});

// The probe and the restart are not atomic: an in-place update can prune the
// bundle between the two. The restart is therefore a real spawn whose exec
// result is observed before this instance exits, so a prune that lands in
// that window still ends at the failure dialog with the app alive.
test("a bundle pruned after the probe fails the successor spawn and falls back to the failure dialog", async () => {
  const { supervisor, spawnCalls, logs, state, timers } = staleBundleHarness();

  await supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  spawnCalls[1].child.emit("exit", 75, null);
  const successor = successorCall(spawnCalls);
  assert.ok(successor);
  assert.deepStrictEqual(state.exits, []);

  successor.child.emit("error", Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" }));

  assert.deepStrictEqual(state.exits, [], "a successor that never started must not take this instance down");
  assert.strictEqual(state.lockRequests, 1, "the single-instance lock is taken back");
  assert.strictEqual(successor.child.killed, false, "there is no process to stop");
  assert.ok(logs.some((line) => line.includes("successor app failed to start (ENOENT)")));
  assert.strictEqual(spawnCalls.length, 3, "no further respawn: the ordinary failure path owns the outcome now");

  // A late "spawn" after the error must not exit either, nor arm a wait.
  successor.child.emit("spawn");
  await flush();
  assert.deepStrictEqual(state.exits, []);
  assert.deepStrictEqual(timers.pending, []);
});

test("a pruned bundle whose app executable survived still restarts the app", async () => {
  const { supervisor, spawnCalls, state, timers } = staleBundleHarness({ appExecutableGone: false });

  await supervisor.start();
  state.pruned = true;
  const enoent = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
  spawnCalls[0].child.emit("error", enoent);
  assert.strictEqual(spawnCalls.length, 2);
  spawnCalls[1].child.emit("error", enoent);

  const successor = successorCall(spawnCalls);
  assert.ok(successor);
  successor.child.emit("spawn");
  await flush();
  assert.deepStrictEqual(state.exits, []);

  state.http.status = 200;
  timers.fire(SUCCESSOR_POLL_MS);
  await flush();
  assert.deepStrictEqual(state.exits, [0]);
});

test("a stale exit while the updater owns the bundle is left alone", async () => {
  const { supervisor, spawnCalls, state } = staleBundleHarness();

  await supervisor.start();
  supervisor.onInstallDispatched();
  spawnCalls[0].child.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(state.exits, []);
});

test("Linux and Windows keep their own stale-asset recovery", async () => {
  for (const platform of ["linux", "win32"]) {
    const { supervisor, spawnCalls, state } = staleBundleHarness({ platform });

    await supervisor.start();
    assert.strictEqual(spawnCalls.length, 1, platform);
    spawnCalls[0].child.emit("exit", 75, null);

    assert.strictEqual(spawnCalls.length, 1, platform);
    assert.deepStrictEqual(state.exits, [], platform);
  }
});

test("a macOS Gatekeeper hint uses the user-facing warning channel", async () => {
  const { supervisor, spawnCalls, warnings } = staleBundleHarness();

  assert.strictEqual(await supervisor.start(), true);
  spawnCalls[0].child.emit("exit", null, "SIGKILL");

  assert.ok(warnings.some((line) => line.includes("macOS Gatekeeper blocked")));
});

test("a stale child SIGKILL stays file-only during recovery", async () => {
  const { supervisor, spawnCalls, logs, warnings, errors } = staleBundleHarness();

  assert.strictEqual(await supervisor.start(), true);
  const staleChild = spawnCalls[0].child;
  staleChild.emit("exit", 75, null);
  assert.strictEqual(spawnCalls.length, 2);

  staleChild.emit("exit", null, "SIGKILL");

  assert.ok(logs.some((line) => line.includes("gateway child exited code=null signal=SIGKILL")));
  assert.ok(!warnings.some((line) => line.includes("macOS Gatekeeper blocked")));
  assert.strictEqual(errors.length, 1, "only the first unexpected exit is user-visible");
});

// ---------------------------------------------------------------------------
// Cross-family conflict: the takeover prompt on a platform that cannot quit the
// other app for the user (#12398). The owner probe runs for real here — only the
// two OS calls it makes (netstat -ano, Win32_Process) and the dialog are faked.
// ---------------------------------------------------------------------------

const PROD_VERSION = "0.7.0";
const NIGHTLY_VERSION = "0.7.0-nightly.20260919";
// A bare selector, which is how classifyPortOwner recognises our own gateway
// without an absolute-path install to bind against. The trust rule itself is
// unchanged and exercised by gateway-stop's own suite.
const OWN_GATEWAY_COMMAND = "kirocrew gateway --port 5476";
const FOREIGN_COMMAND = "ssh -L 5476:localhost:5476 build-host";

function windowsOwnerExecFile(state) {
  return (file, args, options, callback) => {
    const tool = String(file).toLowerCase();
    if (tool.includes("netstat")) {
      state.netstatCalls = (state.netstatCalls || 0) + 1;
      if (state.probeFails || state.failNetstatCall === state.netstatCalls) {
        callback(new Error("netstat unavailable"));
        return;
      }
      callback(null, state.held
        ? "  TCP    0.0.0.0:5476           0.0.0.0:0              LISTENING       4242\r\n"
        : "", "");
      return;
    }
    if (tool.includes("powershell") || tool.includes("wmic")) {
      callback(null, state.command ?? OWN_GATEWAY_COMMAND, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

function posixOwnerExecFile(state) {
  return (file, args, options, callback) => {
    if (file === "osascript") {
      state.quitAttempts.push(args.join(" "));
      state.held = false;
      callback(null, "", "");
      return;
    }
    if (String(file).endsWith("lsof")) {
      state.lsofCalls = (state.lsofCalls || 0) + 1;
      if (state.failLsofCall === state.lsofCalls) {
        const error = new Error("lsof unavailable");
        error.code = "ENOENT";
        callback(error);
        return;
      }
      callback(null, state.held ? "4242\n" : "", "");
      return;
    }
    if (file === "/bin/ps") {
      callback(null, args.includes("ppid=") ? "500\n" : `${OWN_GATEWAY_COMMAND}\n`, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

// A dialog that answers each prompt from a scripted queue (the last answer
// repeats) and records every message box it was asked to show.
function scriptedDialog(responses, onPrompt = () => {}) {
  const shown = [];
  return {
    shown,
    dialog: {
      showMessageBox: async (options) => {
        shown.push(options);
        onPrompt(shown.length);
        const index = Math.min(shown.length - 1, responses.length - 1);
        return { response: responses[index] };
      },
    },
  };
}

function conflictHarness({
  platform,
  ownVersion,
  otherVersion,
  responses,
  state,
  quits,
  onPrompt,
}) {
  const { shown, dialog } = scriptedDialog(responses, onPrompt);
  const httpState = {
    status: 200,
    body: JSON.stringify({ ok: true, app: "kirocrew", version: otherVersion }),
  };
  const harnessResult = harness({
    dialog,
    app: { getVersion: () => ownVersion },
    httpMod: switchableHttp(httpState),
    execFileFn: platform === "win32" ? windowsOwnerExecFile(state) : posixOwnerExecFile(state),
    requestQuit: () => quits.push("quit"),
    processRef: {
      platform,
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      // Signal-0 liveness only. Returning normally says "still alive"; throwing
      // without EPERM says "gone", which is what pidAlive reads.
      kill() {
        if (!state.incumbentAlive) throw new Error("no such process");
      },
    },
  });
  return { ...harnessResult, shown };
}

// waitForPortFree polls the real clock through the global timers, so a test that
// needs it to TIME OUT drives both from node:test's timer mock.
async function settleWithTimeoutMock(promise) {
  let settled = false;
  const result = promise.then((value) => { settled = true; return value; });
  for (let step = 0; step < 40 && !settled; step += 1) {
    await flush();
    mock.timers.tick(31000);
  }
  return result;
}

// otherDisplay is FAMILY_META.displayName; otherApp is its appName, the
// technical Finder/AppleScript target, which is the joined identifier form.
for (const [label, ownVersion, otherVersion, otherDisplay, otherApp] of [
  ["production → Nightly", PROD_VERSION, NIGHTLY_VERSION, "Kiro Crew Nightly", "KiroCrew Nightly"],
  ["Nightly → production", NIGHTLY_VERSION, PROD_VERSION, "Kiro Crew", "KiroCrew"], // brand-ok
]) {
  test(`win32 ${label}: Retry after a manual quit completes the pending launch`, async () => {
    const state = { held: true };
    const quits = [];
    const { supervisor, logs, spawnCalls, shown } = conflictHarness({
      platform: "win32",
      ownVersion,
      otherVersion,
      responses: [0],
      state,
      quits,
      // The user quits the other app while the dialog is up. The freed LISTEN
      // socket is the only thing Retry waits for.
      onPrompt: () => { state.held = false; },
    });

    assert.strictEqual(await supervisor.start(), true);
    assert.strictEqual(shown.length, 1);
    assert.deepStrictEqual(shown[0].buttons, ["I quit it — Retry", "Cancel"]);
    assert.strictEqual(shown[0].cancelId, 1);
    assert.ok(shown[0].detail.endsWith(`Quit ${otherDisplay}, then choose “I quit it — Retry”.`));
    assert.ok(shown[0].detail.includes(shown[0].buttons[0]), "the detail names the button as written");
    assert.ok(shown[0].message.includes(otherVersion));
    assert.ok(logs.some((line) => line.includes("canTakeover=false on win32")));
    assert.ok(logs.some((line) => line === `takeover (manual): ${otherApp} released :5476 — proceeding to spawn`));
    assert.strictEqual(spawnCalls.length, 1);
    assert.deepStrictEqual(quits, []);
  });
}

test("win32 Retry while the port is still held re-prompts, then aborts", async () => {
  mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state: { held: true },
    quits,
  });

  try {
    assert.strictEqual(await settleWithTimeoutMock(supervisor.start()), false);
  } finally {
    mock.timers.reset();
  }

  // Bounded: the prompt comes back, then the launch gives up with a notice
  // rather than asking forever or closing the app silently.
  assert.strictEqual(shown.length, 4);
  assert.ok(shown[1].buttons.includes("I quit it — Retry"));
  // A re-prompt that is byte-identical to the first reads as a glitch.
  assert.ok(shown[0].detail.endsWith(`Quit Kiro Crew Nightly, then choose “I quit it — Retry”.`));
  assert.ok(shown[1].detail.includes("was still running a moment ago"));
  assert.ok(shown[1].detail.includes(shown[1].buttons[0]), "the re-prompt names the button too");
  assert.ok(!shown[1].detail.includes("5476"), "port numbers are not the user's task");
  assert.notStrictEqual(shown[1].detail, shown[0].detail);
  assert.strictEqual(shown[2].detail, shown[1].detail);
  assert.strictEqual(shown[3].type, "error");
  assert.deepStrictEqual(shown[3].buttons, ["OK"]);
  assert.ok(shown[3].message.includes("Kiro Crew Nightly is still running."));
  assert.ok(shown[3].detail.includes("This launch was cancelled."));
  assert.strictEqual(
    logs.filter((line) => line.includes("still holds :5476 after retry")).length,
    3,
  );
  assert.ok(logs.some((line) => line.includes("never released :5476 — aborting this launch")));
  assert.strictEqual(spawnCalls.length, 0);
  assert.deepStrictEqual(quits, ["quit"]);
});

test("win32 Cancel aborts on the first prompt, exactly as before", async () => {
  const quits = [];
  const { supervisor, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [1],
    state: { held: true },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 1);
  assert.strictEqual(spawnCalls.length, 0);
  assert.deepStrictEqual(quits, ["quit"]);
});

for (const [label, state] of [
  ["an unprobeable listener", { held: true, probeFails: true }],
  ["a foreign listener", { held: true, command: FOREIGN_COMMAND }],
  ["no local listener", { held: false }],
]) {
  test(`win32 never prompts for ${label}`, async () => {
    const quits = [];
    const { supervisor, logs, shown } = conflictHarness({
      platform: "win32",
      ownVersion: PROD_VERSION,
      otherVersion: NIGHTLY_VERSION,
      responses: [0],
      state,
      quits,
    });

    assert.strictEqual(await supervisor.start(), true);
    assert.strictEqual(shown.length, 0);
    assert.ok(logs.some((line) => line.includes("reusing existing gateway on :5476")));
    assert.ok(!logs.some((line) => line.includes("prompting for takeover")));
    assert.deepStrictEqual(quits, []);
  });
}

test("darwin still offers the automatic quit and takes over itself", async () => {
  const state = { held: true, quitAttempts: [] };
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "darwin",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state,
    quits,
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(shown.length, 1);
  assert.deepStrictEqual(shown[0].buttons, ["Quit Kiro Crew Nightly & Continue", "Cancel"]);
  assert.ok(shown[0].detail.endsWith("Quit Kiro Crew Nightly and continue here?"));
  assert.deepStrictEqual(state.quitAttempts, ['-e quit app "KiroCrew Nightly"']);
  assert.ok(logs.some((line) => line === "takeover: KiroCrew Nightly released :5476 — proceeding to spawn"));
  assert.ok(!logs.some((line) => line.includes("canTakeover=false")));
  assert.ok(!logs.some((line) => line.includes("takeover (manual)")));
  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(quits, []);
});

test("win32 Retry waits for the incumbent process, not just for the port", async () => {
  mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const state = { held: true, incumbentAlive: true };
  const quits = [];
  const { supervisor, logs, spawnCalls } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state,
    quits,
    // The socket closes on the quit, but the process lives on holding
    // gateway.lock — "port free is not lock free".
    onPrompt: () => { state.held = false; },
  });

  try {
    assert.strictEqual(await settleWithTimeoutMock(supervisor.start()), true);
  } finally {
    mock.timers.reset();
  }

  assert.ok(logs.some((line) => line.includes("takeover (manual): incumbent gateway process still alive after the exit grace")));
  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(quits, []);
});

test("win32 refuses the respawn when the incumbent PID cannot be captured", async () => {
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    // Call 1 classifies the owner; call 2 is the PID snapshot. Failing only the
    // second is the transient-probe case: port free would then be read as lock
    // free, and the replacement would race the incumbent's gateway.lock.
    state: { held: true, incumbentAlive: true, failNetstatCall: 2 },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 0, "no Retry is offered that could not be honoured");
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(logs.some((line) => line.includes("could not capture the incumbent PID on :5476")));
  assert.deepStrictEqual(quits, []);
});

test("linux refuses the respawn too, where unverifiedIncumbent is false by design", async () => {
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "linux",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    // Call 1 classifies the owner, call 2 is the PID snapshot. A probe that
    // named a PID a moment ago and now names none is anomalous on every
    // platform, so the POSIX degrade-to-no-op rule must not apply here:
    // incumbentSnapshotBlocksRespawn is Windows-only, and relying on it would
    // let Linux spawn into the incumbent's still-held gateway.lock.
    state: { held: true, incumbentAlive: true, failLsofCall: 2, quitAttempts: [] },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 0, "no Retry is offered that could not be honoured");
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(logs.some((line) => line.includes("could not capture the incumbent PID on :5476")));
  assert.deepStrictEqual(quits, []);
});
