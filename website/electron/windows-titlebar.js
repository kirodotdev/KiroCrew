"use strict";

// Windows caption-control overlay painting, kept out of main.js so it is unit
// testable without an Electron runtime: the windows, the resolved theme and the
// header height are all injected (same idiom as find-bin.js).
//
// On Windows the dashboard windows are created with `titleBarStyle: "hidden"`
// plus a transparent `titleBarOverlay`, which leaves the native
// minimize/maximize/close buttons floating over the dashboard's own 42px header.
// Two things then have to be repainted whenever they change:
//   - the SYMBOL color, so the glyphs stay legible against the active theme
//     (the background stays fully transparent so the header shows through), and
//   - the overlay HEIGHT, which is in physical px and so must track the zoom
//     factor or the buttons stop being vertically centered in the header row.

// Deliberately not `--text`: the caption glyphs are drawn by Windows over the
// header, so they need a fixed high-contrast pair rather than a theme variable
// the compositor cannot resolve.
const SYMBOL_DARK = "#f4f0fa";
const SYMBOL_LIGHT = "#211d28";
// Fully transparent background: the dashboard header paints the strip instead,
// so the overlay never shows a seam when a theme changes the header color.
const OVERLAY_BACKGROUND = "#00000000";

/**
 * Overlay options for a resolved mode and zoom factor.
 *
 * @param {"dark"|"light"} mode
 * @param {number} zoom  renderer zoom factor; non-finite/absent falls back to 1
 * @param {number} headerPx  header height in CSS px
 */
function titleBarOverlayOptions(mode, zoom, headerPx) {
  const factor = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  return {
    color: OVERLAY_BACKGROUND,
    symbolColor: mode === "dark" ? SYMBOL_DARK : SYMBOL_LIGHT,
    height: Math.round(headerPx * factor),
  };
}

/**
 * Repaint ONE window's overlay. Returns true only if the overlay was actually
 * applied, so a caller can tell "painted" from "not an overlay window".
 *
 * Throwing is swallowed ON PURPOSE, and it is the whole reason this is a
 * function rather than an inline call: only the dashboard windows carry an
 * overlay. The modal helpers (gateway-error dialog, remote-host prompt) are
 * plain framed windows, and Electron THROWS on setTitleBarOverlay for those.
 * `_mcView` is not a usable substitute test — it is assigned by
 * setupWindowContents, which those modals never call, so keying off it would
 * also skip a real dashboard window whose view happens to be mid-teardown.
 *
 * @param {object} win  BaseWindow-like
 * @param {"dark"|"light"} mode
 * @param {number} headerPx
 */
function paintTitleBarOverlay(win, mode, headerPx) {
  if (!win || typeof win.setTitleBarOverlay !== "function") return false;
  try {
    if (win.isDestroyed && win.isDestroyed()) return false;
    const view = win._mcView;
    const zoom = view && view.webContents && !view.webContents.isDestroyed()
      ? view.webContents.getZoomFactor()
      : 1;
    win.setTitleBarOverlay(titleBarOverlayOptions(mode, zoom, headerPx));
    return true;
  } catch {
    return false; // framed window with no overlay, or mid-teardown
  }
}

/**
 * Repaint EVERY window, continuing past the ones that cannot take an overlay.
 *
 * Containment is the contract: a theme switch while a modal is open must not
 * leave the windows after that modal painted for the previous theme, which is
 * exactly what an unguarded loop did.
 *
 * @returns {{painted: number, skipped: number}}
 */
function paintAllTitleBarOverlays(windows, mode, headerPx) {
  let painted = 0;
  let skipped = 0;
  for (const win of windows || []) {
    if (paintTitleBarOverlay(win, mode, headerPx)) painted += 1;
    else skipped += 1;
  }
  return { painted, skipped };
}

module.exports = {
  titleBarOverlayOptions,
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
  SYMBOL_DARK,
  SYMBOL_LIGHT,
  OVERLAY_BACKGROUND,
};
