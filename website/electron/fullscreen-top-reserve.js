"use strict";

// Whether macOS's menu bar can slide down over a native-fullscreen window.
//
// In native fullscreen AppKit owns the top strip: pointing at it reveals the
// menu bar OVER the window, so the dashboard keeps a band clear above its
// header (MAC_FULLSCREEN_TOP_RESERVE_PX in website/src/lib/electron.ts). On a
// built-in display with a camera housing ("notch") macOS instead places the
// fullscreen window BELOW the menu bar strip, so its top edge sits lower than
// the display's top edge and the menu bar never covers it. Reserving the band
// there only adds an empty gap under the menu bar.
//
// The verdict compares the window's top edge with the top edge of the display
// it is on. Anything unreadable (no screen module, a destroyed window, a fake
// in tests) answers `true`, which keeps the reserve: a wasted band is a cosmetic
// cost, a covered header is an unclickable one.

/**
 * @param {object} win     BrowserWindow-like: isDestroyed / isFullScreen / getBounds.
 * @param {object} screen  Electron's `screen` module (getDisplayMatching).
 * @returns {boolean} false only when the window is fullscreen and starts below
 *   the top edge of its display; true otherwise.
 */
function menuBarCanOverlap(win, screen) {
  try {
    if (!win || win.isDestroyed() || !win.isFullScreen()) return true;
    if (!screen || typeof screen.getDisplayMatching !== "function") return true;
    const bounds = win.getBounds();
    const display = screen.getDisplayMatching(bounds);
    const top = display && display.bounds && display.bounds.y;
    if (!Number.isFinite(bounds && bounds.y) || !Number.isFinite(top)) return true;
    return bounds.y <= top;
  } catch {
    return true;
  }
}

module.exports = { menuBarCanOverlap };
