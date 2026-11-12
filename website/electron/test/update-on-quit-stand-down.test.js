// Reproductions for the round-16 review findings on the quit-time install.
// Each test states the EXPECTED behavior and fails at 2277a98a7a (rebased).
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const cp = require("node:child_process");

const { initAutoUpdate } = require("../auto-update");
const { createUpdateArm } = require("../runtime/update/update-arm");
const { makeDeps } = require("./auto-update-test-helpers");

const MANAGED = {
  managedBy: "internal-registry",
  updateCommand: "pkgtool update kirocrew",
  checkCommand: "pkgtool check kirocrew",
};

async function drainTurns() {
  for (let i = 0; i < 100; i += 1) await new Promise((r) => setImmediate(r));
}

function memoryStore(seed = {}) {
  const m = new Map(Object.entries(seed));
  return { get: (k, d) => (m.has(k) ? m.get(k) : d), set: (k, v) => m.set(k, v), delete: (k) => m.delete(k) };
}

function feedFixture({ governed = () => false, stopGateway } = {}) {
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
  deps.stopGateway = stopGateway || (async () => { events.push("gateway-stopped"); });
  deps.getAutoDownloadPreference = () => false;
  deps.isPolicyGoverned = () => governed();
  const u = initAutoUpdate(deps);
  const fireQuit = () => {
    const fn = quitHandlers[quitHandlers.length - 1];
    if (!fn) return { prevented: false };
    quitHandlers.pop();
    let prevented = false;
    fn({ preventDefault: () => { prevented = true; } });
    return { prevented };
  };
  return { u, deps, calls, emit, states, events, fireQuit };
}

// Opus feed-lane.js:704 / GPT F2: Restart now on an armed stage, then the
// quit-time install refuses. The app must still come back.
test("R-a: Restart now whose quit-time install is refused post-gateway-stop still relaunches", async () => {
  let governed = false;
  const f = feedFixture({
    governed: () => governed,
    stopGateway: async () => { governed = true; },
  });
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  f.u.relaunch();
  f.events.length = 0;
  f.fireQuit();
  await drainTurns();
  assert.deepStrictEqual(f.calls.quitAndInstall, [], "the install was refused");
  assert.ok(f.events.includes("relaunch"), `the refused Restart now must relaunch; saw ${JSON.stringify(f.events)}`);
});

test("R-a: Restart now whose quit-time install is refused for governance before the quit still relaunches", async () => {
  let governed = false;
  const f = feedFixture({ governed: () => governed });
  f.emit("update-downloaded", { version: "9.9.9" });
  await f.u.install();
  f.u.relaunch();
  governed = true;
  f.events.length = 0;
  const { prevented } = f.fireQuit();
  await drainTurns();
  assert.strictEqual(prevented, false);
  assert.ok(f.events.includes("relaunch"), `a governance stand-down must not strand a Restart now; saw ${JSON.stringify(f.events)}`);
});

// GPT managed-lane.js:58: the persisted verdict is overwritten by the
// registrar's initial false before live status arrives.
test("R-c: a persisted governed verdict survives the registrar's initial false; the quit does not run the marker command", async () => {
  const store = memoryStore({ updatePolicyGoverned: true, updateArm: { version: "2.0.0", armedAt: "2026-11-11T00:00:00.000Z", attempts: 0 } });
  const { deps } = makeDeps({ externallyManaged: MANAGED });
  const quitHandlers = [];
  const events = [];
  deps.app.once = (ev, fn) => { if (ev === "before-quit") quitHandlers.push(fn); };
  deps.app.removeListener = () => {};
  deps.app.exit = (c) => events.push(`exit:${c}`);
  deps.app.relaunch = () => events.push("relaunch");
  deps.app.quit = () => events.push("quit");
  deps.stopGateway = async () => {};
  deps.getAutoDownloadPreference = () => false;
  deps.isPolicyGoverned = () => null; // no live verdict yet: the dashboard has not reported
  deps.updateArm = createUpdateArm({ store });
  const ran = [];
  const orig = cp.spawn;
  cp.spawn = (command) => {
    ran.push(command);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    setImmediate(() => child.emit("close", 0, null));
    return child;
  };
  try {
    const u = initAutoUpdate(deps);
    await u.check();
    ran.length = 0;
    const fn = quitHandlers.shift();
    if (fn) fn({ preventDefault: () => {} });
    await drainTurns();
    assert.ok(!ran.some((c) => c.includes("update kirocrew")), `marker update ran under a persisted governed verdict: ${JSON.stringify(ran)}`);
    assert.strictEqual(deps.updateArm.wasGoverned(), true, "the persisted verdict was overwritten by the initial false");
  } finally {
    cp.spawn = orig;
  }
});

// GPT update-arm.js:32: a failed arm write is swallowed.
test("R-d: an arm write that fails rejects install() instead of emitting an un-armed downloaded", async () => {
  const store = memoryStore();
  store.set = () => { throw new Error("ENOSPC: no space left on device"); };
  const { deps, states } = makeDeps({ externallyManaged: MANAGED });
  deps.app.once = () => {};
  deps.app.removeListener = () => {};
  deps.app.quit = () => {};
  deps.getAutoDownloadPreference = () => false;
  deps.updateArm = createUpdateArm({ store });
  const orig = cp.spawn;
  cp.spawn = () => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    setImmediate(() => { child.stdout.emit("data", Buffer.from("2.0.0\n")); child.emit("close", 0, null); });
    return child;
  };
  try {
    const u = initAutoUpdate(deps);
    await u.check();
    await assert.rejects(() => u.install(), /ENOSPC/);
    const last = states[states.length - 1];
    assert.ok(!(last.state === "downloaded"), `emitted ${last.state} although nothing was armed`);
  } finally {
    cp.spawn = orig;
  }
});

// Opus UpdateModal.tsx:205: the installing overlay promises a relaunch.
test("R-b: installing_update_body does not promise an automatic relaunch", () => {
  const en = require("../../src/i18n/locales/en.manual.json");
  const body = en.components.updateModal.installing_update_body;
  assert.ok(!/relaunch/i.test(body), `still says: ${body}`);
});
