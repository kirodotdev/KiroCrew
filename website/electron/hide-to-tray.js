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
//
// STALLED EXIT: `leave-full-screen` is Electron's relay of AppKit's
// `windowDidExitFullScreen`, and AppKit can fail to deliver it. Seen on a
// clamshell MacBook on external displays: the Space switched back and the real
// window was ordered in again, but the full-display snapshot overlay AppKit
// animates during the exit stayed on screen and the callback never came. That
// overlay is not one of our windows — no traffic lights, cannot be moved or
// resized, covers every other app — and hiding the REAL window underneath it
// (which is what the old 2s backstop did) leaves the user with only the
// overlay. So the backstop no longer calls `win.hide()`. It calls `app.hide()`:
// that orders out every window the process owns, the AppKit overlay included,
// and satisfies the close gesture; the next Dock click unhides the app and finds
// only the real window (`app.show()` does not bring the overlay back — verified
// against a reproduced stall in fullscreen-transition-watch.js). The same call
// is also the right answer when the exit never even started (`isFullScreen()`
// still true at the backstop): hiding a whole app out of a fullscreen Space is
// ordinary ⌘H behaviour and orphans nothing, whereas `win.hide()` there is the
// black-Space bug this module exists to avoid.
//
// SETTLE: even when `leave-full-screen` does arrive, AppKit still re-orders the
// real window in ~25ms AFTER the callback as the tail of its own transition, so
// a `hide()` issued synchronously inside the event races that order-in. The hide
// is therefore delayed by POST_LEAVE_SETTLE_MS; the delay is cancellable like the
// rest of the deferral.
//
// WHY THE FULLSCREEN PATH HIDES THE APP, NOT THE WINDOW: AppKit does not queue a
// fullscreen toggle issued while one of its own transitions is still animating.
// It abandons the running transition, and the full-display snapshot overlay that
// transition was animating is left on screen owned by nothing — no traffic
// lights, immovable, covering every other app. Hiding only the real WINDOW then
// leaves the user with the overlay as the only thing on screen: the "frozen copy
// of Kiro Crew blocking my other apps" report.
//
// There is no in-process signal for "AppKit has finished". Measured on macOS 26
// against the shipped build: `enter-full-screen` (AppKit's
// `windowDidEnterFullScreen`) arrives well BEFORE the Space animation ends, and
// closing after that event still orphaned an overlay every time. So waiting for
// the terminal event is not a guard, and serialising the exit behind it was tried
// and does not hold.
//
// The close path therefore prevents the interruption rather than treating
// `app.hide()` as a timing-independent cure: it waits until fullscreen motion
// has been quiet for TRANSITION_QUIET_MS before issuing the exit. Once the
// terminal event arrives, app-level hide remains important because it can order
// out an overlay together with the real window; it is re-asserted later because
// macOS can swallow a hide issued near the tail of the animation. The watchdog
// keeps the same app hide/show cycle as a last-resort repair for transitions
// interrupted by some other user action.
//
// Every show path must consequently unhide the application before showing its
// target window; see window-lifecycle.js and global-hotkey.js.
//
// The windowed path keeps `win.hide()`: no Space, no transition, no overlay, and
// hiding the whole app there would also hide unrelated windows the user did not
// close.

// How long to wait for `leave-full-screen` before hiding anyway. Generous
// relative to the ~0.5s macOS Space animation.
const DEFAULT_LEAVE_TIMEOUT_MS = 2000;

// Delay between `leave-full-screen` and the actual hide, so AppKit's post-exit
// order-in has landed before the window is ordered out.
const POST_LEAVE_SETTLE_MS = 300;

// How long after the first app-level hide to issue a second one. A hide issued
// while AppKit is still animating is SWALLOWED — measured on macOS 26: the call
// returns, nothing is ordered out, and an orphan overlay stays on screen. The
// same call once AppKit is quiet clears it (verified against a reproduced
// overlay). Since there is no event for "AppKit is done", the hide is simply
// re-asserted once, late enough to be past any animation. Cancelled by
// cancelPendingTrayHide, so a user who summons the window back in between does
// not get it hidden out from under them.
const HIDE_REASSERT_MS = 1000;

// How long the window must have shown no fullscreen motion before the exit is
// issued. AppKit abandons a transition that a toggle interrupts, orphaning that
// transition's full-display overlay, and it signals completion (`enter-full-screen`)
// well BEFORE it stops working — so quiet time is the only usable read. 700ms was
// measured: closing at random points inside the enter animation orphaned an
// overlay in roughly a third of runs without this wait and in none of 15 runs
// with it, and the hide itself stopped being swallowed too.
const TRANSITION_QUIET_MS = 700;

