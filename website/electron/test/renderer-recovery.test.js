const { test } = require("node:test");
const assert = require("node:assert");
const {
  createRendererRecovery,
  isRecoverableReason,
  DEFAULT_MAX_ATTEMPTS,
} = require("../renderer-recovery");

// A coordinator wired entirely to fakes: Electron main is not available here,
// and a real BrowserWindow would make these tests slow and order-dependent.
function harness(overrides = {}) {
  const reloads = [];
  const logs = [];
  const gaveUp = [];
  let t = 0;
  let quitting = false;
  const rec = createRendererRecovery({
    reload: () => reloads.push(t),
    isQuitting: () => quitting,
    log: (m) => logs.push(m),
    onGiveUp: (info) => gaveUp.push(info),
    now: () => t,
    ...overrides,
  });
  return {
    rec,
    reloads,
    logs,
    gaveUp,
    advance: (ms) => {
      t += ms;
    },
    setQuitting: (v) => {
      quitting = v;
    },
  };
}

test("a crashed renderer triggers a reload", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleGone({ reason: "crashed", exitCode: 5 }), "reloaded");
  assert.strictEqual(h.reloads.length, 1);
});

test("an OOM renderer triggers a reload (the observed V8 abort class)", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleGone({ reason: "oom" }), "reloaded");
  assert.strictEqual(h.reloads.length, 1);
});

test("a clean exit is NOT treated as a crash", () => {
  const h = harness();
  assert.strictEqual(h.rec.handleGone({ reason: "clean-exit" }), "ignored-clean-exit");
  assert.strictEqual(h.reloads.length, 0, "a normal teardown must never reload");
});

test("a death during quit is ignored so recovery cannot fight the shutdown", () => {
  const h = harness();
  h.setQuitting(true);
  assert.strictEqual(h.rec.handleGone({ reason: "killed" }), "ignored-quitting");
  assert.strictEqual(h.reloads.length, 0);
});

test("repeated crashes inside the window stop instead of reload-looping", () => {
  const h = harness({ maxAttempts: 3, windowMs: 60_000 });
  for (let i = 0; i < 3; i++) {
    assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "reloaded");
    h.advance(1000);
  }
  // Budget spent — the 4th death must not reload.
  assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "gave-up");
  assert.strictEqual(h.reloads.length, 3);
  assert.strictEqual(h.gaveUp.length, 1);
  assert.match(h.logs.join("\n"), /giving up/);
});

test("the budget is a SLIDING window, so a once-a-day crash always recovers", () => {
  const h = harness({ maxAttempts: 3, windowMs: 60_000 });
  for (let i = 0; i < 6; i++) {
    assert.strictEqual(
      h.rec.handleGone({ reason: "crashed" }),
      "reloaded",
      `crash ${i} should still recover`
    );
    h.advance(24 * 60 * 60 * 1000); // a day apart, like the observed crashes
  }
  assert.strictEqual(h.reloads.length, 6);
  assert.strictEqual(h.gaveUp.length, 0);
});

test("a throwing reload is contained and reported, never rethrown", () => {
  const h = harness({
    reload: () => {
      throw new Error("window destroyed");
    },
  });
  assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "reloaded");
  assert.match(h.logs.join("\n"), /reload failed/);
});

test("reset clears the attempt budget", () => {
  const h = harness({ maxAttempts: 1 });
  h.rec.handleGone({ reason: "crashed" });
  assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "gave-up");
  h.rec.reset();
  assert.strictEqual(h.rec.attempts, 0);
  assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "reloaded");
});

test("reason classification covers the unexpected-death set only", () => {
  for (const r of ["crashed", "oom", "abnormal-exit", "launch-failed", "integrity-failure", "killed"]) {
    assert.strictEqual(isRecoverableReason(r), true, `${r} should be recoverable`);
  }
  for (const r of ["clean-exit", "", null, undefined, "nonsense"]) {
    assert.strictEqual(isRecoverableReason(r), false, `${r} should not be recoverable`);
  }
});

test("the default attempt budget is small enough to surface a broken build", () => {
  assert.ok(DEFAULT_MAX_ATTEMPTS >= 1 && DEFAULT_MAX_ATTEMPTS <= 5);
});

test("the crash log carries a process snapshot so memory vs CPU is decidable later", () => {
  const h = harness({
    describeProcesses: () => "procs=7 totalCpu=133.7% totalWorkingSet=4211MB",
  });
  h.rec.handleGone({ reason: "crashed", exitCode: 5 });
  const line = h.logs.join("\n");
  assert.match(line, /totalWorkingSet=4211MB/);
  assert.match(line, /totalCpu=133\.7%/);
  assert.match(line, /reason=crashed/);
});

test("the snapshot is captured for the give-up line too, and handed to onGiveUp", () => {
  const h = harness({ maxAttempts: 1, describeProcesses: () => "procs=3 totalWorkingSet=99MB" });
  h.rec.handleGone({ reason: "crashed" });
  h.rec.handleGone({ reason: "crashed" });
  assert.match(h.logs.join("\n"), /giving up[\s\S]*|totalWorkingSet=99MB/);
  assert.strictEqual(h.gaveUp[0].snapshot, "procs=3 totalWorkingSet=99MB");
});

test("a throwing snapshot probe never blocks recovery", () => {
  const h = harness({
    describeProcesses: () => {
      throw new Error("metrics unavailable");
    },
  });
  assert.strictEqual(h.rec.handleGone({ reason: "crashed" }), "reloaded");
  assert.strictEqual(h.reloads.length, 1, "recovery must still happen");
  assert.match(h.logs.join("\n"), /snapshot failed/);
});
