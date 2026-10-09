"use strict";
const path = require("path");
//
// Injectable recovery-strategy decisions, extracted from main.js so they can
// be unit-tested without Electron (mirrors gateway-liveness.js / gateway-wait.js
// / gateway-stop.js). Most exports are pure; revealWindowForConnect drives an
// injected window and is testable with a fake.
//
// PROBLEM: the post-handoff liveness monitor fires onUnresponsive whenever
// /api/status stops answering. main.js used to react ONE way — assume a wedged
// local backend and force-kill the port + respawn. That is correct only when WE
// spawned the gateway. On the reuse path the port-holder is someone else's
// process, and in the remote-tunnel setup (localhost:<port> is an SSH forward to
// a remote gateway) an unresponsive probe almost always means the SSH tunnel
// dropped (lid close, Wi-Fi<->Ethernet handoff, VPN blip), not a wedged backend.
// Killing the port would tear down the tunnel; the old code then fell through to
// a terminal error dialog that QUIT the app on any button — including Retry (the
// perceived "crash on Retry" on network change).
//
// This helper is the single source of truth for that fork: given how the
// gateway was obtained (the launcher's single ownership state), return which
// recovery strategy to run. Keeping it pure means the ownership rule is covered
// by tests independently of the Electron plumbing.

// The launcher's gateway-ownership vocabulary (main.js keeps exactly one of
// these in a single module-level state; see GATEWAY_OWNERSHIP_STATES):
//   "none"           — no gateway yet, or an adopted holder that could NOT be
//                      positively identified as local (tunnel / external).
//   "spawned"        — this app spawned the bundled backend on this port.
//   "reused-local"   — adopted a local same-family Kiro Crew process.
//   "reused-service" — like reused-local, but the holder was SERVICE-classified.
const GATEWAY_OWNERSHIP_STATES = Object.freeze(["none", "spawned", "reused-local", "reused-service"]);

/**
 * Classify an adopted (reuse-path) gateway into the ownership vocabulary.
 * Positive identification requires BOTH a same-family health answer and a
 * local LISTEN owner ("kirocrew"/"service"); anything less (tunnel, no visible
 * owner, probe failure) stays "none" — the never-kill/never-respawn external
 * classification. Pure so the classification rule is unit-testable without
 * Electron.
 *
 * @param {object} o
 * @param {string} o.reason      the reuse decision's reason (from
 *                               decideGatewayAction); only "same-family" is a
 *                               positive family identification.
 * @param {string} o.localOwner  the LISTEN-owner classification for the port
 *                               ("kirocrew" | "service" | "other" | "none" | …).
 * @returns {"reused-service" | "reused-local" | "none"}
 */
function classifyAdoptedGateway({ reason, localOwner }) {
  const local = reason === "same-family" && (localOwner === "kirocrew" || localOwner === "service");
  if (!local) return "none";
  return localOwner === "service" ? "reused-service" : "reused-local";
}

/**
 * Decide how to recover an unresponsive gateway.
 *
 * @param {object} o
 * @param {string} o.gatewayOwnership  one of GATEWAY_OWNERSHIP_STATES: how the
 *                                     gateway on this port was obtained.
 *                                     "spawned" is the only owned state; the
 *                                     reused-* states are adopted local
 *                                     gateways; "none" (or anything
 *                                     unrecognized) is external/unknown.
 * @returns {"respawn" | "reconnect-bounded" | "reconnect"}
 *   "respawn"   — we own the child: kill the wedged tree, free the port, spawn a
 *                 fresh backend, re-run the boot flow.
 *   "reconnect-bounded" — we adopted a LOCAL same-family gateway we do not own.
 *                 Never kill it, but its death is not a tunnel blip: nothing
 *                 will ever bring it back, so wait a BOUNDED interval for it to
 *                 recover, then (once the port clears on its own) spawn our own
 *                 backend. Waiting forever here is the dead-window
 *                 bug: the shell classified a dead local gateway as "a gateway
 *                 we did not spawn (remote tunnel)" and never respawned.
 *   "reconnect" — genuinely external holder (tunnel / unidentified): never kill
 *                 it or spawn locally; re-probe until the external gateway /
 *                 tunnel heals, then reconnect (re-fetching a token, since the
 *                 drop likely invalidated the old one).
 */
