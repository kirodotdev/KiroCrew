"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  createMemoryWatchLog,
  normalizeSample,
  DEFAULT_CAPACITY,
} = require("../memory-watch-log");

const seq = () => {
  let n = 0;
  return () => `t${n++}`;
};

const sample = (over = {}) => ({
  realm: "main",
  usedHeapKB: 600 * 1024,
  limitHeapKB: 4192 * 1024,
  externalKB: 10 * 1024,
  ...over,
});

test("normalizeSample keeps finite numbers and the realm", () => {
  const s = normalizeSample(sample());
  assert.equal(s.realm, "main");
  assert.equal(s.usedHeapKB, 600 * 1024);
  assert.equal(s.limitHeapKB, 4192 * 1024);
  assert.equal(s.externalKB, 10 * 1024);
});

test("normalizeSample carries a missing metric as null, never 0 or -1", () => {
  // The distinction is load-bearing: "unavailable in this realm" and "read zero"
  // lead to opposite conclusions. The instrument this replaces collapsed a
  // genuine 0 into -1.
  const s = normalizeSample(sample({ limitHeapKB: undefined, externalKB: null }));
  assert.equal(s.limitHeapKB, null);
  assert.equal(s.externalKB, null);
  const zero = normalizeSample(sample({ externalKB: 0 }));
  assert.equal(zero.externalKB, 0, "a real zero must survive as zero");
});

test("normalizeSample rejects a sample carrying neither verdict figure", () => {
  assert.equal(normalizeSample({ realm: "main" }), null);
  assert.equal(normalizeSample({ realm: "main", usedHeapKB: NaN, externalKB: "big" }), null);
  assert.equal(normalizeSample(null), null);
  assert.equal(normalizeSample("nope"), null);
});

test("normalizeSample flattens control characters and caps the realm label", () => {
  const s = normalizeSample(sample({ realm: "worker:a\n[mem] forged summary line" }));
  assert.doesNotMatch(s.realm, /[\r\n\t]/);
  const long = normalizeSample(sample({ realm: "w".repeat(200) }));
  assert.equal(long.realm.length, 60);
  assert.equal(normalizeSample(sample({ realm: "" })).realm, "?");
});

test("flush emits summary first, then samples oldest to newest", () => {
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ externalKB: 10 * 1024 }));
  log.record(sample({ externalKB: 900 * 1024, realm: "worker:pierre" }));

  const lines = log.flush();
  assert.equal(lines.length, 3);
  assert.match(lines[0], /pre-crash summary over 2 sample\(s\) across 2 realm\(s\)/);
  assert.match(lines[1], /realm=main/);
  assert.match(lines[2], /realm=worker:pierre/);

  assert.deepEqual(log.flush(), [], "flush drains");
  assert.equal(log.size(), 0);
});

test("summary reports external growth as the verdict", () => {
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ externalKB: 50 * 1024 }));
  log.record(sample({ externalKB: 1200 * 1024 }));
  const [summary] = log.flush();
  assert.match(summary, /peakExternal=1200\.0MB/);
  assert.match(summary, /externalDelta=1150\.0MB/);
});

test("externalMoved=NO-FROZEN-VALUE when every reading is identical", () => {
  // This is the self-check. performance.memory is bucketized and cached for 20
  // minutes unless precise memory info is on, so a probe reading it can return a
  // plausible constant forever. A frozen series must be named as a broken
  // instrument, not read as a flat trend.
  const log = createMemoryWatchLog({ now: seq() });
  for (let i = 0; i < 5; i++) log.record(sample({ externalKB: 12 * 1024 }));
  const [summary] = log.flush();
  assert.match(summary, /externalMoved=NO-FROZEN-VALUE/);
});

test("externalMoved=yes once two different readings appear", () => {
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ externalKB: 12 * 1024 }));
  log.record(sample({ externalKB: 13 * 1024 }));
  const [summary] = log.flush();
  assert.match(summary, /externalMoved=yes/);
});

