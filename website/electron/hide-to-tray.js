"use strict";
//
// Pure, injectable helper extracted from main.js so the "never hide a window out
// of a macOS fullscreen Space" rule is unit-testable without Electron (mirrors
// blocking-prompt.js / window-state.js / gateway-recovery.js).
//
// PROBLEM: on macOS the main window's close button does NOT destroy the window —
// the app keeps running in the tray, so `close` is preventDefault'ed and the
// window is hidden instead. Hiding a window that occupies a NATIVE macOS
// fullscreen Space leaves that Space behind with nothing drawing into it, and
// three symptoms follow:
//   - a black full-screen surface the user is left staring at, because the Space
//     is still mapped but its only window is gone;
//   - the app still reporting as running (that part is intended tray behaviour,
//     but next to the black Space it reads as a hang);
//   - a re-show mapping the window while it is still flagged fullscreen with its
//     Space destroyed, so it comes back at a degenerate frame — the "very small
//     window" that only corrects once AppKit re-lays-out after a couple of
//     focus changes.
//
// FIX: leave native fullscreen FIRST, and hide only once macOS has actually torn
// the Space down. setFullScreen(false) is ASYNCHRONOUS on macOS (there is a
// ~0.5s Space animation), so the hide has to wait for the `leave-full-screen`
// event rather than run on the next line — hiding mid-transition reproduces the
// very bug this avoids.
//
// Simple-fullscreen and kiosk are deliberately NOT touched. Neither allocates a
// Space, so hiding out of them is already clean, and clearing them would
// silently change the mode the user returns to. This helper is narrower than
// blocking-prompt.js's exitImmersiveModes() on purpose: that one restores window
// CHROME so an in-window prompt stays dismissable, which is a different goal.
//
// SCOPE: macOS only, for the same reason. Windows and Linux fullscreen is a
// borderless maximized window with no Space behind it, so hiding out of it is
// already clean there — and exiting fullscreen on those platforms would be a
// visible regression, since the window would come back windowed and the geometry
// listener would persist it as windowed for the next launch too. Off darwin this
// stays exactly the plain hide it has always been.
//
// CANCELLATION: while the hide is deferred (up to the 2s backstop) the window is
// still visible, so a show request landing in that gap — Dock activate, the tray
// "Show" item, the summon hotkey — would either be skipped (`isVisible()` is
// still true) or be silently undone moments later when the deferred hide fires.
// `cancelPendingTrayHide(win)` disarms the pending hide (clears the backstop and
// removes the listener) so the show wins; every show path that expresses user
// intent to see the window calls it first.

// How long to wait for `leave-full-screen` before hiding anyway. Generous
// relative to the ~0.5s macOS Space animation.
const DEFAULT_LEAVE_TIMEOUT_MS = 2000;

// The one pending deferred hide per window, keyed on the window itself so a
// show path can disarm it without threading a handle through main.js. WeakMap:
// a window destroyed (or dropped) mid-deferral must not be kept alive by its
// own cancel closure.
const pendingHides = new WeakMap();

const isDead = (win) => {
  try {
    return typeof win.isDestroyed === "function" && win.isDestroyed();
  } catch {
    return true;
  }
};

/**
 * Hide a window to the tray without orphaning a macOS fullscreen Space.
 *
 * Best-effort and defensive: every probe is guarded, a destroyed window is a
 * no-op, and the window is hidden at most once no matter which path gets there
 * first.
 *
 * @param {object} win  A BrowserWindow/BaseWindow-like object. Only the
 *                      isDestroyed / isFullScreen / setFullScreen / once / off /
 *                      hide members are used, each optional.
 * @param {{isMac?:boolean, timeoutMs?:number, setTimeoutFn?:Function, clearTimeoutFn?:Function}} [opts]
 *                      `isMac` defaults to the real platform; the rest is timer
 *                      injection for tests.
 * @returns {{hidden: boolean, deferred: boolean, leftFullScreen: boolean}}
 *                      `hidden` — hidden synchronously during this call.
 *                      `deferred` — hide is pending on the fullscreen exit.
 *                      `leftFullScreen` — setFullScreen(false) was issued.
 */