function chooseRecoveryStrategy({ gatewayOwnership }) {
  if (gatewayOwnership === "spawned") return "respawn";
  if (gatewayOwnership === "reused-local" || gatewayOwnership === "reused-service") return "reconnect-bounded";
  // "none", undefined, or anything unrecognized: ownership defaults to "not
  // ours" — the safe strategy is the non-destructive reconnect, never a
  // port-kill.
  return "reconnect";
}

// launchd throttles KeepAlive respawns to ~10s between starts; systemd's
// default RestartSec is far shorter. 15s covers both with margin, and the
// cost of over-waiting lands only on the orphan case (a one-time delay
// before we spawn), never on a live rebind (we adopt the instant it binds).
const SERVICE_REBIND_GRACE_MS = 15_000;

/**
 * After a SERVICE-classified port-holder releases the port, the OS service
 * manager (launchd KeepAlive / systemd Restart=) may be about to respawn it:
 * at the moment the socket closes, a transient release during a service
 * restart is indistinguishable from a permanent exit. Spawning immediately
 * races the manager for the port — one side loses with EADDRINUSE.
 *
 * But "service-classified" does not guarantee a manager will respawn it: an
 * orphaned gateway reparents to init (PPID 1) and classifies as
 * service-managed too, and an orphan has no KeepAlive — nothing will ever
 * rebind. So: wait a bounded grace for a rebind, report "rebound" the moment
 * something takes the port back (the caller adopts/reconnects to it), and
 * report "spawn" only when the grace expires with the port still free.
 *
 * Pure/injectable so the decision is unit-testable without Electron.
 *
 * @param {object} o
 * @param {() => Promise<boolean>} o.isPortBound  does the port have a LISTEN owner again?
 * @param {(ms:number) => Promise<void>} o.sleep
 * @param {number} [o.graceMs]
 * @param {number} [o.pollMs]
 * @returns {Promise<"rebound" | "spawn">}
 */
async function waitForServiceRebind({ isPortBound, sleep, graceMs = SERVICE_REBIND_GRACE_MS, pollMs = 500 }) {
  const deadline = Date.now() + graceMs;
  for (;;) {
    if (await isPortBound()) return "rebound";
    if (Date.now() >= deadline) return "spawn";
    await sleep(pollMs);
  }
}

// A graceful gateway stop releases the LISTEN socket EARLY — the process keeps
// running to drain in-flight turns and flush session files, and it holds the
// exclusive gateway.lock flock for its full lifetime. So "port is free" does
// NOT mean "safe to spawn": a replacement started in that window is refused by
// the singleton lock and exits, then the incumbent exits — leaving no gateway
// at all. This helper waits (bounded) for the captured incumbent pids to
// actually die; the kernel releases the flock atomically on process exit.
// Pure/injectable for unit tests. 15s comfortably covers the backend's
// graceful-stop budget (drain ≤5s + flush) after the port has already cleared.
const INCUMBENT_EXIT_GRACE_MS = 15_000;

async function waitForProcessExit({ pids, isAlive, sleep, timeoutMs = INCUMBENT_EXIT_GRACE_MS, pollMs = 250 }) {
  const watched = (pids || []).filter((p) => Number.isInteger(p) && p > 0);
  if (!watched.length) return "exited";
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (!watched.some((p) => isAlive(p))) return "exited";
    if (Date.now() >= deadline) return "timeout";
    await sleep(pollMs);
  }
}

// Capture listener PIDs while the port is still bound. The draining process may
// release its socket before dropping gateway.lock, so recovery needs its PID to
// wait for process exit rather than treating "port free" as "lock free". A null
// result means the snapshot is unsafe: the probe failed or the listener vanished
// before it could be captured.
async function snapshotPortPids({
  port,
  isWindows = false,
  getWindowsPids,
  getPosixPids,
}) {
  const getListenPids = isWindows ? getWindowsPids : getPosixPids;
  if (typeof getListenPids !== "function") return null;
  try {
    const pids = await getListenPids(port);
    return Array.isArray(pids) && pids.length ? pids : null;
  } catch {
    return null;
  }
}

