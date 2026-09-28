const { test } = require("node:test");
const assert = require("node:assert");
const { makeUpdaterLogger, MAX_ARG_CHARS } = require("../update-logger");

function makeSink() {
  const lines = [];
  const sink = (line) => lines.push(line);
  return { sink, lines };
}

// ---------------------------------------------------------------------------
// One call, one tagged line. The launch log is grepped line-by-line, so a
// level that emitted zero lines (or several) would corrupt that contract.
// ---------------------------------------------------------------------------

test("each level emits exactly one line tagged with its level", () => {
  const { sink, lines } = makeSink();
  const log = makeUpdaterLogger(sink);
  log.info("checking", "feed");
  log.warn("slow feed");
  log.error("boom");
  log.debug("probe");
  assert.deepStrictEqual(lines, [
    "[update:info] checking feed",
    "[update:warn] slow feed",
    "[update:error] boom",
    "[update:debug] probe",
  ]);
});

test("multi-line string args are collapsed to a single line", () => {
  const { sink, lines } = makeSink();
  makeUpdaterLogger(sink).info("first\nsecond\n  third");
  assert.strictEqual(lines.length, 1);
  assert.ok(!lines[0].includes("\n"), "emitted line must not contain newlines");
  assert.strictEqual(lines[0], "[update:info] first second third");
});

// ---------------------------------------------------------------------------
// Error serialization: message + FIRST stack frame only. electron-updater's
// HttpError dumps are multi-line; the first frame carries the signal and the
// rest floods the log.
// ---------------------------------------------------------------------------

test("Error serializes to message + first stack frame only", () => {
  const { sink, lines } = makeSink();
  const err = new Error("feed unreachable");
  err.stack = "Error: feed unreachable\n    at fetchFeed (auto-update.js:1:1)\n    at deeper (auto-update.js:2:2)";
  makeUpdaterLogger(sink).error("[update] check failed", err);
  assert.strictEqual(lines.length, 1);
  assert.ok(lines[0].includes("Error: feed unreachable"), "must carry the message");
  assert.ok(lines[0].includes("at fetchFeed (auto-update.js:1:1)"), "must carry the first frame");
  assert.ok(!lines[0].includes("at deeper"), "must drop every frame after the first");
  assert.ok(!lines[0].includes("\n"), "must stay a single line");
});

test("Error with no stack still serializes its message", () => {
  const { sink, lines } = makeSink();
  const err = new Error("bare");
  err.stack = undefined;
  makeUpdaterLogger(sink).error(err);
  assert.strictEqual(lines[0], "[update:error] Error: bare");
});

// ---------------------------------------------------------------------------
// Truncation: any single argument is bounded so one giant HttpError body
// cannot flood the launch log.
// ---------------------------------------------------------------------------

test("an over-long argument is truncated with a marker", () => {
  const { sink, lines } = makeSink();
  const huge = "x".repeat(MAX_ARG_CHARS * 4);
  makeUpdaterLogger(sink).info(huge);
  assert.strictEqual(lines.length, 1);
  assert.ok(lines[0].includes("…[truncated]"), "must mark the cut");
  assert.ok(
    lines[0].length < MAX_ARG_CHARS + 50,
    `line stayed bounded (got ${lines[0].length} chars)`,
  );
});

// ---------------------------------------------------------------------------
// Never throw: the logger sits inside updater lifecycle callbacks, where a
// throw would break the one subsystem it exists to observe.
// ---------------------------------------------------------------------------

test("a throwing sink does not propagate out of any level", () => {
  const log = makeUpdaterLogger(() => { throw new Error("disk full"); });
  assert.doesNotThrow(() => log.info("a"));
  assert.doesNotThrow(() => log.warn("b"));
  assert.doesNotThrow(() => log.error("c", new Error("inner")));
  assert.doesNotThrow(() => log.debug("d"));
});

test("unserializable args do not throw (circular object falls back to String)", () => {
  const { sink, lines } = makeSink();
  const circular = {};
  circular.self = circular;
  assert.doesNotThrow(() => makeUpdaterLogger(sink).info(circular));
  assert.strictEqual(lines.length, 1);
  assert.ok(lines[0].startsWith("[update:info] "));
});
