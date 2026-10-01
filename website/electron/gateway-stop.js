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
// The module a `python -m <module>` gateway is booted from. `kiro_crew` is the
// core. A composed edition boots from its companion's own top-level module so
// the composition root runs instead of the core CLI, and the companion is named
// `kirocrew_<edition>` (the `kirocrew.plugins` entry-point convention the Python
// side reads in `port_resolution._gateway_module_roots()`). This process has no
// installed-entry-point view of that Python environment, so it matches the
// naming convention instead; the core never learns any edition's name. Exact
// top-level module only: a dotted submodule is not a gateway entry point.
const KIROCREW_MODULE_RE = /^(?:kiro_crew|kirocrew_[a-z0-9][a-z0-9_]*)$/;
// A gateway module is only a gateway when argparse's first positional after it
// is a server subcommand -- the same set `port_resolution._KIROCREW_SERVER_SUBCOMMANDS`
// gates `kirocrew stop` on. Without this the module name alone would authorize a
// SIGKILL of any process that merely imports a `kirocrew_*` module on our port.
// Both constants are pinned to their Python twins by
// `test/test_cli.py::TestDesktopGatewayIdentityParity`; change them together.
const KIROCREW_SERVER_SUBCOMMANDS = new Set(["gateway", "dashboard", "start"]);

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
 * selector invokes a Kiro Crew gateway module (`kiro_crew`, or a composed
 * edition's `kirocrew_<edition>` companion — `KIROCREW_MODULE_RE`) followed by
 * a server subcommand (`KIROCREW_SERVER_SUBCOMMANDS`), or a Kiro Crew script.
 * Later process arguments never establish ownership, so SSH aliases and
 * unrelated script arguments cannot authorize a kill.
 *
 * An absolute Windows executable is additionally path-bound: it must be a
 * file the launch resolver selected. Both sides of the path comparison go
 * through `canonicalizePath` (a junction-following realpath in production;
 * identity by default) because a Toolbox-style install launches through a
 * `current` junction while Windows reports the running process by the
 * directory the junction resolved to. A path the resolver did not select stays
 * foreign, so a matching basename elsewhere can still never authorize a kill.
 */
function isKirocrewCommand(commandLine, options) {
  return kirocrewCommandShape(commandLine, options) !== null;
}

/**
 * `isKirocrewCommand`, narrowed to a Kiro Crew SERVER: argparse's first
 * positional after the executable (or after the console script) must be one of
 * `KIROCREW_SERVER_SUBCOMMANDS`, exactly as the Python twin
 * `port_resolution._args_look_like_kirocrew` gates its console-script form.
 * `isKirocrewCommand` alone accepts any `kirocrew` executable, which also
 * matches the built-in MCP servers the gateway spawns as `<root>/bin/kirocrew
 * mcp-core` / `mcp-cron`; the survivor sweep must never signal those.
 */
function isKirocrewGatewayCommand(commandLine, options) {
  const shape = kirocrewCommandShape(commandLine, options);
  return shape !== null && shape.serverSubcommand;
}

/**
 * Classify a command line: `null` when it is not a Kiro Crew command, else
 * `{ serverSubcommand }` -- whether argparse's first positional selects a
 * server subcommand. The `-m <module>` shape only ever matches WITH that
 * subcommand, so it is always `true` there.
 */
function kirocrewCommandShape(
  commandLine,
  { trustedExecutablePaths = [], canonicalizePath = () => "" } = {}
) {
  const tokens = commandLineTokens(commandLine);
  if (!tokens.length) return null;

  const windowsExecutablePath = normalizedWindowsAbsolutePath(tokens[0]);
  if (windowsExecutablePath) {
    const canonical = (candidate) => {
      try {
        return normalizedWindowsAbsolutePath(canonicalizePath(candidate));
      } catch {
        return "";
      }
    };
    const trusted = new Set();
    for (const candidate of trustedExecutablePaths) {
      const normalized = normalizedWindowsAbsolutePath(candidate);
      if (!normalized) continue;
      trusted.add(normalized);
      const resolved = canonical(normalized);
      if (resolved) trusted.add(resolved);
    }
    const observed = [windowsExecutablePath, canonical(windowsExecutablePath)];
    if (!observed.some((candidate) => candidate && trusted.has(candidate))) return null;
  }

  const selector = windowsExecutablePath
    ? { name: executableName(tokens[0]), next: 1 }
    : executableSelector(tokens);
  let index = selector.next;
  // Windows process identity prefixes ExecutablePath to the OS command line.
  // Skip that exact duplicate without skipping a Python-named script argument.
  if (normalizedWindowsPath(tokens[index]) === normalizedWindowsPath(tokens[0])) {
    index += 1;
  }
  if (KIROCREW_EXE_NAMES.has(selector.name)) {
    return { serverSubcommand: KIROCREW_SERVER_SUBCOMMANDS.has(tokens[index]) };
  }
  if (!PYTHON_EXE_RE.test(selector.name)) return null;

  while (index < tokens.length) {
    const token = tokens[index];
    if (token === "-m") {
      // Module AND server subcommand, both in their fixed argparse slots. The
      // subcommand is the first positional after the module, so only that slot
      // is read: a later argument (`-m kiro_crew run gateway`) never qualifies.
      const isGatewayModule = KIROCREW_MODULE_RE.test(tokens[index + 1] || "")
        && KIROCREW_SERVER_SUBCOMMANDS.has(tokens[index + 2]);
      return isGatewayModule ? { serverSubcommand: true } : null;
    }
    if (token === "-c" || token === "-") return null;
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
  if (!(/[\\/]/.test(script || "") && KIROCREW_EXE_NAMES.has(executableName(script)))) return null;
  return { serverSubcommand: KIROCREW_SERVER_SUBCOMMANDS.has(tokens[index + 1]) };
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

function pidIsAlive(pid, signalPidFn) {
  try { signalPidFn(pid, 0); return true; } catch { return false; }
}

// JS-side bound on one survivor identity read. `execFile`'s own timeout only
// signals ps; a ps stuck in an uninterruptible read never exits, and an
// unbounded await here would hold the quit / auto-update caller forever. An
// identity that cannot be read in time resolves to "" -- not a gateway, so no
// signal is sent (fail closed).
const SURVIVOR_COMMAND_READ_MS = 2000;

function readCommandBounded(getCommandFn, pid, timeoutMs = SURVIVOR_COMMAND_READ_MS) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(""), timeoutMs);
    Promise.resolve()
      .then(() => getCommandFn(pid))
      .then((command) => { clearTimeout(timer); resolve(String(command || "")); })
      .catch(() => { clearTimeout(timer); resolve(""); });
  });
}

