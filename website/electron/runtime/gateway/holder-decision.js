"use strict";

const { getRemoteHostConfig } = require("../../host-config");
const { decideGatewayAction } = require("../../instance-guard");
const {
  classifyAdoptedGateway,
  waitForServiceRebind,
  snapshotIncumbentForRespawn,
  recoverIncumbentWithBackoff,
  INCUMBENT_RETRY_STATUS,
} = require("../../gateway-recovery");

// How long a gateway that holds the port for this app's own data folder may
// go on failing its health check at launch before the user is offered to stop
// and restart it. Long enough that a gateway still importing on a slow disk
// answers first; short enough that a wedged one is not a blank splash.
const STUCK_GATEWAY_GRACE_MS = 15_000;
const STUCK_GATEWAY_POLL_MS = 1_000;
// How the stale dialog describes a launchd-parented gateway that is no launchd
// job: a plain local gateway, which nothing restarts (see staleDialogOwner).
const ORPHAN_DIALOG_OWNER = "kirocrew";

/**
 * What a launch does about a gateway already holding its port: adopt it, wait
 * out one that is draining, warn about an older one from this app's bundle,
 * offer to stop and restart one for this data folder that keeps failing its
 * health check, refuse a holder it cannot attribute, or hand a cross-family
 * holder to the takeover prompt.
 *
 * Each verdict it returns is the supervisor's to act on. The one lifecycle
 * slot it changes is the ownership it records for a gateway it adopts.
 *
 * @param {object} deps
 * @param {() => boolean} deps.getRunLocalGateway  whether this launch starts a
 *        local gateway at all, read at each decision.
 * @param {(ownership: string) => void} deps.setGatewayOwnership  records how
 *        an adopted gateway is owned (see classifyAdoptedGateway).
 */
