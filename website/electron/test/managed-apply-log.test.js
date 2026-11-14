// A managed update apply logs the command's pid when it starts and its outcome,
// run time and output size when it ends, never the output text; a timeout reads
// as a timeout, not as "exited 1"; a check run logs nothing (#36690).
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const cp = require("node:child_process");

const { createManagedExec } = require("../runtime/update/managed-exec");
const { runArmedApply } = require("../runtime/update/armed-apply");

function recordingLog() {
  const lines = [];
  return { lines, log: { info: (m) => lines.push(String(m)), error: (m) => lines.push(String(m)) } };
}

function execFor(updateCommand, log, checkCommand = "") {
  return createManagedExec({ managed: { updateCommand, checkCommand }, managedPath: () => process.env.PATH, log });
}

test("a failed apply logs its pid at start and its exit, run time and output size at the end", async () => {
  const { lines, log } = recordingLog();
  // The secret-shaped text must reach the buffer but never the log.
  const exec = execFor("echo PROBE-SECRET-token=abc123 && exit 3", log);
  const result = await runArmedApply({ exec, version: "9.9.9", log });

  assert.deepStrictEqual(result, { ok: false, message: "managed update command exited 3" });
  const start = lines.find((l) => l.startsWith("[update] managed update command started (pid "));
  assert.ok(start, lines.join("\n"));
  const pid = start.slice("[update] managed update command started (pid ".length, -1);
  assert.ok(Number.isInteger(Number(pid)) && Number(pid) > 0, start);
  const end = lines.find((l) => l.startsWith("[update] managed update command ended: exit 3, ran "));
  assert.ok(end, lines.join("\n"));
  assert.ok(end.endsWith(" bytes of output") && !end.includes(" 0 bytes"), end);
  for (const line of lines) {
    assert.ok(!line.includes("PROBE-SECRET") && !line.includes("abc123"), line);
  }
});

test("a timeout kill is logged and reported as a timeout, never as exited 1", async (t) => {
  const orig = cp.spawn;
  t.after(() => { cp.spawn = orig; });
  // A child that never exits on its own: only the timeout's kill closes it.
  cp.spawn = () => {
    const child = new EventEmitter();
    child.pid = 4242;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = () => setImmediate(() => child.emit("close", null, "SIGTERM"));
    return child;
  };
  const { lines, log } = recordingLog();
  const exec = execFor("pkgtool update", log);
  const r = await exec.runManagedCommand("pkgtool update", { timeout: 1, label: "update command" });
  assert.strictEqual(r.timedOut, true);
  assert.strictEqual(r.failed, true);
  assert.deepStrictEqual(lines[0], "[update] managed update command started (pid 4242)");
  assert.ok(lines[1].startsWith("[update] managed update command ended: timed out after 1 ms, ran "), lines[1]);
  assert.ok(!lines.join("\n").includes("exit 1"), lines.join("\n"));

  const fakeExec = { ...exec, runUpdateCommand: async () => r };
  const applied = await runArmedApply({ exec: fakeExec, version: "9.9.9", log });
  assert.strictEqual(applied.ok, false);
  assert.strictEqual(applied.message, "managed update command timed out after 30 minutes");
});

test("a command that exits in time is not timed out while a helper still holds its pipes", async (t) => {
  const orig = cp.spawn;
  t.after(() => { cp.spawn = orig; });
  let killed = false;
  // The shell exits at once; its background helper keeps the pipes open, so
  // `close` arrives only after the 1 ms deadline has passed.
  cp.spawn = () => {
    const child = new EventEmitter();
    child.pid = 4243;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = () => { killed = true; };
    process.nextTick(() => {
      child.stdout.emit("data", Buffer.from("9.9.9\n"));
      child.emit("exit", 0, null);
      setTimeout(() => child.emit("close", 0, null), 20);
    });
    return child;
  };
  const { log } = recordingLog();
  const exec = execFor("unused", log, "pkgtool check");
  const r = await exec.runManagedCommand("pkgtool check", { timeout: 1 });
  assert.strictEqual(killed, false, "a command that already exited must not be killed");
  assert.deepStrictEqual({ code: r.code, failed: r.failed, timedOut: r.timedOut }, { code: 0, failed: false, timedOut: false });
  assert.strictEqual(r.stdout, "9.9.9\n");
});

test("an update command that cannot start says so instead of exited 1", async () => {
  const { log } = recordingLog();
  const exec = execFor("unused", log);
  const fakeExec = { ...exec, runUpdateCommand: async () => ({ code: 1, failed: true, timedOut: false, signal: null }) };
  const applied = await runArmedApply({ exec: fakeExec, version: "9.9.9", log });
  assert.strictEqual(applied.message, "managed update command could not run");
});

test("a check run adds no log line", async () => {
  const { lines, log } = recordingLog();
  const exec = execFor("unused", log, "echo 9.9.9");
  const r = await exec.runManagedCommand("echo 9.9.9", { timeout: exec.MANAGED_CHECK_TIMEOUT_MS });
  assert.strictEqual(r.code, 0);
  assert.deepStrictEqual(lines, []);
});
