// A further quit request while the gateway is still stopping must not cost the
// armed quit-time apply: the first quit is held for the stop, so are the later
// ones, and the app exits once, after the apply, without relaunching.
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const cp = require("node:child_process");

const { initAutoUpdate } = require("../auto-update");
const { makeDeps } = require("./auto-update-test-helpers");

async function drainTurns() {
  for (let i = 0; i < 100; i += 1) await new Promise((r) => setImmediate(r));
}

// Electron's once semantics for before-quit: a listener is spent when it fires,
// and a quit no listener prevented goes through.
function quitBus(deps) {
  let listeners = [];
  const log = { exits: [], relaunches: 0, unpreventedQuits: 0 };
  deps.app.once = (ev, fn) => { if (ev === "before-quit") listeners.push(fn); };
  deps.app.removeListener = (ev, fn) => { listeners = listeners.filter((l) => l !== fn); };
  deps.app.exit = (code) => log.exits.push(code);
  deps.app.relaunch = () => { log.relaunches += 1; };
  const requestQuit = () => {
    const firing = listeners;
    listeners = [];
    let prevented = false;
    for (const fn of firing) fn({ preventDefault: () => { prevented = true; } });
    if (!prevented) log.unpreventedQuits += 1;
    return prevented;
  };
  return { log, requestQuit };
}

function spawnSpy() {
  const orig = cp.spawn;
  const commands = [];
  cp.spawn = (command) => {
    commands.push(command);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    setImmediate(() => {
      if (command === "check") child.stdout.emit("data", Buffer.from("0.5.0.5"));
      child.emit("close", 0, null);
    });
    return child;
  };
  return { commands, restore: () => { cp.spawn = orig; } };
}

test("managed: two more quits during the gateway stop leave the apply to run once, then exit without relaunch", async (t) => {
  const { deps } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const bus = quitBus(deps);
  let finishStop;
  deps.stopGateway = () => new Promise((resolve) => { finishStop = resolve; });
  deps.getAutoDownloadPreference = () => true;
  const spy = spawnSpy();
  t.after(spy.restore);
  await initAutoUpdate(deps).check();
  spy.commands.length = 0;

  assert.strictEqual(bus.requestQuit(), true, "the first quit is held for the stop");
  await drainTurns();
  assert.strictEqual(bus.requestQuit(), true, "a second quit during the stop is held");
  assert.strictEqual(bus.requestQuit(), true, "so is a third");
  assert.deepStrictEqual(spy.commands, [], "the apply never runs while the gateway is still stopping");
  assert.strictEqual(bus.log.unpreventedQuits, 0, "no quit got through to exit the app early");

  finishStop();
  await drainTurns();
  assert.strictEqual(spy.commands.filter((c) => c === "apply").length, 1, "the apply ran exactly once, after the stop");
  assert.deepStrictEqual(bus.log.exits, [0], "the app exits once");
  assert.strictEqual(bus.log.relaunches, 0, "and does not relaunch itself");
});

test("feed: a second quit during the gateway stop still reaches the install", async () => {
  const { deps, calls, emit } = makeDeps();
  const bus = quitBus(deps);
  let finishStop;
  deps.stopGateway = () => new Promise((resolve) => { finishStop = resolve; });
  deps.getAutoDownloadPreference = () => false;
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "9.9.9" });
  assert.strictEqual(bus.requestQuit(), true, "the first quit is held");
  await drainTurns();
  assert.strictEqual(bus.requestQuit(), true, "a second quit during the stop is held");
  assert.strictEqual(bus.log.unpreventedQuits, 0);
  finishStop();
  await drainTurns();
  assert.strictEqual(calls.quitAndInstall.length, 1, "the install was dispatched once");
});
