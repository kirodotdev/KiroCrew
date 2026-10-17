"use strict";

// The bundled backend going stale or missing under the app: the stale-asset
// respawn, the successor copy of the app and its readiness handoff, pruned and
// absent bundles and the PATH fallback, the Windows removed-files dialog, and
// the macOS Gatekeeper hint that shares this harness. Shared fakes live in
// gateway-supervisor-test-helpers.js.

const { test } = require("node:test");
const assert = require("node:assert");

const {
  switchableHttp,
  fakeTimers,
  flush,
  harness,
  APP_EXEC_PATH,
  APP_ARGV,
  successorCall,
  SUCCESSOR_POLL_MS,
  ownerProbe,
  OWN_GATEWAY_COMMAND,
  INSTALLING_PROBE_MS,
  INSTALLING_COMPLETE_LINGER_MS,
  fakeDialogWindows,
} = require("./gateway-supervisor-test-helpers");

// A macOS app whose bundled backend sits at the usual resourcesPath layout. The
// `pruned` flag flips the bundle out from under the supervisor mid-test, the
// way an in-place update does; the fs then reports ENOENT for every bundled
// candidate and findKirocrewBin falls through to the bare PATH name. Whether
// the app's own executable survives is separate (`appExecutableGone`): a swap
// leaves a new one at the same path, a prune takes it too.
function staleBundleHarness({
  platform = "darwin",
  isPackaged = false,
  appExecutableGone = false,
  // Drives the LISTEN-owner probe the handoff runs before it confirms. An
  // ordinary handoff is one where our own successor holds the port, so that is
  // the default; the cases that matter override it with a foreign holder or a
  // probe that cannot run at all.
  ownerExecFile = ownerProbe(OWN_GATEWAY_COMMAND),
  BrowserWindow,
  mainWindow,
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
    // `interpreterOnly` puts a bare interpreter back with no stdlib yet: the
    // shape of files arriving again under a launcher that resolves.
    existsSync(target) { return Boolean(state.interpreterOnly) && target.endsWith("python.exe"); },
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
    ...(BrowserWindow ? { BrowserWindow } : {}),
    mainWindow: mainWindow || {
      isDestroyed: () => false,
      webContents: {
        isDestroyed: () => false,
        send: (channel, message) => state.statuses.push(`${channel}:${message}`),
      },
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
      isPackaged,
      // No `relaunch` on purpose: app.relaunch() cannot report a failed re-exec,
      // so the supervisor must never reach for it on this path.
      releaseSingleInstanceLock() { state.lockReleases += 1; },
      requestSingleInstanceLock() { state.lockRequests += 1; return true; },
      exit(code) { state.exits.push(code); },
    },
  });
  return { ...built, state, timers, requests: httpMod.requests };
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

test("Linux keeps its service manager's stale-asset recovery", async () => {
  const { supervisor, spawnCalls, state } = staleBundleHarness({ platform: "linux" });

  await supervisor.start();
  assert.strictEqual(spawnCalls.length, 1);
  spawnCalls[0].child.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(state.exits, []);
});

test("Windows re-probes after a stale-asset exit and respawns the backend found at the same path", async () => {
  const { supervisor, spawnCalls, logs, state } = staleBundleHarness({ platform: "win32", isPackaged: true });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls[0][0], BUNDLED_BIN);
  spawnCalls[0].child.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.length, 2);
  assert.strictEqual(spawnCalls[1][0], BUNDLED_BIN);
  assert.ok(logs.some((line) => line.includes("stale bundle (exit 75") && line.includes("attempt 1")));
  assert.deepStrictEqual(state.exits, []);
});

