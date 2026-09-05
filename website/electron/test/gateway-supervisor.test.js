"use strict";

const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
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

function harness(overrides = {}) {
  const logs = [];
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
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    shell: { showItemInFolder: () => {} },
    ipcMain: { on: () => {}, removeListener: () => {} },
    port,
    backendUrl: overrides.backendUrl || `http://localhost:${port}`,
    home: "/virtual/kirocrew-home",
    getMainWindow: () => mainWindow,
    isQuitting: () => false,
    requestQuit: () => {},
    cancelPendingTrayHide: () => {},
    exitImmersiveModes: () => {},
    log: (message) => logs.push(message),
    logPath: () => "/virtual/logs/gateway-launch.log",
    fsMod,
    osMod: { homedir: () => "/virtual/home" },
    pathMod: path.posix,
    httpMod: overrides.httpMod || rejectingHttp(),
    spawnFn: (...args) => {
      const child = new EventEmitter();
      child.pid = 1234;
      child.exitCode = null;
      child.kill = () => {};
      child.unref = () => { child.unrefed = true; };
      const call = [...args];
      call.child = child;
      spawnCalls.push(call);
      return child;
    },
    execFileFn: overrides.execFileFn
      || (() => { throw new Error("execFile must not run in this harness"); }),
    execFileSyncFn: () => { throw new Error("execFileSync must not run in this harness"); },
    processRef,
    dirname: "/virtual/electron",
  });

  return { supervisor, store, logs, spawnCalls, fsMod };
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
  const state = { pruned: false, exits: [], execProbes: [], lockReleases: 0, lockRequests: 0 };
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
  const built = harness({
    fsMod,
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
  return { ...built, state };
}

// The successor spawn the supervisor issues when it decides to restart the app.
function successorCall(spawnCalls) {
  return spawnCalls.find((call) => call[0] === APP_EXEC_PATH);
}

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

test("a second stale exit starts a fresh copy of the app and exits only once it is running", async () => {
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness();

  await supervisor.start();
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
  assert.deepStrictEqual(state.exits, [], "this instance must not exit before the successor is confirmed running");

  successor.child.emit("spawn");

  assert.strictEqual(successor.child.unrefed, true);
  assert.deepStrictEqual(state.exits, [0]);
  assert.ok(logs.some((line) => line.includes("successor app started")));
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
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness();

  await supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  spawnCalls[1].child.emit("exit", 75, null);
  const successor = successorCall(spawnCalls);
  assert.ok(successor);
  assert.deepStrictEqual(state.exits, []);

  successor.child.emit("error", Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" }));

  assert.deepStrictEqual(state.exits, [], "a successor that never started must not take this instance down");
  assert.strictEqual(state.lockRequests, 1, "the single-instance lock is taken back");
  assert.ok(logs.some((line) => line.includes("successor app failed to start (ENOENT)")));
  assert.strictEqual(spawnCalls.length, 3, "no further respawn: the ordinary failure path owns the outcome now");

  // A late "spawn" after the error must not exit either.
  successor.child.emit("spawn");
  assert.deepStrictEqual(state.exits, []);
});

test("a pruned bundle whose app executable survived still restarts the app", async () => {
  const { supervisor, spawnCalls, state } = staleBundleHarness({ appExecutableGone: false });

  await supervisor.start();
  state.pruned = true;
  const enoent = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
  spawnCalls[0].child.emit("error", enoent);
  assert.strictEqual(spawnCalls.length, 2);
  spawnCalls[1].child.emit("error", enoent);

  const successor = successorCall(spawnCalls);
  assert.ok(successor);
  successor.child.emit("spawn");
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
