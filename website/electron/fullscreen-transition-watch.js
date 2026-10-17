"use strict";
//
// Pure, injectable helper (mirrors hide-to-tray.js / html-fullscreen.js): a
// watchdog for macOS native fullscreen transitions, plus the one repair that
// clears a transition AppKit abandoned half-way.
//
// PROBLEM: leaving native fullscreen on macOS is an asynchronous AppKit
// transition. AppKit orders the real NSWindow out, animates a full-display
// snapshot overlay it owns, switches the Space back, orders the real window in
// again and only then delivers `windowDidExitFullScreen` — the callback Electron
// turns into `leave-full-screen`. That last step can fail to arrive. Observed on
// a clamshell MacBook driving external displays: the Space switched back and the
// real window was re-ordered in, but the overlay stayed on screen at the full
// display frame and `leave-full-screen` never fired. The overlay is not a
// BaseWindow — it has no title bar, no traffic lights, cannot be moved or
// resized, and covers every other application on that display. Anything that
// then hides the real window (hide-to-tray's backstop, a second click on the
// close control) leaves the user with ONLY the overlay: the "frozen copy of
// Kiro Crew that blocks my other apps" report.
//
// From the main process the overlay is invisible: Electron exposes no handle to
// AppKit-owned windows, and `isFullScreen()` already reads false because the
// style mask flipped at the START of the transition. The one observable signal
// is the missing terminal event. Electron emits `resize` at the start of every
// transition with `isFullScreen()` already reporting the target state, so a
// transition is "in flight" from that `resize` until the matching
// `enter-full-screen` / `leave-full-screen`. A healthy transition completes in
// well under a second; one that has not completed after TRANSITION_TIMEOUT_MS
// is treated as stalled.
//
// REPAIR: `app.hide()` orders out every window the process owns, INCLUDING the
// AppKit overlay, and `app.show()` unhides only real windows — the overlay does
// not come back. Verified against a reproduced stall (recreating the NSWindow
// mid-exit) on macOS 26: bounds/show/focus on the real window left the overlay
// in place; the hide/unhide cycle cleared it and restored the real window at its
// normal frame. When the real window was deliberately hidden (close-to-tray) the
// cycle stops after `app.hide()`: the user's next Dock click unhides the app and
// gets the real window alone.
//
// SCOPE: macOS only. Windows and Linux fullscreen is a borderless maximized
// window with no Space, no AppKit transition and no overlay; off darwin the
// watch is a no-op.

// Longest a healthy enter/exit transition is allowed to take before it counts
// as stalled. The Space animation is ~0.5s; the margin covers slow external
// displays (DisplayLink, high-resolution) and a renderer that is late producing
// its first frame at the new size.
const TRANSITION_TIMEOUT_MS = 4000;

// Gap between `app.hide()` and `app.show()` in the repair cycle. AppKit tears
// the hidden overlay down asynchronously; unhiding on the next line is too soon.
const REPAIR_UNHIDE_DELAY_MS = 350;

const isDead = (win) => {
  try {
    return !win || (typeof win.isDestroyed === "function" && win.isDestroyed());
  } catch {
    return true;
  }
};

const readFullScreen = (win) => {
  try {
    return typeof win.isFullScreen === "function" && win.isFullScreen();
  } catch {
    return false;
  }
};

/**
 * Watch one window's native fullscreen transitions and report the ones that
 * never complete.
 *
 * Duck-typed so tests need no Electron: `win` supplies `on`, `off` (or
 * `removeListener`), `isDestroyed`, `isFullScreen` and `isVisible`.
 *
 * @param {object} win
 * @param {object} [opts]
 * @param {boolean} [opts.isMac]        defaults to the real platform.
 * @param {number} [opts.timeoutMs]     defaults to TRANSITION_TIMEOUT_MS.
 * @param {(info: {target: boolean, fullScreen: boolean, visible: boolean, elapsedMs: number}) => void} [opts.onStall]
 *        Called once per stalled transition. `target` is the state the
 *        transition was heading for (true = entering, false = leaving).
 * @param {(info: {target: boolean, fullScreen: boolean, visible: boolean, elapsedMs: number}) => void} [opts.onAbort]
 *        Called once per transition that was REVERSED before its terminal event
 *        — a toggle issued mid-animation. `target` is the abandoned
 *        transition's direction. The replacement transition completes normally,
 *        so unlike a stall there is no missing event to detect; the abandoned
 *        overlay is nevertheless orphaned and needs the same repair.
 * @param {Function} [opts.setTimeoutFn] / [opts.clearTimeoutFn]  timer injection.
 * @param {() => number} [opts.now]     clock injection.
 * @returns {{ dispose: () => void, pending: () => (boolean|null), exitSettlingFor: () => number }}
 *          `pending()` is the target of the in-flight transition, or null.
 *          `exitSettlingFor()` is the elapsed time since the latest terminal
 *          exit event, or Infinity before one has completed.
 */
