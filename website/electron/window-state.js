"use strict";

// Persisted main-window geometry.
//
// Without this, createWindow() always rebuilds the window at the hardcoded
// default size. When the user quits while in native macOS fullscreen, macOS
// restores the fullscreen Space on the next launch and drops our fixed-size
// window into it. That surfaces as the two long-standing symptoms:
//   - "blacked out": the small WebContentsView doesn't fill the fullscreen
//     window, so the dark BaseWindow backgroundColor (#0f1117) shows through.
//   - "super tiny": the window comes up at the default size inside the big
//     restored fullscreen Space (or at a degenerate restored frame).
//
// The fix is to own the geometry: persist the normal (restore) bounds plus a
// fullScreen flag, then recreate the window with those bounds and re-enter
// fullscreen ourselves so the window fills its Space and the view lays out.

const DEFAULT_BOUNDS = { width: 1280, height: 860 };
const MIN_SIZE = { width: 550, height: 600 };

// At least this many px of the window must overlap a display work area for the
// saved position to be considered usable (else a window saved on an
// now-disconnected monitor would be restored off-screen).
const VISIBLE_MARGIN = 80;

const isFiniteNum = (v) => typeof v === "number" && Number.isFinite(v);

/**
 * True if the window rect overlaps some display's work area by VISIBLE_MARGIN
 * on both axes.
 *
 * @param {Array<{workArea:{x:number,y:number,width:number,height:number}}>} displays
 * @param {{x:number,y:number,width:number,height:number}} b
 */
function isVisibleOn(displays, b) {
  if (!Array.isArray(displays) || displays.length === 0) return false;
  return displays.some((d) => {
    const wa = d && d.workArea;
    if (!wa) return false;
    const overlapW = Math.min(b.x + b.width, wa.x + wa.width) - Math.max(b.x, wa.x);
    const overlapH = Math.min(b.y + b.height, wa.y + wa.height) - Math.max(b.y, wa.y);
    return overlapW >= VISIBLE_MARGIN && overlapH >= VISIBLE_MARGIN;
  });
}

/**
 * Decide the constructor geometry a freshly-created window should use, given
 * previously-saved state and the currently-connected displays. Pure: takes no
 * Electron objects.
 *
 * Rules:
 *   - Missing/malformed saved state            -> default size, not fullscreen.
 *   - Saved size NaN/non-finite or below the    -> default size. This is what
 *     minimum window size                          kills the "super tiny" window.
 *   - Saved position not visible on any         -> drop x/y (OS centers it),
 *     connected display                            keep the (valid) size.
 *   - fullScreen is carried through as a boolean only — never the fs frame.
 *
 * @param {*} saved  Raw value read from persistent storage (may be undefined).
 * @param {{displays?:Array, defaults?:{width:number,height:number}, minSize?:{width:number,height:number}}} [opts]
 * @returns {{width:number, height:number, fullScreen:boolean, x?:number, y?:number}}
 */
function sanitizeWindowState(saved, opts = {}) {
  const defaults = opts.defaults || DEFAULT_BOUNDS;
  const minSize = opts.minSize || MIN_SIZE;
  const displays = Array.isArray(opts.displays) ? opts.displays : [];

  const fullScreen = !!(saved && saved.fullScreen);
  // Legacy saved states predate the key; !! coerces missing/odd values to false.
  const alwaysOnTop = !!(saved && saved.alwaysOnTop);

  let width = isFiniteNum(saved && saved.width) ? saved.width : null;
  let height = isFiniteNum(saved && saved.height) ? saved.height : null;

  // Reject degenerate / too-small sizes -> fall back to defaults.
  if (width === null || height === null || width < minSize.width || height < minSize.height) {
    width = defaults.width;
    height = defaults.height;
  }

  const result = { width, height, fullScreen, alwaysOnTop };

  const x = isFiniteNum(saved && saved.x) ? saved.x : null;
  const y = isFiniteNum(saved && saved.y) ? saved.y : null;
  if (x !== null && y !== null && isVisibleOn(displays, { x, y, width, height })) {
    result.x = x;
    result.y = y;
  }
  // else: omit x/y so Electron centers the window on the primary display.

  return result;
}

/**
 * Capture the geometry to persist from a live BaseWindow/BrowserWindow.
 *
 * Uses getNormalBounds() so we NEVER store the fullscreen (or maximized) frame
 * as the literal window size — only the size to return to when fullscreen is
 * exited. The fullscreen state is stored as a separate flag.
 *
 * @returns {{x:number,y:number,width:number,height:number,fullScreen:boolean}|null}
 */
function captureWindowState(win) {
  if (!win || (typeof win.isDestroyed === "function" && win.isDestroyed())) return null;
  const fullScreen = typeof win.isFullScreen === "function" ? win.isFullScreen() : false;
  const alwaysOnTop = typeof win.isAlwaysOnTop === "function" ? win.isAlwaysOnTop() : false;
  const b =
    (typeof win.getNormalBounds === "function"
      ? win.getNormalBounds()
      : typeof win.getBounds === "function"
        ? win.getBounds()
        : null) || {};
  if (!isFiniteNum(b.width) || !isFiniteNum(b.height)) return null;
  return { x: b.x, y: b.y, width: b.width, height: b.height, fullScreen, alwaysOnTop };
}

module.exports = {
  DEFAULT_BOUNDS,
  MIN_SIZE,
  VISIBLE_MARGIN,
  isVisibleOn,
  sanitizeWindowState,
  captureWindowState,
};
