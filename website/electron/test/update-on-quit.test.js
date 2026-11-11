// Updates install on the user's quit, never while the app runs (D-a to D-d, and
// the public lane). A click ARMS; the apply runs in Electron main after the
// gateway has fully stopped; the app then exits without relaunching; an apply
// the quit left behind is finished at the next app start, before the gateway
// spawns, and Restart now is a plain relaunch.
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const cp = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const { initAutoUpdate } = require("../auto-update");
const { createUpdateArm } = require("../runtime/update/update-arm");
const { createGatewayArm } = require("../runtime/update/gateway-arm");
const { runStartupApply } = require("../runtime/update/startup-apply");
const { createStartupGatewayProbe } = require("../runtime/update/gateway-probe");
const { readPolicyGovernance } = require("../runtime/update/policy-source");

// The policy file is read from the data home, so a test names the verdict
// instead of reading the host's own.
const startupApply = (opts) => runStartupApply({ readGovernance: () => "ungoverned", ...opts });
const { makeDeps } = require("./auto-update-test-helpers");

const MANAGED = {
  managedBy: "internal-registry",
  updateCommand: "pkgtool update kirocrew",
  checkCommand: "pkgtool check kirocrew",
};

function memoryStore(seed = {}) {
  const m = new Map(Object.entries(seed));
  return {
    get: (k, d) => (m.has(k) ? m.get(k) : d),
    set: (k, v) => m.set(k, v),
    delete: (k) => m.delete(k),
    has: (k) => m.has(k),
  };
}

// Let every chained setImmediate hop of the stubbed spawn and the awaits behind
// it run. Counts event-loop turns, never wall-clock time, so a slow loop cannot
// fail a correct apply.
async function drainTurns() {
  for (let i = 0; i < 100; i += 1) await new Promise((r) => setImmediate(r));
}

// A scripted child_process.spawn. `script(command)` returns {code, out}.
function stubSpawn(script) {
  const commands = [];
  const orig = cp.spawn;
  cp.spawn = (command) => {
    commands.push(command);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const { code = 0, out = "" } = script(command) || {};
    setImmediate(() => {
      if (out) child.stdout.emit("data", Buffer.from(out));
      child.emit("close", code, null);
    });
    return child;
  };
  return { commands, restore: () => { cp.spawn = orig; } };
}

// A fixture whose app records exit, relaunch and quit, and whose before-quit
// listeners can be fired like Electron does.
function managedFixture({ stopGateway, isPolicyGoverned, autoDownload = false, gatewayArm = null, startupApplyError = null, store = memoryStore() } = {}) {
  const { deps, states, stateNames } = makeDeps({ externallyManaged: MANAGED });
  const events = [];
  const quitHandlers = [];
  deps.app.once = (ev, fn) => { if (ev === "before-quit") quitHandlers.push(fn); };
  deps.app.removeListener = (ev, fn) => {
    const i = quitHandlers.indexOf(fn);
    if (ev === "before-quit" && i >= 0) quitHandlers.splice(i, 1);
  };
  deps.app.exit = (c) => events.push(`exit:${c}`);
  deps.app.relaunch = () => events.push("relaunch");
  deps.app.quit = () => events.push("quit");
  deps.stopGateway = stopGateway || (async () => { events.push("gateway-stopped"); });
  deps.getAutoDownloadPreference = () => autoDownload;
  if (isPolicyGoverned) deps.isPolicyGoverned = isPolicyGoverned;
  deps.updateArm = createUpdateArm({ store });
  deps.gatewayArm = gatewayArm;
  deps.startupApplyError = startupApplyError;
  const fireQuit = () => {
    const fn = quitHandlers.shift();
    if (!fn) return { prevented: false };
    let prevented = false;
    fn({ preventDefault: () => { prevented = true; } });
    return { prevented };
  };
  const settle = drainTurns;
  return { deps, states, stateNames, events, fireQuit, settle, store, quitHandlers };
}

async function foundFixture(opts) {
  const f = managedFixture(opts);
  // The check offers 2.0.0 until the update command has run, then offers nothing.
  let applied = false;
  const sp = stubSpawn((c) => {
    if (!c.includes("check")) { applied = true; return { code: 0 }; }
    return applied ? { code: 1 } : { code: 0, out: "2.0.0\n" };
  });
  const u = initAutoUpdate(f.deps);
  await u.check();
  sp.commands.length = 0;
  f.events.length = 0;
  return { ...f, u, sp };
}

// ---------------------------------------------------------------------------
// D-a: install() only ARMS.
// ---------------------------------------------------------------------------

