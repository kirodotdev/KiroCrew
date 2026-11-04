"use strict";

// Wedge recovery on Windows when netstat names no owner for a port that is still
// held. Recovery shows the loading screen and waits for the port to free: a
// port that frees in time is respawned into, and one still held when the wait
// runs out gets the restart-required dialog with no respawn. The bind check
// runs for real against a real socket; only netstat and the clock are scripted.

const net = require("node:net");
const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness, fakeDialogWindows, fakeTimers } = require("./gateway-supervisor-test-helpers");

function listenOn(host) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen({ port: 0, host }, () => resolve(server));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

// netstat answers with no row for any port; any other tool is unexpected.
function emptyNetstat(file, _args, _options, callback) {
  if (String(file).toLowerCase().includes("netstat")) {
    callback(null, "", "");
    return;
  }
  callback(new Error(`unexpected command: ${file}`));
}

function windowsHarness(port, { capturePySpyDumpFn } = {}) {
  const execFileCalls = [];
  const dialogs = fakeDialogWindows();
  const mainWindow = {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    isMinimized: () => false,
    restore() {},
    show() {},
    focus() {},
    loadedFiles: [],
    webContents: {
      isDestroyed: () => false,
      loadFile(file) { mainWindow.loadedFiles.push(String(file)); },
      send() {},
    },
  };
  const timers = fakeTimers();
  const built = harness({
    port,
    mainWindow,
    timers,
    BrowserWindow: dialogs.FakeBrowserWindow,
    execFileFn: (file, args, options, callback) => {
      execFileCalls.push([String(file), ...(args || [])].join(" "));
      return emptyNetstat(file, args, options, callback);
    },
    capturePySpyDumpFn,
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
  });
  return { ...built, dialogs, mainWindow, timers, execFileCalls };
}

// Settle recovery: let promises run, and fire each one-shot timer it arms (the
// waits between bind checks) until nothing new is pending. `beforeFire` sees
// each timer first, so a test can release the port partway through the wait.
// Returns the total milliseconds the fired timers stood for.
async function settle(timers, { beforeFire = async () => {} } = {}) {
  let elapsed = 0;
  for (let round = 0; round < 400; round += 1) {
    for (let i = 0; i < 5; i += 1) await flush();
    if (!timers.pending.length) return elapsed;
    const due = timers.pending.splice(0);
    for (const timer of due) {
      await beforeFire(timer, elapsed);
      elapsed += timer.ms || 0;
      timer.fn();
    }
  }
  return elapsed;
}

// Recovery kills and respawns only a gateway this app spawned, so start one
// first, then fail an update install, which hands the window to recovery.
// Returns how many spawns recovery itself made.
async function spawnsDuringRecovery({ supervisor, mainWindow, spawnCalls, timers }, settleOptions) {
  await supervisor.start();
  timers.pending.splice(0);
  // The tree kill would ask the real OS about the fake child's pid; a child
  // that already exited skips it, and the port is still what recovery checks.
  for (const call of spawnCalls) call.child.exitCode = 1;
  const before = spawnCalls.length;
  supervisor.onInstallDispatched();
  supervisor.onInstallFailed(mainWindow);
  if (settleOptions && settleOptions.afterRecoveryStarts) settleOptions.afterRecoveryStarts();
  mainWindow.loadedFiles.splice(0);
  const elapsed = await settle(timers, settleOptions);
  return { spawned: spawnCalls.length - before, elapsed };
}

const { WINDOWS_LOCAL_GATEWAY_WAIT_MS } = require("../gateway-wait");

const loadedSplash = (mainWindow) => mainWindow.loadedFiles.some((f) => f.endsWith("loading.html"));

