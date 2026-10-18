"use strict";

const {
  stopGatewayGracefully: stopGatewayProcessGracefully,
  postShutdown,
  gatewayStillRunningError,
} = require("../../gateway-stop");
const { windowsProcessCommand, windowsTaskkill } = require("../../windows-port");
const { waitForServiceRebind, waitForProcessExit } = require("../../gateway-recovery");
const { secretCandidates } = require("../../home-dir");

// How long the pre-update stop waits for an adopted gateway to release the port
// once it accepted the stop. The backend's own shutdown finishes inside an 18s
// hard deadline, so this covers a full graceful stop with margin.
const ADOPTED_UPDATE_STOP_TIMEOUT_MS = 30_000;

/**
 * Stopping the gateway: the graceful stop of the child this app spawned (on
 * quit and before an update), the pre-update stop of an adopted gateway that
 * runs from this app's own bundle, and the two tree kills -- the Windows one
 * the graceful stop uses, with timeouts tightened to fit the shutdown
 * deadline, and the wedge kill (a descendant sweep on POSIX, windowsTaskkill's
 * own defaults on Windows).
 *
 * Which occasion gets which stop is the supervisor's call. The graceful stop
 * clears the child slots once the child is gone.
 *
 * @param {object} deps
 * @param {() => object|null} deps.getGatewayProcess  the child this app spawned.
 * @param {(child: object|null) => void} deps.setGatewayProcess  clears that
 *        slot once the child is stopped.
 * @param {(paths: string[]) => void} deps.setSpawnedExecutablePaths  clears the
 *        executables that child was spawned from, together with it.
 * @param {() => boolean} deps.getRunLocalGateway  whether this launch runs a
 *        local gateway at all.
 * @param {() => string} deps.getGatewayOwnership  how the gateway on the port is
 *        owned; only an adopted one is stopped before an update.
 */
