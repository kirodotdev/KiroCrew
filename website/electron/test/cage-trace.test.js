"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  createCageTrace,
  traceConfig,
  DEFAULT_ARM_GROWTH_KB,
  DEFAULT_COOLDOWN_MS,
} = require("../cage-trace");

/** A contentTracing double that records calls and lets a test resolve/reject. */
function fakeTracing({ startThrows = false } = {}) {
  const calls = { start: [], stop: 0, dests: [] };
  return {
    calls,
    async startRecording(cfg) {
      calls.start.push(cfg);
      if (startThrows) throw new Error("tracing unavailable");
    },
    async stopRecording(dest) {
      calls.stop += 1;
      calls.dests.push(dest);
      return dest || `/tmp/anonymous-${calls.stop}.json`;
    },
  };
}

function make(over = {}) {
  const contentTracing = over.contentTracing || fakeTracing();
  const logs = [];
  let now = 1_000_000;
  const timers = [];
  const trace = createCageTrace({
    contentTracing,
    tracePath: (slot) => `/logs/cage-trace-${slot}.json`,
    log: (m) => logs.push(m),
    nowMs: () => now,
    setTimer: (fn) => {
      timers.push(fn);
      return { unref() {} };
    },
    clearTimer: () => {},
    ...over,
  });
  return {
    trace,
    contentTracing,
    logs,
    timers,
    advance: (ms) => {
      now += ms;
    },
  };
}

const BIG = DEFAULT_ARM_GROWTH_KB + 1;

test("traceConfig asks for detailed periodic dumps and nothing else", () => {
  const cfg = traceConfig();
  assert.deepEqual(cfg.included_categories, ["disabled-by-default-memory-infra"]);
  assert.deepEqual(cfg.excluded_categories, ["*"], "a full trace would be huge for no added answer");
  // `detailed` is required: light mode reports process totals only and omits the
  // per-allocator breakdown, which is the entire point.
  assert.equal(cfg.memory_dump_config.triggers[0].mode, "detailed");
  assert.ok(cfg.memory_dump_config.triggers[0].periodic_interval_ms > 0);
});

test("does not arm below the growth threshold", async () => {
  const { trace, contentTracing } = make();
  assert.equal(await trace.considerArming(0, DEFAULT_ARM_GROWTH_KB - 1), false);
  assert.equal(contentTracing.calls.start.length, 0);
  assert.equal(trace.isActive(), false);
});

test("does not arm on a missing reading", async () => {
  const { trace, contentTracing } = make();
  assert.equal(await trace.considerArming(null, BIG), false);
  assert.equal(await trace.considerArming(0, null), false);
  assert.equal(contentTracing.calls.start.length, 0);
});

test("arms once growth crosses the threshold", async () => {
  const { trace, contentTracing, logs } = make();
  assert.equal(await trace.considerArming(0, BIG), true);
  assert.equal(contentTracing.calls.start.length, 1);
  assert.equal(trace.isActive(), true);
  assert.match(logs.join("\n"), /armed: external grew/);
  assert.match(logs.join("\n"), /array_buffer virtual_size/);
});

test("only one capture runs at a time", async () => {
  const { trace, contentTracing } = make();
  await trace.considerArming(0, BIG);
  assert.equal(await trace.considerArming(0, BIG), false, "already active");
  assert.equal(contentTracing.calls.start.length, 1);
});

test("the window timer stops the capture and writes the file", async () => {
  const { trace, contentTracing, timers, logs } = make();
  await trace.considerArming(0, BIG);
  assert.equal(timers.length, 1);
  await timers[0]();
  assert.equal(contentTracing.calls.stop, 1);
  assert.equal(trace.isActive(), false);
  assert.match(logs.join("\n"), /capture 1 written \(window elapsed\)/);
});

test("a cooldown blocks re-arming, and it lifts once elapsed", async () => {
  const { trace, timers, advance, contentTracing } = make({ cooldownMs: 5000 });
  await trace.considerArming(0, BIG);
  await timers[0]();
  assert.equal(await trace.considerArming(0, BIG), false, "still cooling down");
  advance(5001);
  assert.equal(await trace.considerArming(0, BIG), true);
  assert.equal(contentTracing.calls.start.length, 2);
});

test("captures are capped per app run so a diagnostic cannot fill a disk", async () => {
  const { trace, timers, contentTracing } = make({ maxCaptures: 2, cooldownMs: 0 });
  await trace.considerArming(0, BIG);
  await timers[0]();
  await trace.considerArming(0, BIG);
  await timers[1]();
  assert.equal(await trace.considerArming(0, BIG), false, "cap reached");
  assert.equal(contentTracing.calls.start.length, 2);
  assert.equal(trace.captureCount(), 2);
});

test("a failed start leaves no active flag behind", async () => {
  // Otherwise the guarded stop would later try to stop a recording that never
  // began -- which on a process-wide facility means stealing someone else's.
  const { trace, logs } = make({ contentTracing: fakeTracing({ startThrows: true }) });
  assert.equal(await trace.considerArming(0, BIG), false);
  assert.equal(trace.isActive(), false);
  assert.match(logs.join("\n"), /could not start capture/);
});

test("stopForCrash writes beside the crash log, not an OS temp file", async () => {
  // `stopRecording()` with no argument writes to a temp file, which is unfindable
  // by an operator and liable to be reaped -- the trace has to land where the crash
  // log is or it may as well not exist.
  const { trace, contentTracing, logs } = make();
  await trace.considerArming(0, BIG);
  const file = await trace.stopForCrash();
  assert.equal(file, "/logs/cage-trace-1.json", "the wired tracePath must be used");
  assert.deepEqual(contentTracing.calls.dests, ["/logs/cage-trace-1.json"]);
  assert.equal(contentTracing.calls.stop, 1);
  assert.match(logs.join("\n"), /renderer died/);
});

test("names captures by ordinal, so traces cannot accumulate across runs", async () => {
  // A timestamped name is unique per launch, so repeated capture-triggering runs
  // would pile multi-MB traces into the logs directory forever. Naming by ordinal
  // means the set of files is the same on every launch and disk is bounded by
  // construction -- no cleanup pass, and nothing to delete in a user's log dir.
  const runDests = async () => {
    const h = make();
    for (let i = 0; i < 3; i += 1) {
      await h.trace.considerArming(0, BIG);
      await h.trace.stopForQuit();
      h.advance(DEFAULT_COOLDOWN_MS + 1);
    }
    return h.contentTracing.calls.dests;
  };

  const first = await runDests();
  assert.deepEqual(first, [
    "/logs/cage-trace-1.json",
    "/logs/cage-trace-2.json",
    "/logs/cage-trace-3.json",
  ]);

  // A second run is a fresh instance, and it must reuse the same three slots
  // rather than opening a fourth, fifth and sixth file.
  assert.deepEqual(await runDests(), first);
});

test("stopping when nothing is active is a no-op, never someone else's trace", async () => {
  const { trace, contentTracing } = make();
  assert.equal(await trace.stopForCrash(), null);
  assert.equal(await trace.stopForQuit(), null);
  assert.equal(contentTracing.calls.stop, 0, "contentTracing is process-wide");
});

test("a failed stop is reported and does not leave the capture active", async () => {
  const tracing = fakeTracing();
  tracing.stopRecording = async () => {
    throw new Error("disk full");
  };
  const { trace, logs } = make({ contentTracing: tracing });
  await trace.considerArming(0, BIG);
  assert.equal(await trace.stopForCrash(), null);
  assert.equal(trace.isActive(), false);
  assert.match(logs.join("\n"), /failed to write/);
});