/**
 * Does an unusable incumbent snapshot have to stop an automatic respawn?
 *
 * A `null` snapshot means the listener probe could not name the draining
 * process. On Windows that is anomalous — netstat ships in-box — so recovery
 * fails closed rather than respawning into the incumbent's `gateway.lock`. On
 * POSIX a missing or blocked `lsof` degrades to a no-op wait, and the
 * replacement's own lock refusal is an honest arbiter, so a host without `lsof`
 * must still be able to boot.
 */
function incumbentSnapshotBlocksRespawn({ pids, isWindows = false }) {
  return pids === null && isWindows;
}

// When the lock probe reports the lock is HELD but its owner is unnameable (a
// draining gateway whose pid is unreadable under a Windows mandatory lock), the
// recovery waits for the lock to be released rather than giving up. A draining
// gateway finishes teardown in seconds to low minutes; this budget covers that
// while still bounding the wait, so a gateway genuinely stuck holding the lock
// surfaces the terminal dialog instead of waiting forever.
const INCUMBENT_LOCK_HELD_BUDGET_MS = 120_000;
const INCUMBENT_LOCK_HELD_POLL_MS = 2_000;

/**
 * Name the incumbent gateway so recovery can wait for it to exit before
 * respawning — first from a single LISTEN-socket lookup, then, when that cannot
 * name it, from a SOCKET-INDEPENDENT lock-holder probe.
 *
 * One socket lookup is taken. When it names the incumbent, that is used. When
 * it names no one — either a shutting-down gateway caught mid-exit, or a
 * draining gateway that has released its socket but still holds `gateway.lock`
 * (which `lsof`/`netstat` can never name, since they see only LISTEN-socket
 * owners) — the lock-holder probe is consulted straight away. The probe closes
 * both cases, so there is no socket-retry loop: retrying the socket would only
 * delay recovery without resolving anything the probe does not.
 *
 * The fallback is `lockHolderProbe` (the CLI's `gateway-pid`, resolved through
 * the backend's maintained `lock_holder` oracle), which reads the incumbent
 * from the lock file and outlives the socket. Its verdicts drive the outcome:
 *
 *  - `"captured"` — a live lock holder is named; the caller waits for THAT pid
 *    to exit (`waitForIncumbentExit`) before spawning. The kernel releases the
 *    lock atomically on exit, so this is race-free.
 *  - `"released"` — the lock itself (not merely the socket) is released; the
 *    incumbent is gone, so the caller may spawn now. This is LOCK-released, the
 *    authoritative signal, distinct from a free socket: a free port is never
 *    treated as a released lock.
 *  - `"held"` — the lock is POSITIVELY held but its owner cannot be named: a
 *    draining gateway whose pid is unreadable under a Windows mandatory lock.
 *    We cannot wait on a pid, but we CAN wait for the lock to be RELEASED — the
 *    same authoritative signal — so the probe is polled on a bounded budget
 *    until it reports "released" (then spawn) or the budget expires (then
 *    terminal). This is what lets recovery heal on a Windows host where the pid
 *    is unreadable.
 *  - `"unverified"` — the probe could not establish the lock's state at all, or
 *    a held lock never released within the budget. The one case that still
 *    surfaces the terminal dialog, because spawning blind would race the lock.
 *
 * With no `lockHolderProbe` injected a socket lookup that names no one yields
 * `"unverified"`.
 *
 * Pure/injectable so the whole policy is unit-testable without Electron.
 *
 * @param {object} o
 * @param {() => Promise<number[]|null>} o.snapshot      capture listener PIDs now
 * @param {(pids:number[]|null) => boolean} o.blocksRespawn  is this snapshot unusable?
 * @param {(ms:number) => Promise<void>} o.sleep
 * @param {() => Promise<{verdict:"captured"|"released"|"held"|"unverified", pid:number|null}>} [o.lockHolderProbe]
 *        socket-independent incumbent identity, consulted when the socket lookup names no one
 * @param {number} [o.lockHeldBudgetMs]  how long to wait for a held-but-unnameable lock to release
 * @param {number} [o.lockHeldPollMs]
 * @param {(message:string) => void} [o.log]
 * @returns {Promise<{verdict:"captured"|"released"|"unverified", pids:number[]|null, via:"snapshot"|"lock"}>}
 */