// The running version's directory was pruned: nothing bundled is left at any
// probed path. A packaged Windows app must not hand the launch to whatever
// kirocrew.exe is first on PATH -- that child serves nothing and exits 0,
// which no recovery reads as a failure.
test("a packaged Windows app whose bundle was pruned refuses the PATH fallback instead of spawning it", async () => {
  const { supervisor, spawnCalls, errors, state } = staleBundleHarness({ platform: "win32", isPackaged: true });

  await supervisor.start();
  state.pruned = true;
  spawnCalls[0].child.emit("exit", 75, null);

  assert.strictEqual(spawnCalls.length, 1, "nothing is spawned from PATH");
  assert.ok(errors.some((line) => line.includes("spawn REFUSED: no bundled backend")));
  assert.ok(state.statuses.some((line) => line.includes("Some app files are missing")));
  assert.deepStrictEqual(state.exits, []);
});

// Opens the failure dialog for a packaged Windows app. With `pruneMidRun` the
// bundle is pruned under a running backend, so the PATH refusal follows a
// stale-asset re-resolve; without it the app boots with no bundle at all.
async function windowsNoBundleDialog({ pruneMidRun }) {
  const { windows, FakeBrowserWindow } = fakeDialogWindows();
  let built = null;
  const window = {
    // Destroyed once a retry has spawned: that ends the post-retry boot before
    // it polls a backend this harness has not got.
    isDestroyed: () => built.spawnCalls.length > (pruneMidRun ? 1 : 0),
    show() {},
    focus() {},
    isMinimized: () => false,
    restore() {},
    webContents: {
      isDestroyed: () => false,
      loadFile() {},
      send: (channel, message) => statuses.push(`${channel}:${message}`),
    },
  };
  const statuses = [];
  built = staleBundleHarness({
    platform: "win32",
    isPackaged: true,
    BrowserWindow: FakeBrowserWindow,
    mainWindow: window,
  });
  if (pruneMidRun) {
    assert.strictEqual(await built.supervisor.start(), true);
    built.state.pruned = true;
    built.spawnCalls[0].child.emit("exit", 75, null);
  } else {
    built.state.pruned = true;
    assert.strictEqual(await built.supervisor.start(), false);
  }
  assert.ok(built.errors.some((line) => line.includes("spawn REFUSED: no bundled backend")));
  void built.supervisor.connect(window);
  await flush();
  assert.strictEqual(windows.length, 1, "the failure dialog opened");
  return { ...built, windows, statuses };
}

const QUIT_ACCENTED = `<button class="ok" onclick="act('quit')">Quit</button>`;

