"use strict";

const { buildGatewayEnvironment } = require("../../gateway-env");
const { isPathFallback } = require("../../find-bin");
const { gatewayCliInvocation } = require("../../gateway-recovery");

// `kirocrew stop --port` sends SIGTERM and waits for the port itself.
const STUCK_GATEWAY_STOP_TIMEOUT_MS = 30_000;

/**
 * This data folder's `gateway.lock`, the fact that ties a running gateway to
 * this app's own home: whether a given pid holds it, which single port holder
 * does, the CLI's pid-pinned stop of that holder, and the CLI's
 * socket-independent probe of who holds the lock at all.
 *
 * Both CLI calls run the backend binary the launch would spawn, under the same
 * scrubbed environment, and never inherit KIROCREW_PORT.
 */
function createHomeLock({
  fs,
  path,
  execFile,
  app,
  processObj,
  home: KIROCREW_HOME,
  port: PORT,
  isWindows: IS_WIN,
  log: glog,
  lsofHoldsFile,
  snapshotGatewayPortPids,
  resolveGatewayBin,
}) {
  // Whether `pid` holds this app's own data folder. Two facts, both required:
  // gateway.lock names that pid, and that pid has gateway.lock open right now.
  // The recorded pid alone survives its writer, so a reused pid serving another
  // home would match it; the open descriptor is what ties the process to THIS
  // home. lsof reports the kernel-resolved path, so a symlinked home is
  // compared by its real path. POSIX only: Windows has no lsof, so nothing is
  // ever proven there.
  async function holdsHomeGatewayLock(pid) {
    if (IS_WIN) return false;
    const lockFile = path.join(KIROCREW_HOME, "gateway.lock");
    let recorded = NaN;
    try {
      recorded = parseInt(String(fs.readFileSync(lockFile, "utf8")).trim(), 10);
    } catch {
      return false;
    }
    if (!Number.isInteger(recorded) || recorded !== pid) return false;
    let realLock = lockFile;
    try { realLock = fs.realpathSync(lockFile); } catch { /* compare the literal path */ }
    return await lsofHoldsFile(pid, lockFile)
      || (realLock !== lockFile && await lsofHoldsFile(pid, realLock));
  }

  // The single LISTEN pid on PORT when it holds this app's own data folder
  // (`holdsHomeGatewayLock`), else 0. An unproven holder keeps today's spawn
  // path.
  async function sameHomeGatewayPid() {
    if (IS_WIN) return 0;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return 0;
    return await holdsHomeGatewayLock(pids[0]) ? pids[0] : 0;
  }

  // Run the CLI's own stop for PORT, pinned to the pid this app proved. With
  // --expect-pid the CLI re-reads the listener and this home's gateway.lock
  // right before the signal and refuses, signalling nothing, unless that pid
  // still holds both -- so a gateway that replaced it is never stopped. POSIX
  // only: its one caller is behind sameHomeGatewayPid.
  function runGatewayStop(pid) {
    const { KIROCREW_PORT: _ignored, ...cleanEnv } = processObj.env;
    // The stop runs the same bundled binary the launch path spawns, so it must
    // inherit the same scrubbed environment: a bundled Python left with an
    // inherited PYTHONPATH/PYTHONHOME from a dev shell would import checkout
    // code instead of its packaged tree. Route it through the one helper that
    // strips those for a bundled binary (gateway-env.js), detecting bundled the
    // same way the spawn does (resolveGatewayBin lands under backend-dist).
    const bin = resolveGatewayBin();
    const bundled = bin.includes("backend-dist");
    return new Promise((resolve) => {
      execFile(bin, ["stop", "--port", String(PORT), "--expect-pid", String(pid)], {
        timeout: STUCK_GATEWAY_STOP_TIMEOUT_MS,
        windowsHide: true,
        env: buildGatewayEnvironment({ ...cleanEnv, KIROCREW_HOME }, { bundled, platform: processObj.platform }),
      }, (error, stdout, stderr) => {
        const output = `${stdout || ""}${stderr || ""}`.trim();
        if (error) glog(`stuck gateway: \`kirocrew stop --port ${PORT}\` failed: ${error.message}${output ? ` — ${output.slice(0, 200)}` : ""}`);
        else glog(`stuck gateway: \`kirocrew stop --port ${PORT}\` finished${output ? ` — ${output.slice(0, 200)}` : ""}`);
        resolve(!error);
      });
    });
  }

  /**
   * Ask the CLI who holds this home's `gateway.lock`, SOCKET-INDEPENDENTLY.
   *
   * `kirocrew gateway-pid` resolves the incumbent through the backend's
   * `lock_holder` oracle — the same one `stop --expect-pid`/`restart` use — so
   * it names a draining gateway that has already released its LISTEN socket but
   * still holds the lock. That is the identity `snapshotGatewayPortPids` (lsof /
   * netstat, LISTEN owners only) cannot see: when the socket snapshot names
   * nothing, recovery would otherwise refuse to respawn and leave the desktop
   * with no gateway. Cross-platform, so it answers on a Windows host too, where
   * the POSIX lock-file check does not run.
   *
   * The command prints one line of JSON and exits 0 for a resolvable lock
   * (holder=process, holder=nobody, or holder=held) or 2 for a truly
   * indeterminate one (holder=indeterminate) — exit 2 surfaces as an execFile
   * error, but the JSON is still on stdout, so the body is parsed regardless of
   * exit code. holder=held means the lock is POSITIVELY held but its owner
   * cannot be named (a draining gateway whose pid is unreadable under a Windows
   * mandatory lock): a gateway is alive, so the caller waits for the lock to be
   * released rather than giving up.
   *
   * Two outcomes differ by whether time can change them. A probe that timed
   * out (execFile killed it at the timeout, or reported ETIMEDOUT) is
   * `unverified`: a slow interpreter start answers on a later attempt, so the
   * caller backs off and asks again, never spawning blind. Everything else that
   * yields no answer gets the same result on every attempt and is `refused`,
   * so the caller ends on its terminal error: a probe the OS could not start
   * (a spawn error code such as ENOENT or EACCES, e.g. the bundled interpreter
   * is missing), a probe that ran to completion without printing JSON (a
   * backend that fails on import), an answer that names no known holder
   * (holder=indeterminate), and a packaged app's refusal to run a
   * PATH-fallback `kirocrew`.
   *
   * @returns {Promise<{verdict:"captured"|"released"|"held"|"unverified"|"refused", pid:number|null}>}
   */
  function probeLockHolderPid() {
    const { KIROCREW_PORT: _ignored, ...cleanEnv } = processObj.env;
    const bin = resolveGatewayBin();
    // The same refusal spawnGateway applies: a packaged Windows app with no
    // backend at a probed path does not run whatever `kirocrew` PATH names.
    // Refused, not a guess: every later attempt resolves the same PATH entry.
    if (IS_WIN && app.isPackaged && isPathFallback(bin)) {
      glog(`liveness: lock probe REFUSED: no bundled backend — a packaged app does not run ${bin} from PATH`);
      return Promise.resolve({ verdict: "refused", pid: null });
    }
    const bundled = bin.includes("backend-dist");
    const { bin: probeBin, args: probeArgs } = gatewayCliInvocation(bin, ["gateway-pid"], path);
    return new Promise((resolve) => {
      execFile(probeBin, probeArgs, {
        timeout: STUCK_GATEWAY_STOP_TIMEOUT_MS,
        windowsHide: true,
        env: buildGatewayEnvironment({ ...cleanEnv, KIROCREW_HOME }, { bundled, platform: processObj.platform }),
      }, (error, stdout) => {
        let payload = null;
        try { payload = JSON.parse(String(stdout || "").trim()); }
        catch { payload = null; }
        if (!payload || typeof payload !== "object") {
          // Only a timeout can answer differently next time. A spawn error
          // (string code) or a finished run with no JSON (numeric exit code,
          // or no error at all) repeats on every attempt.
          const timedOut = Boolean(error) && (error.killed === true || error.code === "ETIMEDOUT");
          if (timedOut) {
            glog(`liveness: \`kirocrew gateway-pid\` gave no usable answer (timed out: ${error.message}) — cannot name the incumbent via the lock this time`);
            resolve({ verdict: "unverified", pid: null });
            return;
          }
          glog(`liveness: lock probe REFUSED: \`kirocrew gateway-pid\` gave no usable answer${error ? ` (${error.message})` : " (exit 0, no JSON)"} — retrying cannot change that, surfacing the error`);
          resolve({ verdict: "refused", pid: null });
          return;
        }
        if (payload.holder === "nobody") {
          glog(`liveness: lock probe says :${PORT}'s gateway.lock is free — the incumbent has exited`);
          resolve({ verdict: "released", pid: null });
          return;
        }
        if (payload.holder === "process" && Number.isInteger(payload.pid) && payload.pid > 0) {
          glog(`liveness: lock probe named the incumbent (pid ${payload.pid}, source ${payload.source || "?"}) — waiting for it to exit`);
          resolve({ verdict: "captured", pid: payload.pid });
          return;
        }
        if (payload.holder === "held") {
          // A live gateway holds the lock but cannot be named (Windows mandatory
          // lock hides the pid). Wait for the lock to be released, not forever.
          glog(`liveness: lock probe says :${PORT}'s gateway.lock is held but its owner is unnameable (likely a draining gateway) — waiting for it to release`);
          resolve({ verdict: "held", pid: null });
          return;
        }
        glog(`liveness: lock probe is indeterminate (${payload.reason || payload.holder || "unnamed"}) — refusing an automatic respawn that could race gateway.lock, and surfacing the error`);
        resolve({ verdict: "refused", pid: null });
      });
    });
  }

  return {
    holdsHomeGatewayLock,
    sameHomeGatewayPid,
    runGatewayStop,
    probeLockHolderPid,
  };
}

module.exports = { createHomeLock };
