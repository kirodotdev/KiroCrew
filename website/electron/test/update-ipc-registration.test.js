// The update IPC handlers must be registered BEFORE the awaited gateway boot.
//
// WHY THIS IS A TEST: preload.js exposes window.updateAPI unconditionally, so
// Settings > About renders a live "Check for updates" button as soon as the
// renderer loads. If composition awaits the gateway before registering these
// handlers, a slow or failed boot leaves that button with no handler and invoke
// rejects with "No handler registered for 'update:check'".
//
// That is not hypothetical: it is how the nightly OTA lane once failed before
// it could assert a bundle swap. Extraction split the invariant across owners:
// ipc-registrar.js owns every update:* handler, while main.js must compose that
// owner before either awaited gateway boot step. Both halves stay pinned here.
"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const { readFileSync } = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const MAIN_SOURCE = readFileSync(path.join(ROOT, "main.js"), "utf8");
const REGISTRAR_SOURCE = readFileSync(path.join(ROOT, "ipc-registrar.js"), "utf8");
const GATEWAY_SOURCE = readFileSync(path.join(ROOT, "gateway-supervisor.js"), "utf8");

const BOOT_AWAITS = ["await gateway.start();", "await gateway.connect(mainWindow);"];
const UPDATE_HANDLERS = [
  'ipcMain.handle("update:get-info"',
  'ipcMain.handle("update:check"',
  'ipcMain.handle("update:download"',
  'ipcMain.handle("update:install"',
  'ipcMain.handle("update:set-channel"',
  'ipcMain.handle("update:set-auto-download"',
];

test("every update:* IPC handler registers before the awaited gateway boot", () => {
  // Non-vacuity and ownership: every preload-exposed bridge must be registered
  // synchronously inside registerUpdater, before that method marks itself done.
  // A stray registration elsewhere in the file must not satisfy this guard.
  const ownerStart = REGISTRAR_SOURCE.indexOf("function registerUpdater() {");
  const ownerDone = REGISTRAR_SOURCE.indexOf("updaterRegistered = true;", ownerStart);
  assert.notStrictEqual(ownerStart, -1, "registerUpdater owner vanished");
  assert.notStrictEqual(ownerDone, -1, "registerUpdater no longer completes registration");
  for (const handler of UPDATE_HANDLERS) {
    const handlerIndex = REGISTRAR_SOURCE.indexOf(handler, ownerStart);
    assert.ok(
      handlerIndex > ownerStart && handlerIndex < ownerDone,
      `missing synchronous registration from registerUpdater: ${handler}`,
    );
  }

  // Ownership alone is insufficient: main must invoke it before both awaits.
  // Anchor all three calls inside the ready callback rather than allowing an
  // unrelated mention elsewhere in the file to satisfy the order assertion.
  const readyIndex = MAIN_SOURCE.indexOf("app.whenReady().then(async () => {");
  assert.notStrictEqual(readyIndex, -1, "app ready composition vanished — re-derive this test");
  const registerIndex = MAIN_SOURCE.indexOf("ipcRegistrar.registerUpdater();", readyIndex);
  assert.notStrictEqual(registerIndex, -1, "main.js no longer registers updater IPC");
  for (const awaitedBoot of BOOT_AWAITS) {
    const bootIndex = MAIN_SOURCE.indexOf(awaitedBoot, readyIndex);
    assert.notStrictEqual(
      bootIndex,
      -1,
      `boot step vanished from main.js: ${awaitedBoot} — re-derive this test`,
    );
    assert.ok(
      registerIndex < bootIndex,
      `ipcRegistrar.registerUpdater() is AFTER ${awaitedBoot}. preload exposes `
        + "the button regardless, so a stalled boot leaves it with no handler.",
    );
  }
});

// The product default remains in main.js while updater wiring belongs to the
// registrar. Losing either silently reverts installs to notify-only because
// auto-update's own unwired default is deliberately false.
test("ipc-registrar wires the auto-download preference into initAutoUpdate", () => {
  assert.match(
    REGISTRAR_SOURCE,
    /getAutoDownloadPreference:\s*\(\)\s*=>\s*\n?\s*store\.get\("autoDownloadUpdates",\s*true\)\s*!==\s*false/,
    "ipc-registrar.js no longer hands initAutoUpdate the auto-download preference. The module defaults it to FALSE, so without this line every desktop install silently drops to notify-only.",
  );
});

test("auto-download is ON by default in the store defaults", () => {
  assert.match(
    MAIN_SOURCE,
    /autoDownloadUpdates:\s*true/,
    "autoDownloadUpdates is no longer defaulted true in the electron-store defaults — desktop auto-update is off by default again.",
  );
});

