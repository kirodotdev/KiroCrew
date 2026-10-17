"use strict";

const { FAMILY_META } = require("../../instance-guard");

/**
 * Hand the data home over from the other release family's app (stable versus
 * nightly) when its gateway already holds this launch's port. Both apps share
 * `~/.kiro/crew`, so only one may run a gateway at a time; this is the prompt
 * that asks the user to let this launch take over.
 *
 * macOS can quit the other app for the user through AppleScript. Everywhere
 * else the prompt asks the user to quit it and retries, and never adds a
 * termination capability of its own. The supervisor decides THAT the holder is
 * the other family (resolveGatewayConflict) and supplies the port waits; this
 * module owns the dialog flow that follows.
 */
function createFamilyTakeover({
  dialog,
  execFile,
  processObj,
  port: PORT,
  log: glog,
  sendStatus,
  snapshotGatewayPortPids,
  waitForPortFree,
  waitForIncumbentExit,
}) {
  // Ask the other channel app to quit through its normal lifecycle. Both app
  // flavors share a bundle identifier, so AppleScript must target app NAME.
  function quitOtherApp(appName) {
    return new Promise((resolve) => {
      if (processObj.platform !== "darwin") { resolve(false); return; }
      execFile(
        "osascript",
        ["-e", `quit app "${appName}"`],
        { timeout: 10000 },
        (err) => resolve(!err),
      );
    });
  }

  // Only macOS can quit the other family's app for the user (quitOtherApp is
  // AppleScript-only), so everywhere else this conflict was a dead end: an
  // aborted launch, then a second launch after a manual quit. Offer that quit as
  // a resumable step instead. It adds NO termination capability: the probes only
  // observe, and the prompt is reachable only after a LOCAL owner is known.
  const MANUAL_QUIT_ROUNDS = 3;

  async function resolveConflictByManualQuit(other, otherVersion) {
    // Port free is not lock free: an uncapturable listener must refuse, not read
    // as "already exited" and race gateway.lock. Stricter than unverifiedIncumbent
    // (Windows-only): reaching this prompt proved the probe names PIDs here.
    const incumbentPids = await snapshotGatewayPortPids(PORT);
    if (incumbentPids === null) {
      glog(`takeover (manual): could not capture the incumbent PID on :${PORT} — refusing a respawn that could race gateway.lock`);
      return "probe-failed";
    }
    for (let round = 1; round <= MANUAL_QUIT_ROUNDS; round += 1) {
      const { response } = await dialog.showMessageBox({
        type: "warning",
        title: `${other.displayName} is running`,
        message: `${other.displayName} (${otherVersion}) is already running with your Kiro Crew data.`,
        detail: round === 1
          ? `Quit ${other.displayName}, then choose “I quit it — Retry”.`
          : `${other.displayName} was still running a moment ago. Quit it, then choose “I quit it — Retry”.`,
        buttons: ["I quit it — Retry", "Cancel"],
        defaultId: 0,
        cancelId: 1,
      });
      if (response !== 0) return "abort";
      sendStatus(`Waiting for ${other.displayName} to quit…`);
      if (await waitForPortFree()) {
        glog(`takeover (manual): ${other.appName} released :${PORT} — proceeding to spawn`);
        await waitForIncumbentExit(incumbentPids, "takeover (manual)");
        return "spawn";
      }
      glog(`takeover (manual): ${other.appName} still holds :${PORT} after retry ${round}/${MANUAL_QUIT_ROUNDS}`);
    }
    glog(`takeover (manual): ${other.appName} never released :${PORT} — aborting this launch`);
    await dialog.showMessageBox({
      type: "error",
      message: `${other.displayName} is still running.`,
      detail: `This launch was cancelled. Quit ${other.displayName}, then open this app again.`,
      buttons: ["OK"],
    });
    return "abort";
  }

  /**
   * The other family owns the port: prompt for the takeover and report what
   * the launch should do next ("spawn", "abort" or "probe-failed").
   *
   * @param {{otherFamily: string, otherVersion: string}} decision
   *        decideGatewayAction's non-reuse verdict.
   */
  async function resolveFamilyConflict(decision) {
    const other = FAMILY_META[decision.otherFamily];
    glog(`gateway on :${PORT} is owned by ${other.appName} (${decision.otherVersion}) — prompting for takeover`);
    const canTakeover = processObj.platform === "darwin";
    if (!canTakeover) {
      glog(`canTakeover=false on ${processObj.platform} — no supported way to quit ${other.appName} from here; offering a manual-quit retry`);
      return resolveConflictByManualQuit(other, decision.otherVersion);
    }
    const { response } = await dialog.showMessageBox({
      type: "warning",
      title: `${other.displayName} is running`,
      message: `${other.displayName} (${decision.otherVersion}) is already running with your Kiro Crew data.`,
      detail: `Only one Kiro Crew app can use ~/.kiro/crew at a time. Quit ${other.displayName} and continue here?`,
      buttons: [`Quit ${other.displayName} & Continue`, "Cancel"],
      defaultId: 0,
      cancelId: 1,
    });
    if (response !== 0) return "abort";
    sendStatus(`Waiting for ${other.displayName} to quit…`);
    await quitOtherApp(other.appName);
    if (!(await waitForPortFree())) {
      glog(`takeover failed: ${other.appName} did not release :${PORT}`);
      await dialog.showMessageBox({
        type: "error",
        message: `${other.displayName} did not quit.`,
        detail: "Quit it manually, then relaunch this app.",
        buttons: ["OK"],
      });
      return "abort";
    }
    glog(`takeover: ${other.appName} released :${PORT} — proceeding to spawn`);
    return "spawn";
  }

  return { resolveFamilyConflict };
}

module.exports = { createFamilyTakeover };
