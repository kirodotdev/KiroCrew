"use strict";
//
// Hang self-healing for the main dashboard window.
//
// The problem this solves: `renderer-recovery.js` heals a renderer that DIES
// (`render-process-gone` → bounded reload), but a renderer that HANGS emits
// `unresponsive` instead — and the main window never listened for it. A wedged
// renderer therefore left the window mapped, painted with its last frame, and
// dead to all input, while the main process stayed healthy (timers, liveness
// probes and update checks kept running), so nothing anywhere noticed.
// Observed in the wild on 2026-09-03: the renderer went silent during a macOS
// sleep dark-wake, the main process ran on for 5+ hours, and the user's only
// way out of the frozen full-screen window was a power-button reboot
// (issue #8264).
//
// The design converts the hang into the failure mode the app already heals:
// after a grace period, `forcefullyCrashRenderer()` turns the hang into a
// `render-process-gone` with reason `crashed`, which renderer-recovery's
// bounded reload path picks up. The grace period exists because `unresponsive`
// also fires for transient stalls (a long GC pause, post-wake CPU churn); a
// `responsive` event within the grace window cancels the kill so a merely-slow
// page keeps its state instead of losing it to a reload.
//
// No second retry budget lives here, deliberately. The loop terminates through
// renderer-recovery's own bound: a force-crash produces either a bounded
// reload (at most 3 per minute) or a give-up, and a dead, given-up renderer
// emits no further `unresponsive` events — so this module can never fire
// unboundedly on its own.
//
// Pure logic + injected dependencies: Electron main is not exercised by the
// unit test runner, so the decisions have to be testable without a live
// BrowserWindow (same pattern as renderer-recovery.js / perf-metrics.js).
//

/** How long a hung renderer gets to come back before it is force-crashed.
 *  Electron only emits `unresponsive` after the renderer has already been
 *  stuck for its internal responsiveness deadline, so this is additional
 *  slack on top of that, not the total. */
const DEFAULT_GRACE_MS = 15_000;

/**
 * Create the hang-recovery coordinator.
 *
 * @param {object} deps
 * @param {() => void} deps.forceCrash      Kill the wedged renderer
 *   (`webContents.forcefullyCrashRenderer()`), handing the failure to the
 *   existing `render-process-gone` recovery path.
 * @param {() => boolean} [deps.isQuitting] True while the app is shutting
 *   down; a teardown-time hang must not race the quit with a forced crash.
 * @param {(msg: string) => void} [deps.log]
 * @param {(fn: () => void, ms: number) => any} [deps.setTimer]
 * @param {(handle: any) => void} [deps.clearTimer]
 * @param {number} [deps.graceMs]
 * @returns {{
 *   handleUnresponsive: () => string,
 *   handleResponsive: () => string,
 *   handleGone: () => string,
 *   armed: boolean,
 * }}
 *   `handleUnresponsive` returns "armed" | "already-armed" | "ignored-quitting";
 *   `handleResponsive` returns "recovered" | "idle";
 *   `handleGone` returns "disarmed" | "idle".
 */
function createHangRecovery({
  forceCrash,
  isQuitting = () => false,
  log = () => {},
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (handle) => clearTimeout(handle),
  graceMs = DEFAULT_GRACE_MS,
} = {}) {
  if (typeof forceCrash !== "function") {
    throw new Error("createHangRecovery: forceCrash is required");
  }
  const grace = Math.max(1, Number(graceMs) || DEFAULT_GRACE_MS);
  let timer = null;

  function expire() {
    timer = null;
    // Re-checked at expiry, not just at arming: a quit can begin during the
    // grace window, and crashing the renderer mid-quit would fight teardown.
    if (isQuitting()) {
      log("renderer still unresponsive at grace expiry, but app is quitting — not crashing it");
      return;
    }
    log(
      `renderer unresponsive for ${grace}ms — force-crashing it so ` +
        `render-process-gone recovery can reload the dashboard`,
    );
    try {
      forceCrash();
    } catch (e) {
      // Never let a failed kill escape into Electron's event emitter.
      log(`force-crash of hung renderer failed: ${e && e.message}`);
    }
  }

  function handleUnresponsive() {
    if (isQuitting()) {
      log("renderer unresponsive during quit — not recovering");
      return "ignored-quitting";
    }
    // Electron may re-emit while a page stays hung; one pending kill is enough.
    if (timer !== null) return "already-armed";
    log(`renderer unresponsive — force-crash in ${grace}ms unless it recovers`);
    timer = setTimer(expire, grace);
    return "armed";
  }

  function handleResponsive() {
    if (timer === null) return "idle";
    clearTimer(timer);
    timer = null;
    log("renderer responsive again within the grace window — hang recovery disarmed");
    return "recovered";
  }

  function handleGone() {
    // The renderer died on its own (or was killed) while the grace timer was
    // armed. The timer must not survive it: `render-process-gone` reloads the
    // dashboard into the SAME WebContents, and a fresh renderer never emits
    // `responsive` (it only pairs with an `unresponsive` from the same
    // process), so a stale expiry would force-crash the healthy replacement.
    if (timer === null) return "idle";
    clearTimer(timer);
    timer = null;
    log("renderer gone while hang recovery was armed — disarming the pending force-crash");
    return "disarmed";
  }

  return {
    handleUnresponsive,
    handleResponsive,
    handleGone,
    get armed() {
      return timer !== null;
    },
  };
}

/**
 * End a hung renderer so `render-process-gone` recovery can reload it.
 *
 * Outside Linux, `forcefullyCrashRenderer()` first has Crashpad dump the hung
 * process from inside the main process. When the app runs as its x86_64 slice
 * under Rosetta, that dump cannot read the renderer's threads (Crashpad logs
 * `thread_get_state(4): (os/kern) invalid argument`) and the main process then
 * aborts with EXC_BREAKPOINT, so the whole app exits instead of reloading.
 * Under translation the renderer is killed by pid instead: Electron reports
 * the death as reason `killed`, which renderer-recovery reloads like a crash.
 *
 * @param {{forcefullyCrashRenderer: () => void, getOSProcessId: () => number}} webContents
 * @param {object} [o]
 * @param {boolean} [o.underTranslation] `app.runningUnderARM64Translation`
 * @param {(pid: number, signal: string) => void} [o.kill]
 * @returns {"force-crashed" | "killed" | "no-process"}
 */
function endHungRenderer(webContents, { underTranslation = false, kill = process.kill } = {}) {
  if (!underTranslation) {
    webContents.forcefullyCrashRenderer();
    return "force-crashed";
  }
  const pid = webContents.getOSProcessId();
  if (!(pid > 0)) return "no-process";
  kill(pid, "SIGKILL");
  return "killed";
}

module.exports = {
  createHangRecovery,
  endHungRenderer,
  DEFAULT_GRACE_MS,
};