test("externalMoved=unknown when no realm ever reported the channel", () => {
  // Distinct from frozen: the channel was never available, so there is nothing
  // to conclude about growth either way.
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ externalKB: null }));
  log.record(sample({ externalKB: null }));
  const [summary] = log.flush();
  assert.match(summary, /externalMoved=unknown/);
  assert.match(summary, /peakExternal=n\/aMB/);
});

test("externalSuspect names a cancelled subtraction that externalMoved cannot see", () => {
  // The failure mode a review raised: if the two readings the subtraction uses
  // ever describe the SAME quantity, the difference is near zero forever -- yet it
  // still jitters, so `externalMoved` reads "yes" and a dead metric passes as
  // healthy. External pinned under 1MB while the JS heap reports hundreds is that
  // signature.
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ usedHeapKB: 600 * 1024, externalKB: 4 }));
  log.record(sample({ usedHeapKB: 900 * 1024, externalKB: 11 }));
  const [summary] = log.flush();
  assert.match(summary, /externalMoved=yes/, "it does vary, which is why moved alone is insufficient");
  assert.match(summary, /externalSuspect=CANCELLED-SUBTRACTION/);
});

test("externalSuspect stays silent when external tracks a real figure", () => {
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ usedHeapKB: 700 * 1024, externalKB: 100 * 1024 }));
  log.record(sample({ usedHeapKB: 720 * 1024, externalKB: 900 * 1024 }));
  const [summary] = log.flush();
  assert.doesNotMatch(summary, /externalSuspect/);
});

test("externalSuspect stays silent when the heap itself is small", () => {
  // A genuinely idle renderer legitimately has near-zero external memory; only a
  // large moving heap alongside pinned-zero external is evidence of cancellation.
  const log = createMemoryWatchLog({ now: seq() });
  log.record(sample({ usedHeapKB: 8 * 1024, externalKB: 3 }));
  log.record(sample({ usedHeapKB: 9 * 1024, externalKB: 5 }));
  const [summary] = log.flush();
  assert.doesNotMatch(summary, /externalSuspect/);
});

test("empty flush returns [] so a crash with no samples adds no noise", () => {
  assert.deepEqual(createMemoryWatchLog().flush(), []);
});

test("ring buffer is bounded to capacity", () => {
  const log = createMemoryWatchLog({ capacity: 3, now: seq() });
  for (let i = 1; i <= 10; i++) log.record(sample({ externalKB: i * 1024 }));
  assert.equal(log.size(), 3);
  const lines = log.flush();
  assert.equal(lines.length, 4, "summary + 3 samples");
  assert.match(lines[1], /external=8\.0MB/, "oldest retained is the 8th");
  assert.match(lines[3], /external=10\.0MB/);
});

test("oldest/latest external readings drive the trace arming check", () => {
  const log = createMemoryWatchLog({ now: seq() });
  assert.equal(log.oldestExternalKB(), null);
  assert.equal(log.latestExternalKB(), null);
  log.record(sample({ externalKB: 100 }));
  log.record(sample({ externalKB: null }));
  log.record(sample({ externalKB: 900 }));
  assert.equal(log.oldestExternalKB(), 100, "skips null readings from the front");
  assert.equal(log.latestExternalKB(), 900, "skips null readings from the back");
});

test("lastLine reflects the most recent sample", () => {
  const log = createMemoryWatchLog({ now: seq() });
  assert.equal(log.lastLine(), null);
  log.record(sample({ realm: "frame:widget", externalKB: 7 * 1024 }));
  assert.match(log.lastLine(), /realm=frame:widget/);
  assert.match(log.lastLine(), /external=7\.0MB/);
});

test("DEFAULT_CAPACITY covers more than the two minutes the old buffers kept", () => {
  assert.equal(typeof DEFAULT_CAPACITY, "number");
  assert.ok(DEFAULT_CAPACITY >= 48, "5s cadence x 48 = 4min, enough to tell a climb from a spike");
});
