"use strict";
// Linux AppImage desktop integration: real-filesystem metadata/xattr smoke
// tests through the packaged helper, and worker-thread orchestration.

const { test } = require("node:test");
const assert = require("node:assert");
const realFs = require("node:fs");
const realOs = require("node:os");
const realPath = require("node:path");
const realChildProcess = require("node:child_process");
const MODULE_PATH = require.resolve("../linux-desktop-integration");
const { integrateLinuxDesktop, startLinuxDesktopIntegration } = require("../linux-desktop-integration");
const {
  LINUX_FILESYSTEM_ONLY,
  BUNDLED_DESKTOP,
  APPDIR,
  HOME,
  xattrFailingFixture,
  EMPTY_XATTR_DIGEST,
  fakeWorkerFactory,
  WORKER_DEPS,
} = require("./linux-desktop-integration-test-helpers");

test("real filesystem keeps managed metadata stable and detects chmod", LINUX_FILESYSTEM_ONLY, (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-"));
  t.after(() => realFs.rmSync(root, { recursive: true, force: true }));
  const home = realPath.join(root, "home");
  const appDir = realPath.join(root, "appdir");
  const dataDir = realPath.join(root, "data");
  const stateDir = realPath.join(root, "config", "kirocrew-desktop");
  const resourcesPath = realPath.join(root, "resources");
  const binDir = realPath.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  realFs.mkdirSync(realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps"), {
    recursive: true,
  });
  realFs.mkdirSync(home, { recursive: true });
  realFs.mkdirSync(binDir, { recursive: true });
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs,
    path: realPath,
    os: realOs,
    platform: "linux",
    installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0",
    stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {},
    log: () => {},
    spawnSync: realChildProcess.spawnSync,
    resourcesPath,
  };

  assert.equal(integrateLinuxDesktop(deps).installed, true);
  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop-appimage.desktop");
  const statePath = realPath.join(stateDir, "linux-desktop-integration", "state.json");
  const record = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher];
  assert.equal(realFs.statSync(launcher).ino, realFs.statSync(record.anchorPath).ino);

  realFs.chmodSync(launcher, 0o600);
  const moved = integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });
  assert.deepEqual(moved, { skipped: true, reason: "launcher customized" });
  assert.doesNotMatch(realFs.readFileSync(launcher, "utf8"), /Moved\.AppImage/);
});

test("real filesystem detects a concurrent user xattr edit via the packaged helper", LINUX_FILESYSTEM_ONLY, (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-xattr-"));
  t.after(() => realFs.rmSync(root, { recursive: true, force: true }));
  const home = realPath.join(root, "home");
  const appDir = realPath.join(root, "appdir");
  const dataDir = realPath.join(root, "data");
  const stateDir = realPath.join(root, "config", "kirocrew-desktop");
  const resourcesPath = realPath.join(root, "resources");
  const binDir = realPath.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  realFs.mkdirSync(realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps"), {
    recursive: true,
  });
  realFs.mkdirSync(home, { recursive: true });
  realFs.mkdirSync(binDir, { recursive: true });
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs,
    path: realPath,
    os: realOs,
    platform: "linux",
    installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0",
    stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {},
    log: () => {},
    spawnSync: realChildProcess.spawnSync,
    resourcesPath,
  };

  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop-appimage.desktop");
  const originalBody = realFs.readFileSync(launcher, "utf8");
  let xattrSupported;
  {
    const py = realPath.join(binDir, "python3.12");
    const result = realChildProcess.spawnSync(py, [
      "-c", "import os,sys; os.setxattr(sys.argv[1], 'user.kirocrew_test', b'marker')", launcher,
    ]);
    xattrSupported = result.status === 0;
  }
  if (!xattrSupported) {
    t.skip("filesystem does not support user xattrs");
    return;
  }

  const res = integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });

  assert.deepEqual(res, { skipped: true, reason: "launcher customized" });
  assert.equal(realFs.readFileSync(launcher, "utf8"), originalBody);
});