// Poll interval while waiting for that quiet period. There is nothing to
// subscribe to; the events have already fired.
const QUIET_POLL_MS = 80;

// The one pending deferred hide per window, keyed on the window itself so a
// show path can disarm it without threading a handle through main.js. WeakMap:
// a window destroyed (or dropped) mid-deferral must not be kept alive by its
// own cancel closure.
const pendingHides = new WeakMap();

// The one pending re-asserted app hide per window, kept separately: it outlives
// the deferral (the window is already hidden by then) but must still be
// cancellable by a show.
const pendingReasserts = new WeakMap();

// User intent outlives both timers above. The fullscreen watchdog fires after
// hideToTray's own backstop, so "is a timer still pending?" cannot answer
// whether its repair should keep the app hidden. This marker is set for every
// fullscreen close and cleared only by an explicit user show.
const trayHideIntents = new WeakSet();

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
 * @param {{isMac?:boolean, timeoutMs?:number, settleMs?:number, reassertMs?:number, graceMs?:number, transitionTarget?:boolean|null, quietFor?:Function, exitSettlingFor?:Function, setTimeoutFn?:Function, clearTimeoutFn?:Function, hideAppFn?:Function, log?:Function}} [opts]
 *                      `isMac` defaults to the real platform; `hideAppFn`
 *                      defaults to Electron's `app.hide()` and is what the
 *                      FULLSCREEN path hides with instead of `win.hide()`;
 *                      `transitionTarget` carries the watch's in-flight target;
 *                      `quietFor` reports milliseconds since AppKit last moved;
 *                      `exitSettlingFor` reports time since a terminal exit;
 *                      the rest is timer and log injection for tests.
 * @returns {{hidden: boolean, hidApp: boolean, deferred: boolean, leftFullScreen: boolean, stalled: boolean, waitedForQuiet: boolean}}
 *                      `hidden` — the WINDOW was hidden synchronously (the
 *                      windowed path).
 *                      `hidApp` — set later, when the fullscreen path hid the
 *                      whole app (the object is mutated in place).
 *                      `deferred` — hide is pending on the fullscreen exit.
 *                      `leftFullScreen` — setFullScreen(false) was issued.
 *                      `stalled` — set later, when the backstop ran because the
 *                      exit never completed.
 *                      `waitedForQuiet` — the close waited for AppKit motion
 *                      before continuing.
 */
