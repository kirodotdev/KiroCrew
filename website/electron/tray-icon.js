"use strict";
//
// Whether the app shows its tray icon (the menu bar icon on macOS), kept out of
// window-lifecycle.js so the rule is unit-testable without Electron (mirrors
// close-action.js).
//
// The icon shows by default. On Windows and Linux it is the way back to a window
// the close button hid, so it can be hidden only while the close button quits.
// macOS keeps the Dock icon, so the rule does not apply there.

const { getCloseAction, CLOSE_ACTION_QUIT } = require("./close-action");

/** electron-store key holding the user's choice. */
const SHOW_TRAY_ICON_KEY = "showTrayIcon";

/**
 * Whether the icon shows, and whether the user may hide it right now.
 *
 * Only an exact `false` hides it, and only while it can be hidden, so a missing
 * or hand-edited value never strands a hidden window.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {{isMac: boolean}} o
 * @returns {{show: boolean, canHide: boolean}}
 */
function getTrayIcon(store, { isMac }) {
  const canHide = isMac || getCloseAction(store) === CLOSE_ACTION_QUIT;
  return { show: !(canHide && store.get(SHOW_TRAY_ICON_KEY) === false), canHide };
}

/**
 * Record the user's choice. A hide is refused while the icon cannot be hidden,
 * and nothing is written then.
 *
 * @param {{get: (key: string) => unknown, set: (key: string, value: unknown) => void}} store
 * @param {unknown} show
 * @param {{isMac: boolean}} o
 * @returns {{show: boolean, canHide: boolean}} the state after the write
 */
function setTrayIcon(store, show, { isMac }) {
  const next = show !== false;
  if (next || getTrayIcon(store, { isMac }).canHide) {
    // electron-store rewrites the whole file on every set; skip a no-op.
    if (store.get(SHOW_TRAY_ICON_KEY) !== next) store.set(SHOW_TRAY_ICON_KEY, next);
  }
  return getTrayIcon(store, { isMac });
}

module.exports = {
  SHOW_TRAY_ICON_KEY,
  getTrayIcon,
  setTrayIcon,
};
