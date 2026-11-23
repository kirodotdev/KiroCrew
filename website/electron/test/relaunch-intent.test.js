// A relaunch is a restart, so the desktop must not tell the gateway it was a user
// stop: only a real Quit ends the YOLO and chat trust the gateway keeps.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const intent = require("../relaunch-intent");
const { initAutoUpdate } = require("../auto-update");
const { makeDeps } = require("./auto-update-test-helpers");

test("every app.relaunch() call notes a relaunch, and a plain quit notes none", () => {
  intent.resetForTests();
  const calls = [];
  const app = { relaunch: (...args) => calls.push(args) };
  intent.watchRelaunch(app);
  intent.watchRelaunch(app); // idempotent: wrapped once
  assert.strictEqual(intent.isRelaunching(), false);
  app.relaunch({ execPath: "/x" });
  assert.strictEqual(intent.isRelaunching(), true);
  assert.deepStrictEqual(calls, [[{ execPath: "/x" }]]);
  intent.resetForTests();
});

test("Restart now on an armed update notes a relaunch although it only quits", async () => {
  intent.resetForTests();
  const { deps, emit } = makeDeps();
  deps.app.once = () => {};
  deps.app.removeListener = () => {};
  deps.app.quit = () => {};
  deps.app.relaunch = () => {};
  deps.stopGateway = async () => {};
  deps.getAutoDownloadPreference = () => false;
  deps.isPolicyGoverned = () => false;
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "9.9.9" });
  await u.install();
  assert.strictEqual(intent.isRelaunching(), false);
  u.relaunch();
  assert.strictEqual(intent.isRelaunching(), true);
  intent.resetForTests();
});

test("the Quit handler sends userStop only when the quit is not a relaunch", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, /watchRelaunch\(app\);/);
  assert.match(
    main,
    /gateway\.stopOnQuit\(\{ userStop: !quittingForUpdate && !osSessionEnding && !isRelaunching\(\) \}\);/,
  );
});
