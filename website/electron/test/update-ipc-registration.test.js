// The update IPC handlers must be registered BEFORE the awaited gateway boot.
//
// WHY THIS IS A TEST: preload.js exposes window.updateAPI unconditionally, so
// Settings > About renders a live "Check for updates" button as soon as the
// renderer loads. main.js used to register ipcMain.handle("update:*") only AFTER
// `await startGateway()` and `await showLoadingThenConnect(win)`. Any slow or
// failed gateway boot therefore left the buttons present with no handler behind
// them, and the renderer's invoke rejected with a raw
// "No handler registered for 'update:check'".
//
// That is not a hypothetical: it is exactly how the nightly OTA lane
// (.github/workflows/ota-test.yml) failed -- its CDP driver called
// window.updateAPI.check() and got that error, so the job died at "Drive the
// update" and never reached "Assert the bundle actually swapped". The one gate
// that would have caught the macOS install-handoff bug could not run.
//
// This asserts on SOURCE ORDER rather than behaviour because main.js is a
// top-level Electron entrypoint: importing it requires a real Electron runtime
// (app, BrowserWindow, session, systemPreferences), which node:test has no way
// to provide. Order is the reachable invariant.
const { test } = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const SRC = readFileSync(path.join(__dirname, "..", "main.js"), "utf8");

// The awaited boot steps that must NOT gate handler registration.
const BOOT_AWAITS = ["await startGateway();", "await showLoadingThenConnect(win);"];
const UPDATE_HANDLERS = [
  'ipcMain.handle("update:get-info"',
  'ipcMain.handle("update:check"',
  'ipcMain.handle("update:download"',
  'ipcMain.handle("update:install"',
  'ipcMain.handle("update:set-channel"',
];

test("every update:* IPC handler registers before the awaited gateway boot", () => {
  // Anchor on the ready-handler's boot sequence, which is the only place these
  // two awaits appear together.
  const bootIndex = Math.min(...BOOT_AWAITS.map((needle) => {
    const at = SRC.lastIndexOf(needle);
    assert.notStrictEqual(at, -1, `boot step vanished from main.js: ${needle} — re-derive this test`);
    return at;
  }));

  for (const handler of UPDATE_HANDLERS) {
    const at = SRC.indexOf(handler);
    assert.notStrictEqual(at, -1, `missing handler registration: ${handler}`);
    assert.ok(
      at < bootIndex,
      `${handler} is registered AFTER the awaited gateway boot. preload exposes the button regardless, so a stalled boot leaves it with no handler and the renderer throws "No handler registered". Move the updater block above startGateway().`,
    );
  }
});

test("initAutoUpdate is wired before the awaited gateway boot", () => {
  const init = SRC.indexOf("updater = initAutoUpdate({");
  const boot = SRC.lastIndexOf("await startGateway();");
  assert.notStrictEqual(init, -1);
  assert.notStrictEqual(boot, -1);
  assert.ok(init < boot, "initAutoUpdate moved back below the gateway boot");
});

test("the updater block does not depend on gateway boot completing", () => {
  // stopGateway must stay a LAZY callback. Passing an already-resolved handle or
  // calling stopGatewayGracefully() eagerly here would reintroduce the ordering
  // dependency this test exists to prevent.
  assert.match(
    SRC,
    /stopGateway:\s*\(\)\s*=>\s*stopGatewayGracefully\(\)/,
    "stopGateway is no longer passed as a lazy callback — the updater block may now depend on gateway state at wiring time",
  );
});

test("liveness recovery stands down during an update install", () => {
  // Field incident (gateway-launch.log 2026-07-29T22:18): the updater stopped
  // the gateway intentionally; the watchdog counted 3 failed probes and
  // respawned it MID-SWAP, which reloaded the page and re-armed the install
  // button. The guard must treat an in-flight install exactly like a quit.
  assert.match(
    SRC,
    /if \(isQuitting \|\| installingUpdate\) return;/,
    "the liveness onUnresponsive guard no longer checks installingUpdate -- recovery can resurrect the gateway during a bundle swap",
  );
  // And the flag must actually be set by the updater's dispatch hook.
  assert.match(
    SRC,
    /onInstallDispatched:[\s\S]{0,200}?installingUpdate = true/,
    "onInstallDispatched no longer sets installingUpdate",
  );
});

test("a failed install re-arms gateway recovery", () => {
  // GPT round-3 finding: onInstallDispatched disarms the watchdog, so without
  // this an install failure leaves the app alive with a permanently dead
  // dashboard (gateway stopped, monitor stopped, nothing restarts either).
  assert.match(
    SRC,
    /onInstallFailed:[\s\S]{0,400}?installingUpdate = false/,
    "onInstallFailed no longer clears installingUpdate",
  );
  assert.match(
    SRC,
    /onInstallFailed:[\s\S]{0,600}?recoverWedgedGateway\(/,
    "onInstallFailed no longer actively restores the gateway -- nothing else will after dispatch",
  );
});

test("updater init failure cannot gate gateway startup (fail-open)", () => {
  // GPT round-4 finding: the registration reorder put initAutoUpdate BEFORE the
  // awaited gateway boot, so an init-time throw (e.g. malformed
  // KIROCREW_UPDATE_FEED reaching a URL parse) would abort the ready handler
  // and leave the app unusable -- strictly worse than a broken updater.
  assert.match(
    SRC,
    /let updater;\s*\n\s*try \{\s*\n\s*updater = initAutoUpdate\(/,
    "initAutoUpdate is no longer wrapped in try/catch -- an init throw now aborts app boot",
  );
  assert.match(
    SRC,
    /disabled: "init-failed"/,
    "the fail-open stub no longer reports a disabled reason -- About would render a live Check button that does nothing",
  );
  // The stub must still register handlers: the catch must appear BEFORE the
  // first ipcMain.handle("update:*") registration, not after.
  const catchAt = SRC.indexOf('disabled: "init-failed"');
  const firstHandler = SRC.indexOf('ipcMain.handle("update:get-info"');
  assert.ok(catchAt !== -1 && firstHandler !== -1 && catchAt < firstHandler,
    "handlers must register against the stub when init fails");
});