test("real helper treats a filesystem without xattr support as an empty selected set", LINUX_FILESYSTEM_ONLY, (t) => {
  const { root, deps, helperRuns, launcher, statePath } = xattrFailingFixture(t, "ENOTSUP");

  assert.deepEqual(integrateLinuxDesktop(deps), { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  assert.ok(realFs.existsSync(launcher));
  const fingerprints = helperRuns.filter((r) => r.mode === "fingerprint");
  assert.ok(fingerprints.length > 0);
  for (const r of fingerprints) {
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, EMPTY_XATTR_DIGEST);
  }
  const record = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher];
  assert.equal(record.extended, EMPTY_XATTR_DIGEST);
  assert.equal(record.pending, undefined);

  // A later managed update runs the lease-held publish path, whose fd-based
  // xattr comparison must also read the unsupported filesystem as empty.
  const moved = integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });
  assert.equal(moved.installed, true);
  assert.match(realFs.readFileSync(launcher, "utf8"), /Moved\.AppImage/);
  const publishes = helperRuns.filter((r) => r.mode === "publish");
  assert.ok(publishes.length > 0);
  for (const r of publishes) {
    assert.equal(r.status, 0, r.stderr);
    assert.equal(JSON.parse(r.stdout).status, "published");
  }
});

test("real helper reports an unexpected xattr failure as a structured error and preserves", LINUX_FILESYSTEM_ONLY, (t) => {
  const { deps, helperRuns, launcher, statePath } = xattrFailingFixture(t, "EIO");

  assert.deepEqual(integrateLinuxDesktop(deps), { skipped: true, reason: "no managed icons" });
  assert.ok(!realFs.existsSync(launcher));
  const fingerprints = helperRuns.filter((r) => r.mode === "fingerprint");
  assert.ok(fingerprints.length > 0);
  for (const r of fingerprints) {
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout, "error");
    assert.doesNotMatch(r.stderr, /Traceback/);
  }
  // Nothing was adopted: no manifest was written, or it records no launcher.
  const artifacts = realFs.existsSync(statePath)
    ? JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts
    : {};
  assert.equal(artifacts[launcher], undefined);
});

test("real publication and cleanup defer while their inode has an open descriptor", LINUX_FILESYSTEM_ONLY, (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-lease-"));
  t.after(() => realFs.rmSync(root, { recursive: true, force: true }));
  const home = realPath.join(root, "home");
  const appDir = realPath.join(root, "appdir");
  const dataDir = realPath.join(root, "data");
  const stateDir = realPath.join(root, "config", "kirocrew-desktop");
  const resourcesPath = realPath.join(root, "resources");
  const binDir = realPath.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  realFs.mkdirSync(realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps"), {
    recursive: true,
  });
  realFs.mkdirSync(home, { recursive: true });
  realFs.mkdirSync(binDir, { recursive: true });
  realFs.symlinkSync("/usr/bin/python3", realPath.join(binDir, "python3.12"));
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  const deps = {
    fs: realFs, path: realPath, os: realOs, platform: "linux", installKind: "appimage",
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    version: "0.5.0", stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : undefined,
    run: () => {}, log: () => {}, spawnSync: realChildProcess.spawnSync, resourcesPath,
  };
  assert.equal(integrateLinuxDesktop(deps).installed, true);
  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop-appimage.desktop");
  const originalBody = realFs.readFileSync(launcher, "utf8");
  const currentFd = realFs.openSync(launcher, "r+");
  try {
    const deferred = integrateLinuxDesktop({
      ...deps,
      env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
      version: "0.6.0",
    });
    assert.deepEqual(deferred, { skipped: true, reason: "launcher customized" });
    assert.equal(realFs.readFileSync(launcher, "utf8"), originalBody);
  } finally {
    realFs.closeSync(currentFd);
  }
  assert.equal(integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  }).installed, true);
  assert.match(realFs.readFileSync(launcher, "utf8"), /Moved\.AppImage/);
  const statePath = realPath.join(stateDir, "linux-desktop-integration", "state.json");
  const recovery = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher].recovery;
  const fd = realFs.openSync(recovery.paths[0], "r+");
  try {
    integrateLinuxDesktop({
      ...deps,
      env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
      version: "0.6.0",
    });
    const retained = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher].recovery;
    assert.deepEqual(retained, recovery);
    for (const p of recovery.paths) assert.ok(realFs.existsSync(p));
  } finally {
    realFs.closeSync(fd);
  }

  integrateLinuxDesktop({
    ...deps,
    env: { ...deps.env, APPIMAGE: realPath.join(root, "Moved.AppImage") },
    version: "0.6.0",
  });
  const cleaned = JSON.parse(realFs.readFileSync(statePath, "utf8")).artifacts[launcher];
  assert.equal(cleaned.recovery, undefined);
  for (const p of recovery.paths) assert.ok(!realFs.existsSync(p));
});

