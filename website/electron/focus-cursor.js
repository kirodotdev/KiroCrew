"use strict";

/**
 * Off-window cursor distance for the focus-mode reveal overlays.
 *
 * A focus-mode overlay is summoned by shoving the pointer at a window edge, so
 * the reveal gesture ENDS with the cursor outside the window -- where the
 * renderer receives no mouse events at all. "Has the user moved away?" therefore
 * cannot be answered in the page: only the main process can follow the cursor
 * past a window edge, via `screen.getCursorScreenPoint()`. Same reason and same
 * shape as the companion overlay's cross-display drag poll
 * (crew-companion/petOverlay.js) and Mochi's (mochi/petOverlays.js).
 *
 * The dismissal signal is the cursor's distance from the window: parking the
 * pointer just outside keeps the overlay, heading for another window dismisses
 * it, and coming back before it has travelled far means the user never left --
 * how Arc's and Zen's edge-revealed sidebars behave.
 *
 * The poll runs ONLY between a renderer arming it (a reveal is open and the
 * cursor just left) and the one transition it reports, so an idle window costs
 * nothing.
 */

/** Poll cadence while the cursor is off-window. Coarse next to the companion's
 *  16ms hitbox poll on purpose: nothing is drawn from this, it only has to beat
 *  a hand movement across the away distance, and it runs on the main thread. */
const CURSOR_POLL_MS = 60;

/** How far beyond the window edge the cursor must travel before the reveal is
 *  dismissed. Roughly a thumb's width of deliberate travel: far enough that
 *  overshooting the edge to summon the overlay does not immediately dismiss it,
 *  close enough that heading for another window does. */
const CURSOR_AWAY_PX = 120;

/**
 * Distance from a point to a rectangle, in the same screen pixels
 * `getCursorScreenPoint` and `getBounds` both report. 0 anywhere inside (and on
 * the edge), otherwise the straight-line gap to the nearest edge -- so exiting
 * through a corner is measured on both axes rather than only the one the pointer
 * happened to cross. Unreadable geometry answers 0, which is "still inside" and
 * therefore never dismisses anything.
 */
function cursorDistanceOutside(bounds, point) {
  if (!bounds || !point) return 0;
  const { x, y, width, height } = bounds;
  const values = [x, y, width, height, point.x, point.y];
  if (!values.every((value) => Number.isFinite(value))) return 0;
  const dx = Math.max(x - point.x, 0, point.x - (x + width));
  const dy = Math.max(y - point.y, 0, point.y - (y + height));
  return Math.hypot(dx, dy);
}

/** The channel the renderer's `watchCursorAway` bridge listens on. */
const CURSOR_AWAY_CHANNEL = "focus-mode:cursor-away";

/**
 * Build the watcher. `screen` is injected rather than required so this module is
 * loadable from a plain node test, which must never load real Electron.
 *
 * `awayPx` / `pollMs` are injected for tests only; production passes neither and
 * gets the constants above, so there is one threshold in the product.
 */
function createFocusCursorWatch({
  screen,
  awayPx = CURSOR_AWAY_PX,
  pollMs = CURSOR_POLL_MS,
  log = () => {},
} = {}) {
  /**
   * Windows currently being watched -> whether the cursor has been SEEN outside
   * that window yet.
   *
   * That flag is load-bearing. The renderer arms the watch from `mouseout`, and
   * the cursor is then still within a pixel of the boundary -- inside
   * `getBounds()` as often as not (the bounds include the titlebar, which the
   * page's viewport does not). Reporting re-entry before a first outside sample
   * would cancel every dismissal the instant it was armed.
   * @type {Map<Electron.BrowserWindow, {sawOutside: boolean}>}
   */
  const watched = new Map();
  let timer = null;

  function boundsOf(win) {
    try {
      return win.getBounds();
    } catch {
      return null; // mid-teardown
    }
  }

  /** Report the one transition and stop watching that window. */
  function report(win, away) {
    watched.delete(win);
    if (watched.size === 0) stopTimer();
    try {
      if (!win.isDestroyed()) win.webContents.send(CURSOR_AWAY_CHANNEL, away);
    } catch {
      /* renderer gone between the check and the send */
    }
  }

  /**
   * One poll tick. Split out from the interval so a test can drive it
   * deterministically, exactly as petOverlay's `dragPollOnce` is.
   */
  function pollOnce() {
    if (watched.size === 0) return;
    let cursor;
    try {
      cursor = screen.getCursorScreenPoint();
    } catch {
      return; // no cursor available (headless, locked screen) -- report nothing
    }
    for (const [win, state] of [...watched]) {
      let destroyed = false;
      try {
        destroyed = win.isDestroyed();
      } catch {
        destroyed = true;
      }
      if (destroyed) {
        watched.delete(win);
        continue;
      }
      const bounds = boundsOf(win);
      if (!bounds) continue;
      const distance = cursorDistanceOutside(bounds, cursor);
      if (distance > 0) {
        state.sawOutside = true;
        if (distance >= awayPx) report(win, true);
        continue;
      }
      // Back inside. Told to the renderer rather than left to its own
      // `mouseover`, because the band the pointer re-enters through can be a
      // `-webkit-app-region: drag` rect, which the compositor resolves before
      // hit-testing -- so the page may never see that re-entry at all.
      if (state.sawOutside) report(win, false);
    }
    if (watched.size === 0) stopTimer();
  }

  function startTimer() {
    if (timer !== null) return;
    timer = setInterval(pollOnce, pollMs);
    // A background poll must never be the reason the process cannot exit.
    timer.unref?.();
  }

  function stopTimer() {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  /**
   * Arm or disarm the watch for one window. The renderer arms it when a reveal
   * is open and the pointer has left, and disarms it on re-entry, on close, and
   * on unmount -- so a window is watched only while the answer can still matter.
   */
  function watch(win, on) {
    if (!win) return;
    if (!on) {
      watched.delete(win);
      if (watched.size === 0) stopTimer();
      return;
    }
    if (watched.has(win)) return;
    watched.set(win, { sawOutside: false });
    log(`focus-mode: watching off-window cursor (${watched.size} window(s))`);
    startTimer();
  }

  /** Drop every watch — for app quit. */
  function stopAll() {
    watched.clear();
    stopTimer();
  }

  return {
    watch,
    pollOnce,
    stopAll,
    watchedCount: () => watched.size,
    isPolling: () => timer !== null,
  };
}

module.exports = {
  createFocusCursorWatch,
  cursorDistanceOutside,
  CURSOR_AWAY_CHANNEL,
  CURSOR_AWAY_PX,
  CURSOR_POLL_MS,
};
