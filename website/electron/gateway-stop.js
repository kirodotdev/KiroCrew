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

// The single definition of "this LISTEN owner is one of ours". Shared by
// forceStopPort (which may only ever SIGKILL our own processes) and
// classifyPortOwner (which may only ever authorise an eviction of one).
// Keep it in one place: a drift between the two would let one of them
// mis-target a stranger's process.
// Match a Kiro Crew PROCESS from a command line, not a mere path substring.
// Two shapes only: the `kirocrew`/`kirocrew-backend` executable as a command
// token (optionally `.exe`, ending at whitespace/quote/EOL — so a
// `/Users/kirocrew/…` or `C:\Users\kirocrew\other.exe` user directory does NOT
// match), or the `-m kiro_crew` python-module invocation. Anything else (an ssh
// forward, an unrelated app under a `kirocrew` home dir) is foreign.
const KIROCREW_PROC_RE =
  /(?:^|[\s"'/\\])kirocrew(?:-backend)?(?:\.exe)?(?=$|[\s"'])|-m\s+kiro_crew(?=$|[\s"'.])/i;

// A gateway whose parent is init (PID 1) is owned by the OS service manager —
// a launchd LaunchAgent on macOS, a systemd unit on Linux — not by this app.
// It must never be evicted: launchd's KeepAlive (and systemd's Restart=)
// respawns it within milliseconds, so a "successful" force-stop frees the port
// only long enough for our retry's bind to race the respawn and fail with a
// confusing "address already in use". Reuse is always the correct move there.
//
// A process whose parent shell has exited is also reparented to PID 1. Treating
// that as non-evictable is equally right: it is not our child either.
const INIT_PPID = 1;

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
  secrets,
  httpMod = http,
  fsMod = fs,
  pathMod = path,
  timeoutMs = 5000,
}) {
  // A migration can leave more than one `.local_secret` on disk (canonical +
  // legacy), and the running gateway is authenticated by whichever one it
  // actually loaded. Try every candidate and let a 200 pick the live one: a
  // stale/wrong secret returning 403 must NOT short-circuit the clean-flush
  // path into a hard SIGTERM that skips session/memory/cron persistence.
  let secretList = Array.isArray(secrets) ? secrets : [];
  if (!secretList.length) {
    try {
      const s = fsMod.readFileSync(pathMod.join(kirocrewHome, ".local_secret"), "utf8");
      secretList = [s];
    } catch { /* none readable */ }
  }
  secretList = [...new Set(secretList.map((s) => (s || "").trim()).filter(Boolean))];
  if (!secretList.length) return Promise.resolve(false);

  let u;
  try { u = new URL(`${backendUrl}/api/shutdown`); } catch { return Promise.resolve(false); }

  const attempt = (secret) => new Promise((resolve) => {
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

  return (async () => {
    for (const secret of secretList) {
      if (await attempt(secret)) return true;
    }
    return false;
  })();
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
    secrets,
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
    postShutdownFn({ backendUrl, kirocrewHome, secrets, httpMod, fsMod, pathMod }).then((ok) => {
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
    getPpid = null,
    isKirocrew = KIROCREW_PROC_RE,
    verifyTimeoutMs = 4000,
    pollIntervalMs = 250,
    log = () => {},
  }
) {
  const owners = await getListenPids(port);
  if (!owners.length) {
    log(`force-stop: no LISTEN owner found on :${port}`);
    return { killed: 0, freed: true, survivors: [], foreignHolder: false, serviceHolder: false };
  }

  // Only signal PIDs we can positively identify as KiroCrew — never SIGKILL an
  // unrelated app that happens to share the port.
  const targets = [];
  let serviceHolder = false;
  for (const pid of owners) {
    const cmd = (await getCommand(pid)).trim();
    if (isKirocrew.test(cmd)) {
      // A service-managed gateway is respawned by launchd/systemd the moment we
      // kill it, so evicting it cannot free the port — it only makes the retry
      // race the respawn. Leave it alone and tell the caller why.
      if (await isServiceManaged(pid, getPpid)) {
        serviceHolder = true;
        log(`force-stop: SKIP pid=${pid} — service-managed KiroCrew gateway (${cmd.slice(0, 80)})`);
        continue;
      }
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
  if (serviceHolder && !freed) {
    log(`force-stop: port :${port} is held by a service-managed gateway — the OS respawns it, so the app must reuse it instead of retrying a spawn`);
  }
  return { killed, freed, survivors, foreignHolder, serviceHolder };
}

/**
 * Classify who LOCALLY owns the LISTEN socket on `port`.
 *
 * This exists because an HTTP identity probe CANNOT distinguish a local rival
 * gateway from a remote one reached through a port-forward: `ssh -L 5476:...`
 * makes a gateway on another machine answer on `localhost:5476` with the same
 * `/api/health` payload a local install would send. Deciding to evict on the
 * payload alone therefore tears down the user's tunnel (and the AppleScript
 * quit targets a local app that isn't even running). The listening socket's
 * owner is the ground truth the payload lacks — on a tunnel it is `ssh`.
 *
 * Deliberately fail-safe: every outcome except a positively identified local
 * KiroCrew process is a reason NOT to evict.
 *   "kirocrew" — a local LISTEN owner matching KIROCREW_PROC_RE. Only this
 *                value may authorise a takeover.
 *   "foreign"  — a local LISTEN owner exists but is not ours (e.g. `ssh`).
 *   "none"     — nothing is listening locally, yet something answered. A race,
 *                or a socket we cannot see; treat as not ours.
 *   "unknown"  — the probe itself could not run (no lsof / EACCES). Never
 *                mistake "couldn't look" for "safe to kill".
 *
 * Side effects are injected so this is unit-testable without Electron or real
 * OS processes (mirrors forceStopPort above).
 *
 * @param {number} port
 * @param {object} deps
 * @param {(port:number)=>Promise<number[]>} deps.getListenPids  lsof -t
 * @param {(pid:number)=>Promise<string>}    deps.getCommand     ps -o command=
 * @returns {Promise<"kirocrew"|"foreign"|"none"|"unknown">}
 */
async function classifyPortOwner(
  port,
  { getListenPids, getCommand, getPpid = null, isKirocrew = KIROCREW_PROC_RE, log = () => {} }
) {
  let pids;
  try {
    pids = await getListenPids(port);
  } catch (e) {
    log(`port-owner: could not probe :${port} (${e && e.message}) — owner unknown, will not evict`);
    return "unknown";
  }
  if (!pids.length) {
    log(`port-owner: no local LISTEN owner on :${port}`);
    return "none";
  }
  for (const pid of pids) {
    const cmd = (await getCommand(pid)).trim();
    if (isKirocrew.test(cmd)) {
      if (await isServiceManaged(pid, getPpid)) {
        log(`port-owner: :${port} held by SERVICE-MANAGED KiroCrew pid=${pid} (${cmd.slice(0, 80)}) — reuse, never evict`);
        return "service";
      }
      log(`port-owner: :${port} held by local KiroCrew pid=${pid} (${cmd.slice(0, 80)})`);
      return "kirocrew";
    }
    log(`port-owner: :${port} held by NON-KiroCrew pid=${pid} (${cmd.slice(0, 80)})`);
  }
  return "foreign";
}

/**
 * Is `pid` owned by the OS service manager rather than by us? See INIT_PPID.
 *
 * Fails CLOSED (returns true, i.e. "do not touch") when the parent cannot be
 * determined: mistaking a service for a wedge kills a gateway the OS instantly
 * respawns, while mistaking a wedge for a service only costs an eviction we can
 * still explain to the user.
 */
async function isServiceManaged(pid, getPpid) {
  if (!getPpid) return false; // caller opted out of the probe (e.g. Windows)
  try {
    const ppid = parseInt(String(await getPpid(pid)).trim(), 10);
    if (!Number.isInteger(ppid)) return true;
    return ppid === INIT_PPID;
  } catch {
    return true;
  }
}

module.exports = {
  postShutdown,
  stopGatewayGracefully,
  forceStopPort,
  classifyPortOwner,
  isServiceManaged,
  KIROCREW_PROC_RE,
  INIT_PPID,
};