function createHolderDecision({
  app,
  store,
  dialog,
  fs = null,
  path,
  processObj,
  port: PORT,
  isWindows: IS_WIN,
  log: glog,
  quitting,
  mainWindow,
  sendStatus,
  setTimeoutFn,
  getRunLocalGateway,
  setGatewayOwnership,
  checkBackend,
  probeGatewayPortBinding,
  probeGatewayPortOwner,
  waitForPortFree,
  snapshotGatewayPortPids,
  psCommand,
  unverifiedIncumbent,
  waitForIncumbentExit,
  sameHomeGatewayPid,
  runGatewayStop,
  probeLockHolderPid,
  fetchHealthInfo,
  fetchGatewayReadiness,
  resolveFamilyConflict,
  launchdJobPids = null,
}) {
  async function isCurrentBundleGateway() {
    if (!app.isPackaged || IS_WIN || !path.isAbsolute(processObj.resourcesPath || "")) return false;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return false;
    const command = (await psCommand(pids[0])).trim();
    const bundleRoot = path.join(processObj.resourcesPath, "backend-dist") + path.sep;
    return command.startsWith(bundleRoot);
  }

  /**
   * A managed install keeps each version in its own folder, so after an update
   * the previous version's detached gateway runs from a sibling folder and
   * isCurrentBundleGateway (this bundle only) does not see it. This recognizes
   * exactly that gateway: the one listener's command starts with this app's
   * own backend-dist path with the folder named for this app's version
   * replaced by the version the gateway itself reports.
   *
   * Only the stale warning reads this. The update-time stop keeps
   * isCurrentBundleGateway, so nothing is stopped because of it.
   */
  async function isPreviousManagedVersionGateway(gatewayVersion) {
    if (!app.isPackaged || IS_WIN || !path.isAbsolute(processObj.resourcesPath || "")) return false;
    const ownVersion = String(app.getVersion() || "");
    const oldVersion = String(gatewayVersion || "");
    const segmentOk = (value) => /^[0-9A-Za-z._+-]+$/.test(value) && value !== "." && value !== "..";
    if (!segmentOk(ownVersion) || !segmentOk(oldVersion) || ownVersion === oldVersion) return false;
    const resourcePaths = [processObj.resourcesPath];
    try {
      const realpath = fs?.realpathSync?.native || fs?.realpathSync;
      if (realpath) resourcePaths.push(realpath(processObj.resourcesPath));
    } catch { /* keep the path as given */ }
    const roots = [];
    for (const resources of new Set(resourcePaths)) {
      const parts = path.normalize(resources).split(path.sep);
      const index = parts.lastIndexOf(ownVersion);
      if (index < 1) continue;
      const sibling = [...parts.slice(0, index), oldVersion, ...parts.slice(index + 1)].join(path.sep);
      roots.push(path.join(sibling, "backend-dist") + path.sep);
    }
    if (!roots.length) return false;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return false;
    const command = (await psCommand(pids[0])).trim();
    return roots.some((root) => command.startsWith(root));
  }

  /**
   * The owner the stale dialog describes. A "service" owner on macOS is only a
   * PPID-1 gateway, and an orphan the previous app left behind has that parent
   * too. Only a gateway launchd runs as a job gets the service note; an
   * unreadable job list keeps it. This reads nothing else: the port-owner
   * classification, and every eviction and takeover decision built on it, stay
   * as they are.
   */
  async function staleDialogOwner(localOwner) {
    if (localOwner !== "service" || !launchdJobPids) return localOwner;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return localOwner;
    try {
      const jobPids = await launchdJobPids();
      if (jobPids instanceof Set && !jobPids.has(Number(pids[0]))) {
        glog(`stale gateway: pid ${pids[0]} on :${PORT} has launchd as its parent but is no launchd job — an orphan, not a service`);
        return ORPHAN_DIALOG_OWNER;
      }
    } catch { /* keep the service note */ }
    return localOwner;
  }

  /**
   * The one dialog about a local gateway this app found already holding its
   * port. `stale` is a same-family gateway older than this app; `stuck` is a
   * gateway for this app's own data folder that keeps failing its health check.
   * Both name the same lock-checked command, so the copy is built in one place.
   *
   * @returns {Promise<"continue"|"stop-restart"|"quit">}
   */
  async function askAboutExistingGateway(kind, { localOwner, oldVersion = "" }) {
    const stopCommand = `kirocrew stop --port ${PORT}`;
    const serviceNote = localOwner === "service"
      ? "\nIf the gateway starts again automatically, stop or update the service that restarts it."
      : "";
    if (kind === "stuck") {
      const { response } = await dialog.showMessageBox({
        type: "warning",
        message: "The gateway is not responding.",
        detail: `A Kiro Crew gateway for this data folder is running on port ${PORT} but has not answered for at least ${Math.round(STUCK_GATEWAY_GRACE_MS / 1000)} seconds.\n\nStop and restart stops that gateway and starts a fresh one. Anything the stuck gateway was still doing is stopped with it.${serviceNote ? "\nIf a background service restarts it automatically, Kiro Crew connects to it once it answers." : ""}`,
        buttons: ["Stop and restart", "Quit"],
        defaultId: 0,
        cancelId: 1,
      });
      return response === 0 ? "stop-restart" : "quit";
    }
    if (kind === "stuck-manual") {
      // No pidfd here to pin the stop to the checked process, so the app
      // does not stop it. It names the lock-checked command instead.
      await dialog.showMessageBox({
        type: "warning",
        message: "The gateway is not responding.",
        detail: `A Kiro Crew gateway for this data folder is running on port ${PORT} but has not answered for at least ${Math.round(STUCK_GATEWAY_GRACE_MS / 1000)} seconds.\n\nTo fix it, quit Kiro Crew.\nRun this command in Terminal:\n${stopCommand}${serviceNote}\nThen reopen Kiro Crew.`,
        buttons: ["Quit"],
        defaultId: 0,
        cancelId: 0,
      });
      return "quit";
    }
    const { response } = await dialog.showMessageBox({
      type: "warning",
      message: "The gateway is still running an older version.",
      detail: `This app is version ${app.getVersion()}. The gateway is still running version ${oldVersion}.\n\nContinue will try to connect to the existing gateway; updated features may be unavailable.\n\nTo finish the update, quit Kiro Crew.\nRun this command in Terminal:\n${stopCommand}${serviceNote}\nThen reopen Kiro Crew.`,
      buttons: ["Continue with existing gateway", "Quit"],
      defaultId: 0,
      cancelId: 0,
    });
    return response === 1 ? "quit" : "continue";
  }

  /**
   * Launch found the port held but no health answer. Today's answer is to
   * spawn anyway. That stays the answer for every holder except one: a local
   * Kiro Crew gateway for this app's own data folder that keeps failing its
   * health check past the grace window. That one is offered a stop-and-restart.
   *
   * @returns {Promise<"spawn"|"healthy"|"abort"|{stopFailed:number[]}>}
   */
  async function resolveUnresponsiveHolder() {
    if (!getRunLocalGateway() || getRemoteHostConfig(store, PORT)?.host) return "spawn";
    if ((await probeGatewayPortBinding(PORT)) !== "bound") return "spawn";
    const localOwner = await probeGatewayPortOwner(PORT);
    if (localOwner !== "kirocrew" && localOwner !== "service") return "spawn";
    const provenPid = await sameHomeGatewayPid();
    if (!provenPid) return "spawn";
    sendStatus("Waiting for the existing gateway to respond…");
    // Counted in polls rather than read off the clock, so the injected timer
    // fully decides how long this waits.
    for (let waited = 0; waited < STUCK_GATEWAY_GRACE_MS; waited += STUCK_GATEWAY_POLL_MS) {
      await new Promise((resolve) => setTimeoutFn(resolve, STUCK_GATEWAY_POLL_MS));
      try { await checkBackend(); return "healthy"; }
      catch { /* still not answering */ }
    }
    glog(`stuck gateway: :${PORT} is held by this data folder's gateway (owner=${localOwner}) and failed its health check for ${STUCK_GATEWAY_GRACE_MS}ms — offering stop and restart`);
    // `kirocrew stop --expect-pid` pins its signal with a pidfd, which only
    // Linux has. Elsewhere the app names the command and stops nothing.
    if (processObj.platform !== "linux") {
      await askAboutExistingGateway("stuck-manual", { localOwner });
      return "abort";
    }
    const choice = await askAboutExistingGateway("stuck", { localOwner });
    if (choice !== "stop-restart") return "abort";
    // The dialog can stay open for minutes. Prove the holder again right
    // before the stop, and require the same pid the grace window judged: a
    // replacement (even one for this home, restarted by a service) has never
    // been shown to be stuck, so it goes through the ordinary decision.
    // A port that emptied meanwhile (the stuck gateway exited on its own) is
    // free to spawn into; any other holder goes through the ordinary decision.
    const portNowFree = async () => (await probeGatewayPortBinding(PORT)) === "free";
    if ((await sameHomeGatewayPid()) !== provenPid) {
      if (await portNowFree()) {
        glog(`stuck gateway: :${PORT} emptied while the dialog was open — starting the bundled backend`);
        return "spawn";
      }
      glog(`stuck gateway: :${PORT} changed holder while the dialog was open — re-validating instead of stopping`);
      return "healthy";
    }
    sendStatus("Stopping the previous gateway…");
    const incumbentPids = await snapshotGatewayPortPids(PORT);
    if (!(await runGatewayStop(provenPid))) {
      // The CLI refuses when the proven pid no longer holds the port. That is
      // a failed stop only if the proven pid is still there.
      if (await portNowFree()) {
        glog(`stuck gateway: :${PORT} emptied before the stop — starting the bundled backend`);
        return "spawn";
      }
      const holders = await snapshotGatewayPortPids(PORT);
      if (holders?.length && !holders.includes(provenPid)) {
        glog(`stuck gateway: :${PORT} was taken by pid ${holders.join(", ")} before the stop — re-validating the new holder`);
        return "healthy";
      }
      return { stopFailed: incumbentPids || [] };
    }
    // A fast service manager can rebind before the port is ever seen free.
    // A different listener pid is that new gateway, not a failed stop.
    const takenByNewPid = async () => {
      if (localOwner !== "service") return false;
      const holders = await snapshotGatewayPortPids(PORT);
      if (!holders?.length || holders.some((pid) => (incumbentPids || []).includes(pid))) return false;
      glog(`stuck gateway: :${PORT} was taken by a new pid ${holders.join(", ")} after the stop — re-validating the new holder`);
      return true;
    };
    if (await takenByNewPid()) return "healthy";
    if (!(await waitForPortFree())) {
      if (await takenByNewPid()) return "healthy";
      return { stopFailed: incumbentPids || [] };
    }
    if (localOwner === "service") {
      // A service manager may bring the gateway straight back. Give it the
      // same bounded rebind grace the drain path does, then re-validate the
      // new holder through the ordinary decision instead of spawning over it.
      sendStatus("Waiting for the gateway to restart…");
      const verdict = await waitForServiceRebind({
        isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
        sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
      });
      if (verdict === "rebound") {
        glog(`stuck gateway: :${PORT} re-bound after the stop — re-validating the new holder`);
        return "healthy";
      }
    }
    await waitForIncumbentExit(incumbentPids, "stuck gateway");
    glog(`stuck gateway: :${PORT} released — starting the bundled backend`);
    return "spawn";
  }

  async function resolveGatewayConflict(rebindDepth = 0) {
    const health = await fetchHealthInfo();
    // A remote host configured for this port makes the holder a tunnel by
    // construction. Nothing local may evict it.
    const remoteHost = getRemoteHostConfig(store, PORT)?.host || "";
    if (remoteHost) {
      glog(`:${PORT} is a configured remote host (${remoteHost}) — holder treated as non-local`);
    }
    const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(PORT);
    const bundledGateway = getRunLocalGateway()
      && health?.version !== app.getVersion()
      && (localOwner === "kirocrew" || localOwner === "service")
      && (await isCurrentBundleGateway() || await isPreviousManagedVersionGateway(health?.version));
    const decision = decideGatewayAction(app.getVersion(), health, {
      localOwner, bundledGateway,
    });
    // The rule this enforces, as one sentence: adopt a responder on this port
    // only when the port can be attributed either to us (a Kiro Crew LISTEN
    // owner, or a service-managed one) or to a crew the user configured here.
    //
    // It is deliberately NOT gated on which reuse reason decideGatewayAction
    // returned. That function has five paths to `reuse` -- an unclassified shell,
    // an unidentified payload, a same-family gateway, a dev-family one, and a
    // cross-family one whose LISTEN owner is not ours -- and a gateway reached
    // through `ssh -L` answers with a perfectly ordinary same-family payload, so
    // a gate on one reason value misses four. `mintLocalToken` then posts this
    // machine's `.local_secret` to it, because the mint only requires a literal
    // loopback origin and a tunnel's local end is one. The header goes out before
    // any 403 is seen, so there is no recovery after the fact.
    //
    // A `foreign` owner with no `remoteHosts` entry is exactly "cannot attribute
    // it". `kirocrew` and `service` still adopt, and so do `none` and `unknown`:
    // the fail-open this narrows is preserved wherever the probe did not
    // positively find someone else holding the port.
    //
    // `warn-stale` cannot reach here with a `foreign` owner: the bundled-path
    // probe above only runs for a `kirocrew` or `service` owner, so that action
    // is unreachable without positive local attribution.
    if (decision.action === "reuse" && localOwner === "foreign" && !remoteHost) {
      glog(`:${PORT} is served by a process this app did not start and no remote crew is configured there — refusing to adopt it`);
      return "foreign-holder";
    }
    if (decision.action === "warn-stale") {
      glog(`bundled gateway ${decision.oldVersion} predates app ${app.getVersion()} — warning before reuse`);
      const choice = await askAboutExistingGateway("stale", {
        localOwner: await staleDialogOwner(localOwner), oldVersion: decision.oldVersion,
      });
      if (choice === "quit") return "abort";
    }
    if (decision.action === "reuse" || decision.action === "warn-stale") {
      // Adopt-or-wait. Only a positive shutting-down verdict refuses adoption;
      // every ambiguity preserves historical fail-open reuse. Remote tunnels are
      // exempt because their local socket is not expected to clear on restart.
      let adoptedDraining = false;
      const readiness = remoteHost ? "unknown" : await fetchGatewayReadiness();
      if (readiness === "shutting-down") {
        glog(`gateway on :${PORT} answers but /api/ready reports shutting-down — refusing to adopt a draining gateway`);
        sendStatus("Waiting for the previous gateway to exit…");
        // Same drain race as the liveness path: a socket lookup can name no PID
        // while the gateway sheds its socket, and a gateway that has released
        // its socket but still holds the lock is never named by a socket probe
        // at all. Take one lookup, then fall back to the CLI's socket-
        // independent lock-holder probe instead of refusing on the miss.
        // An incumbent that stays unnameable is retried on the same capped
        // backoff as the liveness path, never spawned over blind; a probe that
        // refuses ends the launch on the probe-failed error instead.
        const drainCapture = await recoverIncumbentWithBackoff({
          capture: () => snapshotIncumbentForRespawn({
            snapshot: () => snapshotGatewayPortPids(PORT),
            blocksRespawn: (pids) => unverifiedIncumbent(pids),
            sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
            lockHolderProbe: () => probeLockHolderPid(),
            log: (message) => glog(`drain: ${message}`),
          }),
          sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
          cancelled: () => quitting() || Boolean(mainWindow()?.isDestroyed()),
          onWaiting: () => sendStatus(INCUMBENT_RETRY_STATUS),
          log: (message) => glog(`drain: ${message}`),
        });
        if (drainCapture.verdict === "cancelled") return "abort";
        if (drainCapture.verdict === "refused") {
          glog(`drain: could not verify the incumbent on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
          return "probe-failed";
        }
        sendStatus("Waiting for the previous gateway to exit…");
        // "released" = the lock is released (incumbent gone), nothing to outwait;
        // "captured" names the pid to wait on before spawning.
        const drainingPids = drainCapture.verdict === "released" ? null : drainCapture.pids;
        if (await waitForPortFree()) {
          if (localOwner === "service") {
            // A service may be between release and manager rebind. Orphans also
            // classify as service, so wait a bounded grace and then spawn.
            sendStatus("Waiting for the gateway to restart…");
            const verdict = await waitForServiceRebind({
              isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
              sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
            });
            if (verdict === "rebound") {
              // Revalidate the replacement through the full identity/readiness
              // decision. One recursive pass prevents an unbounded drain loop.
              if (rebindDepth < 1) {
                glog(`service rebind: :${PORT} re-bound within the grace window — re-validating the new holder`);
                return resolveGatewayConflict(rebindDepth + 1);
              }
              glog(`service rebind: :${PORT} re-bound again at depth ${rebindDepth} — treating as adopt-anyway to avoid a validation loop`);
            } else {
              glog(`service rebind: :${PORT} stayed free past the grace window (no manager respawned it) — spawning fresh`);
            }
          }
          if (localOwner !== "service" || (await probeGatewayPortBinding(PORT)) === "free") {
            await waitForIncumbentExit(drainingPids, "drain");
            glog(`drain complete: :${PORT} released — spawning a fresh gateway`);
            return "spawn";
          }
        }
        // The holder is not ours to kill. Adopt loudly and let bounded recovery
        // respawn after its eventual death instead of spawning into EADDRINUSE.
        glog(`drain wait timed out — :${PORT} still held; adopting anyway (recovery will respawn if it dies)`);
        adoptedDraining = true;
      }
      glog(`reusing existing gateway on :${PORT} (${decision.reason}) — bundled backend NOT spawned`);
      setGatewayOwnership(classifyAdoptedGateway({ reason: decision.reason, localOwner }));
      sendStatus(adoptedDraining
        ? "Connecting to the existing gateway…"
        : "Gateway already running ✓");
      return "reuse";
    }

    return resolveFamilyConflict(decision);
  }

  return {
    isCurrentBundleGateway,
    resolveUnresponsiveHolder,
    resolveGatewayConflict,
  };
}

module.exports = { createHolderDecision };
