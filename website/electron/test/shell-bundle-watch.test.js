"use strict";

const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  createShellBundleWatch,
  missingShellBundleParts,
  promptRestartForPrunedBundle,
} = require(path.join(__dirname, "..", "shell-bundle-watch.js"));

const EXEC_PATH = "/Apps/0.8.0.3/KiroCrew.app/Contents/MacOS/Kiro Crew";
const RESOURCES = "/Apps/0.8.0.3/KiroCrew.app/Contents/Resources";
const ASAR = `${RESOURCES}/app.asar`;

// A filesystem whose contents a test edits in place: `present` holds every path
// that exists right now.
function fakeFs(present) {
  return {
    present,
    existsSync(target) { return present.has(target); },
  };
}

function fakeTimers() {
  const timeouts = [];
  const intervals = [];
  let nextId = 1;
  return {
    timeouts,
    intervals,
    setTimeoutFn(fn, ms) {
      const id = nextId++;
      timeouts.push({ id, fn, ms });
      return id;
    },
    clearTimeoutFn(id) {
      const index = timeouts.findIndex((timer) => timer.id === id);
      if (index >= 0) timeouts.splice(index, 1);
    },
    setIntervalFn(fn, ms) {
      const id = nextId++;
      intervals.push({ id, fn, ms });
      return id;
    },
    clearIntervalFn(id) {
      const index = intervals.findIndex((timer) => timer.id === id);
      if (index >= 0) intervals.splice(index, 1);
    },
    fireTimeout() {
      const timer = timeouts.shift();
      assert.ok(timer, "a confirming probe is armed");
      timer.fn();
    },
    tick() {
      assert.strictEqual(intervals.length, 1, "one interval is armed");
      intervals[0].fn();
    },
  };
}

function build({
  platform = "darwin",
  isPackaged = true,
  present = new Set([EXEC_PATH, ASAR]),
  isUpdating = () => false,
  isQuitting = () => false,
} = {}) {
  const fs = fakeFs(present);
  const timers = fakeTimers();
  const pruned = [];
  const logs = [];
  const watch = createShellBundleWatch({
    fs,
    path: path.posix,
    processObj: { platform, execPath: EXEC_PATH, resourcesPath: RESOURCES },
    isPackaged,
    isUpdating,
    isQuitting,
    onPruned: (missing) => pruned.push(missing),
    log: (message) => logs.push(message),
    intervalMs: 60_000,
    confirmDelayMs: 5_000,
    setIntervalFn: timers.setIntervalFn,
    clearIntervalFn: timers.clearIntervalFn,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });
  return { watch, fs, timers, pruned, logs };
}

test("missingShellBundleParts names the executable and app.asar that are gone", () => {
  const processObj = { execPath: EXEC_PATH, resourcesPath: RESOURCES };
  assert.deepStrictEqual(
    missingShellBundleParts({ fs: fakeFs(new Set([EXEC_PATH, ASAR])), path: path.posix, processObj }),
    [],
  );
  assert.deepStrictEqual(
    missingShellBundleParts({ fs: fakeFs(new Set()), path: path.posix, processObj }),
    [EXEC_PATH, ASAR],
  );
  assert.deepStrictEqual(
    missingShellBundleParts({ fs: fakeFs(new Set([EXEC_PATH])), path: path.posix, processObj }),
    [ASAR],
  );
});

test("a probe that throws is not read as a missing file", () => {
  const fs = { existsSync() { throw new Error("EIO"); } };
  const processObj = { execPath: EXEC_PATH, resourcesPath: RESOURCES };
  assert.deepStrictEqual(missingShellBundleParts({ fs, path: path.posix, processObj }), []);
});

test("an intact bundle never prompts", () => {
  const { watch, timers, pruned } = build();
  watch.start();
  timers.tick();
  timers.tick();
  assert.strictEqual(timers.timeouts.length, 0);
  assert.deepStrictEqual(pruned, []);
});

test("a pruned bundle found by the periodic probe prompts once, after confirming", () => {
  const { watch, fs, timers, pruned } = build();
  watch.start();
  fs.present.clear();

  timers.tick();
  assert.deepStrictEqual(pruned, [], "a single miss only arms the confirming probe");
  assert.strictEqual(timers.timeouts[0].ms, 5_000);

  timers.fireTimeout();
  assert.deepStrictEqual(pruned, [[EXEC_PATH, ASAR]]);
  assert.strictEqual(timers.intervals.length, 0, "the watch stops once it has asked");

  assert.strictEqual(watch.checkNow(), false);
  assert.deepStrictEqual(pruned.length, 1);
});