function watchFullScreenTransitions(win, opts = {}) {
  const idle = {
    dispose() {},
    pending: () => null,
    quietFor: () => Infinity,
    exitSettlingFor: () => Infinity,
  };
  if (isDead(win) || typeof win.on !== "function") return idle;
  const isMac = opts.isMac === undefined ? process.platform === "darwin" : !!opts.isMac;
  if (!isMac) return idle;

  const setTimer = opts.setTimeoutFn || setTimeout;
  const clearTimer = opts.clearTimeoutFn || clearTimeout;
  const now = typeof opts.now === "function" ? opts.now : Date.now;
  const timeoutMs = Number.isFinite(opts.timeoutMs) ? opts.timeoutMs : TRANSITION_TIMEOUT_MS;
  const onStall = typeof opts.onStall === "function" ? opts.onStall : () => {};
  const onAbort = typeof opts.onAbort === "function" ? opts.onAbort : () => {};
  // Reported when a transition is first observed. The close path's correctness
  // depends entirely on this firing, and it is derived from a `resize` rather
  // than a dedicated event (Electron emits no `will-enter-full-screen` for a
  // BaseWindow), so it is journaled: a build where the flag flips differently
  // would otherwise silently fall back to the unserialised behaviour.
  const onArm = typeof opts.onArm === "function" ? opts.onArm : () => {};

  // The last state a terminal event confirmed (or the constructor state).
  let known = readFullScreen(win);
  let inflight = null; // { target, timer, startedAt }
  // When this window last moved at all in fullscreen terms. AppKit's terminal
  // event does NOT mean AppKit is finished (measured: `enter-full-screen` lands
  // well before the Space animation ends), so "has been quiet for a while" is the
  // only usable read on whether a toggle is safe to issue. See hide-to-tray.js.
  let lastActivityAt = now();
  let lastExitAt = null;
  const touch = () => {
    lastActivityAt = now();
  };

  const disarm = () => {
    if (!inflight) return;
    if (inflight.timer !== null) {
      try {
        clearTimer(inflight.timer);
      } catch {
        /* best effort */
      }
    }
    inflight = null;
  };

  const arm = (target) => {
    disarm();
    const startedAt = now();
    const entry = { target, timer: null, startedAt };
    inflight = entry;
    onArm({ target });
    try {
      entry.timer = setTimer(() => {
        if (inflight !== entry) return;
        inflight = null;
        if (isDead(win)) return;
        const fullScreen = readFullScreen(win);
        // Whatever AppKit did, this is the state the window is in now; the next
        // `resize` must be judged against it, not against the pre-stall state.
        known = fullScreen;
        let visible = false;
        try {
          visible = typeof win.isVisible === "function" && win.isVisible();
        } catch {
          visible = false;
        }
        onStall({ target, fullScreen, visible, elapsedMs: now() - startedAt });
      }, timeoutMs);
      if (entry.timer && typeof entry.timer.unref === "function") entry.timer.unref();
    } catch {
      inflight = null;
    }
  };

  const onResize = () => {
    if (isDead(win)) return;
    touch();
    const current = readFullScreen(win);
    // A resize that flips the flag is the first observable moment of a
    // transition — user-initiated (green control, ⌃⌘F) or ours alike. A flip
    // back to the confirmed state means the transition was reversed before it
    // completed: AppKit abandoned it, and the full-display snapshot overlay it
    // was animating is orphaned on screen even though the NEW transition will
    // deliver its terminal event perfectly normally. That is the second route to
    // the immovable overlay, and the missing-event timer cannot see it.
    if (current === known) {
      const aborted = inflight;
      disarm();
      if (aborted) {
        let visible = false;
        try {
          visible = typeof win.isVisible === "function" && win.isVisible();
        } catch {
          visible = false;
        }
        onAbort({
          target: aborted.target,
          fullScreen: current,
          visible,
          elapsedMs: now() - aborted.startedAt,
        });
      }
      return;
    }
    if (!inflight || inflight.target !== current) arm(current);
  };
  const onEnter = () => {
    lastExitAt = null;
    touch();
    known = true;
    disarm();
  };
  const onLeave = () => {
    touch();
    lastExitAt = lastActivityAt;
    known = false;
    disarm();
  };
  const onClosed = () => dispose();

  win.on("resize", onResize);
  win.on("enter-full-screen", onEnter);
  win.on("leave-full-screen", onLeave);
  win.on("closed", onClosed);

  let disposed = false;
  function dispose() {
    if (disposed) return;
    disposed = true;
    disarm();
    const off =
      typeof win.off === "function"
        ? win.off
        : typeof win.removeListener === "function"
          ? win.removeListener
          : null;
    if (!off) return;
    try {
      off.call(win, "resize", onResize);
      off.call(win, "enter-full-screen", onEnter);
      off.call(win, "leave-full-screen", onLeave);
      off.call(win, "closed", onClosed);
    } catch {
      /* best effort */
    }
  }

  return {
    dispose,
    pending: () => (inflight ? inflight.target : null),
    // Milliseconds since this window last showed any fullscreen-related motion.
    // hide-to-tray waits for this to clear a grace period before it issues its
    // exit, which is what keeps AppKit from abandoning a transition.
    quietFor: () => now() - lastActivityAt,
    // The terminal event precedes AppKit's final order-in. Keep that distinct
    // from pending(), which correctly becomes null as soon as the event fires.
    exitSettlingFor: () => (lastExitAt === null ? Infinity : now() - lastExitAt),
  };
}