test("D-a: install() arms and never runs the update command or relaunches", async (t) => {
  const f = await foundFixture();
  t.after(f.sp.restore);
  await f.u.install();
  assert.deepStrictEqual(f.sp.commands, [], "no updateCommand while the app runs");
  assert.ok(!f.events.includes("relaunch") && !f.events.some((e) => e.startsWith("exit")), "no relaunch, no exit");
  assert.ok(!f.events.includes("gateway-stopped"), "the gateway is not stopped by a click");
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0");
  const last = f.states[f.states.length - 1];
  assert.strictEqual(last.state, "downloaded");
  assert.strictEqual(last.armed, true);
});

test("D-a: install() tells a running gateway (S1) and still arms when none is up", async (t) => {
  const told = [];
  const up = await foundFixture({ gatewayArm: { arm: async () => { told.push("arm"); return true; }, armedVersion: async () => null } });
  t.after(up.sp.restore);
  await up.u.install();
  assert.deepStrictEqual(told, ["arm"]);

  const down = await foundFixture({ gatewayArm: { arm: async () => { throw new Error("connection refused"); }, armedVersion: async () => null } });
  t.after(down.sp.restore);
  await down.u.install();
  assert.strictEqual(down.deps.updateArm.read().version, "2.0.0", "main holds the arm when no gateway is up");
});

test("D-a: the gateway client posts S1 and reads update_armed_version", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, method: init.method, body: init.body });
    return { status: init.method === "POST" ? 202 : 200, json: async () => ({ update_armed_version: "2.0.0" }) };
  };
  const c = createGatewayArm({ backendUrl: "http://127.0.0.1:1", fetchGatewayAuth: async () => ({ value: "tok" }), fetchImpl });
  assert.strictEqual(await c.arm(), true);
  const post = calls.find((x) => x.method === "POST");
  assert.strictEqual(post.body, JSON.stringify({ when: "next-start" }));
  assert.ok(post.url.startsWith("http://127.0.0.1:1/api/update?token=tok"));
  assert.strictEqual(await c.armedVersion(), "2.0.0");
  const none = createGatewayArm({ backendUrl: "http://127.0.0.1:1", fetchGatewayAuth: async () => ({ value: "" }), fetchImpl });
  assert.strictEqual(await none.arm(), false, "no credential means no gateway to tell");
  assert.strictEqual(await none.armedVersion(), null);
});

// A gateway older than the arm contract applies and restarts on POST /api/update
// {when: "next-start"}, so the arm is sent only to one whose status proves it
// speaks the contract: the update_armed_version key is present (null when idle).
// A missing key is an older gateway, which is not the same as "nothing armed".
test("D-a: no arm POST goes to a gateway whose status lacks update_armed_version", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ method: init.method, url });
    return { status: init.method === "POST" ? 200 : 200, json: async () => ({ version: "1.0.0" }) };
  };
  const c = createGatewayArm({ backendUrl: "http://127.0.0.1:1", fetchGatewayAuth: async () => ({ value: "tok" }), fetchImpl });
  assert.strictEqual(await c.arm(), false);
  assert.deepStrictEqual(calls.map((x) => x.method), ["GET"], "only the status was read; nothing was posted");
  assert.strictEqual(await c.armedVersion(), null, "a missing key is not an empty arm");
});

test("D-a: an idle gateway that reports update_armed_version null is sent the arm", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push(init.method);
    return { status: init.method === "POST" ? 202 : 200, json: async () => ({ update_armed_version: null }) };
  };
  const c = createGatewayArm({ backendUrl: "http://127.0.0.1:1", fetchGatewayAuth: async () => ({ value: "tok" }), fetchImpl });
  assert.strictEqual(await c.armedVersion(), "");
  calls.length = 0;
  assert.strictEqual(await c.arm(), true);
  assert.deepStrictEqual(calls, ["GET", "POST"]);
  assert.strictEqual(typeof c.cancel, "undefined", "the unused cancel client is gone");
});

// ---------------------------------------------------------------------------
// D-b: the apply runs on the user's quit, after the gateway stopped, no relaunch.
// ---------------------------------------------------------------------------

test("D-b: the quit applies an armed update after the gateway stopped, then exits WITHOUT relaunching", async (t) => {
  const f = await foundFixture();
  t.after(f.sp.restore);
  await f.u.install();
  f.events.length = 0;
  const orig = cp.spawn;
  cp.spawn = (command, ...rest) => { f.events.push(`run:${command}`); return orig(command, ...rest); };
  t.after(() => { cp.spawn = orig; });
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, true, "the quit is held while the apply runs");
  await f.settle();
  assert.strictEqual(f.events[0], "gateway-stopped");
  assert.strictEqual(f.events[1], "run:pkgtool update kirocrew", "the update command runs only after the stop");
  assert.ok(f.events.includes("exit:0"));
  assert.ok(!f.events.includes("relaunch"), "a quit-time apply never relaunches");
  assert.strictEqual(f.deps.updateArm.read(), null, "a finished apply clears the arm");
});

