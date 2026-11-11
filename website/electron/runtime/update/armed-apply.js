"use strict";

/**
 * Apply an armed update in Electron main, at the one point the desktop owns it.
 *
 * Two callers, one rule: the apply runs only with no gateway of this app
 * running, and the app is never relaunched by a quit-time apply.
 *   - The user's quit (managed-lane.js), after the gateway has fully stopped.
 *     The app then EXITS; the next start is the user's.
 *   - The next app start (applyArmedAtStart), for an apply the quit left behind
 *     (interrupted or failed). It runs before the gateway spawns and before any
 *     window opens, then relaunches ONCE into the new bundle. If it fails the
 *     arm is cleared, the old version starts and the error is shown. The
 *     gateway the desktop hosts skips its own boot-time apply, so exactly one
 *     side applies and the two versions cannot split.
 */

/** A relaunch that brings back the NEW bundle. See createManagedLane for why a bare relaunch is not enough. */
function createRelaunch({ managed, app, log, existsSync = (p) => require("fs").existsSync(p), argv = process.argv }) {
  return function relaunch() {
    const target = (managed && managed.relaunchPath) || "";
    if (target) {
      let present = false;
      try { present = existsSync(target); } catch { present = false; }
      if (present) {
        log.info(`[update] managed relaunch through the marker's relaunchPath ${target}`);
        app.relaunch({ execPath: target, args: argv.slice(1) });
        return;
      }
      log.error(`[update] marker relaunchPath ${target} is not on disk - relaunching the path this app was started as`);
    }
    app.relaunch();
  };
}

/**
 * Run the update command for an armed record and say how it went.
 *
 * @returns {Promise<{ok: boolean, message: string}>}
 */
async function runArmedApply({ exec, version, log }) {
  const { code, failed } = await exec.runUpdateCommand();
  if (code !== 0 || failed) {
    return { ok: false, message: `managed update command exited ${code}` };
  }
  if (await exec.applyChangedNothing(version)) {
    return { ok: false, message: exec.unchangedMessage(version) };
  }
  log.info(`[update] armed update ${version} applied`);
  return { ok: true, message: "" };
}

/**
 * The start-time half (D-c).
 *
 * @returns {Promise<{outcome: "none"|"already-installed"|"relaunching"|"failed", error?: string}>}
 */
async function applyArmedAtStart({ arm, exec, currentVersion, relaunch, app, log }) {
  const rec = arm.read();
  if (!rec) return { outcome: "none" };
  if (rec.version === currentVersion) {
    log.info(`[update] armed update ${rec.version} is already the running version - clearing the arm`);
    arm.clear();
    return { outcome: "already-installed" };
  }
  log.info(`[update] armed update ${rec.version} was not applied at the last quit - applying it before the gateway starts`);
  arm.markAttempt();
  const result = await runArmedApply({ exec, version: rec.version, log });
  // Cleared either way: a success is done, and a failure must not retry on every
  // launch behind the user. The failure is shown instead.
  arm.clear();
  if (result.ok) {
    relaunch();
    app.exit(0);
    return { outcome: "relaunching" };
  }
  log.error(`[update] armed update ${rec.version} failed at start (${result.message}) - starting the version that is installed`);
  arm.recordApplyError(rec.version, result.message);
  return { outcome: "failed", error: result.message };
}

module.exports = { createRelaunch, runArmedApply, applyArmedAtStart };
