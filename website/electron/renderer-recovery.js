"use strict";
//
// Renderer-crash self-healing for the main dashboard window.
//
// The problem this solves: when the dashboard's renderer process dies, Electron
// leaves the window mapped but BLANK — and nothing brings it back. The user sees
// a permanently black window with no top tab strip (the whole SPA, tab bar
// included, lived in that renderer), and the only way out is quitting the app.
// Observed in the wild as a repeating daily crash: `EXC_BREAKPOINT` (a V8 fatal
// abort) raised on a DedicatedWorker thread after a long session, in a renderer
// hosting many remote-crew dashboard SPAs at once.
//
// Electron does emit `render-process-gone` for exactly this, but the app never
// listened, so a dead renderer was terminal. This module decides whether a given
// death warrants an automatic reload, and bounds how often that may happen.
//
// Why bounded rather than always-reload: a renderer that dies DURING startup
// (bad build, integrity failure, immediate OOM) would otherwise reload forever,
// spinning CPU and hiding the failure. After `maxAttempts` deaths inside
// `windowMs`, this stops and reports, so a genuinely broken build surfaces as a
// visible error instead of an invisible loop.
//
// Pure logic + injected dependencies: Electron main is not exercised by the unit
// test runner, so the decision has to be testable without a live BrowserWindow
// (same pattern as perf-metrics.js / token-retry.js).
//

// Reasons that mean "the renderer died unexpectedly and a reload may fix it".
// `clean-exit` is a normal teardown and must never trigger a reload.
const RECOVERABLE_REASONS = new Set([
  "crashed",
  "oom",
  "abnormal-exit",
  "launch-failed",
  "integrity-failure",
  // `killed` reaches us both when the OS reaps the process and when the app
  // itself is tearing down; the isQuitting gate below separates those.
  "killed",
]);

const DEFAULT_MAX_ATTEMPTS = 3;
const DEFAULT_WINDOW_MS = 60_000;

/**
 * Whether `reason` from `render-process-gone` describes a recoverable death.
 */
function isRecoverableReason(reason) {
  return RECOVERABLE_REASONS.has(String(reason || ""));
}

/**
 * Create the recovery coordinator.
 *
 * @param {object} deps
 * @param {() => void} deps.reload          Re-load the dashboard into the window.
 * @param {() => boolean} [deps.isQuitting] True while the app is shutting down.
 * @param {(msg: string) => void} [deps.log]
 * @param {() => string} [deps.describeProcesses] One-line process snapshot
 *   (CPU + working set) captured AT CRASH TIME. This is the whole reason a
 *   post-mortem can tell a memory problem from a CPU problem: macOS crash
 *   reports carry the thread that aborted but not what the process had grown to,
 *   and by the time a human looks the process is gone.
 * @param {(info: object) => void} [deps.onGiveUp] Called once the budget is spent.
 * @param {() => number} [deps.now]
 * @param {number} [deps.maxAttempts]
 * @param {number} [deps.windowMs]
 * @returns {{handleGone: (details: object) => string, attempts: number, reset: () => void}}
 *   `handleGone` returns the decision taken, one of:
 *   "reloaded" | "ignored-clean-exit" | "ignored-quitting" | "gave-up".
 */
function createRendererRecovery({
  reload,
  isQuitting = () => false,
  log = () => {},
  describeProcesses = () => "",
  onGiveUp = () => {},
  now = () => Date.now(),
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
  windowMs = DEFAULT_WINDOW_MS,
} = {}) {
  // Timestamps of recent recovery attempts, pruned to the sliding window so a
  // stable app that crashes once a day never exhausts its budget.
  let recent = [];
  const cap = Math.max(1, Number(maxAttempts) || DEFAULT_MAX_ATTEMPTS);
  const span = Math.max(1, Number(windowMs) || DEFAULT_WINDOW_MS);

  function handleGone(details = {}) {
    const reason = details.reason;
    // A deliberate teardown is not a crash: reloading here would fight the quit.
    if (isQuitting()) {
      log(`renderer gone during quit (reason=${reason}) — not recovering`);
      return "ignored-quitting";
    }
    if (!isRecoverableReason(reason)) {
      log(`renderer exited cleanly (reason=${reason}) — not recovering`);
      return "ignored-clean-exit";
    }

    const t = now();
    recent = recent.filter((ts) => t - ts < span);
    // Captured BEFORE the reload so it describes the crashed state, not the
    // recovered one. Never allowed to block recovery if the probe throws.
    let snapshot = "";
    try {
      snapshot = describeProcesses() || "";
    } catch (e) {
      snapshot = `snapshot failed: ${e && e.message}`;
    }
    const detail =
      `reason=${reason}, exitCode=${details.exitCode}` +
      (snapshot ? `, ${snapshot}` : "");

    if (recent.length >= cap) {
      log(
        `renderer died ${recent.length + 1}x within ${span}ms (${detail}) — ` +
          `giving up to avoid a reload loop`
      );
      onGiveUp({ reason, exitCode: details.exitCode, attempts: recent.length, snapshot });
      return "gave-up";
    }

    recent.push(t);
    log(`renderer died (${detail}) — reloading dashboard (attempt ${recent.length}/${cap})`);
    try {
      reload();
    } catch (e) {
      // Never let a failed reload escape into Electron's event emitter.
      log(`renderer reload failed: ${e && e.message}`);
    }
    return "reloaded";
  }

  return {
    handleGone,
    reset() {
      recent = [];
    },
    get attempts() {
      return recent.length;
    },
  };
}

module.exports = {
  createRendererRecovery,
  isRecoverableReason,
  RECOVERABLE_REASONS,
  DEFAULT_MAX_ATTEMPTS,
  DEFAULT_WINDOW_MS,
};