function createShutdown({
  fs,
  path,
  http,
  processObj,
  port: PORT,
  backendUrl: BACKEND_URL,
  isWindows: IS_WIN,
  log: glog,
  setTimeoutFn,
  getGatewayProcess,
  setGatewayProcess,
  setSpawnedExecutablePaths,
  getRunLocalGateway,
  getGatewayOwnership,
  probeGatewayPortBinding,
  waitForPortFree,
  isCurrentBundleGateway,
  snapshotGatewayPortPids,
  sameHomeGatewayPid,
  runGatewayStop,
  posixDescendantPids,
  psCommand,
  isTrustedWindowsGatewayCommand,
}) {
  // The command a refused update names for stopping the adopted gateway by
  // hand. A service-managed gateway is stopped through its service (the CLI's
  // `stop` without --port stops the service first), because a signal to the
  // process alone lets the service manager start it again. Any other adopted
  // gateway gets the same lock-checked command the stale/stuck dialogs name.
  function adoptedGatewayStopCommand() {
    return getGatewayOwnership() === "reused-service" ? "kirocrew stop" : `kirocrew stop --port ${PORT}`;
  }

  /**
   * POST /api/shutdown, then POSIX SIGTERM -> SIGKILL or the bounded Windows
   * tree kill, for the child this app spawned. Quit runs it, and the pre-update
   * stop (stopGatewayForUpdate) awaits it before swapping bundle bytes.
   * Ownership intentionally remains "spawned" after stop: if an update install
   * fails, the recovery hook must know it is allowed to respawn the child that
   * the updater deliberately stopped.
   */
  async function stopGatewayGracefully({ timeoutMs = 15000 } = {}) {
    const gateway = getGatewayProcess();
    if (!gateway || gateway.exitCode !== null) {
      setGatewayProcess(null);
      setSpawnedExecutablePaths([]);
      return;
    }
    glog("Stopping gateway gracefully...");
    // Resolve the home at call time: the gateway publishes its listener entries
    // there. postShutdown sends only the entry for the listener BACKEND_URL
    // dials, never the home-wide `.local_secret`.
    const currentHome = path.dirname(secretCandidates()[0]);
    await stopGatewayProcessGracefully(gateway, {
      backendUrl: BACKEND_URL,
      kirocrewHome: currentHome,
      timeoutMs,
      // 3s PowerShell + 2s WMIC + 5s taskkill fits inside the 18s hard
      // shutdown deadline. The tree sweep is awaited because taskkill can emit
      // the parent's exit while descendants are still being reaped.
      killTreeFn: killGatewayTreeOnWindowsBounded,
      // POSIX: the child may be a launcher shim that forked the gateway. These
      // let the stop find and stop that gateway once the shim is gone.
      listDescendantsFn: posixDescendantPids,
      getCommandFn: psCommand,
      signalPidFn: (pid, signal) => processObj.kill(pid, signal),
    });
    setGatewayProcess(null);
    setSpawnedExecutablePaths([]);
  }

  /**
   * The stop both update lanes await before they replace the app's files.
   *
   * The child this app spawned is stopped exactly as on quit. A gateway the app
   * ADOPTED (found already serving and reused) is stopped too when it runs from
   * this app's own backend-dist: it loads modules lazily from those files, so a
   * swap under it leaves the old process importing new code into old in-memory
   * modules, and every chat call fails until it is restarted. Any other holder
   * (a tunnel, a separate CLI or service install) runs from files the update
   * does not touch and is left alone.
   *
   * The adopted gateway is asked to stop through POST /api/shutdown with this
   * home's secret, so it flushes and exits itself. When it does not accept that,
   * Linux falls back to the CLI's pid-pinned stop, exactly as the stuck-gateway
   * "Stop and restart" does, and only for the pid proven to hold this home's
   * gateway.lock. Elsewhere there is no pidfd to pin a signal to the checked
   * process, so nothing is signalled.
   *
   * Rejects with GATEWAY_STILL_RUNNING (gateway-stop.js) when a gateway from
   * this bundle is still running afterwards: still holding the port, released
   * it but not exited, or started again by a service manager. The lane then
   * refuses the swap instead of running it.
   */
  async function stopGatewayForUpdate({
    timeoutMs = 15000,
    adoptedTimeoutMs = ADOPTED_UPDATE_STOP_TIMEOUT_MS,
  } = {}) {
    await stopGatewayGracefully({ timeoutMs });
    if (!getRunLocalGateway()) return;
    if (getGatewayOwnership() !== "reused-local" && getGatewayOwnership() !== "reused-service") return;
    if (!(await isCurrentBundleGateway())) return;
    const incumbentPids = await snapshotGatewayPortPids(PORT);
    const pid = incumbentPids?.length === 1 ? incumbentPids[0] : 0;
    glog(`update: the adopted gateway on :${PORT} (pid ${pid || "?"}) runs from this app's bundle — stopping it before the update`);
    // postShutdown sends only the entry this gateway published for the
    // listener BACKEND_URL dials, never the home-wide `.local_secret`.
    let accepted = await postShutdown({
      backendUrl: BACKEND_URL,
      kirocrewHome: path.dirname(secretCandidates()[0]),
      httpMod: http,
      fsMod: fs,
      pathMod: path,
    });
    if (!accepted && pid && processObj.platform === "linux" && (await sameHomeGatewayPid()) === pid) {
      glog(`update: :${PORT} did not accept /api/shutdown — using the pid-pinned stop for pid ${pid}`);
      accepted = await runGatewayStop(pid);
    }
    if (!accepted) {
      glog(`update: the adopted gateway on :${PORT} did not accept a stop`);
    } else if (await waitForPortFree(adoptedTimeoutMs)) {
      // A graceful stop releases the port before the process exits, and the
      // draining process still loads code from these files, so the swap waits
      // for the exit itself.
      const exit = await waitForProcessExit({
        pids: incumbentPids,
        isAlive: (probePid) => {
          try { processObj.kill(probePid, 0); return true; }
          catch (error) { return Boolean(error && error.code === "EPERM"); }
        },
        sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
        timeoutMs: adoptedTimeoutMs,
      });
      if (exit !== "exited") {
        glog(`update: pid ${pid} released :${PORT} but is still running — refusing to replace its files`);
        throw gatewayStillRunningError(PORT, adoptedGatewayStopCommand());
      }
      if (getGatewayOwnership() === "reused-service") {
        // A service manager can start the gateway again from the same files.
        // Give it the same bounded rebind grace the launch paths do, so a
        // restarted gateway is seen before the swap rather than during it.
        await waitForServiceRebind({
          isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
          sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
        });
      }
    }
    if (await isCurrentBundleGateway()) {
      glog(`update: a gateway from this app's bundle still holds :${PORT} — refusing to replace its files`);
      throw gatewayStillRunningError(PORT, adoptedGatewayStopCommand());
    }
    glog(`update: no gateway from this app's bundle holds :${PORT}`);
  }

  function killGatewayTreeOnWindowsBounded(pid) {
    return windowsTaskkill(pid, {
      isTrustedCommand: isTrustedWindowsGatewayCommand,
      getCommandFn: (probePid) => windowsProcessCommand(probePid, {
        powershellTimeoutMs: 3000,
        wmicTimeoutMs: 2000,
      }),
      timeoutMs: 5000,
    });
  }

  // Wedge recovery has no graceful endpoint: the loop serving it is frozen.
  // POSIX lets the parent reap its own children; Windows requires a tree kill or
  // detached kiro-cli/MCP descendants survive with the data-home locks.
  async function killGatewayProcessTree(gateway, signal) {
    if (!gateway || gateway.exitCode !== null) return;
    const killPid = () => {
      try { gateway.kill(signal); }
      catch (error) { glog(`${signal} failed: ${error && error.message}`); }
    };
    if (!gateway.pid) { killPid(); return; }
    if (!IS_WIN) {
      // The child may be a launcher that forked the real gateway (a package
      // manager's shim) instead of exec'ing it. Killing only the child leaves
      // that gateway alive, re-parented to init, holding the port and lock --
      // and a PPID-1 holder is then treated as service-managed and never
      // evicted. List the tree BEFORE the kill re-parents it.
      const descendants = await posixDescendantPids(gateway.pid);
      for (const pid of descendants) {
        try { processObj.kill(pid, signal); }
        catch (error) { glog(`${signal} descendant pid=${pid} failed: ${error && error.message}`); }
      }
      if (descendants.length) glog(`${signal} sent to ${descendants.length} descendant(s) of pid=${gateway.pid}`);
      killPid();
      return;
    }
    try {
      await windowsTaskkill(gateway.pid, {
        isTrustedCommand: isTrustedWindowsGatewayCommand,
      });
    } catch (error) {
      glog(`tree kill refused (${error && error.message}) — falling back to a single-pid kill`);
      killPid();
    }
  }

  return {
    stopGatewayGracefully,
    stopGatewayForUpdate,
    killGatewayProcessTree,
  };
}

module.exports = { createShutdown };
