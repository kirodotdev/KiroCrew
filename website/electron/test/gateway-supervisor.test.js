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
    BrowserWindow: overrides.BrowserWindow || class {},
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
    predictLocalPort: overrides.predictLocalPort,
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

test("no listener-probe outcome authorises the local secret", async () => {
  // The INVARIANT, not the current state: a port's LISTEN owner cannot authorise
  // this send at all, because `isKirocrewCommand` matches the basename of the
  // command line `ps` reports -- any process running as this user can present
  // itself as ours with `exec -a kirocrew`. So the probe must not be consulted,
  // and re-introducing a branch that mints on `kirocrew` or `service` reddens
  // this: every owner verdict, INCLUDING the two that look like ours, is refused
  // when the gateway is not one this process started.
  const attempts = [];
  const mintingHttp = {
    get(url, options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      if (options && options.headers && options.headers["X-Local-Secret"]) {
        attempts.push(String(url));
      }
      queueMicrotask(() => request.emit("error", new Error("connection refused")));
      return request;
    },
  };
  const secretFs = {
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync() {},
    existsSync() { return true; },
    openSync() { return 41; },
    closeSync() {},
    readFileSync() { return "a-local-secret\n"; },
  };

  for (const owner of ["kirocrew", "service", "foreign", "none", "unknown"]) {
    attempts.length = 0;
    const execFileFn = owner === "unknown"
      ? (_file, _args, _options, callback) => {
        const error = new Error("lsof unavailable");
        error.code = "ENOENT";
        callback(error);
      }
      : ownerProbe(
        owner === "foreign" ? "ssh -L 5476:localhost:5476 crew.example.com" : OWN_GATEWAY_COMMAND,
        { listening: owner !== "none", ppid: owner === "service" ? 1 : 500 },
      );
    // No spawn has happened, so ownership is "none" whatever the port reports.
    const { supervisor } = harness({ httpMod: mintingHttp, execFileFn, fsMod: secretFs });

    assert.strictEqual(await supervisor.fetchLocalToken(), "", `${owner}: no token`);
    assert.deepStrictEqual(attempts, [], `${owner}: the secret is not sent`);
  }
});

test("the local secret is not sent to a port a remote crew is configured on", async () => {
  // Driven through the sequence that actually reaches this, because the guard is
  // otherwise unreachable and the test would pass on the ownership check alone --
  // vacuous, and it was: spawn our own gateway on a crew-free port (ownership
  // becomes "spawned", which authorises the mint), then let the failure dialog's
  // Add Remote Crew write a crew for that very port. From then on the port's
  // holder is a tunnel by construction, so the secret must not go to it even
  // though this process did start a gateway here.
  const secretRequests = [];
  const store = fakeStore({ remoteHosts: {} });
  const { supervisor, spawnCalls } = harness({
    store,
    execFileFn: listenPidsProbe([FAKE_CHILD_PID]),
    httpMod: {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        if (options && options.headers && options.headers["X-Local-Secret"]) {
          secretRequests.push(String(url));
        }
        queueMicrotask(() => request.emit("error", new Error("connection refused")));
        return request;
      },
    },
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      accessSync() {},
      existsSync() { return true; },
      openSync() { return 41; },
      closeSync() {},
      readFileSync() { return "a-local-secret\n"; },
    },
  });

  await supervisor.start();
  assert.strictEqual(spawnCalls.length, 1, "this process started the gateway");
  // Control: with no crew recorded, the spawn DOES authorise the mint. Without
  // this the test cannot tell "refused for the crew" from "refused for everything".
  await supervisor.fetchLocalToken();
  assert.strictEqual(secretRequests.length, 1, "our own spawn mints while no crew is recorded");

  // Now the dialog's Add Remote Crew records a crew on this same port.
  secretRequests.length = 0;
  store.set("remoteHosts", { 5476: { host: "crew.example.com" } });

  assert.strictEqual(await supervisor.fetchLocalToken(), "", "no token for a crew's port");
  assert.deepStrictEqual(secretRequests, [], "and no secret leaves the machine");
});

test("the kernel must name our own child as the port's listener before the secret moves", async () => {
  // Liveness says our child is running; it does not say our child is what
  // answered. The pre-spawn probe established the port was free THEN, and the
  // child binds only after its interpreter has imported, so anything may bind in
  // that window -- an `ssh -L` reconnect answers exactly as the gateway would.
  //
  // A pid cannot be presented the way a command line can, which is why this is
  // the check and the command-based one is not: `snapshotGatewayPortPids` returns
  // raw kernel pids. Three cases, and only the first may mint.
  const cases = [
    ["our child holds the port", listenPidsProbe([FAKE_CHILD_PID]), true],
    ["someone else holds it", listenPidsProbe([FAKE_CHILD_PID + 999]), false],
    ["the probe cannot run at all", (_f, _a, _o, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    }, false],
  ];

  for (const [label, execFileFn, mints] of cases) {
    const secretRequests = [];
    const { supervisor, spawnCalls } = harness({
      execFileFn,
      httpMod: {
        get(url, options, callback) {
          const request = new EventEmitter();
          request.destroy = () => {};
          if (options && options.headers && options.headers["X-Local-Secret"]) {
            secretRequests.push(String(url));
          }
          queueMicrotask(() => request.emit("error", new Error("connection refused")));
          return request;
        },
      },
      fsMod: {
        constants: { X_OK: 1 },
        mkdirSync() {},
        accessSync() {},
        existsSync() { return true; },
        openSync() { return 41; },
        closeSync() {},
        readFileSync() { return "a-local-secret\n"; },
      },
    });

    await supervisor.start();
    assert.strictEqual(spawnCalls.length, 1, `${label}: this process started the gateway`);
    await supervisor.fetchLocalToken();
    assert.strictEqual(
      secretRequests.length > 0,
      mints,
      `${label}: the secret ${mints ? "is" : "is not"} sent`,
    );

    // And the own-port half holds whatever the pid says: another port is not the
    // child we started, so it is refused even in the case that may mint.
    secretRequests.length = 0;
    await supervisor.fetchLocalToken("http://127.0.0.1:9099");
    assert.deepStrictEqual(secretRequests, [], `${label}: another port is never ours`);
  }
});

