// The quit waits for the gateway's graceful stop: on Windows the app's exit
// otherwise kills the gateway mid-shutdown and orphans its children.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { createQuitHold } = require("../quit-hold");

/** A deferred stop, a fake app and a manual timer, so each step is driven by the test. */
function fixture({ isUpdateQuit = () => false, teardown } = {}) {
  let finishStop;
  const stop = new Promise((resolve) => { finishStop = resolve; });
  const calls = { teardown: 0, quit: 0, cleared: 0 };
  const timers = [];
  const logs = [];
  const app = { quit: () => { calls.quit += 1; } };
  const onBeforeQuit = createQuitHold({
    app,
    isUpdateQuit,
    capMs: 1000,
    setTimeoutFn: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeoutFn: () => { calls.cleared += 1; },
    log: (message) => logs.push(message),
    teardown: teardown || (() => { calls.teardown += 1; return stop; }),
  });
  const quitEvent = () => {
    const event = { prevented: false, preventDefault() { this.prevented = true; } };
    onBeforeQuit(event);
    return event.prevented;
  };
  return { quitEvent, finishStop, calls, timers, logs };
}

/** Let the hold's promise chain run. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("the first quit is held until the stop settles, then asked for again and let through", async () => {
  const f = fixture();
  assert.strictEqual(f.quitEvent(), true, "the quit that starts the stop is held");
  assert.strictEqual(f.calls.teardown, 1);
  await settle();
  assert.strictEqual(f.calls.quit, 0, "nothing quits while the stop is pending");
  f.finishStop();
  await settle();
  assert.strictEqual(f.calls.quit, 1, "the held quit is replayed once the stop settles");
  assert.strictEqual(f.calls.cleared, 1, "the cap timer is cleared");
  assert.strictEqual(f.quitEvent(), false, "the replayed quit goes through");
  assert.strictEqual(f.calls.teardown, 1, "the teardown runs once");
});

test("a second quit during the stop is held and starts no second stop", async () => {
  const f = fixture();
  f.quitEvent();
  assert.strictEqual(f.quitEvent(), true);
  assert.strictEqual(f.calls.teardown, 1);
  f.finishStop();
  await settle();
  assert.strictEqual(f.calls.quit, 1, "one replay, however many quits were held");
});

test("the installer's own quit for an update is never held", async () => {
  let updating = false;
  const f = fixture({ isUpdateQuit: () => updating });
  f.quitEvent();
  updating = true;
  assert.strictEqual(f.quitEvent(), false, "the installer's quit goes through while the stop runs");
  const g = fixture({ isUpdateQuit: () => true });
  assert.strictEqual(g.quitEvent(), false);
  assert.strictEqual(g.calls.teardown, 1, "its teardown still runs");
});

test("a stop that never settles is cut off at the cap", async () => {
  const f = fixture();
  f.quitEvent();
  assert.strictEqual(f.timers.length, 1);
  assert.strictEqual(f.timers[0].ms, 1000);
  f.timers[0].fn();
  await settle();
  assert.strictEqual(f.calls.quit, 1);
  assert.match(f.logs.join("\n"), /still pending after 1000ms/);
});

test("a teardown that throws or a stop that rejects still ends the quit", async () => {
  const thrown = fixture({ teardown: () => { throw new Error("boom"); } });
  assert.strictEqual(thrown.quitEvent(), true);
  await settle();
  assert.strictEqual(thrown.calls.quit, 1);
  assert.match(thrown.logs.join("\n"), /teardown failed — boom/);
  const rejected = fixture({ teardown: () => Promise.reject(new Error("stop failed")) });
  rejected.quitEvent();
  await settle();
  assert.strictEqual(rejected.calls.quit, 1);
});

test("main.js holds before-quit for the stop that stopOnQuit returns", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, /app\.on\("before-quit", createQuitHold\(\{ app, log: glog, isUpdateQuit: \(\) => quittingForUpdate, teardown:/);
  assert.match(main, /return gateway\.stopOnQuit\(/);
  const supervisor = fs.readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8");
  assert.match(supervisor, /return stopGatewayGracefully\(\{ userStop \}\)/);
});