async function snapshotIncumbentForRespawn({
  snapshot,
  blocksRespawn,
  sleep,
  lockHolderProbe = null,
  lockHeldBudgetMs = INCUMBENT_LOCK_HELD_BUDGET_MS,
  lockHeldPollMs = INCUMBENT_LOCK_HELD_POLL_MS,
  log = () => {},
}) {
  // One socket lookup. When it names the incumbent, use it. There is no retry
  // loop: the lock probe below resolves BOTH the transient drain gap (a gateway
  // mid-exit) and the socket-shed case (socket released, lock still held), so a
  // socket retry would only delay recovery without closing any gap the probe
  // leaves.
  const pids = await snapshot();
  if (!blocksRespawn(pids)) {
    return { verdict: "captured", pids, via: "snapshot" };
  }
  // The listener snapshot named no one. A draining gateway that has shed its
  // socket but still holds the lock cannot be named by a socket probe at all,
  // so ask the lock itself, which outlives the socket.
  if (lockHolderProbe) {
    log(
      "the listener snapshot named no incumbent "
      + "— asking the gateway lock directly (it outlives the socket)",
    );
    const lockDeadline = Date.now() + lockHeldBudgetMs;
    for (;;) {
      const lock = await lockHolderProbe();
      if (lock.verdict === "captured") {
        // The lock named a live pid — wait for THAT process to exit.
        return { verdict: "captured", pids: [lock.pid], via: "lock" };
      }
      if (lock.verdict === "released") {
        // The lock is released: the incumbent has fully exited, spawn now.
        return { verdict: "released", pids: null, via: "lock" };
      }
      if (lock.verdict === "held") {
        // A live gateway holds the lock but cannot be named (Windows mandatory
        // lock hides the pid). We cannot wait on a pid, but we CAN wait for the
        // lock to be released — the authoritative "safe to spawn" signal — on a
        // bounded budget. A gateway stuck holding the lock past the budget is
        // the genuinely-unrecoverable case the terminal dialog is for.
        if (Date.now() >= lockDeadline) {
          log(
            `the gateway lock is still held after waiting ${lockHeldBudgetMs}ms `
            + "and its owner stayed unnameable — surfacing the terminal error",
          );
          return { verdict: "unverified", pids: null, via: "lock" };
        }
        log(
          "the gateway lock is held but its owner is unnameable (a draining gateway) "
          + `— waiting ${lockHeldPollMs}ms for it to release before spawning`,
        );
        await sleep(lockHeldPollMs);
        continue;
      }
      // Truly indeterminate: the probe could not establish the lock's state.
      return { verdict: "unverified", pids: null, via: "lock" };
    }
  }
  return { verdict: "unverified", pids: null, via: "snapshot" };
}

/**
 * Build the terminal recovery dialog without loading Electron.
 *
 * @param {object} o
 * @param {number|string} o.port
 * @param {"wedged"|"held"} [o.variant]
 * @param {boolean} [o.probeFailed]
 * @param {boolean} [o.isPrimaryWindow]
 * @returns {{
 *   title:string, message:string, portConflict:false,
 *   primaryAction:"quit", primaryLabel:string, showQuitButton:false
 * }}
 */
function unrecoverableGatewayDialog({
  port,
  variant = "wedged",
  probeFailed = false,
  isPrimaryWindow = false,
}) {
  const held = variant === "held";
  return {
    title: probeFailed
      ? `Kiro Crew: can't verify what's using port ${port}`
      : held
        ? `Kiro Crew: port ${port} is in use`
        : `Kiro Crew: backend stuck on port ${port}`,
    message: probeFailed
      ? `Kiro Crew could not verify which process owns port ${port}, so it did not `
        + `risk terminating an unrelated process or starting a second gateway. `
        + `Quit and reopen Kiro Crew to try again. If the port is still blocked, `
        + `restart your computer.`
      : held
        ? `Another process is holding port ${port}, so Kiro Crew can't reconnect to `
          + `it or start its own backend there. Quit the process using port ${port} `
          + `(the launch log below names it; look for the "port-owner:" line), `
          + `then reopen this app.`
        : `The Kiro Crew backend is wedged and cannot be stopped. It is in an `
          + `uninterruptible state and is still holding port ${port}, so it can't be `
          + `force-stopped or restarted in place. Restart your computer to clear it. `
          + `(This is a known backend hang; see the launch log below for the cause.)`,
    portConflict: false,
    primaryAction: "quit",
    primaryLabel: isPrimaryWindow ? "Quit Kiro Crew" : "Close",
    showQuitButton: false,
  };
}