test("the secret stops the moment our gateway child dies, before anything replaces it", async () => {
  // `gatewayOwnership` is not a liveness signal and must not be read as one:
  // `stopGatewayGracefully`'s own docstring says it stays "spawned" after a stop
  // so an aborted update may respawn, and the child's exit handler clears
  // `gatewayProcess` while leaving ownership alone. Between that death and the
  // next spawn the port is free for anything to bind, and a token refresh in that
  // window would hand it the secret. So the gate is the live child.
  const secretRequests = [];
  const { supervisor, spawnCalls } = harness({
    execFileFn: listenPidsProbe([FAKE_CHILD_PID]),
    httpMod: {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        if (options && options.headers && options.headers["X-Local-Secret"]) {
          secretRequests.push(String(url));
        }
        queueMicrotask(() => request.emit("error", new Error("connection refused")));
        return request;
      },
    },
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      accessSync() {},
      existsSync() { return true; },
      openSync() { return 41; },
      closeSync() {},
      readFileSync() { return "a-local-secret\n"; },
    },
  });

  await supervisor.start();
  assert.strictEqual(spawnCalls.length, 1, "this process started the gateway");
  // Control: while that child is alive the mint happens, so a refusal below is
  // attributable to its death and not to the gate refusing everything.
  await supervisor.fetchLocalToken();
  assert.strictEqual(secretRequests.length, 1, "a live child mints");

  // The child exits. Ownership deliberately survives this; the child does not.
  secretRequests.length = 0;
  const { child } = spawnCalls[0];
  child.exitCode = 1;
  child.emit("exit", 1, null);
  await flush();

  assert.strictEqual(await supervisor.fetchLocalToken(), "", "a dead child mints nothing");
  assert.deepStrictEqual(secretRequests, [], "and no secret reaches whatever took the port");
});