/**
 * After the child exits, stop any descendant listed before its SIGTERM that is
 * still alive and still a gateway -- the gateway a launcher shim forked. It
 * gets SIGTERM (its own handler flushes state), then SIGKILL once BOTH
 * `deadline` has passed AND `graceMs` has elapsed since the last SIGTERM was
 * sent. Identity is re-read right before each signal, so a pid that exited and
 * was reused by an unrelated process is never signalled. The identity test is
 * the SERVER one (`isKirocrewGatewayCommand`): a `kirocrew mcp-core` /
 * `mcp-cron` descendant is a Kiro Crew executable too, but it belongs to the
 * backend's own orphan sweep, not to this one.
 */
async function stopForkedGatewaySurvivors(pids, { deadline, graceMs, getCommandFn, signalPidFn, pollMs }) {
  const stillGateway = async (pid) => {
    if (!pidIsAlive(pid, signalPidFn)) return false;
    return isKirocrewGatewayCommand(await readCommandBounded(getCommandFn, pid));
  };
  // Validate and signal each pid in the same step: no other pid's identity
  // read (a /bin/ps spawn) may sit between this pid's check and its signal.
  let survivors = [];
  for (const pid of pids) {
    if (!(await stillGateway(pid))) continue;
    try { signalPidFn(pid, "SIGTERM"); survivors.push(pid); } catch {}
  }
  if (!survivors.length) return;
  // The grace window is measured from the SIGTERM, not from entry: each
  // identity read above may take up to SURVIVOR_COMMAND_READ_MS, and a
  // deadline fixed before the loop would charge that read time against the
  // gateway's cooperative shutdown.
  const killAt = Math.max(deadline, Date.now() + graceMs);
  while (Date.now() < killAt) {
    survivors = survivors.filter((pid) => pidIsAlive(pid, signalPidFn));
    if (!survivors.length) return;
    await new Promise((r) => { setTimeout(r, pollMs); });
  }
  for (const pid of survivors) {
    if (await stillGateway(pid)) { try { signalPidFn(pid, "SIGKILL"); } catch {} }
  }
}