/**
 * Reveal a window for a connect/loading flow, raising ONLY when the user asked
 * for it. Effectful (it drives the injected window) but injectable, so the
 * raise-vs-silent rule is unit-testable without Electron.
 *
 * Cold launch and user-initiated connects (app start, dialog Retry, a new
 * connection window) keep the historical raise-and-focus `show()`.
 *
 * Liveness-recovery reconnects touch NOTHING: the user did nothing, so the
 * app must not raise, focus, un-minimize, or re-surface itself (the
 * remote-tunnel setup reconnects on every screen lock/unlock — see #6373).
 * The splash and dashboard load fine into a background, minimized, or hidden
 * window; a window the user hid to tray stays hidden until THEY reopen it.
 * The escalation rule lives in main.js: silent while self-healing, but any
 * state that needs input (token prompt, failure dialog, the terminal
 * unrecoverable-gateway dialog) does a full reveal via revealForUserDecision
 * at the point it is reached.
 *
 * @param {object} win  BrowserWindow-like: show().
 * @param {object} [o]
 * @param {boolean} [o.reconnect=false]  true on liveness-recovery paths.
 */
function revealWindowForConnect(win, { reconnect = false } = {}) {
  if (reconnect) return;
  win.show();
}

// The gateway's own stale-asset watchdog exits with this status when the
// install directory it was started from has been replaced or pruned underneath
// it (EX_TEMPFAIL: "retry later"). Mirrors STALE_ASSET_EXIT_CODE in the
// backend's dashboard/stale_asset_watchdog.py; keep the two in sync.
const STALE_ASSET_EXIT_CODE = 75;

/**
 * Decide how the supervisor reacts to a bundled backend that vanished
 * underneath it on macOS or Windows.
 *
 * Two signals mean "the bundle we spawned from is gone": the gateway exited
 * with STALE_ASSET_EXIT_CODE (its watchdog saw its own assets disappear), or a
 * spawn of a binary that passed the executable probe moments earlier failed
 * ENOENT (the file was removed between probe and exec). Both happen when an
 * update replaces the .app in place while a session is running. Nothing inside
 * the process can learn where the new bundle is -- every Electron path API is
 * a launch-time snapshot -- so recovery is a fresh filesystem probe of the same
 * candidate list: when the bundle was swapped at the same path the new backend
 * is found there; when it was pruned the probe falls through to a user-level
 * install or a PATH lookup. (A packaged Windows app refuses that PATH lookup
 * before spawning anything -- see spawnGateway -- so on Windows a prune with
 * nothing reinstalled at the same path ends at the "installation still
 * finishing" dialog, whose probe re-runs this same candidate list and starts
 * the backend once one is back at that path.)
 *
 * The budget is one re-resolve per incident. A second stale signal in the same
 * incident means the probe found nothing usable, so the only remaining move is
 * to restart the whole app -- and only when the caller has verified there is
 * still something to restart. A pruned bundle takes this app's own executable
 * with it, so the caller probes that executable first and passes the result
 * in; when it is gone the verdict is "none" and the ordinary failure dialog is
 * shown instead of exiting the app into nothing. (The caller then also
 * confirms the fresh copy actually started before exiting, which closes the
 * window between the probe and the restart.)
 *
 * @param {object} o
 * @param {boolean} o.isMac             macOS swaps bundles under a running app.
 * @param {boolean} [o.isWindows=false]  so does Windows: an install manager that
 *                                      lays each version down side by side can
 *                                      prune the running version's directory
 *                                      without stopping the app. Every
 *                                      file the shell has mapped (its own
 *                                      executable, app.asar) survives, because a
 *                                      running image cannot be deleted there, and
 *                                      the backend tree goes. Linux stays out:
 *                                      its service manager restarts the gateway
 *                                      on exit 75.
 * @param {boolean} o.bundled           the child that failed was the bundled
 *                                      backend. A PATH or dev install that is
 *                                      missing or exits 75 has nothing stale to
 *                                      re-resolve, and a dev machine with no
 *                                      kirocrew at all must never relaunch.
 * @param {number|null} [o.exitCode]    the child's exit status, when it exited.
 * @param {string} [o.spawnErrorCode]   the spawn error's code, when spawn failed.
 * @param {number} [o.attempts=0]       re-resolves already spent on this incident.
 * @param {boolean} [o.quitting=false]  the app is shutting down.
 * @param {boolean} [o.installingUpdate=false] the updater owns the bundle right
 *                                      now; respawning would race the swap.
 * @param {boolean} [o.relaunchTargetExists=false] the caller re-probed its own
 *                                      executable (the one a restart re-runs)
 *                                      and found it. Defaults to false so a
 *                                      caller that never checked can only reach
 *                                      the dialog, never a blind exit.
 * @returns {"reresolve" | "relaunch" | "none"}
 *   "reresolve" — probe the candidate list again and respawn.
 *   "relaunch"  — the re-resolved child is stale too and the app can still be
 *                 re-run; restart the app.
 *   "none"      — not a stale-bundle signal, not ours to act on, or nothing
 *                 launchable remains; take the ordinary failure path.
 */