test("initAutoUpdate is owned by the registrar composed before gateway boot", () => {
  assert.notStrictEqual(
    REGISTRAR_SOURCE.indexOf("updater = initAutoUpdate({"),
    -1,
    "ipc-registrar.js no longer initializes auto-update",
  );
  const registerIndex = MAIN_SOURCE.indexOf("ipcRegistrar.registerUpdater();");
  assert.notStrictEqual(registerIndex, -1);
  for (const awaitedBoot of BOOT_AWAITS) {
    const bootIndex = MAIN_SOURCE.indexOf(awaitedBoot);
    assert.notStrictEqual(bootIndex, -1);
    assert.ok(registerIndex < bootIndex, `registerUpdater moved below ${awaitedBoot}`);
  }
});

test("the updater block does not depend on gateway boot completing", () => {
  // The stop must remain a LAZY callback. Eagerly stopping here would make
  // updater registration depend on gateway state and violate boot ordering.
  assert.match(
    REGISTRAR_SOURCE,
    /stopGateway:\s*\(\)\s*=>\s*gateway\.stopGracefully\(\)/,
    "stopGateway is no longer passed as a lazy callback — updater registration may now depend on gateway state",
  );
});

test("liveness recovery stands down during an update install", () => {
  // The gateway supervisor now owns both the watchdog and install flag. The
  // guard must treat an in-flight install exactly like quit, or it can respawn
  // the gateway in the middle of a bundle swap.
  assert.match(
    GATEWAY_SOURCE,
    /if \(quitting\(\) \|\| installingUpdate\) return;/,
    "the liveness onUnresponsive guard no longer checks installingUpdate -- recovery can resurrect the gateway during a bundle swap",
  );
  assert.match(
    GATEWAY_SOURCE,
    /function onInstallDispatched\(\)[\s\S]{0,200}?installingUpdate = true/,
    "onInstallDispatched no longer sets installingUpdate",
  );
  assert.match(
    REGISTRAR_SOURCE,
    /onInstallDispatched:\s*\(\)\s*=>\s*\{[\s\S]{0,200}?gateway\.onInstallDispatched\(\)/,
    "ipc-registrar.js no longer connects updater dispatch to the gateway liveness owner",
  );
  assert.match(
    REGISTRAR_SOURCE,
    /onInstallDispatched:\s*\(\)\s*=>\s*\{[\s\S]{0,200}?closeCrewCompanionForUpdate\(\)/,
    "ipc-registrar.js no longer closes the Crew Companion overlay at update dispatch -- it floats orphaned over the vanished dashboard during the quit handoff",
  );
});

test("a failed install re-arms gateway recovery", () => {
  // Dispatch disarms the watchdog, so a failed install must clear the flag and
  // actively recover; a failed swap does not quit and nothing else will do it.
  assert.match(
    GATEWAY_SOURCE,
    /function onInstallFailed\([^)]*\)[\s\S]{0,400}?installingUpdate = false/,
    "onInstallFailed no longer clears installingUpdate",
  );
  assert.match(
    GATEWAY_SOURCE,
    /function onInstallFailed\([^)]*\)[\s\S]{0,600}?recoverWedgedGateway\(/,
    "onInstallFailed no longer actively restores the gateway -- nothing else will after dispatch",
  );
  assert.match(
    REGISTRAR_SOURCE,
    /onInstallFailed:\s*\(\)\s*=>\s*\{[\s\S]{0,200}?gateway\.onInstallFailed\(\)/,
    "ipc-registrar.js no longer connects install failure to gateway recovery",
  );
  assert.match(
    REGISTRAR_SOURCE,
    /onInstallFailed:\s*\(\)\s*=>\s*\{[\s\S]{0,200}?reopenCrewCompanionAfterUpdate\(\)/,
    "ipc-registrar.js no longer reopens the Crew Companion overlay when a failed install leaves the app running",
  );
});

test("updater init failure cannot gate gateway startup (fail-open)", () => {
  // Registration precedes boot, so require/feed/init failures must become a
  // disabled updater rather than rejecting the entire ready callback.
  assert.match(
    REGISTRAR_SOURCE,
    /function registerUpdater\(\)[\s\S]{0,400}?try \{\s*\n\s*updater = initAutoUpdate\(/,
    "initAutoUpdate is no longer wrapped in try/catch -- an init throw now aborts app boot",
  );
  assert.match(
    REGISTRAR_SOURCE,
    /disabled: "init-failed"/,
    "the fail-open stub no longer reports a disabled reason -- About would render a live Check button that does nothing",
  );
  // The catch/stub must precede handler registration so handlers still bind
  // against the disabled handle after initialization fails.
  const stubIndex = REGISTRAR_SOURCE.indexOf('disabled: "init-failed"');
  const firstHandlerIndex = REGISTRAR_SOURCE.indexOf('ipcMain.handle("update:get-info"');
  assert.ok(
    stubIndex !== -1 && firstHandlerIndex !== -1 && stubIndex < firstHandlerIndex,
    "handlers must register against the stub when init fails",
  );
});