test("a bundle swapped back in at the same path before the second probe does not prompt", () => {
  const { watch, fs, timers, pruned } = build();
  watch.start();
  fs.present.delete(ASAR);
  timers.tick();
  fs.present.add(ASAR);
  timers.fireTimeout();
  assert.deepStrictEqual(pruned, []);
  assert.strictEqual(timers.intervals.length, 1, "the watch keeps running");
});

test("checkNow from a stale-bundle respawn finds the prune without waiting for the interval", () => {
  const { watch, fs, timers, pruned } = build();
  watch.start();
  fs.present.clear();

  assert.strictEqual(watch.checkNow(), true);
  assert.strictEqual(watch.checkNow(), true, "a second call reuses the pending confirmation");
  assert.strictEqual(timers.timeouts.length, 1);
  timers.fireTimeout();
  assert.strictEqual(pruned.length, 1);
});

test("the backend recovering does not count as the shell recovering", () => {
  // Exit 75, re-resolve to a launcher outside the bundle, own executable gone:
  // the supervisor reports the respawn and the watch must still ask.
  const { watch, timers, pruned } = build({ present: new Set() });
  watch.start();
  watch.checkNow();
  timers.fireTimeout();
  assert.notDeepStrictEqual(pruned, [], "a healthy backend must not end the incident for the shell");
});

test("nothing is probed while an update owns the bundle or the app is quitting", () => {
  let updating = true;
  let quitting = false;
  const { watch, timers, pruned } = build({
    present: new Set(),
    isUpdating: () => updating,
    isQuitting: () => quitting,
  });
  watch.start();
  assert.strictEqual(watch.checkNow(), false);

  updating = false;
  assert.strictEqual(watch.checkNow(), true);
  updating = true;
  timers.fireTimeout();
  assert.deepStrictEqual(pruned, [], "an update that starts mid-confirmation wins");

  updating = false;
  quitting = true;
  assert.strictEqual(watch.checkNow(), false);
});

for (const [label, options] of [
  ["Windows", { platform: "win32" }],
  ["Linux", { platform: "linux" }],
  ["an unpackaged macOS run", { isPackaged: false }],
]) {
  test(`${label} is not watched`, () => {
    const { watch, timers, pruned } = build({ ...options, present: new Set() });
    assert.strictEqual(watch.enabled, false);
    watch.start();
    assert.strictEqual(timers.intervals.length, 0);
    assert.strictEqual(watch.checkNow(), false);
    assert.deepStrictEqual(pruned, []);
  });
}

test("stop disarms both the interval and a pending confirmation", () => {
  const { watch, fs, timers, pruned } = build();
  watch.start();
  fs.present.clear();
  watch.checkNow();
  watch.stop();
  assert.strictEqual(timers.intervals.length, 0);
  assert.strictEqual(timers.timeouts.length, 0);
  assert.deepStrictEqual(pruned, []);
});

test("an onPruned that throws is logged, not raised", () => {
  const timers = fakeTimers();
  const logs = [];
  const watch = createShellBundleWatch({
    fs: fakeFs(new Set()),
    path: path.posix,
    processObj: { platform: "darwin", execPath: EXEC_PATH, resourcesPath: RESOURCES },
    isPackaged: true,
    onPruned: () => { throw new Error("no dialog"); },
    log: (message) => logs.push(message),
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
    setIntervalFn: timers.setIntervalFn,
    clearIntervalFn: timers.clearIntervalFn,
  });
  watch.checkNow();
  assert.doesNotThrow(() => timers.fireTimeout());
  assert.ok(logs.some((line) => line.includes("restart prompt failed: no dialog")));
});

test("the restart prompt quits only when the user chooses to", async () => {
  for (const [response, expectQuit] of [[0, true], [1, false]]) {
    let quits = 0;
    let options = null;
    const quit = await promptRestartForPrunedBundle({
      dialog: { showMessageBox: async (shown) => { options = shown; return { response }; } },
      requestQuit: () => { quits += 1; },
    });
    assert.strictEqual(quit, expectQuit);
    assert.strictEqual(quits, expectQuit ? 1 : 0);
    assert.deepStrictEqual(options.buttons, ["Quit Kiro Crew", "Later"]);
    assert.strictEqual(options.cancelId, 1, "dismissing the dialog keeps the app running");
  }
});

test("a restart prompt that cannot be shown keeps the app running", async () => {
  let quits = 0;
  const quit = await promptRestartForPrunedBundle({
    dialog: { showMessageBox: async () => { throw new Error("no display"); } },
    requestQuit: () => { quits += 1; },
  });
  assert.strictEqual(quit, false);
  assert.strictEqual(quits, 0);
});