/**
 * Stop the gateway child gracefully and await its exit.
 *   1. POST /api/shutdown (clean flush + self-exit)
 *   2. the endpoint didn't take (older gateway / unreachable / wedged loop):
 *      - POSIX:   SIGTERM, then SIGKILL if it still hasn't exited by timeoutMs
 *      - Windows: a TREE kill (see below) — there is no step 3 to escalate to
 * Resolves once the process is fully gone — callers (quit / auto-update) rely
 * on the exit having completed before proceeding.
 *
 * WHY WINDOWS TAKES A DIFFERENT STEP 2. The POSIX escalation assumes signals:
 * SIGTERM reaches the gateway's own handler, which flushes sessions, memory and
 * cron and reaps its kiro-cli / MCP / app-server children before exiting, and
 * SIGKILL is a genuinely stronger follow-up for a child that ignored it. Neither
 * holds on Windows: Node maps BOTH signal names onto TerminateProcess, so no
 * handler runs, nothing is flushed, and the SIGKILL step is unreachable because
 * the SIGTERM already hard-terminated the pid. What survives is every
 * DESCENDANT, reparented and still holding the data home's locks and the same
 * .local_secret — and because the port frees, the caller's verification reports
 * success while those orphans race the replacement gateway. Windows has no
 * process group a single kill can reach, so the tree kill (`taskkill /T /F`) is
 * the only correct step-2 there. This mirrors the backend, which routes its own
 * stop path through platform_compat.kill_process_tree for exactly this reason.
 *
 * WHY POSIX LISTS THE TREE. The child may be a launcher shim that FORKED the
 * real gateway instead of exec'ing it (a package manager can install the
 * command that way). SIGTERM then reaches only the shim: it dies, its 'exit'
 * fires, and the gateway lives on re-parented to init, holding the port and
 * the lock. So, when the caller supplies the probes, the tree is listed BEFORE
 * the signal re-parents it. SIGTERM still goes to the child alone -- a real
 * gateway reaps its own children on SIGTERM, so the graceful path is
 * unchanged. Once the child is gone, any listed descendant that is still alive
 * AND still reads as a gateway command is the forked gateway: it gets its own
 * SIGTERM, then SIGKILL at the deadline. Other leftovers (kiro-cli, MCP) are
 * not touched here; the backend's own orphan sweep owns them. The SIGKILL
 * escalation of a child that outlived the deadline goes to the child alone:
 * only the identity-checked survivor sweep ever signals a listed pid.
 *
 * @param {import("child_process").ChildProcess} proc
 * @param {object} opts
 * @param {string} [opts.platform] process.platform override (tests)
 * @param {(pid:number) => Promise<void>} [opts.killTreeFn] tree-kill used on
 *   win32. Injected because the real one shells out to taskkill. The single-pid
 *   kill is the floor: a caller that supplies none keeps it, and a tree kill that
 *   rejects degrades to it. The caller MUST bound the tree kill's own timeouts to
 *   fit inside this function's deadline — it is awaited, never pre-empted, since
 *   cutting it short would kill the parent alone and orphan the very descendants
 *   it exists to reap.
 * @param {(pid:number) => Promise<number[]>} [opts.listDescendantsFn] POSIX:
 *   every live descendant of pid, deepest first. With getCommandFn, turns on
 *   the shim handling above; without either, POSIX signals the child alone.
 * @param {(pid:number) => Promise<string>} [opts.getCommandFn] POSIX: a pid's
 *   command line, used to confirm a survivor is a gateway before signalling it.
 * @param {(pid:number, signal:string|number) => void} [opts.signalPidFn]
 *   process.kill, injectable for tests.
 * @param {number} [opts.survivorGraceMs] POSIX: the least time a forked
 *   gateway gets between its own SIGTERM and SIGKILL. The sweep runs only after
 *   the child is gone, so by then the child's own deadline may already be spent
 *   -- a shim that ignored SIGTERM dies only to the SIGKILL at timeoutMs, and
 *   without this floor its gateway would be SIGKILLed the moment it was
 *   SIGTERMed. Default 10000 matches the backend's own cooperative budget
 *   (`gateway_shutdown_budget.GRACEFUL_SHUTDOWN_SECS`). Worst case this adds
 *   that much to the stop: only when a shim outlived timeoutMs AND its gateway
 *   ignores SIGTERM -- the case where the alternative is a lost flush.
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
    platform = process.platform,
    killTreeFn = null,
    listDescendantsFn = null,
    getCommandFn = null,
    signalPidFn = (pid, signal) => process.kill(pid, signal),
    survivorPollMs = 200,
    survivorGraceMs = 10000,
    termGraceMs = survivorGraceMs,
  } = {}
) {
  if (!proc || proc.exitCode !== null) return;
  const useTreeKill = platform === "win32" && typeof killTreeFn === "function";
  const listsTree = platform !== "win32"
    && typeof listDescendantsFn === "function"
    && typeof getCommandFn === "function";
  const deadline = Date.now() + timeoutMs;
  // The tree as it stood just before SIGTERM, and the listing in flight. Kept
  // outside the executor so the survivor sweep below can read them.
  let termSnapshot = [];
  let listingInFlight = null;
  // True between "decided to SIGTERM" and "SIGTERM sent" while the tree is listed.
  let termPending = false;
  // In-flight tree kill, awaited before this function reports the gateway gone.
  // taskkill /T terminates the PARENT first and then walks the rest of the tree, so
  // the process 'exit' event fires while descendants are still being reaped —
  // resolving on that alone would hand the auto-update caller a green light
  // mid-sweep and let it swap the app's files with children still live on the data
  // home's locks. Tracked outside the executor so the await below can see it.
  let treeKillInFlight = null;
  await new Promise((resolve) => {
    let settled = false;
    const done = () => { if (!settled) { settled = true; resolve(); } };
    proc.once("exit", done);
    if (proc.exitCode !== null) return done();
    // Kill with the widest scope available; the single-pid kill is the floor.
    //
    // The tree kill is allowed to run to completion rather than being pre-empted
    // on a timer: cutting it short would kill the PARENT alone and orphan exactly
    // the descendants it exists to reap, which is the bug, not a mitigation. What
    // keeps that safe is the CALLER passing timeouts whose sum fits inside this
    // function's deadline (see main.js) — otherwise the hard timer would resolve
    // while the kill was still in flight and the auto-update caller would swap the
    // app's files underneath a live gateway.
    //
    // A REJECTION still falls back to the single-pid kill. windowsTaskkill fails
    // closed by design when its identity probe cannot run or the pid was recycled,
    // and a swallowed rejection would leave no kill attempted at all — strictly
    // worse than what it replaced. Losing the descendants is a leak; losing the
    // parent as well is corruption, so the floor is unconditional.
    const killWith = (signal) => {
      const killPid = () => {
        if (proc.exitCode === null) { try { proc.kill(signal); } catch {} }
      };
      if (useTreeKill) { treeKillInFlight = killTreeFn(proc.pid).catch(killPid); return; }
      if (!listsTree || !proc.pid || signal !== "SIGTERM") { killPid(); return; }
      // List BEFORE signalling: the signal re-parents the tree to init.
      termPending = true;
      listingInFlight = Promise.resolve()
        .then(() => listDescendantsFn(proc.pid))
        .catch(() => [])
        .then((pids) => {
          termSnapshot = Array.isArray(pids) ? pids : [];
          termPending = false;
          killPid();
          // The listing ran before SIGTERM, so it ate into a budget that was armed
          // at entry. Give the child its full grace window from ITS SIGTERM.
          armEscalation(Math.max(deadline, Date.now() + termGraceMs));
        });
    };
    // Escalate at timeoutMs but DON'T resolve here — wait for the real 'exit' so
    // callers are guaranteed the process is gone (and signalCode is accurate). A
    // hard safety net resolves even if 'exit' never fires.
    let killTimer = null;
    let hardTimer = null;
    function armEscalation(killAt) {
      clearTimeout(killTimer);
      clearTimeout(hardTimer);
      if (settled) return;
      const delay = Math.max(0, killAt - Date.now());
      killTimer = setTimeout(() => {
        // SIGTERM has not gone out yet: the listing re-arms this once it has.
        if (termPending) return;
        if (proc.exitCode === null) {
          // On Windows the first kill was already terminal, so the only thing left
          // to add is SCOPE: sweep the tree in case descendants outlived it.
          killWith("SIGKILL");
        }
      }, delay);
      hardTimer = setTimeout(done, delay + 3000);
    }
    armEscalation(deadline);
    proc.once("exit", () => { clearTimeout(killTimer); clearTimeout(hardTimer); });
    // Prefer the clean endpoint; kill only if it didn't take.
    postShutdownFn({ backendUrl, kirocrewHome, secrets, httpMod, fsMod, pathMod }).then((ok) => {
      if (ok || proc.exitCode !== null) return;
      killWith("SIGTERM");
    });
  });
  // The parent is gone (or the deadline expired). Now let the tree sweep finish, so
  // "the gateway is stopped" covers its descendants too -- taskkill /T kills the
  // parent FIRST, so the 'exit' above fires mid-sweep.
  //
  // Bounded, not open-ended: a tree kill that never settles (a wedged probe, a
  // hung taskkill) must not hold the caller forever, because every caller of this
  // function is on a shutdown path with somewhere to be. The caller sizes the tree
  // kill's own timeouts to fit inside timeoutMs (see main.js), so in practice this
  // adds only the sweep's remaining tail; the race is the backstop for when that
  // sizing is wrong.
  if (listingInFlight) {
    await Promise.race([listingInFlight, new Promise((r) => { setTimeout(r, timeoutMs); })]);
  }
  if (termSnapshot.length) {
    // The survivor's own grace window starts at ITS SIGTERM, not the child's.
    await stopForkedGatewaySurvivors(termSnapshot, {
      deadline, graceMs: survivorGraceMs,
      getCommandFn, signalPidFn, pollMs: survivorPollMs,
    });
  }
  if (treeKillInFlight) {
    await Promise.race([
      treeKillInFlight,
      // Deliberately NOT unref()'d. This function is awaited by its caller on a
      // shutdown path (quit / auto-update), so a live timer here is exactly what
      // should hold the process open until the backstop fires or the tree kill
      // settles. An unref'd timer cannot keep the event loop alive on its own;
      // once nothing else is pending (the common case in a test process, and any
      // real caller once this is the last outstanding thing) the loop drains and
      // resolves the surrounding Promise.race before the backstop ever runs.
      new Promise((r) => { setTimeout(r, timeoutMs); }),
    ]);
  }
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
 * Is anything holding the LISTEN socket on `port`?
 *
 * Most callers need only that, not the holder's identity: they are waiting for a
 * port to clear, or watching for a service manager to rebind one. The kernel's
 * pid list answers the question on its own, so this reads no process command
 * line and consults no identity predicate. A same-user process is therefore
 * unable to move the verdict by choosing what argv it presents, because nothing
 * here looks at argv.
 *
 *   "bound"   — at least one local pid holds the LISTEN socket.
 *   "free"    — nothing is listening locally.
 *   "unknown" — the probe itself could not run (no lsof / EACCES). Distinct from
 *               "free" so a caller can refuse to act on a port it cannot see,
 *               exactly as classifyPortOwner's own "unknown" does.
 *
 * @param {number} port
 * @param {object} deps
 * @param {(port:number)=>Promise<number[]>} deps.getListenPids  lsof -t
 * @returns {Promise<"bound"|"free"|"unknown">}
 */
async function probePortBinding(port, { getListenPids, log = () => {} }) {
  let pids;
  try {
    pids = await getListenPids(port);
  } catch (e) {
    log(`port-binding: could not probe :${port} (${e && e.message}) — binding unknown`);
    return "unknown";
  }
  if (!pids.length) {
    log(`port-binding: :${port} is free`);
    return "free";
  }
  log(`port-binding: :${port} is held by pid ${pids.join(", ")}`);
  return "bound";
}

/**
 * Classify who LOCALLY owns the LISTEN socket on `port`.
 *
 * Callers that only need to know whether the port is occupied must use
 * probePortBinding instead: the identity judgement below rests on the holder's
 * own command line, which a same-user process controls, so spending it on a
 * question the pid list already answers widens that weakness for no gain.
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
  probePortBinding,
  isServiceManaged,
  isKirocrewCommand,
  isKirocrewGatewayCommand,
  INIT_PPID,
};