test("wedge recovery on a port netstat does not list and that never frees shows restart-required after the wait", async () => {
  const held = await listenOn("127.0.0.1");
  const port = held.address().port;
  try {
    const built = windowsHarness(port);
    const { spawned, elapsed } = await spawnsDuringRecovery(built);

    assert.strictEqual(spawned, 0, "no gateway is started into the held port");
    assert.ok(elapsed >= WINDOWS_LOCAL_GATEWAY_WAIT_MS, `the refusal is waited out first (${elapsed}ms)`);
    assert.ok(loadedSplash(built.mainWindow), "the loading screen shows while it waits");
    assert.ok(
      built.logs.some((l) => l.includes("port not confirmed free after force-stop (held by an unlisted owner)")),
      built.logs.join("\n"),
    );
    assert.strictEqual(built.dialogs.windows.length, 1, "the restart-required dialog is shown");
    const shown = decodeURIComponent(built.dialogs.windows[0].loaded.join("\n"));
    assert.match(shown, /verify what(?:'|&#39;|&#x27;)s using port/);
  } finally {
    await closeServer(held);
  }
});

test("wedge recovery on a free port with no netstat owner still respawns", async () => {
  const probe = await listenOn("127.0.0.1");
  const port = probe.address().port;
  await closeServer(probe);

  const built = windowsHarness(port);
  const { spawned } = await spawnsDuringRecovery(built);

  // The respawn then polls the backend on the real clock until the window goes.
  built.mainWindow.destroyed = true;

  assert.strictEqual(spawned, 1, `the gateway is respawned once:\n${built.logs.join("\n")}`);
  assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
  assert.ok(!built.logs.some((l) => l.includes("port not confirmed free after force-stop")));
  assert.ok(!built.logs.some((l) => l.includes("waiting up to")), "a free port is not waited on");
});

test("wedge recovery on a port that refuses a bind at first and frees shortly after still respawns", async () => {
  let held = await listenOn("127.0.0.1");
  const port = held.address().port;
  const built = windowsHarness(port);
  try {
    // The port frees three seconds into the wait.
    const { spawned, elapsed } = await spawnsDuringRecovery(built, {
      beforeFire: async (_timer, sofar) => {
        if (held && sofar >= 3000) {
          await closeServer(held);
          held = null;
        }
      },
    });
    built.mainWindow.destroyed = true;

    assert.strictEqual(held, null, "the port was released during the wait");
    assert.strictEqual(spawned, 1, `the gateway is respawned once:\n${built.logs.join("\n")}`);
    assert.ok(elapsed < WINDOWS_LOCAL_GATEWAY_WAIT_MS, `respawned before the wait ran out (${elapsed}ms)`);
    assert.ok(loadedSplash(built.mainWindow), "the loading screen shows while it waits");
    assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
    assert.ok(built.logs.some((l) => l.includes(`:${port} became bindable after`)), built.logs.join("\n"));
    assert.ok(!built.logs.some((l) => l.includes("port not confirmed free after force-stop")));
  } finally {
    built.mainWindow.destroyed = true;
    if (held) await closeServer(held);
  }
});

test("an update install dispatched while recovery waits for the port stops the respawn", async () => {
  let held = await listenOn("127.0.0.1");
  const port = held.address().port;
  const built = windowsHarness(port);
  try {
    let dispatched = false;
    const { spawned } = await spawnsDuringRecovery(built, {
      beforeFire: async (_timer, sofar) => {
        if (!dispatched && sofar >= 1000) {
          built.supervisor.onInstallDispatched();
          dispatched = true;
        }
        if (held && sofar >= 3000) {
          await closeServer(held);
          held = null;
        }
      },
    });
    built.mainWindow.destroyed = true;

    assert.ok(dispatched, "the install was dispatched during the wait");
    assert.strictEqual(spawned, 0, `no gateway races the install:\n${built.logs.join("\n")}`);
    assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
  } finally {
    built.mainWindow.destroyed = true;
    if (held) await closeServer(held);
  }
});

test("an update that is dispatched and fails during the port wait leaves one recovery, which respawns once", async () => {
  let held = await listenOn("127.0.0.1");
  const port = held.address().port;
  const built = windowsHarness(port);
  try {
    let dispatched = false;
    let failed = false;
    const { spawned } = await spawnsDuringRecovery(built, {
      beforeFire: async (_timer, sofar) => {
        // The stale wait stops at its next check, so the install fails in the
        // same tick; the failure's own recovery then waits for the port.
        if (!dispatched && sofar >= 1000) {
          built.supervisor.onInstallDispatched();
          dispatched = true;
          built.supervisor.onInstallFailed(built.mainWindow);
          failed = true;
        }
        if (held && sofar >= 3000) {
          await closeServer(held);
          held = null;
        }
      },
    });
    built.mainWindow.destroyed = true;

    assert.ok(failed, "the install was dispatched and failed during the wait");
    assert.strictEqual(held, null, "the port was released during the wait");
    assert.strictEqual(spawned, 1, `only the failed install's recovery respawns:\n${built.logs.join("\n")}`);
    assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
  } finally {
    built.mainWindow.destroyed = true;
    if (held) await closeServer(held);
  }
});

test("an update dispatched before recovery reaches the port check stops that recovery's respawn", async () => {
  const probe = await listenOn("127.0.0.1");
  const port = probe.address().port;
  await closeServer(probe);
  const built = windowsHarness(port);

  // Recovery has started and is in its first await (the dump and the kill run
  // before the port is checked) when the install is dispatched.
  const { spawned } = await spawnsDuringRecovery(built, {
    afterRecoveryStarts: () => built.supervisor.onInstallDispatched(),
  });
  built.mainWindow.destroyed = true;

  assert.strictEqual(spawned, 0, `no gateway races the install:\n${built.logs.join("\n")}`);
  assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
});

test("a recovery overtaken by an install that fails leaves the failed install's gateway alone", async () => {
  const probe = await listenOn("127.0.0.1");
  const port = probe.address().port;
  await closeServer(probe);

  // Recovery A's stack dump is held open. While it waits, an install is
  // dispatched and fails, and that failure's recovery B respawns.
  let releaseFirstDump;
  let dumps = 0;
  const built = windowsHarness(port, {
    capturePySpyDumpFn: () => {
      dumps += 1;
      if (dumps === 1) return new Promise((resolve) => { releaseFirstDump = resolve; });
      return Promise.resolve();
    },
  });
  await built.supervisor.start();
  built.timers.pending.splice(0);
  for (const call of built.spawnCalls) call.child.exitCode = 1;
  built.supervisor.onInstallDispatched();
  built.supervisor.onInstallFailed(built.mainWindow);
  await settle(built.timers);
  assert.ok(releaseFirstDump, "recovery A is waiting on its dump");

  built.supervisor.onInstallDispatched();
  built.supervisor.onInstallFailed(built.mainWindow);
  await settle(built.timers);
  const replacement = built.spawnCalls[built.spawnCalls.length - 1];
  assert.strictEqual(replacement.child.exitCode, null, "recovery B spawned a fresh gateway");

  const killsBefore = built.execFileCalls.filter((c) => /taskkill/i.test(c)).length;
  releaseFirstDump();
  await settle(built.timers);
  built.mainWindow.destroyed = true;

  assert.strictEqual(
    built.execFileCalls.filter((c) => /taskkill/i.test(c)).length,
    killsBefore,
    `stale recovery A must not kill B's gateway:\n${built.execFileCalls.join("\n")}`,
  );
  assert.strictEqual(replacement.child.killed, false);
  assert.strictEqual(built.spawnCalls.filter((c) => c.child === replacement.child).length, 1);
});

test("an install dispatched during the port wait ends that wait without the dialog", async () => {
  const held = await listenOn("127.0.0.1");
  const port = held.address().port;
  const built = windowsHarness(port);
  try {
    let dispatched = false;
    const { spawned, elapsed } = await spawnsDuringRecovery(built, {
      beforeFire: async (_timer, sofar) => {
        if (!dispatched && sofar >= 2000) {
          built.supervisor.onInstallDispatched();
          dispatched = true;
        }
      },
    });
    built.mainWindow.destroyed = true;

    assert.ok(dispatched);
    assert.strictEqual(spawned, 0);
    assert.ok(elapsed < WINDOWS_LOCAL_GATEWAY_WAIT_MS, `the stale wait stops early (${elapsed}ms)`);
    assert.strictEqual(built.dialogs.windows.length, 0, "no restart-required dialog");
    assert.ok(built.logs.some((l) => l.includes("bind wait on :") && l.includes("abandoned")), built.logs.join("\n"));
  } finally {
    await closeServer(held);
  }
});