function hideToTray(win, opts = {}) {
  const result = {
    hidden: false,
    hidApp: false,
    deferred: false,
    leftFullScreen: false,
    stalled: false,
    waitedForQuiet: false,
  };
  if (!win || isDead(win)) return result;
  // A second close can arrive after setFullScreen(false) has already flipped
  // the style flag but before the first close has hidden the app. Check the
  // existing owner before probing fullscreen, or that second event would look
  // windowed and call win.hide() in the middle of the AppKit transition.
  if (pendingHides.has(win) || pendingReasserts.has(win)) {
    result.deferred = true;
    return result;
  }
  const log = typeof opts.log === "function" ? opts.log : () => {};

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

  const transitionTarget =
    opts.transitionTarget === true || opts.transitionTarget === false
      ? opts.transitionTarget
      : null;
  const exitInFlight = transitionTarget === false;
  const settleMs = Number.isFinite(opts.settleMs) ? opts.settleMs : POST_LEAVE_SETTLE_MS;
  const exitSettlingFor = opts.exitSettlingFor;
  const readExitSettlingFor = () => {
    try {
      return typeof exitSettlingFor === "function" ? exitSettlingFor() : Infinity;
    } catch {
      return Infinity;
    }
  };
  const sinceExit = readExitSettlingFor();
  // pending() correctly clears at the terminal event, but AppKit can still
  // order the real window afterward. Keep that bounded tail on the same safe
  // app-hide path as the active exit.
  const exitTail =
    !fullScreen
    && !exitInFlight
    && Number.isFinite(sinceExit)
    && sinceExit < settleMs;

  // Common path: a stable windowed window has no Space to orphan, so hide it
  // immediately. A user exit already in flight only needs a terminal listener;
  // issuing another setFullScreen(false) would abandon AppKit's current work.
  const waitsForTerminal = fullScreen || exitInFlight;
  const canDefer =
    exitTail
    || (waitsForTerminal
      && typeof win.once === "function"
      && (!fullScreen || typeof win.setFullScreen === "function"));
  if (!canDefer) {
    result.hidden = hideNow();
    return result;
  }

  trayHideIntents.add(win);

  const setTimer = opts.setTimeoutFn || setTimeout;
  const clearTimer = opts.clearTimeoutFn || clearTimeout;
  const timeoutMs = Number.isFinite(opts.timeoutMs) ? opts.timeoutMs : DEFAULT_LEAVE_TIMEOUT_MS;
  const reassertMs = Number.isFinite(opts.reassertMs) ? opts.reassertMs : HIDE_REASSERT_MS;
  const graceMs = Number.isFinite(opts.graceMs) ? opts.graceMs : TRANSITION_QUIET_MS;
  const quietFor = opts.quietFor;
  const hideApp = typeof opts.hideAppFn === "function" ? opts.hideAppFn : defaultHideApp;

  // Hide the whole application. The fullscreen path's only hide: it is the one
  // call that also orders out AppKit's abandoned transition overlay, and it is
  // safe on a window still inside its Space.
  const hideAppNow = () => {
    if (isDead(win)) return false;
    try {
      hideApp();
      return true;
    } catch {
      return false;
    }
  };

  // Re-assert the hide once AppKit is certainly quiet; see HIDE_REASSERT_MS.
  const reassertHide = () => {
    try {
      const handle = setTimer(() => {
        pendingReasserts.delete(win);
        if (isDead(win)) return;
        hideAppNow();
      }, reassertMs);
      if (handle && typeof handle.unref === "function") handle.unref();
      // Stored as a canceller rather than a handle so the injected clear is the
      // one used; a test's fake timer is not a real one.
      pendingReasserts.set(win, () => {
        try {
          clearTimer(handle);
        } catch {
          /* best effort */
        }
      });
    } catch {
      /* best effort — the first hide has already been issued */
    }
  };

  let settled = false;
  let timer = null;
  let quietTimer = null;
  // Every listener this call arms is tracked so settle() can remove any that
  // never fire: `once` only self-removes when it fires.
  const armed = [];
  const listen = (event, handler) => {
    try {
      win.once(event, handler);
      armed.push([event, handler]);
      return true;
    } catch {
      return false;
    }
  };
  const unlistenAll = () => {
    const off =
      typeof win.off === "function"
        ? win.off
        : typeof win.removeListener === "function"
          ? win.removeListener
          : null;
    if (off) {
      for (const [event, handler] of armed) {
        try {
          off.call(win, event, handler);
        } catch {
          /* best effort */
        }
      }
    }
    armed.length = 0;
  };

  // Exactly-once settlement, shared by every exit: the terminal event (after its
  // settle delay), the backstop, and a cancellation. `mode` distinguishes them:
  // "hide" tears the machinery down and hides the window; "stalled" tears it
  // down and hides the app; a cancel (mode "none") tears it down and leaves the
  // window visible — the user just asked for it back.
  const settle = (mode) => {
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
    if (quietTimer !== null) {
      try {
        clearTimer(quietTimer);
      } catch {
        /* best effort */
      }
      quietTimer = null;
    }
    // An unfired listener left behind would stay for the life of the process and
    // accumulate one per swallowed transition. Harmless individually, but it is
    // the listener leak that eventually trips MaxListenersExceededWarning.
    unlistenAll();
    // The fullscreen path always hides the APP: whatever AppKit orphaned during
    // the transition goes out with it, and only `app.hide()` can reach that
    // overlay. `stalled` differs from `hide` solely in the journal line.
    if (mode === "hide") {
      result.hidApp = hideAppNow();
      reassertHide();
    }
    if (mode === "stalled") {
      result.stalled = true;
      const stillFullScreen = readFullScreen(win);
      log(
        `tray hide: fullscreen exit did not complete within ${timeoutMs}ms` +
          ` (isFullScreen=${stillFullScreen}) — hiding the app`,
      );
      result.hidApp = hideAppNow();
      reassertHide();
    }
  };

  // The event landed: AppKit's own post-exit order-in is still ~25ms away, so
  // give it room before the app is ordered out. The settle delay reuses the one
  // timer slot, so a cancel during it disarms the hide exactly as it does during
  // the wait for the event.
  function onLeave() {
    if (settled) return;
    try {
      clearTimer(timer);
    } catch {
      /* best effort */
    }
    timer = null;
    try {
      timer = setTimer(() => settle("hide"), settleMs);
      if (timer && typeof timer.unref === "function") timer.unref();
    } catch {
      settle("hide");
    }
  }
  function onBackstop() {
    settle("stalled");
  }

  // Registered before the listener is armed so no window exists in which the
  // hide is pending but not cancellable. A stale entry is impossible: every
  // settle path deletes it, and cancelling an already-settled hide is a no-op
  // behind the `settled` guard.
  pendingHides.set(win, () => settle("none"));

  if (waitsForTerminal && !listen("leave-full-screen", onLeave)) {
    // The window is still inside its Space. A window-level hide here recreates
    // the black-Space/overlay bug, so settle through the same app-level path as
    // every other fullscreen failure and keep its re-assertion cancellable.
    settle("hide");
    return result;
  }

  // Backstop: if AppKit never delivers `leave-full-screen` (the exit stalled with
  // its overlay still on screen) the close gesture must still take effect. The
  // budget covers the quiet wait as well as the exit itself.
  try {
    timer = setTimer(onBackstop, timeoutMs + (exitTail ? settleMs : graceMs));
    // Node/Electron timers keep the event loop alive; a pending hide must never
    // be the reason the process lingers on a real quit.
    if (timer && typeof timer.unref === "function") timer.unref();
  } catch {
    /* best effort — the leave-full-screen listener is still armed */
  }

  // The caller observed this exit through the transition watch. The terminal
  // listener and backstop above are already attached synchronously; there is no
  // toggle to issue because AppKit is doing that work now.
  if (exitInFlight) {
    result.deferred = true;
    return result;
  }

  // Issue the exit only once AppKit has been quiet for the grace period. Polled
  // because there is nothing to subscribe to: the whole point is that AppKit's
  // own events have already fired while it is still working.
  const requestExit = () => {
    if (settled) return;
    try {
      win.setFullScreen(false);
      result.leftFullScreen = true;
    } catch {
      // The exit never started, so nothing will fire the listener. Hiding the app
      // is still right: a window inside a Space must not be hidden on its own.
      settle("hide");
    }
  };
  const quietProbe = exitTail ? exitSettlingFor : quietFor;
  const quietThresholdMs = exitTail ? settleMs : graceMs;
  const afterQuiet = exitTail ? () => settle("hide") : requestExit;
  const whenQuiet = () => {
    if (settled) return;
    let since = Infinity;
    try {
      since = typeof quietProbe === "function" ? quietProbe() : Infinity;
    } catch {
      since = Infinity;
    }
    if (!Number.isFinite(since) || since >= quietThresholdMs) {
      afterQuiet();
      return;
    }
    result.waitedForQuiet = true;
    try {
      const handle = setTimer(
        whenQuiet,
        Math.max(QUIET_POLL_MS, quietThresholdMs - since),
      );
      if (handle && typeof handle.unref === "function") handle.unref();
      quietTimer = handle;
    } catch {
      afterQuiet(); // cannot poll — better to honour the close now than never
    }
  };
  whenQuiet();

  if (settled) return result;

  result.deferred = true;
  return result;
}

