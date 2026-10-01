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
 * Every descendant of `rootPid`, deepest first, from a `ps -A -o pid=,ppid=`
 * table. `rootPid` itself is not included.
 *
 * The desktop's own child can be a launcher that forks the real gateway rather
 * than exec'ing it (a package manager's shim is one). Signalling only that
 * child leaves the gateway alive, re-parented to init, still holding the port
 * and gateway.lock.
 *
 * @param {number} rootPid
 * @param {string} psTable  one `pid ppid` pair per line
 * @returns {number[]}
 */
function descendantPids(rootPid, psTable) {
  const children = new Map();
  for (const line of String(psTable || "").split(/\r?\n/)) {
    const [pid, ppid] = line.trim().split(/\s+/).map((value) => parseInt(value, 10));
    if (!Number.isInteger(pid) || !Number.isInteger(ppid) || pid <= 1) continue;
    if (!children.has(ppid)) children.set(ppid, []);
    children.get(ppid).push(pid);
  }
  const found = [];
  const seen = new Set([rootPid]);
  const walk = (pid) => {
    for (const child of children.get(pid) || []) {
      if (seen.has(child)) continue;
      seen.add(child);
      walk(child);
      found.push(child);
    }
  };
  walk(rootPid);
  return found;
}

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

  // Whether `pid` has `file` open, from lsof's own view of the process. A pid
  // recorded in a lock file can be reused; an open descriptor on that exact
  // path cannot be left behind by a process that has exited. Resolves false on
  // any probe failure, so an unreadable host never authorises a stop.
  function lsofHoldsFile(pid, file) {
    return new Promise((resolve) => {
      execFile(
        resolveLsof(),
        ["-nP", "-a", "-p", String(pid), "-Fn"],
        { timeout: 5000 },
        (error, stdout) => {
          if (error && !stdout) { resolve(false); return; }
          resolve(String(stdout || "").split("\n").some((line) => line === `n${file}`));
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

  // Every live descendant of `pid`, deepest first. Empty when ps cannot run
  // or hangs: the caller still signals `pid` itself, exactly as before. The
  // execFile timeout only signals ps; a ps stuck in an uninterruptible read
  // never exits, so a JS-side deadline keeps recovery from waiting forever.
  function posixDescendantPids(pid) {
    return new Promise((resolve) => {
      const deadline = setTimeout(() => resolve([]), 6000);
      if (typeof deadline.unref === "function") deadline.unref();
      execFile(
        "/bin/ps",
        ["-A", "-o", "pid=,ppid="],
        { timeout: 5000 },
        (error, stdout) => {
          clearTimeout(deadline);
          resolve(error ? [] : descendantPids(pid, stdout));
        },
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
    posixDescendantPids,
    winListenPids,
    lsofListenPids,
    lsofHoldsFile,
    psCommand,
    psPpid,
    snapshotGatewayPortPids,
    unverifiedIncumbent,
    waitForIncumbentExit,
    forceStopGatewayPort,
  };
}

module.exports = { createPortHolders, descendantPids };
