// Pins the pet probe's disabled-state cadence: once the probe has established
// that Mochi is disabled, the reconcile loop must BACK OFF rather than poll
// /api/apps at a flat 5s. These tests exercise the pure cadence policy
// (nextReconcileDelay) the same way probeLog.test.js exercises probeLog —
// sliced out of index.js and run in a VM, so no Electron main-process require
// is pulled in.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

function cadenceHarness() {
  const start = source.indexOf("const MOCHI_PET_RECONCILE_MS =");
  const marker = "function nextReconcileDelay(";
  const fnStart = source.indexOf(marker, start);
  // End of the function body: the first line that is exactly "}" after it.
  const end = source.indexOf("\n}", fnStart) + 2;
  assert.ok(start !== -1 && fnStart > start && end > fnStart,
    "cadence policy must remain sliceable from index.js");
  const slice = source.slice(start, end);
  return vm.runInNewContext(
    `${slice}\n({ nextReconcileDelay, base: MOCHI_PET_RECONCILE_MS, cap: MOCHI_PET_RECONCILE_MAX_MS, IDLE: RECONCILE_IDLE, ACTIVE: RECONCILE_ACTIVE })`,
    {},
  );
}

test("a settled disabled state grows the delay instead of staying flat", () => {
  const { nextReconcileDelay, base, IDLE } = cadenceHarness();
  // The defect: a flat interval would keep returning the base forever. A single
  // idle outcome must already move OFF the base.
  const next = nextReconcileDelay(base, IDLE);
  assert.ok(next > base, `expected the delay to grow past ${base}, got ${next}`);
});

test("repeated disabled ticks bound the request count far below a flat 5s loop", () => {
  const { nextReconcileDelay, base, cap, IDLE } = cadenceHarness();
  // Simulate a fixed wall-clock window of a disabled app and count how many
  // ticks (= /api/apps requests, = log lines) actually fire.
  const windowMs = 60 * 60 * 1000; // one hour
  let delay = base;
  let elapsed = 0;
  let requests = 0;
  while (elapsed + delay <= windowMs) {
    elapsed += delay;
    requests += 1;
    delay = nextReconcileDelay(delay, IDLE);
  }
  const flatCount = Math.floor(windowMs / base); // a flat base-rate loop's count
  // Backoff must cut the rate by more than 10x over the window.
  assert.ok(requests * 10 < flatCount,
    `backed-off requests (${requests}) should be << flat-5s requests (${flatCount})`);
  // And the delay must be clamped at the ceiling, never unbounded.
  assert.ok(delay <= cap, `delay ${delay} must not exceed the ceiling ${cap}`);
});

test("the ceiling clamps the delay and the loop keeps ticking", () => {
  const { nextReconcileDelay, base, cap, IDLE } = cadenceHarness();
  let delay = base;
  for (let i = 0; i < 100; i += 1) delay = nextReconcileDelay(delay, IDLE);
  assert.equal(delay, cap, "delay must saturate at the ceiling, not grow forever");
  // At the ceiling a further idle tick stays at the ceiling — bounded, not zero:
  // the loop still runs, so a re-enable is noticed within one ceiling interval.
  assert.equal(nextReconcileDelay(cap, IDLE), cap);
});

test("any non-idle outcome snaps the cadence back to the base", () => {
  const { nextReconcileDelay, base, cap, ACTIVE } = cadenceHarness();
  assert.equal(nextReconcileDelay(cap, ACTIVE), base,
    "an enabled / remote-pet / unknown tick must return to the base cadence");
});
