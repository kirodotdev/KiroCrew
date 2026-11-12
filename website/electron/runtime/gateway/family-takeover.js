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
 * termination capability of its own. A macOS gateway that outlived the other
 * app is offered the supervisor's orphan stop instead, since quitting an app
 * that is gone frees nothing. The supervisor decides THAT the holder is the
 * other family (resolveGatewayConflict) and supplies the port waits and that
 * stop; this module owns the dialog flow that follows.
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

  async function resolveConflictByManualQuit(other, otherVersion, { holder = "" } = {}) {
    // A gateway with no app window (holder "service", run by a service manager)
    // is stopped, not quit. An orphan never reaches here: resolveFamilyConflict
    // offers it the orphan stop first. The prompt names
    // one thing, the gateway, and says where the command runs, in the wording of
    // the stuck-gateway dialog (holder-decision.js).
    const stopCommand = `kirocrew stop --port ${PORT}`;
    const step = holder ? `Stop the ${other.displayName} gateway` : `Quit ${other.displayName}`;
    const retry = holder ? "I stopped it — Retry" : "I quit it — Retry";
    const how = holder
      ? `\n\nRun this command in Terminal:\n${stopCommand}${holder === "service"
        ? "\n\nIf it starts again by itself, stop the service that restarts it."
        : ""}`
      : "";
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
        // A windowless gateway's retry round says so in its heading, so it does
        // not read as the same prompt again.
        message: holder && round > 1
          ? `${other.displayName} is still running.`
          : `${other.displayName} (${otherVersion}) is already running with your Kiro Crew data.`,
        detail: (round === 1
          ? `${step}, then choose “${retry}”.`
          : `${other.displayName} was still running a moment ago. ${step}, then choose “${retry}”.`) + how,
        buttons: [retry, "Cancel"],
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
      // Reached only when the retries ran out; Cancel returns above.
      detail: holder
        ? `${other.displayName} kept running, so this launch stopped. ${step}, then open this app again.${how}`
        : `This launch was cancelled. ${step}, then open this app again.${how}`,
      buttons: ["OK"],
    });
    return "abort";
  }

  // The other family's gateway outlived the app that started it (see
  // orphanedHolderPid in holder-decision.js), so quitting that app cannot free
  // the port. The prompt offers to stop the gateway itself; `stopOrphan`
  // proves the holder again, runs the stop and reports what the launch does.
  async function resolveOrphanedConflict(other, otherVersion, stopOrphan) {
    glog(`gateway on :${PORT} is ${other.appName}'s (${otherVersion}) but outlived its app, offering to stop it`);
    const { response } = await dialog.showMessageBox({
      type: "warning",
      title: `A ${other.displayName} gateway is running`,
      message: `A ${other.displayName} gateway (${otherVersion}) is still running with your Kiro Crew data.`,
      detail: `It kept running after the app that started it quit, so quitting ${other.displayName} would not stop it. Only one Kiro Crew app can use ~/.kiro/crew at a time.\n\nStop and continue stops that gateway with kirocrew stop --port ${PORT} and starts this app's gateway. Anything it was still doing is stopped with it.`,
      buttons: ["Stop and continue", "Cancel"],
      defaultId: 0,
      cancelId: 1,
    });
    if (response !== 0) return "abort";
    return stopOrphan();
  }

  /**
   * The other family owns the port: prompt for the takeover and report what
   * the launch should do next ("spawn", "abort" or "probe-failed", or for an
   * orphan whatever `stopOrphan` reports, {stopFailed} included).
   *
   * @param {{otherFamily: string, otherVersion: string, holder?: "service"}} decision
   *        decideGatewayAction's non-reuse verdict.
   * @param {{stopOrphan?: (() => Promise<any>)|null}} [options]  set when the
   *        holder is an orphan: the stop the orphan prompt runs.
   */
  async function resolveFamilyConflict(decision, { stopOrphan = null } = {}) {
    const other = FAMILY_META[decision.otherFamily];
    if (stopOrphan) return resolveOrphanedConflict(other, decision.otherVersion, stopOrphan);
    glog(`gateway on :${PORT} is owned by ${other.appName} (${decision.otherVersion}) — prompting for takeover`);
    if (decision.holder) {
      glog(`the holder is ${other.appName}'s gateway with no app window (${decision.holder}) — asking the user to stop it`);
      return resolveConflictByManualQuit(other, decision.otherVersion, { holder: decision.holder });
    }
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
