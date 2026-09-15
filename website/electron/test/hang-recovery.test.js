const { test } = require("node:test");
const assert = require("node:assert");
const { createHangRecovery, DEFAULT_GRACE_MS } = require("../hang-recovery");

// A coordinator wired entirely to fakes: Electron main is not available here,
// and a real hung WebContents cannot be simulated in a unit test at all. Timers
// are captured, not scheduled, so a test fires expiry deterministically.
function harness(overrides = {}) {
  const crashes = [];
  const logs = [];
  const timers = new Map();
  let nextHandle = 1;
  let quitting = false;
  const rec = createHangRecovery({
    forceCrash: () => crashes.push(timers.size),
    isQuitting: () => quitting,
    log: (m) => logs.push(m),
    setTimer: (fn, ms) => {
      const handle = nextHandle++;
      timers.set(handle, { fn, ms });
      return handle;
    },
    clearTimer: (handle) => {
      timers.delete(handle);
    },
    ...overrides,
  });
  return {
    rec,
    crashes,
    logs,
    timers,
    fire: () => {
      // Run every pending timer, the way expiry would.
      for (const [handle, { fn }] of [...timers]) {
        timers.delete(handle);
        fn();
      }
    },
    setQuitting: (v) => {
      quitting = v;
    },
  };
}

test("a hang arms the grace timer and expiry force-crashes the renderer", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleUnresponsive(), "armed");
  assert.strictEqual(h.rec.armed, true);
  assert.strictEqual(h.crashes.length, 0, "no crash before the grace expires");
  h.fire();
  assert.strictEqual(h.crashes.length, 1);
  assert.match(h.logs.join("\n"), /force-crashing/);
});

test("responsive within the grace window disarms without crashing", () => {
  const h = harness();
  h.rec.handleUnresponsive();
  assert.strictEqual(h.rec.handleResponsive(), "recovered");
  assert.strictEqual(h.rec.armed, false);
  assert.strictEqual(h.timers.size, 0, "the pending timer is cleared");
  h.fire();
  assert.strictEqual(h.crashes.length, 0);
});

test("responsive with nothing armed is an idle no-op", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleResponsive(), "idle");
  assert.strictEqual(h.crashes.length, 0);
});

test("repeated unresponsive while armed does not stack timers", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleUnresponsive(), "armed");
  assert.strictEqual(h.rec.handleUnresponsive(), "already-armed");
  assert.strictEqual(h.rec.handleUnresponsive(), "already-armed");
  assert.strictEqual(h.timers.size, 1);
  h.fire();
  assert.strictEqual(h.crashes.length, 1, "one hang episode, one crash");
});

test("a hang during quit is ignored", () => {
  const h = harness();
  h.setQuitting(true);
  assert.strictEqual(h.rec.handleUnresponsive(), "ignored-quitting");
  assert.strictEqual(h.rec.armed, false);
  h.fire();
  assert.strictEqual(h.crashes.length, 0);
});

test("a quit that begins during the grace window suppresses the crash at expiry", () => {
  const h = harness();
  h.rec.handleUnresponsive();
  h.setQuitting(true);
  h.fire();
  assert.strictEqual(h.crashes.length, 0);
  assert.match(h.logs.join("\n"), /quitting — not crashing/);
});

test("a throwing forceCrash is contained and logged", () => {
  const h = harness({
    forceCrash: () => {
      throw new Error("boom");
    },
  });
  h.rec.handleUnresponsive();
  assert.doesNotThrow(() => h.fire());
  assert.match(h.logs.join("\n"), /force-crash of hung renderer failed/);
  assert.match(h.logs.join("\n"), /boom/);
});

test("a new hang after an expiry can arm again", () => {
  const h = harness();
  h.rec.handleUnresponsive();
  h.fire();
  assert.strictEqual(h.crashes.length, 1);
  assert.strictEqual(h.rec.handleUnresponsive(), "armed");
  h.fire();
  assert.strictEqual(h.crashes.length, 2);
});

test("a renderer death inside the grace window disarms the pending crash", () => {
  // The hung renderer can crash on its own before the grace expires;
  // render-process-gone then reloads the dashboard into the SAME WebContents,
  // and the fresh renderer never emits `responsive` — so without the disarm
  // the stale expiry would force-crash the healthy replacement.
  const h = harness();
  h.rec.handleUnresponsive();
  assert.strictEqual(h.rec.handleGone(), "disarmed");
  assert.strictEqual(h.rec.armed, false);
  assert.strictEqual(h.timers.size, 0, "the pending timer is cleared");
  h.fire();
  assert.strictEqual(h.crashes.length, 0, "the replacement renderer is not killed");
  assert.match(h.logs.join("\n"), /renderer gone while hang recovery was armed/);
});

test("renderer death with nothing armed is an idle no-op", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleGone(), "idle");
  assert.strictEqual(h.crashes.length, 0);
});

test("a hang in the reloaded renderer can re-arm after a mid-grace death", () => {
  const h = harness();
  h.rec.handleUnresponsive();
  h.rec.handleGone();
  assert.strictEqual(h.rec.handleUnresponsive(), "armed");
  h.fire();
  assert.strictEqual(h.crashes.length, 1, "the new hang episode still recovers");
});

test("forceCrash is required", () => {
  assert.throws(() => createHangRecovery({}), /forceCrash is required/);
});

test("the default grace is sane and overridable", () => {
  assert.ok(DEFAULT_GRACE_MS >= 1000, "default grace is at least a second");
  const captured = [];
  const rec = createHangRecovery({
    forceCrash: () => {},
    setTimer: (fn, ms) => {
      captured.push(ms);
      return 1;
    },
    clearTimer: () => {},
    graceMs: 250,
  });
  rec.handleUnresponsive();
  assert.deepStrictEqual(captured, [250]);
});