test("startLinuxDesktopIntegration settles the plan on the main thread without a worker", async () => {
  const { calls, createWorker } = fakeWorkerFactory();
  const cases = [
    [{ ...WORKER_DEPS, platform: "darwin" }, "not linux"],
    [{ ...WORKER_DEPS, installKind: "deb" }, "not an AppImage install"],
    [{ ...WORKER_DEPS, env: { HOME } }, "missing AppImage runtime paths"],
    [{ ...WORKER_DEPS, env: { ...WORKER_DEPS.env, KIROCREW_DISABLE_DESKTOP_INTEGRATION: "1" } },
      "opt-out via KIROCREW_DISABLE_DESKTOP_INTEGRATION"],
  ];
  for (const [deps, reason] of cases) {
    const result = await startLinuxDesktopIntegration(deps, { createWorker });
    assert.deepEqual(result, { skipped: true, reason }, reason);
  }
  assert.equal(calls.length, 0, "no worker thread for a plan that does not integrate");
});

test("startLinuxDesktopIntegration hosts this module as the worker with serializable state only", async () => {
  const logs = [];
  const { calls, createWorker } = fakeWorkerFactory((worker) => {
    worker.emit("message", { type: "log", message: "reconciled kirocrew-desktop at 1 size(s)" });
    worker.emit("message", "not an object");
    worker.emit("message", { type: "result", result: { installed: true, iconName: "kirocrew-desktop", sizes: 1 } });
    worker.emit("exit", 0);
  });

  const result = await startLinuxDesktopIntegration({
    ...WORKER_DEPS,
    env: { ...WORKER_DEPS.env, NOT_A_STRING: 42 },
    uid: undefined,
    log: (m) => logs.push(m),
  }, { createWorker });

  assert.deepEqual(result, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  assert.deepEqual(logs, ["reconciled kirocrew-desktop at 1 size(s)"]);
  assert.equal(calls.length, 1);
  const { file, options } = calls[0];
  assert.equal(file, MODULE_PATH, "the module is its own worker entry");
  assert.deepEqual(options.execArgv, []);
  assert.equal(options.workerData.role, "kirocrew-linux-desktop-integration");
  assert.deepEqual(options.workerData.env, WORKER_DEPS.env, "non-string env values are dropped");
  assert.equal(options.workerData.uid, null);
  assert.equal(options.workerData.stateDir, WORKER_DEPS.stateDir);
  assert.equal(options.workerData.resourcesPath, WORKER_DEPS.resourcesPath);
  // Nothing the worker cannot receive is handed over: no functions, no modules.
  for (const value of Object.values(options.workerData)) {
    assert.notEqual(typeof value, "function");
  }
});

test("startLinuxDesktopIntegration never rejects when the worker fails or dies silently", async () => {
  const logs = [];
  const failing = fakeWorkerFactory((worker) => {
    worker.emit("error", new Error("boom"));
    worker.emit("exit", 1);
  });
  const errored = await startLinuxDesktopIntegration(
    { ...WORKER_DEPS, log: (m) => logs.push(m) }, { createWorker: failing.createWorker }
  );
  assert.deepEqual(errored, { skipped: true, reason: "worker failed" });
  assert.ok(logs.some((m) => /integration worker failed: boom/.test(m)));

  const silent = fakeWorkerFactory((worker) => worker.emit("exit", 7));
  const died = await startLinuxDesktopIntegration(WORKER_DEPS, { createWorker: silent.createWorker });
  assert.deepEqual(died, { skipped: true, reason: "worker exited with code 7" });

  const unstartable = await startLinuxDesktopIntegration(WORKER_DEPS, {
    createWorker: () => { throw new Error("no threads"); },
  });
  assert.deepEqual(unstartable, { skipped: true, reason: "worker unavailable" });

  const noResult = fakeWorkerFactory((worker) => {
    worker.emit("message", { type: "result", result: null });
  });
  const empty = await startLinuxDesktopIntegration(WORKER_DEPS, { createWorker: noResult.createWorker });
  assert.deepEqual(empty, { skipped: true, reason: "worker returned no result" });
});

// The real worker against a real filesystem and a deliberately slow stand-in
// for the packaged interpreter. The old orchestration ran the same
// reconciliation inline on the main thread, where each helper start blocked
// the event loop; the race below fails for that shape, because no main-thread
// timer can fire until a synchronous reconciliation returns.
test("startLinuxDesktopIntegration keeps the main thread responsive while helpers run", LINUX_FILESYSTEM_ONLY, async (t) => {
  const root = realFs.mkdtempSync(realPath.join(realOs.tmpdir(), "kirocrew-ldi-worker-"));
  t.after(() => realFs.rmSync(root, { recursive: true, force: true }));
  const home = realPath.join(root, "home");
  const appDir = realPath.join(root, "appdir");
  const dataDir = realPath.join(root, "data");
  const stateDir = realPath.join(root, "config", "kirocrew-desktop");
  const resourcesPath = realPath.join(root, "resources");
  const binDir = realPath.join(resourcesPath, "backend-dist", "kirocrew-backend", "bin");
  const callLog = realPath.join(binDir, "calls.log");
  realFs.mkdirSync(realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps"), {
    recursive: true,
  });
  realFs.mkdirSync(home, { recursive: true });
  realFs.mkdirSync(binDir, { recursive: true });
  realFs.writeFileSync(realPath.join(appDir, "kirocrew-desktop.desktop"), BUNDLED_DESKTOP);
  realFs.writeFileSync(
    realPath.join(appDir, "usr", "share", "icons", "hicolor", "16x16", "apps", "kirocrew-desktop.png"),
    "REAL PNG"
  );
  // Stand-in interpreter: argv is `-I -c <script> <mode> <target> ...`. Every
  // start costs 100ms of wall clock, like a cold interpreter, then answers the
  // fingerprint contract (64 hex digits); any other mode reports a structured
  // error, which the module treats as "retain and preserve".
  realFs.writeFileSync(realPath.join(binDir, "python3.12"), [
    "#!/bin/sh",
    `printf '%s\\n' "$4" >> "${callLog}"`,
    "sleep 0.1",
    "if [ \"$4\" = fingerprint ]; then printf '%064d\\n' 0; else echo error; fi",
    "",
  ].join("\n"), { mode: 0o755 });

  const logs = [];
  const integration = startLinuxDesktopIntegration({
    env: { APPIMAGE: realPath.join(root, "KiroCrew.AppImage"), APPDIR: appDir, HOME: home,
      XDG_DATA_HOME: dataDir },
    platform: "linux",
    installKind: "appimage",
    version: "0.5.0",
    stateDir,
    uid: typeof process.geteuid === "function" ? process.geteuid() : null,
    resourcesPath,
    log: (m) => logs.push(m),
  });
  assert.ok(integration instanceof Promise);

  // A main-thread timer must win against a reconciliation that spends at
  // least several hundred milliseconds inside helper processes.
  let ticks = 0;
  const interval = setInterval(() => { ticks += 1; }, 5);
  // Register the cleanup IMMEDIATELY, not after the assertions: an unref'd
  // interval keeps the Node event loop alive, so a throwing assertion before a
  // tail clearInterval would hang the test process forever instead of failing.
  t.after(() => clearInterval(interval));
  const first = await Promise.race([
    new Promise((resolve) => setTimeout(() => resolve("main-thread-timer"), 25)),
    integration.then(() => "integration"),
  ]);
  assert.equal(first, "main-thread-timer", "the main thread stayed free while the worker ran");

  const result = await integration;
  assert.deepEqual(result, { installed: true, iconName: "kirocrew-desktop", sizes: 1 });
  const calls = realFs.readFileSync(callLog, "utf8").trim().split("\n");
  assert.ok(calls.length >= 8, `expected a burst of helper starts, saw ${calls.length}`);
  assert.ok(calls.every((mode) => mode === "fingerprint"), calls.join(","));
  assert.ok(ticks >= 5, `main-thread timers kept firing during the run (${ticks} ticks)`);

  const launcher = realPath.join(dataDir, "applications", "kirocrew-desktop-appimage.desktop");
  assert.match(realFs.readFileSync(launcher, "utf8"), /^StartupWMClass=kirocrew-desktop$/m);
  assert.ok(logs.some((m) => /reconciled kirocrew-desktop at 1 size\(s\)/.test(m)), logs.join("\n"));
});
