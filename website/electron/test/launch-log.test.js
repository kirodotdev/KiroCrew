"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createLaunchLog } = require("../launch-log");

const fixedNow = () => new Date("2026-10-01T15:27:02.300Z");

/**
 * A console whose streams behave like a revoked tty once `dead` is set: the
 * write is accepted, and the failure arrives on a later tick as an 'error'
 * event on the stream -- which is how Node reports a failed stdio write. With
 * no 'error' listener on the stream, Node raises it as an uncaughtException;
 * `onUncaught` stands in for that process-level handler.
 */
function deadTtyConsole(stdout, stderr, onUncaught) {
  const state = { dead: false, writes: 0 };
  const write = (stream) => () => {
    state.writes += 1;
    if (state.dead) {
      setImmediate(() => {
        const error = new Error("write EIO");
        if (stream.listenerCount("error") > 0) {
          stream.emit("error", error);
        } else {
          onUncaught(error);
        }
      });
    }
  };
  return { state, console: { warn: write(stderr), error: write(stderr), log: write(stdout) } };
}

const ticks = (n) =>
  new Promise((resolve) => {
    let left = n;
    const step = () => (--left <= 0 ? resolve() : setImmediate(step));
    setImmediate(step);
  });

test("a stdio 'error' is swallowed instead of reaching uncaughtException", () => {
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  createLaunchLog({ append: () => {}, console: { warn() {}, error() {} }, streams: [stdout, stderr] });
  // An EventEmitter with no 'error' listener throws on emit; in the real
  // process that throw is the uncaughtException that started the loop.
  assert.doesNotThrow(() => stderr.emit("error", new Error("write EIO")));
  assert.doesNotThrow(() => stdout.emit("error", new Error("write EIO")));
});

/**
 * Wires a launch log to a dead-tty console plus main.js's last-resort handler,
 * which reports every uncaught error through gerror.
 */
function harness({ guardStreams }) {
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  const counts = { uncaught: 0 };
  let log;
  const safetyNet = (error) => {
    counts.uncaught += 1;
    // Bounded so the unguarded control below terminates.
    if (counts.uncaught < 50) {
      log.gerror("uncaughtException: " + error.message);
    }
  };
  const { state, console: sink } = deadTtyConsole(stdout, stderr, safetyNet);
  const lines = [];
  log = createLaunchLog({
    append: (entry) => lines.push(entry),
    console: sink,
    streams: guardStreams ? [stdout, stderr] : [],
    now: fixedNow,
  });
  return { log, state, counts, lines };
}

test("control: without the stdio listeners the safety net feeds itself", async () => {
  // This is the #15950 loop: each gerror mirrors to the dead tty, whose
  // failure is the next uncaughtException.
  const { log, state, counts } = harness({ guardStreams: false });
  state.dead = true;
  log.gwarn("Stopping gateway gracefully...");
  await ticks(200);
  assert.equal(counts.uncaught, 50);
});

test("a dead tty disables the console mirror and logs one diagnostic, no loop", async () => {
  const { log, state, counts, lines } = harness({ guardStreams: true });
  state.dead = true;
  log.gwarn("Stopping gateway gracefully...");
  await ticks(20);

  assert.equal(log.isConsoleAlive(), false);
  assert.equal(counts.uncaught, 0, "the stream error never reaches the safety net");
  assert.equal(state.writes, 1, "only the first gwarn reached the dead console");
  const diagnostics = lines.filter((l) => l.includes("console mirror disabled"));
  assert.equal(diagnostics.length, 1);
  assert.match(diagnostics[0], /stdio write failed \(write EIO\)/);

  // The file log carries on after the mirror is gone.
  log.gerror("later failure");
  log.gwarn("later warning");
  await ticks(5);
  assert.equal(state.writes, 1);
  assert.ok(lines.some((l) => l === "[2026-10-01T15:27:02.300Z] later failure\n"));
  assert.ok(lines.some((l) => l === "[2026-10-01T15:27:02.300Z] later warning\n"));
});

test("with live stdio, warnings and errors still mirror to the console", () => {
  const calls = [];
  const lines = [];
  const log = createLaunchLog({
    append: (entry) => lines.push(entry),
    console: { warn: (m) => calls.push(["warn", m]), error: (m) => calls.push(["error", m]) },
    streams: [new EventEmitter(), new EventEmitter()],
    now: fixedNow,
  });
  log.glog("plain");
  log.gwarn("careful");
  log.gerror("broken");
  assert.deepEqual(lines, [
    "[2026-10-01T15:27:02.300Z] plain\n",
    "[2026-10-01T15:27:02.300Z] careful\n",
    "[2026-10-01T15:27:02.300Z] broken\n",
  ]);
  assert.deepEqual(calls, [
    ["warn", "[gateway-launch] careful"],
    ["error", "[gateway-launch] broken"],
  ]);
});

test("a failed file write falls back to the console only while stdio is alive", () => {
  const stderr = new EventEmitter();
  const calls = [];
  const log = createLaunchLog({
    append: () => {
      throw new Error("ENOSPC");
    },
    console: { warn: (m) => calls.push(m), error: (m) => calls.push(m) },
    streams: [stderr],
  });
  log.glog("first");
  assert.deepEqual(calls, ["[gateway-launch] first (log write failed: ENOSPC)"]);

  stderr.emit("error", new Error("write EIO"));
  assert.doesNotThrow(() => log.gerror("second"));
  assert.equal(calls.length, 1, "nothing reaches a dead console, even as a fallback");
});

test("a missing stream is skipped", () => {
  assert.doesNotThrow(() =>
    createLaunchLog({ append: () => {}, console: { warn() {}, error() {} }, streams: [null, undefined] }),
  );
});
