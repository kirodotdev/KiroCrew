"use strict";

const { findKirocrewBin } = require("../../find-bin");
const { forceStopPort, isKirocrewCommand } = require("../../gateway-stop");
const {
  canonicalWindowsPath,
  windowsGatewayExecutablePaths,
  windowsListenPids,
  windowsProcessCommand,
  windowsTaskkill,
} = require("../../windows-port");
const {
  waitForProcessExit,
  snapshotPortPids,
  incumbentSnapshotBlocksRespawn,
} = require("../../gateway-recovery");

const LSOF_CANDIDATES = ["/usr/sbin/lsof", "/usr/bin/lsof"];

/**
 * The operating-system view of whoever holds a gateway port: the LISTEN pids
 * (lsof, or netstat on Windows), a pid's command line and parent, whether a
 * Windows command line is a gateway this app may treat as its own, the
 * incumbent snapshot and exit wait that stand between a freed port and
 * gateway.lock, and the force-stop that clears a wedged holder.
 *
 * The listener and command probes take the supervisor's injected execFile,
 * fs, path and process, so node:test drives them with fakes. The Windows
 * force-stop path is the exception: its netstat, command-line and taskkill
 * calls go through windows-port.js with that module's own child_process, so
 * the injected execFile does NOT fake them. The supervisor keeps the decisions
 * that read these answers (occupancy versus identity, adopt versus respawn);
 * this module only asks the host.
 *
 * @param {object} deps
 * @param {() => string[]} deps.getSpawnedExecutablePaths  executables the
 *        CURRENT child was spawned from, read at call time.
 */
function createPortHolders({
  fs,
  os,
  path,
  execFile,
  processObj,
  dirname,
  isWindows: IS_WIN,
  log: glog,
  getSpawnedExecutablePaths,
}) {
  const windowsRealpath = (candidate) => fs.realpathSync.native(candidate);

  function isTrustedWindowsGatewayCommand(command) {
    const gatewayBin = findKirocrewBin(
      fs,
      os,
      path,
      processObj.resourcesPath,
      dirname,
    );
    return isKirocrewCommand(command, {
      trustedExecutablePaths: [
        ...windowsGatewayExecutablePaths(gatewayBin, { realpathSync: windowsRealpath }),
        ...getSpawnedExecutablePaths(),
      ],
      canonicalizePath: (candidate) => canonicalWindowsPath(candidate, windowsRealpath),
    });
  }

  // The Windows OS probes take the factory's injected execFile, exactly as the
  // POSIX ones do; production passes the real child_process.execFile.
  const winListenPids = (p) => windowsListenPids(p, { execFileFn: execFile });

  // Signal 0 probes without delivering on POSIX. EPERM still means the process
  // is alive and may be holding gateway.lock.
  function pidAlive(pid) {
    try { processObj.kill(pid, 0); return true; }
    catch (error) { return !!(error && error.code === "EPERM"); }
  }

  // Capture the listener while the socket is still bound. Once it clears,
  // neither lsof nor netstat can name the process still holding gateway.lock.
  function snapshotGatewayPortPids(probePort) {
    return snapshotPortPids({
      port: probePort,
      isWindows: IS_WIN,
      getWindowsPids: winListenPids,
      getPosixPids: lsofListenPids,
    });
  }

  function unverifiedIncumbent(pids) {
    return incumbentSnapshotBlocksRespawn({ pids, isWindows: IS_WIN });
  }

  // Port free is not lock free. Wait for captured incumbent PIDs to die so the
  // kernel has released gateway.lock before attempting the replacement spawn.
  async function waitForIncumbentExit(pids, label) {
    const verdict = await waitForProcessExit({
      pids,
      isAlive: pidAlive,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    });
    if (verdict === "timeout") {
      glog(`${label}: incumbent gateway process still alive after the exit grace (port already free) — spawning anyway; a lock refusal will surface via the start-failure watcher`);
    }
    return verdict;
  }

  // Packaged GUI apps inherit a minimal PATH. macOS and Linux install lsof in
  // different absolute locations; probe both before falling back to PATH.
  function resolveLsof() {
    for (const candidate of LSOF_CANDIDATES) {
      try { if (fs.existsSync(candidate)) return candidate; }
      catch { /* unreadable candidate */ }
    }
    return "lsof";
  }

  function lsofListenPids(probePort) {
    return new Promise((resolve, reject) => {
      execFile(
        resolveLsof(),
        ["-nP", `-iTCP:${probePort}`, "-sTCP:LISTEN", "-t"],
        { timeout: 5000 },
        (error, stdout) => {
          // lsof exits non-zero with empty output for no match. Only an EXECUTE
          // failure is unknown; treating it as a free port permits blind kills.
          if (error && (error.code === "ENOENT" || error.code === "EACCES")) {
            reject(error);
            return;
          }
          resolve(String(stdout || "").split(/\s+/)
            .map((value) => parseInt(value, 10))
            .filter((value) => Number.isInteger(value) && value > 1));
        },
      );
    });
  }

  function psCommand(pid) {
    return new Promise((resolve) => {
      execFile(
        "/bin/ps",
        ["-p", String(pid), "-o", "command="],
        { timeout: 5000 },
        (_error, stdout) => resolve(String(stdout || "")),
      );
    });
  }

  // PPID 1 distinguishes service-managed gateways (and conservative orphans)
  // which must never be evicted into a launchd/systemd respawn race.
  function psPpid(pid) {
    return new Promise((resolve) => {
      execFile(
        "/bin/ps",
        ["-p", String(pid), "-o", "ppid="],
        { timeout: 5000 },
        (_error, stdout) => resolve(String(stdout || "")),
      );
    });
  }

  function forceStopGatewayPort(probePort) {
    if (IS_WIN) {
      return forceStopPort(probePort, {
        getListenPids: windowsListenPids,
        getCommand: windowsProcessCommand,
        kill: (pid) => windowsTaskkill(pid, {
          isTrustedCommand: isTrustedWindowsGatewayCommand,
        }),
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        isKirocrew: isTrustedWindowsGatewayCommand,
        failClosedOnProbeError: true,
        log: glog,
      });
    }
    return forceStopPort(probePort, {
      getListenPids: lsofListenPids,
      getCommand: psCommand,
      getPpid: psPpid,
      kill: (pid, signal) => processObj.kill(pid, signal),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      log: glog,
    });
  }

  return {
    windowsRealpath,
    isTrustedWindowsGatewayCommand,
    winListenPids,
    lsofListenPids,
    psCommand,
    psPpid,
    pidAlive,
    snapshotGatewayPortPids,
    unverifiedIncumbent,
    waitForIncumbentExit,
    forceStopGatewayPort,
  };
}

module.exports = { createPortHolders };
