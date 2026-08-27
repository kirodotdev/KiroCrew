"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  createBigAllocLog,
  normalizeEvent,
  DEFAULT_CAPACITY,
} = require("../big-alloc-log");

const seq = () => {
  let n = 0;
  return () => `t${n++}`;
};

test("normalizeEvent coerces and defaults", () => {
  const e = normalizeEvent({ kind: "Uint8Array", bytes: 128, outcome: "requested", stack: "a <- b" });
  assert.equal(e.kind, "Uint8Array");
  assert.equal(e.bytes, 128);
  assert.equal(e.outcome, "requested");
  assert.equal(e.stack, "a <- b");
  assert.equal(e.error, "");
});

test("normalizeEvent rejects non-positive / non-numeric bytes", () => {
  assert.equal(normalizeEvent({ bytes: 0 }), null);
  assert.equal(normalizeEvent({ bytes: -5 }), null);
  assert.equal(normalizeEvent({ bytes: "big" }), null);
  assert.equal(normalizeEvent({ bytes: NaN }), null);
  assert.equal(normalizeEvent(null), null);
  assert.equal(normalizeEvent("nope"), null);
});

test("normalizeEvent caps kind, stack, error and keeps error only when failed", () => {
  const e = normalizeEvent({
    kind: "x".repeat(100),
    bytes: 10,
    outcome: "failed",
    stack: "s".repeat(9000),
    error: "e".repeat(500),
  });
  assert.equal(e.kind.length, 40);
  assert.equal(e.stack.length, 4000);
  assert.equal(e.error.length, 200);
  assert.equal(e.outcome, "failed");

  const ok = normalizeEvent({ bytes: 10, outcome: "requested", error: "should be dropped" });
  assert.equal(ok.error, "");
});

test("normalizeEvent flattens control characters to block log-line injection", () => {
  const e = normalizeEvent({
    kind: "Uint8Array",
    bytes: 10,
    outcome: "failed",
    stack: "siteA\n[big-alloc] forged summary line\r\tmore",
    error: "boom\nsecond line",
  });
  // No raw newline/tab/CR survives into a value that gets written to the log.
  assert.doesNotMatch(e.stack, /[\r\n\t]/);
  assert.doesNotMatch(e.error, /[\r\n\t]/);
  assert.ok(e.stack.includes("forged summary line")); // content kept, control chars flattened
});

test("record + flush emits summary first then events oldest to newest", () => {
  const log = createBigAllocLog({ now: seq() });
  assert.equal(log.record({ kind: "ArrayBuffer", bytes: 100 * 1024 * 1024, outcome: "requested", stack: "siteA" }).bytes, 100 * 1024 * 1024);
  log.record({ kind: "Uint8Array", bytes: 70 * 1024 * 1024, outcome: "failed", stack: "siteB", error: "RangeError" });

  const lines = log.flush();
  assert.equal(lines.length, 3);
  assert.match(lines[0], /pre-crash summary over 2 event\(s\)/);
  assert.match(lines[0], /peakBytes=104857600/);
  assert.match(lines[0], /peakKind=ArrayBuffer/);
  assert.match(lines[0], /failed=1/);
  assert.match(lines[1], /kind=ArrayBuffer/);
  assert.match(lines[1], /from=siteA/);
  assert.match(lines[2], /kind=Uint8Array/);
  assert.match(lines[2], /outcome=failed/);
  assert.match(lines[2], /error=RangeError/);

  // flush drains
  assert.deepEqual(log.flush(), []);
  assert.equal(log.size(), 0);
});

test("empty flush returns [] (no noise when nothing large was allocated)", () => {
  const log = createBigAllocLog();
  assert.deepEqual(log.flush(), []);
});

test("malformed reports are dropped and not recorded", () => {
  const log = createBigAllocLog();
  assert.equal(log.record({ bytes: 0 }), null);
  assert.equal(log.size(), 0);
});

test("ring buffer is bounded to capacity", () => {
  const log = createBigAllocLog({ capacity: 3, now: seq() });
  for (let i = 1; i <= 10; i++) log.record({ kind: "ArrayBuffer", bytes: i * 1024 * 1024, stack: `s${i}` });
  assert.equal(log.size(), 3);
  const lines = log.flush();
  // summary + 3 events
  assert.equal(lines.length, 4);
  // oldest retained is the 8th allocation
  assert.match(lines[1], /from=s8/);
  assert.match(lines[3], /from=s10/);
});

test("lastLine reflects the most recent event", () => {
  const log = createBigAllocLog({ now: seq() });
  assert.equal(log.lastLine(), null);
  log.record({ kind: "Float64Array", bytes: 80 * 1024 * 1024, stack: "here" });
  assert.match(log.lastLine(), /kind=Float64Array/);
  assert.match(log.lastLine(), /from=here/);
});

test("DEFAULT_CAPACITY is exported and positive", () => {
  assert.equal(typeof DEFAULT_CAPACITY, "number");
  assert.ok(DEFAULT_CAPACITY > 0);
});