test("D-b: a governed install leaves the arm alone: the quit applies nothing and is not held", async (t) => {
  const policy = { governed: false };
  const f = await foundFixture({ isPolicyGoverned: () => policy.governed });
  t.after(f.sp.restore);
  await f.u.install();
  // Policy governance arrives after the arm: the policy provider owns the install.
  policy.governed = true;
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, false, "the quit is not held for a marker command");
  await f.settle();
  assert.deepStrictEqual(f.sp.commands, [], "the marker's updateCommand never ran");
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0", "the arm stays for when governance lifts");
});

test("D-b: governance that arrives while the gateway stops also skips the apply", async (t) => {
  const policy = { governed: false };
  const f = await foundFixture({
    isPolicyGoverned: () => policy.governed,
    stopGateway: async () => { policy.governed = true; },
  });
  t.after(f.sp.restore);
  await f.u.install();
  f.fireQuit();
  await f.settle();
  assert.deepStrictEqual(f.sp.commands, [], "the marker's updateCommand never ran");
  assert.ok(f.events.includes("exit:0"), "the user's quit is still honored");
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0");
});

test("D-b: a quit with nothing armed and auto-download off is not held", async (t) => {
  const f = await foundFixture({ autoDownload: false });
  t.after(f.sp.restore);
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, false);
  assert.deepStrictEqual(f.sp.commands, []);
});

// ---------------------------------------------------------------------------
// D-c: an interrupted or failed quit-time apply stays armed; the next app start
// finishes it before the gateway spawns, then relaunches once.
// ---------------------------------------------------------------------------

test("D-c: a failed quit-time apply keeps the arm and does not relaunch", async (t) => {
  const f = managedFixture();
  const sp = stubSpawn((c) => (c.includes("check") ? { code: 0, out: "2.0.0\n" } : { code: 1 }));
  t.after(sp.restore);
  const u = initAutoUpdate(f.deps);
  await u.check();
  await u.install();
  f.events.length = 0;
  f.fireQuit();
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0", "the arm stays so the next start retries");
  assert.ok(f.events.includes("exit:0"));
  assert.ok(!f.events.includes("relaunch"));
});

test("D-c: a gateway still running at quit leaves the arm and applies nothing", async (t) => {
  const { gatewayStillRunningError } = require("../gateway-stop");
  const f = await foundFixture({ stopGateway: async () => { throw gatewayStillRunningError(18800); } });
  t.after(f.sp.restore);
  await f.u.install();
  f.fireQuit();
  await f.settle();
  assert.deepStrictEqual(f.sp.commands, []);
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0");
});

function startFixture({ code = 0, check = "" } = {}) {
  const events = [];
  const store = memoryStore();
  const arm = createUpdateArm({ store });
  arm.arm("2.0.0");
  const sp = stubSpawn((c) => (c.includes("check") ? (check ? { code: 0, out: check } : { code: 1 }) : { code }));
  const app = {
    getVersion: () => "1.0.0",
    relaunch: () => events.push("relaunch"),
    exit: (c) => events.push(`exit:${c}`),
  };
  const log = { info: () => {}, error: () => {} };
  return { events, store, arm, sp, app, log };
}

