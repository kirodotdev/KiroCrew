// The quit-time apply's closing log line must say what the app does next.
const { test } = require("node:test");
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const cp = require("node:child_process");

const { initAutoUpdate } = require("../auto-update");
const { makeDeps } = require("./auto-update-test-helpers");

async function appliedQuitLog({ restartNow }) {
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const lines = [];
  deps.log = { info: (m) => lines.push(String(m)), warn: () => {}, error: (m) => lines.push(String(m)) };
  deps.getAutoDownloadPreference = () => true;
  deps.app.relaunch = () => {};
  deps.app.quit = () => {};
  const exited = [];
  deps.app.exit = (code) => exited.push(code);
  const orig = cp.spawn;
  let applied = false;
  cp.spawn = (command) => {
    if (command === "apply") applied = true;
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    setImmediate(() => {
      // Once applied, the check reports up to date (non-zero), as a real one does.
      if (command === "check" && !applied) child.stdout.emit("data", Buffer.from("0.5.0.5"));
      child.emit("close", command === "check" && applied ? 1 : 0, null);
    });
    return child;
  };
  try {
    const u = initAutoUpdate(deps);
    await u.check();
    if (restartNow) u.relaunch();
    appOnce.find((r) => r.ev === "before-quit").fn({ preventDefault: () => {} });
    for (let i = 0; i < 100 && exited.length === 0; i += 1) await new Promise((r) => setImmediate(r));
  } finally {
    cp.spawn = orig;
  }
  return lines.find((l) => l.includes("managed apply on quit finished"));
}

test("a plain quit's apply log says it exits without relaunch", async () => {
  const line = await appliedQuitLog({ restartNow: false });
  assert.ok(line && line.endsWith("exiting without relaunch"), String(line));
});

test("a Restart now's apply log says it is relaunching", async () => {
  const line = await appliedQuitLog({ restartNow: true });
  assert.ok(line && line.endsWith("- relaunching"), String(line));
  assert.ok(!line.includes("without relaunch"), line);
});
