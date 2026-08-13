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

const KIROCREW_EXE_NAMES = new Set(["kirocrew", "kirocrew-backend"]);
const PYTHON_EXE_RE = /^(?:python(?:\d+(?:\.\d+)*)?w?|py)$/i;

function commandLineTokens(commandLine) {
  const tokens = [];
  const input = String(commandLine || "").replace(/^\s*CommandLine=/i, "").trim();
  const tokenRe = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match;
  while ((match = tokenRe.exec(input)) !== null) {
    tokens.push(match[1] ?? match[2] ?? match[3]);
  }
  return tokens;
}

function executableName(token) {
  const basename = String(token || "").replace(/\\/g, "/").split("/").pop().toLowerCase();
  return basename.endsWith(".exe") ? basename.slice(0, -4) : basename;
}

function normalizedWindowsPath(token) {
  return String(token || "").replace(/\//g, "\\").toLowerCase();
}

function normalizedWindowsAbsolutePath(token) {
  const value = String(token || "").replace(/\//g, "\\");
  if (!/^(?:[A-Za-z]:\\|\\\\)/.test(value)) return "";
  return path.win32.normalize(value).toLowerCase();
}

/**
 * Resolve the executable selector a command line starts with.
 *
 * A POSIX `ps -o command=` line is unquoted, so an executable path containing a
 * space arrives split across tokens: an install under `/Users/Jane Doe/...`
 * would otherwise resolve to the executable name "jane" and classify our own
 * gateway as foreign. Rejoin leading tokens while they can still be path
 * continuations, and stop at the first option (`-x`) or second absolute path, so
 * a later ARGUMENT can never pose as the executable.
 *
 * @returns {{name:string, next:number}} the selector's executable name and the
 *   index of the first token after it.
 */
function executableSelector(tokens) {
  const first = tokens[0] || "";
  const fallback = { name: executableName(first), next: 1 };
  if (!first.startsWith("/")) return fallback;
  let candidate = first;
  for (let index = 1; ; index++) {
    const name = executableName(candidate);
    if (KIROCREW_EXE_NAMES.has(name) || PYTHON_EXE_RE.test(name)) return { name, next: index };
    const token = tokens[index];
    if (token === undefined || token.startsWith("-") || token.startsWith("/")) return fallback;
    candidate += ` ${token}`;
  }
}

/**
 * Match only a Kiro Crew executable, or a Python process whose first execution
 * selector invokes the `kiro_crew` module or a Kiro Crew script. Later process
 * arguments never establish ownership, so SSH aliases and unrelated script
 * arguments cannot authorize a kill. Absolute Windows executables must also
 * match the exact path selected by the launch resolver.
 */
function isKirocrewCommand(commandLine, { trustedExecutablePaths = [] } = {}) {
  const tokens = commandLineTokens(commandLine);
  if (!tokens.length) return false;

  const windowsExecutablePath = normalizedWindowsAbsolutePath(tokens[0]);
  if (windowsExecutablePath) {
    const trusted = new Set(
      trustedExecutablePaths
        .map(normalizedWindowsAbsolutePath)
        .filter(Boolean)
    );
    if (!trusted.has(windowsExecutablePath)) return false;
  }

  const selector = windowsExecutablePath
    ? { name: executableName(tokens[0]), next: 1 }
    : executableSelector(tokens);
  if (KIROCREW_EXE_NAMES.has(selector.name)) return true;
  if (!PYTHON_EXE_RE.test(selector.name)) return false;

  let index = selector.next;
  // Windows process identity prefixes ExecutablePath to the OS command line.
  // Skip that exact duplicate without skipping a Python-named script argument.
  if (normalizedWindowsPath(tokens[index]) === normalizedWindowsPath(tokens[0])) {
    index += 1;
  }

  while (index < tokens.length) {
    const token = tokens[index];
    if (token === "-m") return tokens[index + 1] === "kiro_crew";
    if (token === "-c" || token === "-") return false;
    if (token === "--") {
      index += 1;
      break;
    }
    if (token === "-W" || token === "-X") {
      index += 2;
      continue;
    }
    if (!token.startsWith("-")) break;
    index += 1;
  }

  const script = tokens[index];
  return /[\\/]/.test(script || "") && KIROCREW_EXE_NAMES.has(executableName(script));
}

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
  // The caller may pass more than one candidate secret, and the running gateway
  // is authenticated by whichever one it actually loaded. Try every candidate
  // and let a 200 pick the live one: a stale/wrong secret returning 403 must NOT
  // short-circuit the clean-flush path into a hard SIGTERM that skips
  // session/memory/cron persistence.
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
 * With `failClosedOnProbeError`, an unavailable owner probe returns
 * `probeFailed:true, freed:false` instead of throwing or claiming the port is
 * free. Windows uses this because netstat failures must block a blind respawn.
 *
 * All side effects are injected so this is unit-testable without Electron or a
 * real OS process:
 *   - getListenPids(port) -> Promise<number[]>   (lsof -t)
 *   - getCommand(pid)     -> Promise<string>     (ps -o command=)
 *   - kill(pid, signal)                          (process.kill; may throw)
 *   - sleep(ms)           -> Promise<void>
 *
 * @returns {Promise<{killed:number, freed:boolean, survivors:number[], foreignHolder:boolean, probeFailed?:boolean}>}
 */
async function forceStopPort(
  port,
  {
    getListenPids,
    getCommand,
    kill,
    sleep,
    getPpid = null,
    isKirocrew = isKirocrewCommand,
    verifyTimeoutMs = 4000,
    pollIntervalMs = 250,
    failClosedOnProbeError = false,
    log = () => {},
  }
) {
  let owners;
  try {
    owners = await getListenPids(port);
  } catch (e) {
    if (!failClosedOnProbeError) throw e;
    log(`force-stop: LISTEN probe failed on :${port} (${e && e.message})`);
    return {
      killed: 0, freed: false, survivors: [], foreignHolder: false,
      serviceHolder: false, probeFailed: true,
    };
  }
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
    const ours = isKirocrew(cmd);
    if (ours) {
      // A service-managed gateway is respawned by launchd/systemd the moment we
      // kill it, so evicting it cannot free the port — it only makes the retry
      // race the respawn. Leave it alone and tell the caller why.
      if (await isServiceManaged(pid, getPpid)) {
        serviceHolder = true;
        log(`force-stop: SKIP pid=${pid} — service-managed KiroCrew gateway (${cmd.slice(0, 80)})`);
        continue;
      }
      try {
        await kill(pid, "SIGKILL");
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
    try {
      remaining = new Set(await getListenPids(port));
    } catch (e) {
      if (!failClosedOnProbeError) throw e;
      log(`force-stop: verify LISTEN probe failed on :${port} (${e && e.message})`);
      return {
        killed, freed: false, survivors: [], foreignHolder: false,
        serviceHolder, probeFailed: true,
      };
    }
    survivors = survivors.filter((pid) => remaining.has(pid));
  }

  // If we never had any of our own targets to verify (foreign-only holder), the
  // loop above didn't re-probe — do one explicit check so `freed` reflects the
  // real port state instead of vacuously claiming free because WE killed nothing.
  if (!targets.length) {
    try {
      remaining = new Set(await getListenPids(port));
    } catch (e) {
      if (!failClosedOnProbeError) throw e;
      log(`force-stop: verify LISTEN probe failed on :${port} (${e && e.message})`);
      return {
        killed, freed: false, survivors: [], foreignHolder: false,
        serviceHolder, probeFailed: true,
      };
    }
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
 *   "kirocrew" — a local LISTEN owner matching isKirocrewCommand. Only this
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
  { getListenPids, getCommand, getPpid = null, isKirocrew = isKirocrewCommand, log = () => {} }
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
    const ours = isKirocrew(cmd);
    if (ours) {
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
  isKirocrewCommand,
  INIT_PPID,
};