test("a later busy-port refusal retires the unreadable-probe outcome", async () => {
  // Both readers test that flag first, so a leftover keeps reporting a successor
  // this click never spawned, never names the port to free, and keeps the accent
  // on Quit under a paragraph asking for a port to be freed. The three post-click
  // outcomes each retire the other two; this is the pairing the first version
  // missed.
  const timers = fakeTimers();
  const ready = { answers: false };
  const built = clientOnlyClickHarness({
    actions: ["enable-retry", "enable-retry", "quit"],
    timers,
    httpMod: {
      get(url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const isReady = String(url).includes("/api/ready");
        queueMicrotask(() => {
          if (isReady && ready.answers) {
            const response = new EventEmitter();
            response.statusCode = 200;
            response.resume = () => {};
            if (typeof callback === "function") callback(response);
            response.emit("data", JSON.stringify({ ready: true }));
            response.emit("end");
            return;
          }
          request.emit("error", new Error("connection refused"));
        });
        return request;
      },
    },
    execFileFn: (_file, _args, _options, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    },
  });

  assert.strictEqual(await built.supervisor.start(), false);
  await built.supervisor.connect(built.mainWindow);
  // First click: the successor answers, the probe cannot run, so this is the
  // unreadable-probe outcome.
  for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
  built.spawnCalls[0].child.emit("spawn");
  await flush();
  ready.answers = true;
  timers.fire(SUCCESSOR_POLL_MS);
  for (let i = 0; i < 200 && built.documents.length < 2; i += 1) await flush();
  assert.match(built.documents[1], /could not check which program holds that port/);

  // Second click: readiness already answers, so the pre-spawn probe finds the
  // port occupied and nothing is spawned -- the busy-port outcome, which is now
  // the newer fact.
  for (let i = 0; i < 260 && built.documents.length < 3; i += 1) await flush();
  assert.strictEqual(built.documents.length, 3, "the dialog reopens after the second click");
  const second = built.documents[2];
  assert.match(second, /did not begin/, "the newer outcome is the occupied port");
  assert.doesNotMatch(second, /could not check which program holds that port/,
    "the earlier unreadable-probe story must not be reprinted");
  assert.match(second, /<div class="title">[^<]*already in use/, "and the title moves with it");
  assert.doesNotMatch(second, /class="ok" onclick="act\('quit'\)"/,
    "the Quit accent belonged to the other state and goes with it");
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

function staleBundleHarness({
  platform = "darwin",
  appExecutableGone = false,
  // Drives the LISTEN-owner probe the handoff runs before it confirms. An
  // ordinary handoff is one where our own successor holds the port, so that is
  // the default; the cases that matter override it with a foreign holder or a
  // probe that cannot run at all.
  ownerExecFile = ownerProbe(OWN_GATEWAY_COMMAND),
} = {}) {
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
    ...(ownerExecFile ? { execFileFn: ownerExecFile } : {}),
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
  // The port is read for an existing gateway before the successor is exec'd, so
  // the spawn lands a turn after the event that asks for it.
  await flush();
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

// Drives the LISTEN-owner probe to a chosen verdict. `command` is what ps reports
// for the pid holding the port, which is the only thing that separates our own
// gateway from a tunnel occupying the same number.
function ownerProbe(command, { listening = true, ppid = 500 } = {}) {
  return (file, args, _options, callback) => {
    if (String(file).endsWith("lsof")) {
      callback(null, listening ? "4242\n" : "", "");
      return;
    }
    if (file === "/bin/ps") {
      callback(null, args.includes("ppid=") ? `${ppid}\n` : `${command}\n`, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

// Reports a chosen LISTEN pid set for any port, which is what the mint compares
// against its own child's pid. Separate from `ownerProbe`: that one exists to
// drive the COMMAND-based classification, and the mint deliberately never asks
// that question.
function listenPidsProbe(pids) {
  return (file, _args, _options, callback) => {
    if (String(file).endsWith("lsof")) {
      callback(null, pids.join("\n") + (pids.length ? "\n" : ""), "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

// The pid the harness's spawnFn gives every fake child.
const FAKE_CHILD_PID = 1234;

async function handoffToReadiness(built) {
  const { spawnCalls, timers, state } = built;
  await built.supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  spawnCalls[1].child.emit("exit", 75, null);
  await flush();
  const successor = successorCall(spawnCalls);
  assert.ok(successor, "a successor copy of this app is spawned");
  successor.child.emit("spawn");
  await flush();
  // Something now answers on the successor's port. WHO is the open question.
  state.http.status = 200;
  state.http.body = JSON.stringify({ ready: true });
  timers.fire(SUCCESSOR_POLL_MS);
  await flush();
  return successor;
}

test("a foreign listener answering on the successor's port does not confirm the handoff", async () => {
  // The pre-spawn probe only establishes the port was free THEN. A manual
  // `ssh -L` that binds inside the poll window answers /api/ready exactly like a
  // gateway would, and confirmation is an unconditional exit with no recovery
  // step -- after which the next launch reads the tunnel as a local gateway and
  // the idle heartbeat sends it X-Internal-Secret. So readiness alone must not
  // confirm.
  const built = staleBundleHarness({
    ownerExecFile: ownerProbe("ssh -L 5476:localhost:5476 crew.example.com"),
  });

  await handoffToReadiness(built);

  assert.deepStrictEqual(
    built.state.exits,
    [],
    "this instance must not exit to a port it has not identified as its own gateway",
  );
  // And it fails now rather than spending the rest of the window: nothing this
  // poll waits for can turn a foreign holder into our gateway.
  assert.ok(
    built.logs.some((line) => line.includes("held by another process")),
    "the failure names the real reason instead of reporting a timeout",
  );
});

test("a positively identified local gateway on that port does confirm it", async () => {
  // The successor spawns its own gateway CHILD, so the process holding the port
  // is a grandchild of this one and never the pid we spawned. The probe
  // classifies the process, not the instance, which is why an ordinary handoff
  // still passes this gate.
  const built = staleBundleHarness({ ownerExecFile: ownerProbe(OWN_GATEWAY_COMMAND) });

  await handoffToReadiness(built);

  assert.deepStrictEqual(built.state.exits, [0], "an identified gateway confirms the handoff");
  assert.ok(
    !built.logs.some((line) => line.includes("listener probe unavailable")),
    "and it confirms on positive identification, not on the degraded path",
  );
});

test("a listener probe that cannot run refuses the handoff and names why", async () => {
  // classifyPortOwner's own rule is never to mistake "couldn't look" for "safe
  // to kill", and of the two answers available here confirming is the
  // destructive one: it is an unconditional app.exit(0), after which the next
  // launch treats the holder as a local gateway and the idle heartbeat sends it
  // X-Internal-Secret. Refusing costs a surfaced failure on a host whose
  // port-probe tooling is missing, and the failure path re-offers the button.
  const built = staleBundleHarness({
    ownerExecFile: (file, _args, _options, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    },
  });

  const successor = await handoffToReadiness(built);

  assert.deepStrictEqual(
    built.state.exits,
    [],
    "an owner the probe could not read must not authorise this instance to exit",
  );
  // Waiting instead would reach the deadline and then report a successor that
  // never served, which is the wrong cause.
  assert.ok(
    built.logs.some((line) => line.includes("could not check which process holds")),
    "the failure names the unreadable probe rather than a timeout",
  );
  assert.ok(
    built.logs.some((line) => line.includes("listener probe unavailable")),
    "and the log records that the positive check could not run",
  );
  // Refusing does not leave two instances behind. The lock is released before
  // the spawn, on purpose, so the successor can win it -- so the tidying here is
  // explicit: the unconfirmed successor is stopped and the lock is re-taken.
  assert.strictEqual(
    successor.child.killed,
    true,
    "the unconfirmed successor is stopped rather than left running",
  );
  assert.ok(
    built.state.lockRequests >= 1,
    "and this instance re-takes the single-instance lock it released before spawning",
  );
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
  // The port is read for an existing gateway before the successor is exec'd, so
  // the spawn lands a turn after the event that asks for it.
  await flush();
  const successor = successorCall(spawnCalls);
  assert.ok(successor, "a successor copy of this app is spawned");
  assert.ok(requests.some((url) => url.endsWith("/api/ready")),
    "the port is read before the handoff, so an existing gateway cannot be mistaken for the successor");
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

// Driving the failure dialog itself: the fake window records the document the
// dialog loads and answers it the way the page does, by setting a title the
// supervisor reads back. That makes the enable-retry click reachable without an
// Electron runtime, so the states the user actually sees can be asserted rather
// than inferred from the source.
function clientOnlyClickHarness({
  readyAnswers,
  actions,
  store,
  port,
  execFileFn,
  httpMod: httpOverride,
  platform = "test",
  timers,
  appExecMissing = false,
}) {
  const documents = [];
  const queue = [...actions];
  const state = { exits: [], lockReleases: 0 };

  class DialogWindow {
    constructor() { this.handlers = new Map(); }
    setMenu() {}
    on(event, handler) { this.handlers.set(event, handler); }
    isDestroyed() { return false; }
    loadURL(url) {
      documents.push(decodeURIComponent(
        String(url).replace(/^data:text\/html;charset=utf-8,/, ""),
      ));
      const action = queue.shift() || "quit";
      setImmediate(() => {
        const titled = this.handlers.get("page-title-updated");
        if (titled) titled({}, `mc-action:${action}`);
        const closed = this.handlers.get("closed");
        if (closed) closed();
      });
    }
  }

  // Refuses this app's own status probe, so the client-only failure surfaces, and
  // optionally ANSWERS the successor's readiness probe, which is what makes the
  // handoff refuse before spawning anything.
  const httpMod = {
    get(url, _options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      const isReady = String(url).includes("/api/ready");
      queueMicrotask(() => {
        if (isReady && readyAnswers) {
          const response = new EventEmitter();
          response.statusCode = 200;
          response.resume = () => {};
          if (typeof callback === "function") callback(response);
          response.emit("data", "{}");
          response.emit("end");
          return;
        }
        request.emit("error", new Error("connection refused"));
      });
      return request;
    },
  };

  const mainWindow = {
    isDestroyed: () => false,
    isMinimized: () => false,
    restore() {},
    show() {},
    focus() {},
    webContents: { loadFile() {}, send() {} },
  };

  const built = harness({
    store: store || fakeStore({
      runLocalGateway: false,
      remoteHosts: { 7778: { host: "crew.example.com" } },
    }),
    port: port ?? 7778,
    predictLocalPort: () => 5476,
    BrowserWindow: DialogWindow,
    httpMod: httpOverride || httpMod,
    mainWindow,
    ...(execFileFn ? { execFileFn } : {}),
    ...(timers ? { timers } : {}),
    app: {
      exit: (code) => state.exits.push(code),
      releaseSingleInstanceLock: () => { state.lockReleases += 1; },
      requestSingleInstanceLock: () => true,
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
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      // The app executable is present, so a re-exec is possible and the button
      // is offered; nothing else on disk is. With `appExecMissing` it is absent
      // too, which is the machine where the button is withheld and the message
      // falls back to the explicit-port route.
      accessSync(target) {
        if (target === APP_EXEC_PATH && !appExecMissing) return;
        const error = new Error("not found");
        error.code = "ENOENT";
        throw error;
      },
      existsSync() { return false; },
      openSync() { return 41; },
      closeSync() {},
      readFileSync() { throw new Error("no log"); },
    },
  });

  return { ...built, documents, mainWindow, state };
}

test("a gateway is not started on a port a configured crew is on", async () => {
  // Selection TARGETS a crew's port on purpose, so a live tunnel there is found
  // and adopted. Reaching the spawn means nothing answered, and binding it now is
  // the shadowing the whole path exists to prevent: the conflict resolver reads
  // the same remoteHosts entry and would call the gateway this app just started
  // that crew. PORT is fixed for this process, so refusing is the only answer
  // available here -- moving is the successor's job.
  const { supervisor, documents, mainWindow, spawnCalls, logs } = clientOnlyClickHarness({
    readyAnswers: false,
    actions: ["quit"],
    store: fakeStore({
      runLocalGateway: true,
      remoteHosts: { 5476: { host: "crew.example.com", remotePort: "7777" } },
    }),
    port: 5476,
  });

  assert.strictEqual(await supervisor.start(), false, "the launch does not report a gateway");
  assert.strictEqual(spawnCalls.length, 0, "and nothing was bound to the crew's port");
  // Whole-line equality, not "the host appears somewhere in the line". A
  // containment test on a host-shaped literal is satisfied by any string that
  // merely carries it -- `evil.test/crew.example.com` passes one -- so writing a
  // test that way declares that much strength sufficient. The host's POSITION in
  // the sentence is part of what is being pinned, so the whole sentence is the
  // assertion. Ownership itself is never decided this way: the gate reads
  // getRemoteHostConfig(store, PORT), an exact port-keyed lookup.
  assert.ok(
    logs.some((line) => line === "not starting a gateway on :5476: the crew "
      + "crew.example.com is configured there and a gateway bound here would shadow it"),
    "the refusal names the port and the crew that holds it in configuration",
  );

  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 1; i += 1) await flush();
  assert.strictEqual(documents.length, 1, "the failure dialog opens");
  const shown = documents[0];
  // End to end: the record carries localStartBlocked, the classifier reads that
  // as a launch that started nothing here, and the copy names the button the
  // dialog therefore renders. A record that classified as a crash instead would
  // print this remedy under a log pane and withhold the button it names.
  assert.match(shown, /reserved for reaching crew\.example\.com/, "the message explains why nothing was started");
  // The BUTTON, not the sentence naming it: the message names Start Local Gateway
  // either way, so only the rendered control proves the dialog agrees with the
  // record. A launch that classified as a crash would print this remedy and
  // withhold the control, under a log pane for a state that did not crash.
  assert.match(shown, /onclick="act\('enable-retry'\)"/, "the button it names is rendered");
  assert.doesNotMatch(shown, /onclick="act\('reveal'\)"/, "and no crash log pane appears");
  // Retry names the crew here even though the accent has not moved: one control
  // must not read as two across openings of the same dialog.
  assert.match(
    shown,
    /onclick="act\('retry'\)">Retry crew\.example\.com</,
    "the retry slot names the crew it reaches",
  );
  assert.doesNotMatch(
    shown,
    /set not to start a gateway/,
    "the setting is on, so that sentence must not appear",
  );
});

// Something IS answering on the port, and its payload carries no Kiro Crew
// identity -- the shape decideGatewayAction reuses by design. Whether that
// responder is a legacy gateway or an unrelated service is exactly what the
// LISTEN owner decides, so these two tests differ only in the owner probe.
function unidentifiedResponder() {
  return {
    get(url, _options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      queueMicrotask(() => {
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        if (typeof callback === "function") callback(response);
        response.emit("data", JSON.stringify({ ok: true }));
        response.emit("end");
      });
      return request;
    },
  };
}

test("a port held by something that is neither our gateway nor a crew is refused, not adopted", async () => {
  // decideGatewayAction reuses any responder it cannot identify, which is the
  // fail-open that keeps a gateway too old to carry identity fields adoptable.
  // Telling that legacy gateway from an unrelated service takes the two facts
  // this scope has and that function does not: the LISTEN owner, and whether a
  // crew is configured here. Neither says ours, so adopting would point this
  // app's gateway calls -- and the internal secret the heartbeat sends -- at
  // somebody else's service.
  const { supervisor, documents, mainWindow, spawnCalls, logs } = clientOnlyClickHarness({
    actions: ["quit"],
    store: fakeStore({ runLocalGateway: true, remoteHosts: {} }),
    port: 5476,
    httpMod: unidentifiedResponder(),
    execFileFn: ownerProbe("some-other-server --port 5476"),
  });

  assert.strictEqual(await supervisor.start(), false, "the holder is not reported as our gateway");
  assert.strictEqual(spawnCalls.length, 0, "and the port is not ours to bind either");
  // Whole-line equality here too, for the reason above: the sibling assertion is
  // the same shape and a strictness that holds at only one of two sites is not a
  // convention. What decided this refusal is the LISTEN owner -- a tokenized
  // command line matched against a set of executable names -- and the absence of
  // a port-keyed crew entry. Neither is a substring test.
  assert.ok(
    logs.some((line) => line === ":5476 is served by a process this app did not "
      + "start and no remote crew is configured there \u2014 refusing to adopt it"),
    "the refusal names what it found rather than timing out",
  );

  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 1; i += 1) await flush();
  assert.strictEqual(documents.length, 1, "the failure dialog opens");
  const shown = documents[0];
  assert.match(shown, /served by a program this app did not start/);
  assert.match(shown, /choose Add Remote Crew and fill in its address/, "saving the crew is the first remedy");
  assert.match(shown, /If it is unrelated software, quit it and then retry/);
  // The title says what is true of this port. Left on the generic line it read
  // "no gateway on port N" above a body saying that port IS served.
  assert.match(shown, /<div class="title">[^<]*port 5476 is in use by another program/,
    "the title agrees with the body");
  // And the button is withheld: this message names freeing the port or saving the
  // crew, never this control, and pressing it re-enters the same refusal.
  assert.doesNotMatch(shown, /act\('enable-retry'\)/,
    "a control that returns the user to this same dialog is not offered");
  assert.doesNotMatch(
    shown,
    /free port/,
    "no crew is configured here, so nothing re-execs onto another port",
  );
  // Nothing was launched, so this is not a crash and must not be dressed as one:
  // a log pane here shows an earlier run's log under a state that never spawned.
  assert.doesNotMatch(shown, /onclick="act\('reveal'\)"/, "no crash log pane");
});

test("the same unidentified payload IS adopted when our own gateway holds the port", async () => {
  // The control for the refusal above: identical payload, identical probe
  // mechanism, and the one fact that differs is who holds the port. A legacy
  // local gateway stays adoptable, which is the fail-open the refusal narrows
  // rather than removes.
  const { supervisor, spawnCalls } = clientOnlyClickHarness({
    actions: ["quit"],
    store: fakeStore({ runLocalGateway: true, remoteHosts: {} }),
    port: 5476,
    httpMod: unidentifiedResponder(),
    execFileFn: ownerProbe(OWN_GATEWAY_COMMAND),
  });

  assert.strictEqual(await supervisor.start(), true, "the existing gateway is adopted");
  assert.strictEqual(spawnCalls.length, 0, "nothing is spawned over it");
});

test("the handoff waits for the successor's own platform budget, not one flat number", async () => {
  // A primary local gateway on Windows gets 120s to bind, because importing a
  // freshly installed bundled Python tree exceeds the ordinary deadline there.
  // The successor IS that, so a flat 60s here expired while the successor was
  // still inside its own budget, and the deadline's fail() killed a gateway that
  // was starting normally. The deadline is derived from the same function the
  // successor applies to itself, plus one Electron-boot margin -- which is why
  // the non-Windows value is unchanged.
  for (const [platform, expected] of [["darwin", 60_000], ["win32", 150_000]]) {
    const timers = fakeTimers();
    const built = clientOnlyClickHarness({
      readyAnswers: false,
      actions: ["enable-retry", "quit"],
      platform,
      timers,
    });

    assert.strictEqual(await built.supervisor.start(), false, platform);
    await built.supervisor.connect(built.mainWindow);
    for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
    assert.strictEqual(built.spawnCalls.length, 1, `${platform}: the click spawns one successor`);
    built.spawnCalls[0].child.emit("spawn");
    await flush();

    const armed = timers.pending.filter((t) => t.ms !== SUCCESSOR_POLL_MS).map((t) => t.ms);
    assert.deepStrictEqual(armed, [expected], `${platform}: the readiness deadline`);
  }
});

test("a probe that cannot run reports its own outcome, not a successor that never served", async () => {
  // The successor answered readiness and was then stopped by this app, so
  // reporting "the restarted app never served one" would deny what the user
  // watched -- and offering the same button would repeat a check that cannot
  // succeed on this host. The state has its own flag and its own message, and the
  // message names the route that does recover.
  const timers = fakeTimers();
  // The pre-spawn probe must find the port free, or the click refuses before it
  // spawns anything (that is the occupied-port state). Readiness therefore starts
  // silent and answers only once the successor is running -- which is the real
  // sequence, and the one where "who holds this port" becomes the open question.
  const ready = { answers: false };
  const built = clientOnlyClickHarness({
    actions: ["enable-retry", "quit"],
    timers,
    httpMod: {
      get(url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const isReady = String(url).includes("/api/ready");
        queueMicrotask(() => {
          if (isReady && ready.answers) {
            const response = new EventEmitter();
            response.statusCode = 200;
            response.resume = () => {};
            if (typeof callback === "function") callback(response);
            response.emit("data", JSON.stringify({ ready: true }));
            response.emit("end");
            return;
          }
          request.emit("error", new Error("connection refused"));
        });
        return request;
      },
    },
    execFileFn: (_file, _args, _options, callback) => {
      const error = new Error("lsof unavailable");
      error.code = "ENOENT";
      callback(error);
    },
  });

  assert.strictEqual(await built.supervisor.start(), false);
  await built.supervisor.connect(built.mainWindow);
  for (let i = 0; i < 80 && built.spawnCalls.length === 0; i += 1) await flush();
  assert.strictEqual(built.spawnCalls.length, 1, "the click spawns one successor");
  built.spawnCalls[0].child.emit("spawn");
  await flush();
  ready.answers = true;
  timers.fire(SUCCESSOR_POLL_MS);
  for (let i = 0; i < 200 && built.documents.length < 2; i += 1) await flush();

  assert.strictEqual(built.documents.length, 2, "the dialog reopens after the refusal");
  const reopened = built.documents[1];
  assert.match(reopened, /could not check which program holds that port/,
    "the message states what actually failed");
  assert.doesNotMatch(reopened, /never served one/,
    "it must not deny a successor that answered");
  assert.match(reopened, /KIROCREW_PORT set to a port number that has no remote host/,
    "and it names the route that actually starts a gateway here");
  assert.doesNotMatch(reopened, /open it again instead/,
    "a plain reopen lands on the crew-configured refusal and starts nothing");
  // The accent follows the sentence, and this sentence asks for Quit. An orange
  // Retry under text saying to quit is a contradiction a reader cannot resolve --
  // they reported not daring to click the highlighted control.
  assert.match(reopened, /class="ok" onclick="act\('quit'\)"/,
    "Quit carries the accent in this state");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('retry'\)"/,
    "so Retry must not");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('enable-retry'\)"/,
    "and neither must the button the text says reaches the same check");
  // The title is what a reader takes in first, so it names the local outcome
  // rather than repeating the crew line every other state also carries.
  assert.match(reopened, /<div class="title">[^<]*could not identify what is on the port/,
    "the title names this outcome");
  // Two stories, two paragraphs: the local start, then what to do about the crew.
  assert.ok(
    (reopened.match(/<div class="msg">[\s\S]*?<\/div>/) || [""])[0].split("<p>").length > 2,
    "the message renders as more than one paragraph",
  );
  assert.deepStrictEqual(built.state.exits, [],
    "this instance stays, since it could not identify the holder");
});

test("a Retry after an occupied-port refusal still names the port", async () => {
  // Retry discards the failure record and startGateway builds a fresh one, so a
  // busy port written only onto the old object would vanish -- and the rebuilt
  // message would say this app is set not to start a gateway, which the click has
  // already made false. The port is supervisor state for that reason.
  const { supervisor, documents, mainWindow } = clientOnlyClickHarness({
    readyAnswers: true,
    actions: ["enable-retry", "retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  for (let i = 0; i < 80 && documents.length < 3; i += 1) await flush();

  assert.strictEqual(documents.length, 3, "the dialog reopens after the refusal and again after Retry");
  const rebuilt = documents[2];
  assert.match(rebuilt, /did not begin/, "the rebuilt record still reports the refusal");
  assert.match(rebuilt, /port 5476/, "and still names the occupied port");
  // Two post-click states shared a title and a button set and differed by one
  // word in the body, so a reader could not tell which situation they were in.
  // The title now carries the outcome.
  assert.match(rebuilt, /<div class="title">[^<]*port 5476 is already in use/,
    "the title names this outcome rather than repeating the crew line");
  assert.doesNotMatch(
    rebuilt,
    /set not to start a gateway/,
    "the setting is on by now, so that sentence must not come back",
  );
});

test("a lost restart promotes Start Local Gateway too, because its own copy asks for it", async () => {
  // The successor here is a fake EventEmitter, so ending it is what reaches the
  // state a lost restart leaves behind -- no real readiness deadline has to
  // elapse for the dialog to be driven to it.
  const { supervisor, documents, mainWindow, spawnCalls } = clientOnlyClickHarness({
    readyAnswers: false,
    actions: ["enable-retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  for (let i = 0; i < 60 && spawnCalls.length === 0; i += 1) await flush();
  assert.strictEqual(spawnCalls.length, 1, "the click spawns one successor");
  const { child } = spawnCalls[0];
  child.exitCode = 1;
  child.emit("exit", 1, null);
  for (let i = 0; i < 200 && documents.length < 2; i += 1) await flush();

  assert.strictEqual(documents.length, 2, "the dialog reopens after the lost restart");
  const reopened = documents[1];
  assert.match(reopened, /did not finish/, "this is the lost-restart state");
  // This state's own paragraph ends "choose Start Local Gateway to try again", so
  // the accent has to sit there. Gated on the busy port alone it stayed on Retry,
  // which reaches only the crew the same message has just said is unreachable --
  // so the highlighted control was the one that cannot help.
  assert.match(reopened, /class="ok" onclick="act\('enable-retry'\)"/,
    "Start Local Gateway carries the accent after a lost restart");
  assert.doesNotMatch(reopened, /class="ok" onclick="act\('retry'\)"/,
    "Retry must not keep the accent here either");
  assert.strictEqual(
    (reopened.match(/<button[^>]*onclick="act\('enable-retry'\)"/g) || []).length,
    1,
    "promoted, it must not also render as a secondary",
  );
  // Same set and same order as every other opening of this dialog, so only the
  // accent differs. Built primary-first, promotion moved Start Local Gateway to
  // the leftmost slot and Retry to the third, which is a habit-press hazard
  // between two openings that carry the same title.
  const order = (doc) => (doc.match(/onclick="act\('([a-z-]+)'\)"/g) || [])
    .map((m) => m.replace(/.*act\('([a-z-]+)'\).*/, "$1"));
  assert.deepStrictEqual(
    order(reopened),
    order(documents[0]),
    "the promoted row keeps the unpromoted row's action order",
  );
  assert.match(
    reopened,
    /onclick="act\('retry'\)">Retry crew\.example\.com</,
    "the demoted Retry names the crew it reaches",
  );
});

test("clicking Start Local Gateway onto an occupied port names the port and keeps the button", async () => {
  // predictLocalPort answers 5476 and something is already serving there. Nothing
  // is spawned and nothing is torn down, so the user must not be told a restart
  // ran, and the button must not be spent: freeing that port is outside this app
  // and makes the same click work.
  const { supervisor, documents, mainWindow, state, spawnCalls } = clientOnlyClickHarness({
    readyAnswers: true,
    actions: ["enable-retry", "quit"],
  });

  assert.strictEqual(await supervisor.start(), false);
  await supervisor.connect(mainWindow);
  // The reopen is dispatched from the refusal callback rather than awaited by
  // connect(), so wait for the document itself. Bounded, so a reopen that never
  // happens fails the assertion below instead of hanging.
  for (let i = 0; i < 50 && documents.length < 2; i += 1) await flush();

  assert.strictEqual(documents.length, 2, "the dialog reopens after the refused handoff");
  const [first, second] = documents;
  assert.match(first, /Start Local Gateway/, "the first dialog offers the button");

  assert.match(second, /did not begin/, "the reopened dialog says nothing restarted");
  assert.match(second, /port 5476/, "it names the port that is occupied");
  assert.doesNotMatch(second, /port 7778 is already served/,
    "the occupied port is the successor's, not this process's own");
  assert.doesNotMatch(second, /did not finish/,
    "it must not report a restart that never happened");
  assert.match(second, /enable-retry/,
    "the button survives, because the occupied port is not this app's to fix");
  // The message asks for Start Local Gateway, and Retry reaches only the crew
  // that is already unreachable -- so the accent has to sit on the action the
  // sentence names, or the colouring points the eye at the one that cannot work.
  assert.match(second, /class="ok" onclick="act\('enable-retry'\)"/,
    "Start Local Gateway is the primary action in this state");
  assert.doesNotMatch(second, /class="ok" onclick="act\('retry'\)"/,
    "Retry must not keep the accent here");
  // Promoted to primary, it must not also render as a secondary: one control. The
  // count is over BUTTONS, since the Enter key binding names the same action.
  assert.strictEqual(
    (second.match(/<button[^>]*onclick="act\('enable-retry'\)"/g) || []).length,
    1,
    "Start Local Gateway appears as exactly one button",
  );
  // Moving the accent must not remove the control: this same message still tells
  // the user to repair the tunnel and "then retry to reach ... again", so a
  // window without a Retry button leaves that sentence naming nothing, and a
  // user who has just fixed the tunnel can only quit.
  //
  // Demoted, it is the one button whose target is ambiguous: the sentence above
  // it is about a local start that just refused, so a bare "Retry" reads as
  // retrying THAT. The label names the crew instead.
  assert.match(
    second,
    /<button class="cancel" onclick="act\('retry'\)">Retry crew\.example\.com<\/button>/,
    "Retry stays available as a secondary and names the crew it reaches",
  );
  // The ORDER must be the order the unpromoted dialog uses, not primary-first.
  // Built primary-first, promotion moved Start Local Gateway from the third slot
  // to the first and pushed Retry the other way, so the leftmost button did two
  // different things between two openings of a dialog with the same title and a
  // habit press landed on the wrong recovery. Each action owns a slot; only the
  // accent travels. Compared against the FIRST dialog, which is the unpromoted
  // rendering of the same row -- asserting a literal order here would pass just
  // as well if both renderings moved together.
  const slots = (doc) => (doc.match(/onclick="act\('([a-z-]+)'\)"/g) || [])
    .map((m) => m.replace(/.*act\('([a-z-]+)'\).*/, "$1"));
  assert.deepStrictEqual(
    slots(second),
    slots(first),
    "the promoted row renders the same actions in the same order as the unpromoted one",
  );
  assert.ok(
    slots(second).indexOf("retry") < slots(second).indexOf("enable-retry"),
    "and the retry slot stays ahead of the local-start slot in both",
  );
  assert.strictEqual(
    (second.match(/<button[^>]*onclick="act\('retry'\)"/g) || []).length,
    1,
    "Retry appears as exactly one button",
  );

  assert.deepStrictEqual(state.exits, [], "this instance keeps running");
  assert.strictEqual(state.lockReleases, 0, "nothing was torn down");
  assert.strictEqual(successorCall(spawnCalls), undefined, "no successor is exec'd");
});

test("a gateway already answering on the successor's port abandons the handoff", async () => {
  const { supervisor, spawnCalls, logs, state, timers } = staleBundleHarness();

  await supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  // Something else is serving on the port the successor would bind: a gateway a
  // terminal started, or a side-by-side install. Its readiness is indistinguishable
  // from a successor's, so confirming on it would exit this instance on a
  // stranger's liveness -- and if the successor then died during initialization,
  // nothing would be left running at all.
  state.http.status = 200;
  state.http.body = JSON.stringify({ ready: true });
  spawnCalls[1].child.emit("exit", 75, null);
  await flush();

  assert.strictEqual(successorCall(spawnCalls), undefined,
    "no successor is exec'd, because its readiness could not be told from the gateway already there");
  assert.deepStrictEqual(state.exits, [], "this instance keeps running");
  assert.strictEqual(state.lockReleases, 0, "the single-instance lock is kept, so a later manual launch still routes here");
  assert.ok(!timers.pending.some((timer) => timer.ms === SUCCESSOR_READY_TIMEOUT_MS),
    "no handoff wait is armed for a handoff that never began");
  assert.ok(logs.some((line) => line.includes("already has a responder") && line.includes("surfacing the failure")),
    "the refusal is logged");
});

test("a legacy gateway answering 404 on the successor's port also abandons the handoff", async () => {
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness();

  await supervisor.start();
  spawnCalls[0].child.emit("exit", 75, null);
  // A gateway too old to serve the readiness endpoint answers 404 there. The
  // readiness classifier calls that "unknown", the same answer a refused
  // connection gives, so a check written on the classifier would read this
  // occupied port as an empty one and hand a successor a port someone holds.
  state.http.status = 404;
  state.http.body = "not found";
  spawnCalls[1].child.emit("exit", 75, null);
  await flush();

  assert.strictEqual(successorCall(spawnCalls), undefined,
    "no successor is exec'd: a 404 is a responder, not an empty port");
  assert.deepStrictEqual(state.exits, [], "this instance keeps running");
  assert.strictEqual(state.lockReleases, 0, "the single-instance lock is kept");
  assert.ok(logs.some((line) => line.includes("already has a responder")));
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
  // The port is read for an existing gateway before the successor is exec'd.
  await flush();
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

  // The port is read for an existing gateway before the successor is exec'd.
  await flush();
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

for (const [label, state, adopted] of [
  ["an unprobeable listener", { held: true, probeFails: true }, true],
  ["a foreign listener", { held: true, command: FOREIGN_COMMAND }, false],
  ["no local listener", { held: false }, true],
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

    // Not prompting and not quitting is the rule here, and it is unchanged in all
    // three: a tunnel is not ours to evict, and the AppleScript quit would target
    // a local app that is not running.
    //
    // Whether the holder is ADOPTED is a different question, and only the foreign
    // case answers it differently now. A positively foreign LISTEN owner with no
    // crew recorded on the port cannot be attributed to us or to a crew the user
    // named, and adopting it posts this machine's local secret to it -- the mint
    // only requires a literal loopback origin, which a tunnel's local end is. An
    // unreadable probe and an unseen socket are not that, so they keep the
    // historical reuse: this narrows the fail-open to the case that carries the
    // exposure rather than closing it.
    assert.strictEqual(await supervisor.start(), adopted, label);
    assert.strictEqual(shown.length, 0, `${label}: no takeover prompt`);
    assert.deepStrictEqual(quits, [], `${label}: nothing is quit`);
    assert.ok(!logs.some((line) => line.includes("prompting for takeover")), label);
    if (adopted) {
      assert.ok(logs.some((line) => line.includes("reusing existing gateway on :5476")), label);
    } else {
      assert.ok(
        logs.some((line) => line.includes("this app did not start and no remote crew is configured")),
        `${label}: the refusal names why it was not adopted`,
      );
      assert.ok(!logs.some((line) => line.includes("reusing existing gateway")), label);
    }
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

test("fetchRemoteToken runs ssh from PATH with stdin closed and batch mode", async () => {
  const execCalls = [];
  const { supervisor } = harness({
    store: fakeStore({ remoteHosts: { 5476: { host: "devbox" } } }),
    execFileFn(file, args, options, callback) {
      execCalls.push({ file, args, options });
      callback(null, "http://localhost:5476?token=abc\n", "");
    },
  });

  assert.deepStrictEqual(await supervisor.fetchRemoteToken(5476), { token: "abc", error: null });
  assert.strictEqual(execCalls.length, 1);
  assert.strictEqual(execCalls[0].file, "ssh");
  assert.deepStrictEqual(
    execCalls[0].args.slice(0, 5),
    ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"],
  );
  assert.strictEqual(execCalls[0].args[5], "devbox");
});

test("fetchRemoteToken reports a missing ssh client by name", async () => {
  const { supervisor } = harness({
    store: fakeStore({ remoteHosts: { 5476: { host: "devbox" } } }),
    execFileFn(file, args, options, callback) {
      callback(Object.assign(new Error(`spawn ${file} ENOENT`), { code: "ENOENT" }), "", "");
    },
  });

  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(5476),
    { token: "", error: "ssh client not found: ssh" },
  );
});
