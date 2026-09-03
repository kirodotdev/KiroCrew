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
      spawnCalls.push(args);
      const child = new EventEmitter();
      child.pid = 1234;
      child.exitCode = null;
      child.kill = () => {};
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