// Nothing is installing once the running version's tree was pruned, so the
// dialog says the files were removed and puts quit-and-reopen first, with the
// accent on Quit. The probe stays armed: a backend back at a probed path still
// starts on its own.
test("a pruned Windows backend opens the removed-files dialog and still auto-starts when files return", async () => {
  const { windows, timers, state, spawnCalls, errors } = await windowsNoBundleDialog({ pruneMidRun: true });
  const dialog = windows[0];
  const html = decodeURIComponent(dialog.loaded[0]);
  assert.match(html, /app files missing/);
  assert.match(html, /app files were removed, likely by an update\. Quit and reopen Kiro Crew/);
  assert.doesNotMatch(html, /installation still finishing|still being installed|five minutes/);
  assert.ok(html.includes(QUIT_ACCENTED), "the accent follows the sentence onto Quit");
  assert.match(html, /var primaryAct = "act\('quit'\)";/, "Enter presses the highlighted Quit");
  assert.ok(errors.length > 0);

  timers.tick(INSTALLING_PROBE_MS);
  assert.deepStrictEqual(dialog.scripts, [], "an unchanged probe repaints nothing");

  state.pruned = false;
  timers.tick(INSTALLING_PROBE_MS);
  assert.match(dialog.completionChrome(), /installation finished/);
  // The pruned dialog opened with Quit accented; the completion frame puts the
  // accent back on the (now busy) Retry so "Quit anyway" never reads as the
  // recommended action.
  assert.match(dialog.completionChrome(), /act\('quit'\)"\) b\.className = 'cancel';/);
  assert.match(dialog.completionChrome(), /act\('retry'\)"\) b\.className = 'ok';/);
  timers.fire(INSTALLING_COMPLETE_LINGER_MS);
  await flush();
  assert.strictEqual(spawnCalls.length, 2, "the returned backend is started by the dialog's own retry");
  assert.strictEqual(spawnCalls[1][0], BUNDLED_BIN);
});

// Retry starts a fresh launch, which resets the stale-asset budget; the dialog
// it reopens must still describe the removed backend, not an install.
test("a Retry from the removed-files dialog reopens it with the same copy", async () => {
  const { windows } = await windowsNoBundleDialog({ pruneMidRun: true });
  windows[0].click("retry");
  await flush();
  await flush();
  assert.strictEqual(windows.length, 2, "the refusal reopened the dialog");
  const html = decodeURIComponent(windows[1].loaded[0]);
  assert.match(html, /app files were removed/);
  assert.doesNotMatch(html, /still being installed/);
});

// Nothing is installing behind the pruned dialog, so the splash under it must
// not say "Finishing installation".
test("the splash behind the removed-files dialog does not claim an install", async () => {
  const { statuses } = await windowsNoBundleDialog({ pruneMidRun: true });
  assert.ok(statuses.some((line) => line.includes("Some app files are missing. Use Quit in the dialog, then reopen Kiro Crew.")), statuses.join(" | "));
  assert.ok(!statuses.some((line) => line.includes("Finishing installation")), statuses.join(" | "));
});

// Files arriving again under a pruned dialog do not change its copy: the
// remedy stays quit-and-reopen until the probe finds a complete tree.
test("the removed-files dialog keeps its copy while files arrive", async () => {
  const { windows, timers, state, statuses } = await windowsNoBundleDialog({ pruneMidRun: true });
  const dialog = windows[0];
  state.pruned = false;
  state.interpreterOnly = true;
  timers.tick(INSTALLING_PROBE_MS);
  assert.deepStrictEqual(dialog.scripts, [], "nothing is repainted while parts are still missing");
  assert.match(statuses.at(-1), /Some app files are missing/);
});

// Reveal Log reopens the dialog through the caller's loop; it reopens on the
// same pruned copy, with Quit accented and on Enter.
test("a removed-files dialog reopened from Reveal Log keeps its copy and accent", async () => {
  const { windows, state } = await windowsNoBundleDialog({ pruneMidRun: true });
  state.pruned = false;
  state.interpreterOnly = true;
  windows[0].click("reveal");
  await flush();
  await flush();
  assert.strictEqual(windows.length, 2, "the dialog reopened");
  const html = decodeURIComponent(windows[1].loaded[0]);
  assert.match(html, /app files missing/);
  assert.ok(html.includes(QUIT_ACCENTED));
  assert.match(html, /var primaryAct = "act\('quit'\)";/);
});

// A launch that never saw a backend has no incident behind it, so it keeps the
// install copy (an installer may still be writing the tree).
test("a packaged Windows app that boots with no bundle keeps the install copy", async () => {
  const { windows } = await windowsNoBundleDialog({ pruneMidRun: false });
  const html = decodeURIComponent(windows[0].loaded[0]);
  assert.match(html, /installation still finishing/);
  assert.match(html, /still being installed/);
  assert.doesNotMatch(html, /app files were removed/);
  assert.ok(!html.includes(QUIT_ACCENTED));
});

test("a packaged Windows app that boots with no bundle reports it instead of spawning from PATH", async () => {
  const { supervisor, spawnCalls, errors, state } = staleBundleHarness({ platform: "win32", isPackaged: true });
  state.pruned = true;

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(errors.some((line) => line.includes("spawn REFUSED: no bundled backend")));
});

// A source checkout finds the developer's own install by name, so only a
// packaged app gives the PATH fallback up.
test("an unpackaged Windows launch still falls back to kirocrew.exe on PATH", async () => {
  const { supervisor, spawnCalls, state } = staleBundleHarness({ platform: "win32", isPackaged: false });
  state.pruned = true;

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls.length, 1);
  assert.strictEqual(spawnCalls[0][0], "kirocrew.exe");
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
