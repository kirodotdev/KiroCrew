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
  const settle = () => new Promise((r) => setTimeout(r, 30));
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
  assert.strictEqual(calls[0].method, "POST");
  assert.strictEqual(calls[0].body, JSON.stringify({ when: "next-start" }));
  assert.ok(calls[0].url.startsWith("http://127.0.0.1:1/api/update?token=tok"));
  assert.strictEqual(await c.armedVersion(), "2.0.0");
  const none = createGatewayArm({ backendUrl: "http://127.0.0.1:1", fetchGatewayAuth: async () => ({ value: "" }), fetchImpl });
  assert.strictEqual(await none.arm(), false, "no credential means no gateway to tell");
  assert.strictEqual(await none.armedVersion(), null);
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

test("D-b: the governed lane no longer skips the quit-time apply", async (t) => {
  const policy = { governed: false };
  const f = await foundFixture({ isPolicyGoverned: () => policy.governed });
  t.after(f.sp.restore);
  await f.u.install();
  // Policy governance arrives after the arm: quit is still the sanctioned apply point.
  policy.governed = true;
  const { prevented } = f.fireQuit();
  assert.strictEqual(prevented, true);
  await f.settle();
  assert.ok(f.sp.commands.includes("pkgtool update kirocrew"), "a governed install applies on the user's quit");
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
  const r = await runStartupApply({ app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(r.outcome, "relaunching");
  assert.deepStrictEqual(f.events, ["relaunch", "exit:0"]);
  assert.deepStrictEqual(f.sp.commands, ["pkgtool update kirocrew", "pkgtool check kirocrew"]);
  assert.strictEqual(f.arm.read(), null, "cleared before the relaunch, so the new bundle cannot loop");
});

test("D-c: a failed start-time apply starts the old version, clears the arm and records the error", async (t) => {
  const f = startFixture({ code: 1 });
  t.after(f.sp.restore);
  const r = await runStartupApply({ app: f.app, store: f.store, managed: MANAGED, managedPath: () => "/usr/bin", log: f.log });
  assert.strictEqual(r.outcome, "failed");
  assert.deepStrictEqual(f.events, [], "no relaunch, no exit: the installed version starts");
  assert.strictEqual(f.arm.read(), null);
  assert.match(f.arm.takeApplyError().message, /exited 1/);
});

test("D-c: with no arm, or an arm for the version already running, nothing runs", async (t) => {
  const none = startFixture();
  t.after(none.sp.restore);
  none.arm.clear();
  assert.strictEqual((await runStartupApply({ app: none.app, store: none.store, managed: MANAGED, managedPath: () => "", log: none.log })).outcome, "none");
  const same = startFixture();
  t.after(same.sp.restore);
  same.app.getVersion = () => "2.0.0";
  assert.strictEqual((await runStartupApply({ app: same.app, store: same.store, managed: MANAGED, managedPath: () => "", log: same.log })).outcome, "already-installed");
  assert.deepStrictEqual(same.sp.commands, []);
  const notManaged = startFixture();
  t.after(notManaged.sp.restore);
  assert.strictEqual((await runStartupApply({ app: notManaged.app, store: notManaged.store, managed: null, managedPath: () => "", log: notManaged.log })).outcome, "none");
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

function feedFixture({ autoDownload = false } = {}) {
  const { deps, calls, emit, states } = makeDeps();
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
  return { u, deps, calls, emit, states, events, fireQuit, settle: () => new Promise((r) => setTimeout(r, 30)) };
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
  const r = await runStartupApply({ app: f.app, store: f.store, managed: { ...MANAGED, relaunchPath: exe }, managedPath: () => "/usr/bin", log: f.log });
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
