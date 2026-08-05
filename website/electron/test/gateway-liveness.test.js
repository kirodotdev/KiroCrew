const { test } = require("node:test");
const assert = require("node:assert");
const { createLivenessMonitor } = require("../gateway-liveness");

// Drive tick() directly with a stubbed probe — no timers, fully deterministic.
function harness({ results, failureThreshold = 3, isWindowAlive = () => true } = {}) {
  let i = 0;
  const events = [];
  const m = createLivenessMonitor({
    // results[i] === true => probe resolves (alive); false => rejects (unresponsive)
    probe: () => (results[Math.min(i++, results.length - 1)]
      ? Promise.resolve()
      : Promise.reject(new Error("unresponsive"))),
    onUnresponsive: () => events.push("unresponsive"),
    onRecovered: () => events.push("recovered"),
    isWindowAlive,
    failureThreshold,
    log: () => {},
  });
  return { m, events };
}

test("healthy backend never fires recovery", async () => {
  const { m, events } = harness({ results: [true, true, true, true, true] });
  for (let k = 0; k < 5; k++) await m.tick();
  assert.deepStrictEqual(events, []);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
});

test("fires onUnresponsive after threshold consecutive failures", async () => {
  const { m, events } = harness({ results: [false, false, false, false], failureThreshold: 3 });
  await m.tick(); // 1
  await m.tick(); // 2
  assert.deepStrictEqual(events, []);
  await m.tick(); // 3 -> trips
  assert.deepStrictEqual(events, ["unresponsive"]);
});

test("onUnresponsive fires exactly once per episode (debounced)", async () => {
  const { m, events } = harness({ results: [false, false, false, false, false, false], failureThreshold: 3 });
  for (let k = 0; k < 6; k++) await m.tick();
  assert.deepStrictEqual(events, ["unresponsive"]);
  // Once fired, further ticks are no-ops until stop()/start().
  assert.strictEqual(m.getState().fired, true);
});

test("a single failure then recovery does not fire, and emits recovered", async () => {
  const { m, events } = harness({ results: [false, false, true], failureThreshold: 3 });
  await m.tick(); // fail 1
  await m.tick(); // fail 2
  await m.tick(); // recover before threshold
  assert.deepStrictEqual(events, ["recovered"]);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
});

test("failure counter resets after an intermittent success", async () => {
  // fail, fail, ok (reset), fail, fail -> still only 2 in a row, no fire
  const { m, events } = harness({ results: [false, false, true, false, false], failureThreshold: 3 });
  for (let k = 0; k < 5; k++) await m.tick();
  assert.deepStrictEqual(events, ["recovered"]);
});

test("stops itself when the window is gone", async () => {
  let alive = true;
  const m = createLivenessMonitor({
    probe: () => Promise.reject(new Error("x")),
    onUnresponsive: () => { throw new Error("should not fire after window close"); },
    isWindowAlive: () => alive,
    failureThreshold: 1,
    log: () => {},
  });
  m.start();
  alive = false;
  await m.tick(); // observes dead window -> stop(), no fire
  assert.strictEqual(m.getState().running, false);
});

test("start/stop lifecycle is idempotent and uses injected timers", () => {
  let id = 0;
  const cleared = [];
  const m = createLivenessMonitor({
    probe: () => Promise.resolve(),
    onUnresponsive: () => {},
    setIntervalFn: () => ++id,
    clearIntervalFn: (h) => cleared.push(h),
    intervalMs: 5,
  });
  m.start();
  assert.strictEqual(m.getState().running, true);
  const firstId = id;
  m.start(); // idempotent — no second timer
  assert.strictEqual(id, firstId);
  m.stop();
  assert.deepStrictEqual(cleared, [firstId]);
  assert.strictEqual(m.getState().running, false);
});

test("restart after firing re-arms for a new episode", async () => {
  const { m, events } = harness({ results: [false, false, false], failureThreshold: 3 });
  await m.tick();
  await m.tick();
  await m.tick();
  assert.deepStrictEqual(events, ["unresponsive"]);
  m.start(); // fresh episode
  assert.strictEqual(m.getState().fired, false);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
  m.stop();
});