function readFullScreen(win) {
  try {
    return typeof win.isFullScreen === "function" && win.isFullScreen();
  } catch {
    return false;
  }
}

// Resolved lazily so node:test can load this module without an Electron runtime.
function defaultHideApp() {
  const { app } = require("electron");
  app.hide();
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
  // Clearing the durable intent is as important as clearing timers: the
  // fullscreen watchdog can fire after both timer maps have drained.
  const hadIntent = trayHideIntents.delete(win);
  // A re-asserted app hide is pending AFTER the window is already hidden, so a
  // show has to disarm it too or the window the user just summoned is hidden
  // again a second later.
  const reassert = pendingReasserts.get(win);
  if (reassert) {
    pendingReasserts.delete(win);
    reassert();
  }
  const cancel = pendingHides.get(win);
  if (!cancel) return hadIntent || !!reassert;
  cancel();
  return true;
}

/**
 * Whether a fullscreen close still intends this application to stay hidden.
 *
 * Unlike a pending-timer query, this remains true after hideToTray's 2.7s
 * backstop and one-second re-assertion have both completed. The independent
 * four-second fullscreen watchdog therefore cannot accidentally re-show a
 * window the user already dismissed. An explicit user show clears it through
 * cancelPendingTrayHide().
 *
 * @param {object} win
 * @returns {boolean}
 */
function shouldKeepAppHidden(win) {
  if (!win) return false;
  return trayHideIntents.has(win);
}

module.exports = {
  hideToTray,
  cancelPendingTrayHide,
  shouldKeepAppHidden,
  DEFAULT_LEAVE_TIMEOUT_MS,
  POST_LEAVE_SETTLE_MS,
  HIDE_REASSERT_MS,
  TRANSITION_QUIET_MS,
};