function hideToTray(win, opts = {}) {
  const result = { hidden: false, deferred: false, leftFullScreen: false };
  if (!win || isDead(win)) return result;

  const hideNow = () => {
    // Re-probe: between scheduling and firing, the window may have been
    // destroyed (a real quit racing the fullscreen animation).
    if (isDead(win)) return false;
    try {
      if (typeof win.hide !== "function") return false;
      win.hide();
      return true;
    } catch {
      return false; // best effort — never let a hide throw past the caller
    }
  };

  // Only macOS puts a fullscreen window in a Space of its own, so only macOS can
  // orphan one. Everywhere else this must stay the plain hide it was, or a
  // fullscreen window would reopen windowed on platforms that never had the bug.
  const isMac = opts.isMac === undefined ? process.platform === "darwin" : !!opts.isMac;
  if (!isMac) {
    result.hidden = hideNow();
    return result;
  }

  let fullScreen = false;
  try {
    fullScreen = typeof win.isFullScreen === "function" && win.isFullScreen();
  } catch {
    fullScreen = false;
  }

  // Common path: a windowed window has no Space to orphan, so hide immediately.
  const canDefer =
    fullScreen && typeof win.setFullScreen === "function" && typeof win.once === "function";
  if (!canDefer) {
    result.hidden = hideNow();
    return result;
  }

  const setTimer = opts.setTimeoutFn || setTimeout;
  const clearTimer = opts.clearTimeoutFn || clearTimeout;
  const timeoutMs = Number.isFinite(opts.timeoutMs) ? opts.timeoutMs : DEFAULT_LEAVE_TIMEOUT_MS;

  let settled = false;
  let timer = null;
  // Exactly-once settlement, shared by all three exits: the event, the
  // backstop, and a cancellation. `hide` distinguishes the first two (tear the
  // machinery down, then hide) from a cancel (tear it down and leave the window
  // visible — the user just asked for it back).
  const settle = (hide) => {
    if (settled) return;
    settled = true;
    pendingHides.delete(win);
    if (timer !== null) {
      try {
        clearTimer(timer);
      } catch {
        /* best effort */
      }
      timer = null;
    }
    // `once` only self-removes when it actually FIRES, so if the backstop got
    // here first the listener would stay armed for the life of the process and
    // accumulate one per swallowed transition. Harmless individually, but it is
    // the listener leak that eventually trips MaxListenersExceededWarning.
    try {
      const off =
        typeof win.off === "function"
          ? win.off
          : typeof win.removeListener === "function"
            ? win.removeListener
            : null;
      if (off) off.call(win, "leave-full-screen", finish);
    } catch {
      /* best effort */
    }
    if (hide) hideNow();
  };
  function finish() {
    settle(true);
  }

  // Registered before the listener is armed so no window exists in which the
  // hide is pending but not cancellable. A stale entry is impossible: every
  // settle path deletes it, and cancelling an already-settled hide is a no-op
  // behind the `settled` guard.
  pendingHides.set(win, () => settle(false));

  try {
    win.once("leave-full-screen", finish);
  } catch {
    // Cannot observe the transition, so a deferred hide would never land.
    pendingHides.delete(win);
    result.hidden = hideNow();
    return result;
  }

  // Backstop: if AppKit swallows the transition (a fullscreen animation already
  // in flight, or the event otherwise never lands) the close button would
  // silently do nothing at all — a worse outcome than the Space we are avoiding.
  // Hide anyway once the animation window has comfortably passed.
  try {
    timer = setTimer(finish, timeoutMs);
    // Node/Electron timers keep the event loop alive; a pending hide must never
    // be the reason the process lingers on a real quit.
    if (timer && typeof timer.unref === "function") timer.unref();
  } catch {
    /* best effort — the leave-full-screen listener is still armed */
  }

  try {
    win.setFullScreen(false);
    result.leftFullScreen = true;
  } catch {
    // The exit never started, so nothing will fire the listener. Hide now
    // rather than leaving the window mapped until the backstop expires.
    finish();
    result.hidden = true;
    return result;
  }

  result.deferred = true;
  return result;
}

/**
 * Disarm a hide that hideToTray() deferred to the fullscreen exit, so a show
 * request that lands inside that window (Dock activate, tray "Show", the summon
 * hotkey) is not silently undone when the exit completes. Clears the backstop
 * timer and removes the `leave-full-screen` listener; the fullscreen exit
 * itself is NOT reversed — the window simply stays visible, windowed, which is
 * what a user asking for the window back expects.
 *
 * Call it before performing any user-initiated show. Safe to call always: a
 * window with no pending hide is a no-op.
 *
 * @param {object} win  The window that was passed to hideToTray().
 * @returns {boolean}   true when a pending deferred hide was disarmed.
 */
function cancelPendingTrayHide(win) {
  if (!win) return false;
  const cancel = pendingHides.get(win);
  if (!cancel) return false;
  cancel();
  return true;
}

module.exports = { hideToTray, cancelPendingTrayHide, DEFAULT_LEAVE_TIMEOUT_MS };