function shouldReresolveBackend({
  isMac,
  isWindows = false,
  bundled,
  exitCode = null,
  spawnErrorCode = "",
  attempts = 0,
  quitting = false,
  installingUpdate = false,
  relaunchTargetExists = false,
}) {
  if (!(isMac || isWindows) || quitting || installingUpdate) return "none";
  if (!isStaleBundleSignal({ exitCode, spawnErrorCode })) return "none";
  // Past the first attempt the child under judgment is whatever the re-probe
  // found (possibly not bundled), and the incident is already established.
  if (attempts >= 1) return relaunchTargetExists ? "relaunch" : "none";
  return bundled ? "reresolve" : "none";
}

/**
 * True when a child's fate says the bundle it came from is gone: the gateway
 * exited with STALE_ASSET_EXIT_CODE, or a binary that passed the executable
 * probe failed to spawn with ENOENT.
 *
 * @param {object} o
 * @param {number|null} [o.exitCode]
 * @param {string} [o.spawnErrorCode]
 */
function isStaleBundleSignal({ exitCode = null, spawnErrorCode = "" }) {
  return exitCode === STALE_ASSET_EXIT_CODE || spawnErrorCode === "ENOENT";
}

// Resolve how to invoke a `kirocrew` SUBCOMMAND through execFile, applying the
// same Windows unwrap the spawn path uses: Node's shell-free execFile refuses a
// `.cmd`/`.bat` (spawn EINVAL), so a bundled `bin\kirocrew.cmd` is replaced by
// the bundled `python.exe` one directory up, run with `-s -P -m kiro_crew`
// exactly as the spawn path does (`-s`/`-P` keep the user site and the spawn
// cwd off sys.path). Any other bin (a POSIX console script, a dev entry point)
// is called as-is. Pure: `pathMod` defaults to the host's `path`.
function gatewayCliInvocation(bin, subArgs, pathMod = path) {
  if (bin.endsWith("kirocrew.cmd")) {
    return {
      bin: pathMod.resolve(pathMod.dirname(bin), "..", "python.exe"),
      args: ["-s", "-P", "-m", "kiro_crew", ...subArgs],
    };
  }
  return { bin, args: subArgs };
}

module.exports = {
  gatewayCliInvocation,
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  revealWindowForConnect,
  shouldReresolveBackend,
  isStaleBundleSignal,
  STALE_ASSET_EXIT_CODE,
  GATEWAY_OWNERSHIP_STATES,
  waitForServiceRebind,
  waitForProcessExit,
  snapshotPortPids,
  incumbentSnapshotBlocksRespawn,
  snapshotIncumbentForRespawn,
  unrecoverableGatewayDialog,
  SERVICE_REBIND_GRACE_MS,
  INCUMBENT_EXIT_GRACE_MS,
};
