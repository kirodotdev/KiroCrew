/**
 * Graceful gateway stop, extracted from main.js for testability.
 *
 * The embedded Python gateway is a long-running child process. Before quit
 * (and before any Squirrel auto-update bundle swap) it must be stopped
 * cleanly: POST /api/shutdown so it flushes session/memory/cron state and
 * exits itself, falling back to SIGTERM then SIGKILL. main.js injects the live
 * child process + module-level config; tests inject a real spawned process and
 * a local HTTP server. Deps (http/fs/path/timers) are injectable so the logic
 * is unit-testable without Electron.
 */

const http = require("http");
const fs = require("fs");
const path = require("path");

/**
 * POST /api/shutdown with the local secret (mirrors the dashboard's
 * X-Local-Secret auth). Resolves true on HTTP 200, false on any failure
 * (missing secret, connection error, timeout, non-200) so the caller can fall
 * back to signals.
 *
 * @returns {Promise<boolean>}
 */
function postShutdown({
  backendUrl,
  kirocrewHome,
  httpMod = http,
  fsMod = fs,
  pathMod = path,
  timeoutMs = 5000,
}) {
  return new Promise((resolve) => {
    let secret = "";
    try {
      secret = fsMod.readFileSync(pathMod.join(kirocrewHome, ".local_secret"), "utf8").trim();
    } catch {
      return resolve(false);
    }
    if (!secret) return resolve(false);
    let u;
    try { u = new URL(`${backendUrl}/api/shutdown`); } catch { return resolve(false); }
    const req = httpMod.request(
      {
        hostname: u.hostname,
        port: u.port,
        path: u.pathname,
        method: "POST",
        headers: { "X-Local-Secret": secret },
        timeout: timeoutMs,
      },
      (res) => { res.resume(); resolve(res.statusCode === 200); }
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.end();
  });
}

/**
 * Stop the gateway child gracefully and await its exit.
 *   1. POST /api/shutdown (clean flush + self-exit)
 *   2. SIGTERM if the endpoint didn't take (older gateway / unreachable)
 *   3. SIGKILL if it still hasn't exited within timeoutMs
 * Resolves once the process is fully gone — callers (quit / auto-update) rely
 * on the exit having completed before proceeding.
 *
 * @param {import("child_process").ChildProcess} proc
 * @param {object} opts
 * @returns {Promise<void>}
 */
async function stopGatewayGracefully(
  proc,
  {
    backendUrl,
    kirocrewHome,
    timeoutMs = 15000,
    postShutdownFn = postShutdown,
    httpMod,
    fsMod,
    pathMod,
  } = {}
) {
  if (!proc || proc.exitCode !== null) return;
  await new Promise((resolve) => {
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    proc.once("exit", done);
    if (proc.exitCode !== null) return done();
    // Send SIGKILL at timeoutMs but DON'T resolve here — wait for the real
    // 'exit' so callers are guaranteed the process is gone (and signalCode is
    // accurate). A hard safety net resolves even if 'exit' never fires.
    const killTimer = setTimeout(() => {
      if (proc.exitCode === null) { try { proc.kill("SIGKILL"); } catch {} }
    }, timeoutMs);
    const hardTimer = setTimeout(done, timeoutMs + 3000);
    proc.once("exit", () => { clearTimeout(killTimer); clearTimeout(hardTimer); });
    // Prefer the clean endpoint; signal-nudge only if it didn't take.
    postShutdownFn({ backendUrl, kirocrewHome, httpMod, fsMod, pathMod }).then((ok) => {
      if (!ok && proc.exitCode === null) { try { proc.kill("SIGTERM"); } catch {} }
    });
  });
}

/**
 * Force-stop whatever KiroCrew process is LISTENing on `port`, then VERIFY the
 * port actually freed before reporting success.
 *
 * The old inline version SIGKILLed the owner and resolved after a fixed 800ms
 * delay, treating "signal accepted" as "process dead". That is wrong for a
 * gateway wedged in an uninterruptible kernel wait (macOS `U` state, e.g. a
 * blocking close() on a dead socket): SIGKILL is queued but never delivered, so
 * the process lives on and keeps the port. The caller then respawned into a
 * guaranteed "address already in use" and surfaced a confusing "exited code 1".
 *
 * This version polls the listener set after killing and returns `freed` based on
 * whether the port is ACTUALLY free afterwards (not merely whether our targets
 * died), plus `survivors` (the KiroCrew PIDs we tried to kill that are still
 * holding the port) and `foreignHolder` (a non-KiroCrew process still owns it).
 * `freed === false` means a respawn would just fail to bind — the caller MUST
 * NOT respawn; it should tell the user a restart is required (`survivors`, an
 * unkillable wedge) or that another app holds the port (`foreignHolder`).
 *
 * All side effects are injected so this is unit-testable without Electron or a
 * real OS process:
 *   - getListenPids(port) -> Promise<number[]>   (lsof -t)
 *   - getCommand(pid)     -> Promise<string>     (ps -o command=)
 *   - kill(pid, signal)                          (process.kill; may throw)
 *   - sleep(ms)           -> Promise<void>
 *
 * @returns {Promise<{killed:number, freed:boolean, survivors:number[], foreignHolder:boolean}>}
 */
async function forceStopPort(
  port,
  {
    getListenPids,
    getCommand,
    kill,
    sleep,
    // Also match the legacy kiroclaw process names: after the KiroClaw → KiroCrew
    // rename, a leftover kiroclaw gateway can still be holding the port on an
    // upgraded host, and must be recoverable (not treated as a foreign holder).
    isKirocrew = /kiro_crew|kirocrew|kiro_claw|kiroclaw/i,
    verifyTimeoutMs = 4000,
    pollIntervalMs = 250,
    log = () => {},
  }
) {
  const owners = await getListenPids(port);
  if (!owners.length) {
    log(`force-stop: no LISTEN owner found on :${port}`);
    return { killed: 0, freed: true, survivors: [], foreignHolder: false };
  }

  // Only signal PIDs we can positively identify as KiroCrew — never SIGKILL an
  // unrelated app that happens to share the port.
  const targets = [];
  for (const pid of owners) {
    const cmd = (await getCommand(pid)).trim();
    if (isKirocrew.test(cmd)) {
      try {
        kill(pid, "SIGKILL");
        targets.push(pid);
        log(`force-stop: SIGKILL pid=${pid} (${cmd.slice(0, 80)})`);
      } catch (e) {
        log(`force-stop: kill pid=${pid} failed: ${e && e.message}`);
      }
    } else {
      log(`force-stop: SKIP pid=${pid} — not a KiroCrew process (${cmd.slice(0, 80)})`);
    }
  }

  // Verify the kill took: poll until none of the PIDs we killed still hold the
  // port, or we run out of time. A normal process disappears within a poll or
  // two; a wedged (uninterruptible) one never will — that is the signal we need.
  const killed = targets.length;
  let survivors = targets.slice();
  let remaining = new Set(owners);
  const deadline = verifyTimeoutMs;
  let waited = 0;
  while (survivors.length && waited < deadline) {
    await sleep(pollIntervalMs);
    waited += pollIntervalMs;
    remaining = new Set(await getListenPids(port));
    survivors = survivors.filter((pid) => remaining.has(pid));
  }

  // If we never had any of our own targets to verify (foreign-only holder), the
  // loop above didn't re-probe — do one explicit check so `freed` reflects the
  // real port state instead of vacuously claiming free because WE killed nothing.
  if (!targets.length) {
    remaining = new Set(await getListenPids(port));
  }

  // `freed` means the port is genuinely free, NOT just "our targets died". A
  // foreign process still listening keeps freed=false so the caller surfaces a
  // restart/port-conflict path rather than respawning into a doomed bind.
  const freed = remaining.size === 0;
  const foreignHolder = !freed && survivors.length === 0;
  if (survivors.length) {
    log(`force-stop: port :${port} STILL held after ${waited}ms by pid ${survivors.join(", ")} `
      + `— process is unkillable (likely uninterruptible sleep); a system restart is required`);
  } else if (foreignHolder) {
    log(`force-stop: port :${port} held by a non-KiroCrew process we won't kill — respawn would fail to bind`);
  }
  return { killed, freed, survivors, foreignHolder };
}

module.exports = { postShutdown, stopGatewayGracefully, forceStopPort };
