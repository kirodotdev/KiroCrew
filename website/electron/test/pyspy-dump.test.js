const { test } = require("node:test");
const assert = require("node:assert");
const { capturePySpyDump, resolvePySpy } = require("../pyspy-dump");

test("writes dump file and returns ok on success", async () => {
  const writes = [];
  const res = await capturePySpyDump({
    pid: 4242,
    dumpDir: "/tmp/dumps",
    bin: "py-spy",
    run: (bin, args, timeoutMs, cb) => {
      assert.strictEqual(bin, "py-spy");
      assert.deepStrictEqual(args, ["dump", "--pid", "4242", "--nonblocking"]);
      cb(null, "Thread 0x1 (idle)\n  frozen_frame (foo.py:42)\n", "");
    },
    writeFileSync: (p, data) => writes.push([p, data]),
    now: () => new Date("2026-06-25T01:02:03.456Z"),
  });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(writes.length, 1);
  assert.match(writes[0][0], /py-spy-dump-4242-2026-06-25T01-02-03-456Z\.txt$/);
  assert.match(writes[0][1], /frozen_frame/);
});

test("returns ok:false with install hint when py-spy is missing (ENOENT)", async () => {
  const err = new Error("spawn py-spy ENOENT");
  err.code = "ENOENT";
  let logged = "";
  const res = await capturePySpyDump({
    pid: 5,
    dumpDir: "/tmp/dumps",
    bin: "py-spy",
    run: (b, a, t, cb) => cb(err),
    writeFileSync: () => {
      throw new Error("should not write on failure");
    },
    log: (m) => {
      logged += m;
    },
  });
  assert.strictEqual(res.ok, false);
  assert.match(res.reason, /not installed/);
  assert.match(logged, /brew install py-spy/);
});

test("surfaces py-spy permission failure (macOS sudo case)", async () => {
  const err = new Error("exited code 1");
  err.code = 1;
  const res = await capturePySpyDump({
    pid: 9,
    dumpDir: "/tmp",
    bin: "py-spy",
    run: (b, a, t, cb) => cb(err, "", "Permission denied (os error 1). Try running again with sudo."),
    writeFileSync: () => {},
  });
  assert.strictEqual(res.ok, false);
  assert.match(res.reason, /Permission denied/);
});

test("returns ok:false and never invokes py-spy when pid is missing", async () => {
  let ran = false;
  const res = await capturePySpyDump({
    pid: 0,
    dumpDir: "/tmp",
    run: () => {
      ran = true;
    },
    writeFileSync: () => {
      throw new Error("should not write");
    },
  });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, "no pid");
  assert.strictEqual(ran, false);
});

test("resolvePySpy falls back to bare PATH lookup when no candidate exists", () => {
  assert.strictEqual(
    resolvePySpy(() => false),
    "py-spy",
  );
});

test("resolvePySpy returns the first existing candidate", () => {
  const hit = "/opt/homebrew/bin/py-spy";
  assert.strictEqual(
    resolvePySpy((p) => p === hit),
    hit,
  );
});

test("JS-side backstop resolves when py-spy never calls back (hung ptrace)", async () => {
  // Model a py-spy stuck in an uninterruptible ptrace wait: run() never invokes
  // its callback. Without the backstop the promise would hang forever, blocking
  // the recovery SIGKILL. We inject a fake timer so the test is instant.
  let scheduled = null;
  // Do NOT await yet — run() never calls back, so the promise only settles once
  // we fire the captured backstop timer below.
  const p = capturePySpyDump({
    pid: 4242,
    dumpDir: "/tmp",
    bin: "py-spy",
    timeoutMs: 5000,
    run: () => { /* never calls cb — simulates a hung py-spy */ },
    writeFileSync: () => { throw new Error("should not write on timeout"); },
    setTimeoutFn: (fn, ms) => { scheduled = { fn, ms }; return { unref() {} }; },
    clearTimeoutFn: () => {},
  });
  // Captured the backstop with the documented grace beyond timeoutMs.
  assert.strictEqual(scheduled.ms, 5250);
  // Fire it as the event loop would at the deadline.
  scheduled.fn();
  const res = await p;
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.reason, "timeout");
});

test("backstop is cleared when py-spy answers normally (no late timeout)", async () => {
  let cleared = false;
  const res = await capturePySpyDump({
    pid: 4242,
    dumpDir: "/tmp",
    bin: "py-spy",
    run: (b, a, t, cb) => cb(null, "Thread 0x1\n  frame (x.py:1)\n", ""),
    writeFileSync: () => {},
    now: () => new Date("2026-06-25T00:00:00.000Z"),
    setTimeoutFn: () => ({ unref() {} }),
    clearTimeoutFn: () => { cleared = true; },
  });
  assert.strictEqual(res.ok, true);
  assert.strictEqual(cleared, true);
});