test("D-c: the next app start applies the leftover arm, relaunches ONCE and exits", async (t) => {
  const f = startFixture();
  t.after(f.sp.restore);
  const r = await startupApply({ app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(r.outcome, "relaunching");
  assert.deepStrictEqual(f.events, ["relaunch", "exit:0"]);
  assert.deepStrictEqual(f.sp.commands, ["pkgtool update kirocrew", "pkgtool check kirocrew"]);
  assert.strictEqual(f.arm.read(), null, "cleared before the relaunch, so the new bundle cannot loop");
});

test("D-c: a failed start-time apply starts the old version, clears the arm and records the error", async (t) => {
  const f = startFixture({ code: 1 });
  t.after(f.sp.restore);
  const r = await startupApply({ app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(r.outcome, "failed");
  assert.deepStrictEqual(f.events, [], "no relaunch, no exit: the installed version starts");
  assert.strictEqual(f.arm.read(), null);
  assert.match(f.arm.takeApplyError().message, /exited 1/);
});

test("D-c: with no arm, or an arm for the version already running, nothing runs", async (t) => {
  const none = startFixture();
  t.after(none.sp.restore);
  none.arm.clear();
  assert.strictEqual((await startupApply({ app: none.app, store: none.store, managed: MANAGED, managedPath: () => "", log: none.log })).outcome, "none");
  const same = startFixture();
  t.after(same.sp.restore);
  same.app.getVersion = () => "2.0.0";
  assert.strictEqual((await startupApply({ app: same.app, store: same.store, managed: MANAGED, managedPath: () => "", log: same.log })).outcome, "already-installed");
  assert.deepStrictEqual(same.sp.commands, []);
  const notManaged = startFixture();
  t.after(notManaged.sp.restore);
  assert.strictEqual((await startupApply({ app: notManaged.app, store: notManaged.store, managed: null, managedPath: () => "", log: notManaged.log })).outcome, "none");
});

test("D-c: with no arm the start path probes nothing, reads no policy and never holds the gateway start", async (t) => {
  const calls = [];
  const spy = {
    probeGateway: async () => { calls.push("probe"); return { running: false }; },
    readGovernance: () => { calls.push("policy"); return "ungoverned"; },
  };
  const noArm = startFixture();
  t.after(noArm.sp.restore);
  noArm.arm.clear();
  const r = await runStartupApply({ app: noArm.app, store: noArm.store, managed: MANAGED, managedPath: () => "", log: noArm.log, ...spy });
  assert.strictEqual(r.outcome, "none");
  const notManaged = startFixture();
  t.after(notManaged.sp.restore);
  await runStartupApply({ app: notManaged.app, store: notManaged.store, managed: null, managedPath: () => "", log: notManaged.log, ...spy });
  assert.deepStrictEqual(calls, [], "neither the port probe nor the policy read ran");
  assert.deepStrictEqual(noArm.events, [], "no relaunch and no exit: main goes straight on to gateway.start()");
});

test("D-c: the start-time port probe only reads listeners; it never binds or listens on a port", () => {
  for (const file of ["gateway-probe.js", "policy-source.js"]) {
    const source = fs.readFileSync(path.join(__dirname, "..", "runtime", "update", file), "utf8");
    assert.doesNotMatch(source, /\b(?:createServer|listen|bind)\s*\(|require\("net"\)/, file);
  }
});

test("D-c: the lane shows a failed start-time apply once, as an install error", () => {
  const f = managedFixture({ startupApplyError: { version: "2.0.0", message: "managed update command exited 1" } });
  initAutoUpdate(f.deps);
  const err = f.states.find((s) => s.state === "error");
  assert.ok(err, "the error is emitted");
  assert.strictEqual(err.phase, "install");
});

// ---------------------------------------------------------------------------
// D-d: updateAPI.relaunch() is a plain quit and relaunch.
// ---------------------------------------------------------------------------

test("D-d: relaunch() quits and relaunches without running any update command", async (t) => {
  const f = await foundFixture();
  t.after(f.sp.restore);
  assert.strictEqual(typeof f.u.relaunch, "function");
  f.u.relaunch();
  assert.deepStrictEqual(f.events, ["relaunch", "quit"]);
  assert.deepStrictEqual(f.sp.commands, [], "relaunch itself never runs updateCommand");
});

test("D-d: the preload bridge and IPC registrar expose relaunch", () => {
  const preload = fs.readFileSync(path.join(__dirname, "..", "preload.js"), "utf8");
  assert.match(preload, /relaunch: \(\) => ipcRenderer\.invoke\("update:relaunch"\)/);
  const registrar = fs.readFileSync(path.join(__dirname, "..", "ipc-registrar.js"), "utf8");
  assert.match(registrar, /ipcMain\.handle\("update:relaunch"/);
});

// ---------------------------------------------------------------------------
// The public (electron-updater) lane: download only, install on quit.
// ---------------------------------------------------------------------------

function feedFixture({ autoDownload = false, osPlatform = null } = {}) {
  const { deps, calls, emit, states } = makeDeps();
  if (osPlatform) deps.osPlatform = osPlatform;
  const notifications = [];
  deps.Notification = function Notification(options) { notifications.push(options); return { show: () => {} }; };
  const quitHandlers = [];
  const events = [];
  deps.app.once = (ev, fn) => { if (ev === "before-quit") quitHandlers.push(fn); };
  deps.app.removeListener = (ev, fn) => {
    const i = quitHandlers.indexOf(fn);
    if (ev === "before-quit" && i >= 0) quitHandlers.splice(i, 1);
  };
  deps.app.quit = () => events.push("quit");
  deps.app.relaunch = () => events.push("relaunch");
  deps.stopGateway = async () => { events.push("gateway-stopped"); };
  deps.getAutoDownloadPreference = () => autoDownload;
  const u = initAutoUpdate(deps);
  const fireQuit = () => {
    const fn = quitHandlers[quitHandlers.length - 1];
    if (!fn) return { prevented: false };
    quitHandlers.pop();
    let prevented = false;
    fn({ preventDefault: () => { prevented = true; } });
    return { prevented };
  };
  return { u, deps, calls, emit, states, events, fireQuit, notifications, settle: drainTurns };
}

test("public lane: a click never stops the gateway or calls quitAndInstall; it arms", async () => {
  const f = feedFixture();
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  assert.deepStrictEqual(f.calls.quitAndInstall, [], "never quitAndInstall from a click while running");
  assert.ok(!f.events.includes("gateway-stopped"));
  const last = f.states[f.states.length - 1];
  assert.strictEqual(last.state, "downloaded");
  assert.strictEqual(last.armed, true);
});

test("public lane: the user's quit installs the armed build and does not ask for a relaunch", async () => {
  const f = feedFixture({ autoDownload: false });
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, true);
  await f.settle();
  assert.strictEqual(f.calls.quitAndInstall.length, 1);
  assert.deepStrictEqual(f.calls.quitAndInstall[0], [false, false], "isForceRunAfter is false: no relaunch after a quit-time install");
});

test("public lane: Restart now (relaunch) on an armed stage keeps the relaunch the user asked for", async () => {
  const f = feedFixture();
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  assert.strictEqual(typeof f.u.relaunch, "function");
  f.u.relaunch();
  assert.deepStrictEqual(f.events, ["quit"], "the quit's install brings the app back; no second relaunch");
  f.fireQuit();
  await f.settle();
  assert.deepStrictEqual(f.calls.quitAndInstall[0], [false, true], "a user-asked restart relaunches after the install");
});

test("public lane: Restart now whose quit-time install fails still relaunches the app", async () => {
  const f = feedFixture();
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  f.u.relaunch();
  f.fireQuit();
  await f.settle();
  assert.strictEqual(f.calls.quitAndInstall.length, 1, "the quit dispatched the install");
  assert.ok(!f.events.includes("relaunch"), "nothing relaunches before the installer answers");
  f.emit("error", new Error("installer rejected the stage"));
  const tail = f.events.slice(f.events.lastIndexOf("gateway-stopped") + 1);
  assert.deepStrictEqual(tail, ["relaunch", "quit"], "a failed install ends in the relaunch the user asked for, then the quit");
});

test("public lane: a plain quit whose install fails does not relaunch", async () => {
  const f = feedFixture();
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  f.fireQuit();
  await f.settle();
  f.emit("error", new Error("installer rejected the stage"));
  assert.ok(!f.events.includes("relaunch"), "no Restart now was asked for");
  assert.strictEqual(f.events[f.events.length - 1], "quit");
});

// ---------------------------------------------------------------------------
// Carried over from the click-install tests this change retired: the same
// guarantees, now on the quit path and on relaunch().
// ---------------------------------------------------------------------------

test("auto-download on: a discovered update applies on the user's quit with no click", async (t) => {
  const f = await foundFixture({ autoDownload: true });
  t.after(f.sp.restore);
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, true);
  await f.settle();
  assert.deepStrictEqual(f.sp.commands.slice(0, 1), ["pkgtool update kirocrew"]);
  assert.ok(f.events.includes("exit:0"));
  assert.ok(!f.events.includes("relaunch"));
});

test("an apply on quit the check still offers keeps the arm and exits without relaunching", async (t) => {
  const f = managedFixture();
  // The update command exits 0 but the check still offers 2.0.0: the install did not change.
  const sp = stubSpawn((c) => (c.includes("check") ? { code: 0, out: "2.0.0\n" } : { code: 0 }));
  t.after(sp.restore);
  const u = initAutoUpdate(f.deps);
  await u.check();
  await u.install();
  f.events.length = 0;
  f.fireQuit();
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0");
  assert.ok(f.events.includes("exit:0"));
  assert.ok(!f.events.includes("relaunch"));
});

function withMarkerFile(t) {
  const os = require("node:os");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-relaunch-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const exe = path.join(dir, "app-binary");
  fs.writeFileSync(exe, "");
  return { exe, missing: path.join(dir, "gone", "app-binary") };
}

test("relaunch() goes through the marker's relaunchPath when it is on disk", async (t) => {
  const { exe } = withMarkerFile(t);
  const f = managedFixture();
  f.deps.externallyManaged = { ...MANAGED, relaunchPath: exe };
  const launches = [];
  f.deps.app.relaunch = (o) => launches.push(o || null);
  const u = initAutoUpdate(f.deps);
  u.relaunch();
  assert.strictEqual(launches.length, 1);
  assert.strictEqual(launches[0].execPath, exe);
});

test("relaunch() falls back to the bare relaunch when relaunchPath is not on disk", async (t) => {
  const { missing } = withMarkerFile(t);
  const f = managedFixture();
  f.deps.externallyManaged = { ...MANAGED, relaunchPath: missing };
  const launches = [];
  f.deps.app.relaunch = (o) => launches.push(o || null);
  const u = initAutoUpdate(f.deps);
  u.relaunch();
  assert.deepStrictEqual(launches, [null]);
});

test("the start-time apply relaunches through relaunchPath too", async (t) => {
  const { exe } = withMarkerFile(t);
  const f = startFixture();
  t.after(f.sp.restore);
  const launches = [];
  f.app.relaunch = (o) => launches.push(o || null);
  const r = await startupApply({ app: f.app, store: f.store, managed: { ...MANAGED, relaunchPath: exe }, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(r.outcome, "relaunching");
  assert.strictEqual(launches[0].execPath, exe);
});

test("an arm the dashboard made (the gateway's update_armed_version) is adopted and applied on quit", async (t) => {
  const f = await foundFixture({ gatewayArm: { arm: async () => true, armedVersion: async () => "2.0.0" } });
  t.after(f.sp.restore);
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0", "main adopts what the gateway holds as armed");
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, true, "auto-download is off, yet the arm makes the quit apply it");
  await f.settle();
  assert.ok(f.sp.commands.includes("pkgtool update kirocrew"));
  assert.ok(!f.events.includes("relaunch"));
});

test("a gateway that withdraws the arm it made takes main's adopted copy with it", async (t) => {
  let held = "2.0.0";
  const f = await foundFixture({ gatewayArm: { arm: async () => true, armedVersion: async () => held } });
  t.after(f.sp.restore);
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0");
  held = "";
  await f.u.check();
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read(), null);
});

// ---------------------------------------------------------------------------
// A governed install (edition lane): the dashboard arms through POST /api/update
// and main, which never discovers a version there, must still adopt the arm.
// ---------------------------------------------------------------------------

test("a governed-from-start install does not adopt a dashboard arm: the policy provider owns it", async (t) => {
  const timers = [];
  const origSetInterval = global.setInterval;
  global.setInterval = (fn, ms) => { timers.push({ fn, ms }); return { unref() {} }; };
  t.after(() => { global.setInterval = origSetInterval; });
  const sp = stubSpawn((c) => (c.includes("check") ? { code: 0, out: "2.0.0\n" } : { code: 0 }));
  t.after(sp.restore);
  // Governance is set BEFORE the arm exists, so no check ever discovered a version.
  const f = managedFixture({ isPolicyGoverned: () => true, gatewayArm: { arm: async () => true, armedVersion: async () => "2.0.0" } });
  initAutoUpdate(f.deps);
  assert.strictEqual(f.deps.updateArm.read(), null);
  const poll = timers.find((x) => x.ms === 30 * 1000);
  assert.ok(poll, "the gateway arm poll is installed");
  poll.fn();
  await f.settle();
  assert.strictEqual(f.deps.updateArm.read(), null, "main does not hold an arm it would never apply");
  assert.deepStrictEqual(f.fireQuit(), { prevented: false }, "the quit is not held");
  await f.settle();
  assert.ok(!sp.commands.includes("pkgtool update kirocrew"), "the marker's updateCommand never ran");
  assert.strictEqual(f.deps.updateArm.wasGoverned(), true, "the verdict is kept for the next start");
});

test("a governed check stand-down keeps the arm and the verdict, and the quit still applies nothing", async (t) => {
  const policy = { governed: false };
  const f = await foundFixture({ isPolicyGoverned: () => policy.governed });
  t.after(f.sp.restore);
  await f.u.install();
  policy.governed = true;
  await f.u.check();
  assert.strictEqual(f.deps.updateArm.read().version, "2.0.0", "the stand-down keeps the arm");
  assert.strictEqual(f.deps.updateArm.wasGoverned(), true);
  f.fireQuit();
  await f.settle();
  assert.deepStrictEqual(f.sp.commands, [], "a governed quit runs no marker command");
});

// ---------------------------------------------------------------------------
// The start-time apply never replaces files under a gateway of this bundle.
// ---------------------------------------------------------------------------

test("D-c: a gateway from this bundle still running at start keeps the arm, skips the apply and names the stop command", async (t) => {
  const f = startFixture();
  t.after(f.sp.restore);
  const notices = [];
  const r = await startupApply({
    app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log,
    probeGateway: async () => ({ running: true, stopCommand: "kirocrew stop --port 18800" }),
    notifyGatewayRunning: (cmd) => notices.push(cmd),
  });
  assert.strictEqual(r.outcome, "gateway-running");
  assert.deepStrictEqual(f.sp.commands, [], "no update command ran");
  assert.deepStrictEqual(f.events, [], "no relaunch, no exit");
  assert.strictEqual(f.arm.read().version, "2.0.0", "the arm stays");
  assert.deepStrictEqual(notices, ["kirocrew stop --port 18800"]);
});

test("D-c: a probe that cannot answer also defers the apply; a free port applies", async (t) => {
  const broken = startFixture();
  t.after(broken.sp.restore);
  const r = await startupApply({
    app: broken.app, store: broken.store, managed: MANAGED, managedPath: () => "/usr/bin", log: broken.log,
    probeGateway: async () => { throw new Error("ps failed"); },
  });
  assert.strictEqual(r.outcome, "gateway-running");
  assert.strictEqual(broken.arm.read().version, "2.0.0");
  const free = startFixture();
  t.after(free.sp.restore);
  const ok = await startupApply({
    app: free.app, store: free.store, managed: MANAGED, managedPath: () => "/usr/bin", log: free.log,
    probeGateway: async () => ({ running: false }),
  });
  assert.strictEqual(ok.outcome, "relaunching");
});

test("D-c: a start whose last recorded verdict was governed runs no marker command and keeps the arm", async (t) => {
  const f = startFixture();
  t.after(f.sp.restore);
  f.arm.recordGoverned(true);
  const r = await startupApply({
    app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log,
    probeGateway: async () => ({ running: false }),
  });
  assert.strictEqual(r.outcome, "governed");
  assert.deepStrictEqual(f.sp.commands, [], "the policy provider owns the install, so no updateCommand");
  assert.deepStrictEqual(f.events, [], "no relaunch, no exit");
  assert.strictEqual(f.arm.read().version, "2.0.0", "the arm stays");
  f.arm.recordGoverned(false);
  const again = await startupApply({ app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(again.outcome, "relaunching", "once governance is lifted the leftover arm applies");
});

test("D-c: a stored ungoverned verdict does not bypass a policy provider installed since", async (t) => {
  for (const current of ["governed", "unknown"]) {
    const f = startFixture();
    t.after(f.sp.restore);
    f.arm.recordGoverned(false);
    const r = await runStartupApply({
      app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log,
      probeGateway: async () => ({ running: false }),
      readGovernance: () => current,
    });
    assert.strictEqual(r.outcome, "governed", current);
    assert.deepStrictEqual(f.sp.commands, [], "the marker's update command did not run");
    assert.strictEqual(f.arm.read().version, "2.0.0", "the arm stays");
    assert.strictEqual(f.arm.wasGoverned(), true, `${current}: the quit before a live verdict must read governed`);
  }
});

test("the policy source reads a provider pin from the policy file, and a file it cannot read as unknown", () => {
  const files = new Map();
  const fs = {
    readFileSync: (p) => {
      if (!files.has(p)) throw Object.assign(new Error("gone"), { code: "ENOENT" });
      const v = files.get(p);
      if (v instanceof Error) throw v;
      return v;
    },
  };
  const read = () => readPolicyGovernance({ home: "/h", fs, path: path.posix });
  assert.strictEqual(read(), "ungoverned", "no policy file");
  files.set("/h/security_policy.json", JSON.stringify({ updates: { source: "https://x", min_version: "1.0.0" } }));
  assert.strictEqual(read(), "ungoverned", "source and floor pins leave the install's own updater in charge");
  files.set("/h/security_policy.json", JSON.stringify({ updates: { apply_command: "pkg up" } }));
  assert.strictEqual(read(), "governed");
  files.set("/h/security_policy.json", JSON.stringify({ updates: { platform_commands: { linux: { check_command: "pkg check" } } } }));
  assert.strictEqual(read(), "governed");
  files.set("/h/security_policy.json", JSON.stringify({ distribution: { source: "https://policy.example/p.json" } }));
  assert.strictEqual(read(), "unknown", "a file that declares a distribution source");
  files.set("/h/security_policy.json", JSON.stringify({ distribution: { source: "  " } }));
  assert.strictEqual(read(), "ungoverned", "an empty distribution source names nothing");
  files.delete("/h/security_policy.json");
  for (const name of ["KIROCREW_POLICY_URL", "KIROCREW_SECURITY_POLICY"]) {
    assert.strictEqual(readPolicyGovernance({ home: "/h", fs, path: path.posix, env: { [name]: "x" } }), "unknown", name);
    assert.strictEqual(readPolicyGovernance({ home: "/h", fs, path: path.posix, env: { [name]: "" } }), "ungoverned", `${name} empty`);
  }
  files.set("/h/security_policy.json", "{ not json");
  assert.strictEqual(read(), "unknown");
  files.set("/h/security_policy.json", Object.assign(new Error("denied"), { code: "EACCES" }));
  assert.strictEqual(read(), "unknown");
});

test("D-c: an unidentified gateway on a busy port defers the apply; a free port applies", async (t) => {
  const cases = [
    { binding: "bound", outcome: "gateway-running" },
    { binding: "unknown", outcome: "gateway-running" },
    { binding: "free", outcome: "relaunching" },
  ];
  for (const { binding, outcome } of cases) {
    const f = startFixture();
    t.after(f.sp.restore);
    const r = await startupApply({
      app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log,
      // Windows, or a listener whose identity could not be read: "not this bundle's".
      probeGateway: createStartupGatewayProbe({
        isCurrentBundleGateway: async () => false,
        probePortBinding: async () => binding,
        probeLockHolder: async () => ({ verdict: "released", pid: null }),
        stopCommand: "kirocrew stop --port 18800",
      }),
    });
    assert.strictEqual(r.outcome, outcome, binding);
    if (outcome === "gateway-running") {
      assert.strictEqual(r.stopCommand, "kirocrew stop --port 18800");
      assert.deepStrictEqual(f.sp.commands, [], "no update command ran");
      assert.strictEqual(f.arm.read().version, "2.0.0", "the arm stays");
    }
  }
  const throwing = createStartupGatewayProbe({
    isCurrentBundleGateway: async () => false,
    probePortBinding: async () => { throw new Error("lsof missing"); },
    stopCommand: "kirocrew stop",
  });
  assert.strictEqual((await throwing()).running, true, "a probe that fails counts as a gateway that may be running");
});

test("a free port with the home lock held or unreadable keeps the arm; a free lock applies", async (t) => {
  const cases = [
    { lock: async () => ({ verdict: "captured", pid: 4242 }), outcome: "gateway-running", what: "booting or draining gateway holds the lock" },
    { lock: async () => ({ verdict: "held", pid: null }), outcome: "gateway-running", what: "lock held, owner unnameable" },
    { lock: async () => ({ verdict: "unverified", pid: null }), outcome: "gateway-running", what: "probe timed out" },
    { lock: async () => ({ verdict: "refused", pid: null }), outcome: "gateway-running", what: "lock state indeterminate" },
    { lock: async () => { throw new Error("spawn failed"); }, outcome: "gateway-running", what: "probe threw" },
    { lock: null, outcome: "gateway-running", what: "no lock probe wired" },
    { lock: async () => ({ verdict: "released", pid: null }), outcome: "relaunching", what: "lock free and port free" },
  ];
  for (const { lock, outcome, what } of cases) {
    const f = startFixture();
    t.after(f.sp.restore);
    const r = await startupApply({
      app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log,
      probeGateway: createStartupGatewayProbe({
        isCurrentBundleGateway: async () => false,
        probePortBinding: async () => "free",
        probeLockHolder: lock,
        stopCommand: "kirocrew stop --port 18800",
      }),
    });
    assert.strictEqual(r.outcome, outcome, what);
    if (outcome === "gateway-running") {
      assert.deepStrictEqual(f.sp.commands, [], `${what}: no update command ran`);
      assert.strictEqual(f.arm.read().version, "2.0.0", `${what}: the arm stays`);
    }
  }
});

test("public lane: on Windows, Restart now says the app reopens itself and a plain quit names the Start menu", async () => {
  for (const restart of [true, false]) {
    const f = feedFixture({ osPlatform: "win32" });
    f.emit("update-downloaded", { version: "9.9.9" });
    await f.u.install();
    if (restart) f.u.relaunch();
    f.fireQuit();
    await f.settle();
    assert.strictEqual(f.notifications.length, 1);
    if (restart) {
      assert.match(f.notifications[0].body, /will reopen after installing/i);
      assert.doesNotMatch(f.notifications[0].body, /Start menu/i);
    } else {
      assert.match(f.notifications[0].body, /Start menu/i);
    }
  }
});