/**
 * Clear a stalled fullscreen-exit transition by cycling the application's
 * hidden state, then re-show the real window if it was visible.
 *
 * Best-effort and idempotent. Takes the Electron `app` (only `hide`/`show` are
 * used) and the window (only `isDestroyed`/`isVisible`/`show`), so a test can
 * assert the sequence without a runtime.
 *
 * @param {object} args
 * @param {{hide: () => void, show: () => void}} args.app
 * @param {object} args.win
 * @param {boolean} [args.isMac]
 * @param {boolean|(() => boolean)} [args.keepHidden]  Skip the unhide even if
 *        the window was visible. A function is re-evaluated when the delayed
 *        unhide fires, so a close arriving during that delay still wins.
 * @param {Function} [args.setTimeoutFn]
 * @returns {{hidden: boolean, unhideScheduled: boolean}}
 */
function repairStalledFullScreenExit({ app, win, isMac, keepHidden, setTimeoutFn } = {}) {
  const result = { hidden: false, unhideScheduled: false };
  const mac = isMac === undefined ? process.platform === "darwin" : !!isMac;
  if (!mac || !app || typeof app.hide !== "function" || isDead(win)) return result;

  const mustStayHidden = () => {
    try {
      return typeof keepHidden === "function" ? !!keepHidden() : !!keepHidden;
    } catch {
      // An uncertain close intent must not re-surface a window after the repair
      // has already hidden it. Dock/tray activation remains an explicit escape.
      return true;
    }
  };

  let wasVisible = false;
  try {
    wasVisible = typeof win.isVisible === "function" && win.isVisible();
  } catch {
    wasVisible = false;
  }

  try {
    app.hide();
    result.hidden = true;
  } catch {
    return result;
  }

  // A window the user deliberately hid (close-to-tray) stays hidden: the next
  // Dock click unhides the app and finds only the real window. Unhiding here
  // would re-surface a window the user just dismissed.
  if (mustStayHidden() || !wasVisible || typeof app.show !== "function") return result;

  const setTimer = setTimeoutFn || setTimeout;
  try {
    const timer = setTimer(() => {
      if (isDead(win) || mustStayHidden()) return;
      try {
        app.show();
        if (typeof win.show === "function") win.show();
      } catch {
        /* best effort */
      }
    }, REPAIR_UNHIDE_DELAY_MS);
    if (timer && typeof timer.unref === "function") timer.unref();
    result.unhideScheduled = true;
  } catch {
    /* best effort — the app is at least un-ghosted, if hidden */
  }
  return result;
}

module.exports = {
  watchFullScreenTransitions,
  repairStalledFullScreenExit,
  TRANSITION_TIMEOUT_MS,
  REPAIR_UNHIDE_DELAY_MS,
};
