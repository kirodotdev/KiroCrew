"use strict";

const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  MODULE_PATH,
  fakeStore,
  rejectingHttp,
  flush,
  harness,
  ownerProbe,
  OWN_GATEWAY_COMMAND,
  FOREIGN_COMMAND,
} = require("./gateway-supervisor-test-helpers");

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
    "mintLocalToken",
    "fetchRemoteToken",
    "entryUrl",
    "probePrimaryPortOwner",
    "stopForUpdate",
    "stopOnQuit",
    "reopenTunnel",
    "syncTunnel",
    "onInstallDispatched",
    "onInstallFailed",
  ]);
});

test("runtime gateway owners take every host dependency from the supervisor", () => {
  // The factory injects fs, os, path, http, child_process, timers, process and
  // the Electron directory; an owner that loads its own would bypass every fake
  // above and, for __dirname, point at runtime/gateway instead of the app.
  const ownerDir = path.join(__dirname, "..", "runtime", "gateway");
  const owners = fs.readdirSync(ownerDir).filter((name) => name.endsWith(".js")).sort();
  assert.deepStrictEqual(owners, [
    "failure-dialog.js",
    "family-takeover.js",
    "holder-decision.js",
    "home-lock.js",
    "launch-preflight.js",
    "port-holders.js",
    "reconnect.js",
    "remote-crew-prompt.js",
    "shutdown.js",
    "successor-handoff.js",
    "token-sources.js",
  ]);
  const facade = fs.readFileSync(MODULE_PATH, "utf8");
  for (const owner of owners) {
    const source = fs.readFileSync(path.join(ownerDir, owner), "utf8");
    assert.doesNotMatch(source, /require\(\s*["']electron["']\s*\)/, `${owner} loads Electron`);
    assert.doesNotMatch(
      source,
      /require\(\s*["'](?:node:)?(?:fs|os|path|http|child_process)["']\s*\)/,
      `${owner} must use the supervisor's injected host modules`,
    );
    assert.doesNotMatch(source, /__dirname/, `${owner} must use the injected Electron directory`);
    assert.doesNotMatch(source, /require\(\s*"\.\.\/\.\.\/gateway-supervisor"\s*\)/, `${owner} requires the facade`);
    const stem = owner.replace(/\.js$/, "");
    assert.match(
      facade,
      new RegExp(`require\\("\\./runtime/gateway/${stem}"\\)`),
      `the facade composes ${owner} through an explicit, packaged require`,
    );
  }
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

    assert.strictEqual(
      await supervisor.mintLocalToken(),
      "",
      `${owner}: no token`,
    );
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
  await supervisor.mintLocalToken();
  // How MANY requests the mint makes is not this control's subject: the mint
  // reads every candidate address that can be the dialed listener, and that walk
  // is pinned in local-token.test.js. What this needs is that the mint was
  // authorised at all, which is what separates "refused for the crew" below from
  // "refused for everything".
  assert.ok(secretRequests.length > 0, "our own spawn mints while no crew is recorded");

  // Now the dialog's Add Remote Crew records a crew on this same port.
  secretRequests.length = 0;
  store.set("remoteHosts", { 5476: { host: "crew.example.com" } });

  assert.strictEqual(
    await supervisor.mintLocalToken(),
    "",
    "no token for a crew's port",
  );
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
    await supervisor.mintLocalToken();
    assert.strictEqual(
      secretRequests.length > 0,
      mints,
      `${label}: the secret ${mints ? "is" : "is not"} sent`,
    );

    // And the own-port half holds whatever the pid says: another port is not the
    // child we started, so it is refused even in the case that may mint.
    secretRequests.length = 0;
    await supervisor.mintLocalToken("http://127.0.0.1:9099");
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
  await supervisor.mintLocalToken();
  assert.ok(secretRequests.length > 0, "a live child mints");

  // The child exits. Ownership deliberately survives this; the child does not.
  secretRequests.length = 0;
  const { child } = spawnCalls[0];
  child.exitCode = 1;
  child.emit("exit", 1, null);
  await flush();

  assert.strictEqual(
    await supervisor.mintLocalToken(),
    "",
    "a dead child mints nothing",
  );
  assert.deepStrictEqual(secretRequests, [], "and no secret reaches whatever took the port");
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

test("a status for a destroyed WebContents is dropped, not thrown", async () => {
  // The main window's WebContents object survives its renderer dying, and it
  // is in exactly that state during a renderer crash loop. `send` on it
  // throws "Object has been destroyed"; callers reach sendStatus from async
  // paths with no catch, so the throw surfaced as an unhandledRejection
  // (#14524). The guard has to read the destroyed state, not the object's
  // presence, which optional chaining alone cannot see.
  const sends = [];
  const destroyedContents = {
    isDestroyed: () => true,
    send: (...args) => {
      sends.push(args);
      throw new Error("Object has been destroyed");
    },
  };
  const { supervisor, spawnCalls } = harness({
    store: fakeStore({ runLocalGateway: false }),
    httpMod: rejectingHttp(),
    mainWindow: { isDestroyed: () => false, webContents: destroyedContents },
  });

  // startGateway's first line is a status; with the old code this threw.
  assert.strictEqual(await supervisor.start(), false);
  assert.deepStrictEqual(sends, [], "nothing is sent to a destroyed WebContents");
  assert.strictEqual(spawnCalls.length, 0);
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

test("stopForUpdate is a filesystem-free no-op when no child exists and nothing was adopted", async () => {
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

  await supervisor.stopForUpdate();
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

// A gateway already holds the port. `owner` decides what ps reports for its
// pid; `healthy` decides whether it answers HTTP; `lockPid` is what this app's
// own data folder records in gateway.lock and `holdsLock` whether the listener
// has that file open. `rebinds` makes a service manager bring the port back
// right after the stop. Timers resolve at once, so the grace window is counted
// in polls without waiting on the clock.
function stuckHolderHarness({
  owner = OWN_GATEWAY_COMMAND, healthy = false, lockPid = 4242, holdsLock = true,
  response = 0, ppid = 500, stopFails = false, rebinds = false, swapAfterDialog = false,
  replacedBy = 0, pidAfterDialog = 0, lockRealPath = "", exitsDuringDialog = false,
  refusedForPid = 0, platform = "linux", env = { KIROCREW_PORT: "9999" },
  bundledBin = false,
} = {}) {
  // The one bundled launcher candidate findKirocrewBin probes first for this
  // harness's arch (arm64) + resourcesPath. When bundledBin is set, accessSync
  // answers X_OK for it, so resolveGatewayBin returns the bundled binary and
  // both the stop and the follow-on spawn run it (the real stuck-restart flow).
  const BUNDLED_STUCK_BIN = "/virtual/resources/backend-dist/kirocrew-backend-arm64/bin/kirocrew";
  const execCalls = [];
  const dialogs = [];
  const state = { listening: true, quits: 0 };
  const http = healthy
    ? {
      get(url, _options, callback) {
        const req = new EventEmitter();
        req.destroy = () => {};
        queueMicrotask(() => {
          const res = new EventEmitter();
          res.statusCode = 200;
          res.resume = () => {};
          callback(res);
          res.emit("data", JSON.stringify(url.endsWith("/api/ready")
            ? { ready: true }
            : { app: "kirocrew", version: "0.6.0" }));
          res.emit("end");
        });
        return req;
      },
    }
    : rejectingHttp();
  const instance = harness({
    processRef: {
      platform, arch: "arm64", env, resourcesPath: "/virtual/resources",
      kill(_pid, signal) {
        if (signal === 0) { const error = new Error("gone"); error.code = "ESRCH"; throw error; }
        throw new Error("the supervisor must never signal the stuck gateway itself");
      },
    },
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      accessSync(target) {
        if (bundledBin && target === BUNDLED_STUCK_BIN) return;
        const error = new Error("not found"); error.code = "ENOENT"; throw error;
      },
      existsSync() { return false; },
      openSync() { return 41; },
      closeSync() {},
      readFileSync(file) {
        if (file === "/virtual/kirocrew-home/gateway.lock") {
          return `${state.dialogShown && pidAfterDialog ? pidAfterDialog : lockPid}\n`;
        }
        throw new Error(`unexpected filesystem read: ${file}`);
      },
      realpathSync(file) {
        if (lockRealPath && file === "/virtual/kirocrew-home/gateway.lock") return lockRealPath;
        return file;
      },
    },
    httpMod: http,
    timers: {
      setTimeoutFn(fn) { queueMicrotask(fn); return 0; },
      clearTimeoutFn() {},
      setIntervalFn() { return 0; },
      clearIntervalFn() {},
    },
    execFileFn(file, args, options, callback) {
      execCalls.push({ file: String(file), args, options });
      if (String(file).endsWith("lsof") && args.includes("-p")) {
        const ours = holdsLock && !state.swapped;
        const pid = args[args.indexOf("-p") + 1];
        const lockPath = lockRealPath || "/virtual/kirocrew-home/gateway.lock";
        callback(null, ours ? `p${pid}\nn${lockPath}\n` : `p${pid}\nn/virtual/other-home/gateway.lock\n`, "");
        return;
      }
      if (String(file).endsWith("lsof") && replacedBy && state.stopped) {
        callback(null, `${replacedBy}\n`, "");
        return;
      }
      if (String(file).endsWith("lsof") && exitsDuringDialog && state.dialogShown && !args.includes("-p")) {
        callback(null, "", "");
        return;
      }
      if (String(file).endsWith("lsof") && refusedForPid && state.refused && !args.includes("-p")) {
        callback(null, `${refusedForPid}\n`, "");
        return;
      }
      if (String(file).endsWith("lsof") && pidAfterDialog && state.dialogShown) {
        callback(null, `${pidAfterDialog}\n`, "");
        return;
      }
      if (String(file).endsWith("lsof")) {
        callback(null, state.listening ? "4242\n" : "", "");
        // The first two post-stop readings see the port free (the replaced-pid
        // check, then the port-free wait); the service rebinds after that.
        if (rebinds && state.stopped) {
          state.postStopProbes = (state.postStopProbes || 0) + 1;
          if (state.postStopProbes >= 2) state.listening = true;
        }
        return;
      }
      if (file === "/bin/ps") {
        callback(null, args.includes("ppid=") ? `${ppid}\n` : `${owner}\n`, "");
        return;
      }
      if (args.includes("stop") && refusedForPid) {
        state.refused = true;
        callback(Object.assign(new Error("exit 1"), { code: 1 }), "", "Port 5476 is held by another pid");
        return;
      }
      if (args.includes("stop")) {
        state.stopped = true;
        if (stopFails) {
          callback(Object.assign(new Error("exit 1"), { code: 1 }), "", "refused");
          return;
        }
        state.listening = false;
        callback(null, "Stopped gateway on port 5476", "");
        return;
      }
      callback(new Error(`unexpected command: ${file}`));
    },
    dialog: {
      async showMessageBox(options) {
        dialogs.push(options);
        state.dialogShown = true;
        if (swapAfterDialog) state.swapped = true;
        return { response };
      },
    },
    requestQuit: () => { state.quits += 1; },
  });
  const stopCalls = () => execCalls.filter((call) => call.args.includes("stop"));
  return { ...instance, execCalls, dialogs, stopCalls, state };
}

test("a stuck gateway for this data folder is stopped through the CLI, then the bundled backend starts", async () => {
  const built = stuckHolderHarness();
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.dialogs.length, 1, "one dialog offers the restart");
  assert.equal(built.dialogs[0].message, "The gateway is not responding.");
  assert.deepEqual(built.dialogs[0].buttons, ["Stop and restart", "Quit"]);
  assert.match(built.dialogs[0].detail, /has not answered for at least 15 seconds/);
  assert.match(built.dialogs[0].detail, /Stop and restart stops that gateway and starts a fresh one\. Anything the stuck gateway was still doing is stopped with it\./);
  assert.doesNotMatch(built.dialogs[0].detail, /kirocrew stop/, "the app runs the stop; the user is not told to type it");
  const stops = built.stopCalls();
  assert.equal(stops.length, 1, "the lock-checked CLI stop ran once");
  assert.deepEqual(stops[0].args.slice(-5), ["stop", "--port", "5476", "--expect-pid", "4242"],
    "the stop is pinned to the pid the grace window proved");
  assert.equal(stops[0].options.env.KIROCREW_HOME, "/virtual/kirocrew-home");
  assert.equal(stops[0].options.env.KIROCREW_PORT, undefined);
  assert.equal(built.spawnCalls.length, 1, "the bundled backend starts after the stop");
  assert.deepEqual(built.spawnCalls[0][1].slice(0, 4), ["gateway", "--no-open", "--port", "5476"]);
});

test("the stuck-gateway stop strips PYTHONPATH/PYTHONHOME from the bundled binary's env", async () => {
  // runGatewayStop execFile's the SAME bundled binary the launch path spawns, so
  // it must inherit the same scrubbed environment. A bundled Python left with an
  // inherited PYTHONPATH/PYTHONHOME from a dev shell would import checkout code
  // instead of its packaged tree -- the override isolation this PR exists to
  // enforce, applied only to the launch path, left a hole on the stop path.
  const built = stuckHolderHarness({
    bundledBin: true,
    env: {
      KIROCREW_PORT: "9999",
      PYTHONPATH: "/home/dev/checkout/src",
      PYTHONHOME: "/home/dev/other-python",
      PATH: "/usr/bin",
    },
  });
  assert.equal(await built.supervisor.start(), true);
  const stops = built.stopCalls();
  assert.equal(stops.length, 1, "the lock-checked CLI stop ran once");
  // The binary it stops is the bundled one (under backend-dist), so the scrub
  // applies -- the same bin the follow-on spawn uses.
  assert.ok(stops[0].file.includes("backend-dist"), "the stop runs the bundled binary");
  const stopEnv = stops[0].options.env;
  assert.equal(stopEnv.PYTHONPATH, undefined, "PYTHONPATH must be stripped for the bundled stop");
  assert.equal(stopEnv.PYTHONHOME, undefined, "PYTHONHOME must be stripped for the bundled stop");
  // The scrub is narrow: KIROCREW_HOME is still set, KIROCREW_PORT still dropped,
  // and an unrelated inherited var (PATH) is preserved.
  assert.equal(stopEnv.KIROCREW_HOME, "/virtual/kirocrew-home");
  assert.equal(stopEnv.KIROCREW_PORT, undefined);
  assert.equal(stopEnv.PATH, "/usr/bin", "an unrelated inherited var is preserved");
});

test("declining the stuck-gateway restart quits without stopping or spawning", async () => {
  const built = stuckHolderHarness({ response: 1 });
  assert.equal(await built.supervisor.start(), false);
  assert.equal(built.dialogs.length, 1);
  assert.equal(built.stopCalls().length, 0);
  assert.equal(built.spawnCalls.length, 0);
  assert.equal(built.state.quits, 1);
});

test("a service that brings the stopped gateway back is re-validated, not spawned over", async () => {
  const built = stuckHolderHarness({ ppid: 1, rebinds: true });
  assert.equal(await built.supervisor.start(), true);
  assert.match(built.dialogs[0].detail, /If a background service restarts it automatically, Kiro Crew connects to it once it answers\./);
  assert.equal(built.stopCalls().length, 1);
  assert.equal(built.spawnCalls.length, 0, "no second backend races the service's respawn");
  assert.ok(built.logs.some((line) => line.includes("re-bound after the stop")));
});

test("a holder that changes while the dialog is open is re-validated, never stopped", async () => {
  const built = stuckHolderHarness({ swapAfterDialog: true });
  await built.supervisor.start();
  assert.equal(built.dialogs.length, 1);
  assert.equal(built.stopCalls().length, 0, "the stop needs a fresh proof after the choice");
  assert.ok(built.logs.some((line) => line.includes("changed holder while the dialog was open")));
});

test("a service that rebinds before the port is seen free is re-validated, not reported as a failed stop", async () => {
  const built = stuckHolderHarness({ ppid: 1, replacedBy: 5151 });
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.stopCalls().length, 1);
  assert.equal(built.spawnCalls.length, 0);
  assert.ok(built.logs.some((line) => line.includes("taken by a new pid 5151")));
});

test("a same-home replacement started while the dialog is open is never stopped", async () => {
  const built = stuckHolderHarness({ pidAfterDialog: 6060 });
  await built.supervisor.start();
  assert.equal(built.dialogs.length, 1);
  assert.equal(built.stopCalls().length, 0, "only the pid the grace window judged may be stopped");
  assert.ok(built.logs.some((line) => line.includes("changed holder while the dialog was open")));
});

test("a stuck gateway that exits while the dialog is open is replaced by the bundled backend", async () => {
  const built = stuckHolderHarness({ exitsDuringDialog: true });
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.stopCalls().length, 0, "nothing is left to stop");
  assert.equal(built.spawnCalls.length, 1, "the free port is spawned into, not adopted as empty");
});

test("a CLI refusal because another pid took the port re-validates, not a failed stop", async () => {
  const built = stuckHolderHarness({ refusedForPid: 7070 });
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.stopCalls().length, 1);
  assert.ok(built.logs.some((line) => line.includes("was taken by pid 7070 before the stop")));
  assert.equal(built.spawnCalls.length, 0, "the new holder is re-validated, not spawned over");
});

test("on macOS a stuck gateway gets the manual command and Quit, never a stop", async () => {
  const built = stuckHolderHarness({ platform: "darwin" });
  assert.equal(await built.supervisor.start(), false);
  assert.equal(built.dialogs.length, 1);
  assert.equal(built.dialogs[0].message, "The gateway is not responding.");
  assert.deepEqual(built.dialogs[0].buttons, ["Quit"]);
  assert.match(built.dialogs[0].detail, /Run this command in Terminal:\nkirocrew stop --port 5476\n/);
  assert.equal(built.stopCalls().length, 0, "macOS has no pidfd, so the app never runs the stop");
  assert.equal(built.spawnCalls.length, 0);
  assert.equal(built.state.quits, 1);
});

test("on macOS a service-owned stuck gateway's manual dialog carries the service note", async () => {
  const built = stuckHolderHarness({ platform: "darwin", ppid: 1 });
  await built.supervisor.start();
  assert.match(built.dialogs[0].detail, /If the gateway starts again automatically, stop or update the service that restarts it\./);
  assert.equal(built.stopCalls().length, 0);
});

test("a symlinked data folder is matched by the lock file's real path", async () => {
  const built = stuckHolderHarness({ lockRealPath: "/private/virtual/kirocrew-home/gateway.lock" });
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.dialogs.length, 1);
  assert.equal(built.stopCalls().length, 1);
});

test("a reused pid that does not hold this data folder's lock is never stopped", async () => {
  const built = stuckHolderHarness({ holdsLock: false });
  await built.supervisor.start();
  assert.equal(built.dialogs.length, 0);
  assert.equal(built.stopCalls().length, 0);
  assert.equal(built.spawnCalls.length, 1);
});

test("an ssh forward on the port is never stopped: no dialog, today's spawn path", async () => {
  const built = stuckHolderHarness({ owner: FOREIGN_COMMAND });
  await built.supervisor.start();
  assert.equal(built.dialogs.length, 0);
  assert.equal(built.stopCalls().length, 0);
  assert.equal(built.spawnCalls.length, 1);
});

test("a gateway for another data folder is never stopped", async () => {
  const built = stuckHolderHarness({ lockPid: 777 });
  await built.supervisor.start();
  assert.equal(built.dialogs.length, 0);
  assert.equal(built.stopCalls().length, 0);
  assert.equal(built.spawnCalls.length, 1);
});

test("a healthy gateway for this data folder is reused with no dialog and no stop", async () => {
  const built = stuckHolderHarness({ healthy: true });
  assert.equal(await built.supervisor.start(), true);
  assert.equal(built.dialogs.length, 0);
  assert.equal(built.stopCalls().length, 0);
  assert.equal(built.spawnCalls.length, 0);
  assert.ok(built.logs.some((line) => line.includes("reusing existing gateway")));
});

test("a failed CLI stop surfaces a start failure instead of spawning into the held port", async () => {
  const built = stuckHolderHarness({ stopFails: true });
  assert.equal(await built.supervisor.start(), false);
  assert.equal(built.stopCalls().length, 1);
  assert.equal(built.spawnCalls.length, 0);
  assert.ok(built.logs.some((line) => line.includes("kirocrew stop --port 5476` failed")));
});
