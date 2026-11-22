"use strict";
//
// Whether minimizing the main window hides it to the tray instead of the
// taskbar, kept out of window-lifecycle.js so the rule is unit-testable without
// Electron (mirrors tray-icon.js).
//
// Off by default and never on macOS. It is offered only where a tray can bring
// the window back: always on Windows, and on Linux only when the desktop runs a
// StatusNotifier tray (GNOME has none by default). While the tray icon is
// hidden it stays off.

const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const { resolveSystemBinary } = require("./linux-desktop-integration");

/** electron-store key holding the user's choice. */
const MINIMIZE_TO_TRAY_KEY = "minimizeToTray";

// Asks the StatusNotifier watcher whether a tray host is listening. With no
// watcher on the bus the call fails, which reads as no tray.
const STATUS_NOTIFIER_HOST_QUERY = [
  "--session",
  "--print-reply=literal",
  "--dest=org.kde.StatusNotifierWatcher",
  "/StatusNotifierWatcher",
  "org.freedesktop.DBus.Properties.Get",
  "string:org.kde.StatusNotifierWatcher",
  "string:IsStatusNotifierHostRegistered",
];

/**
 * Whether this desktop has a tray that can bring a hidden window back.
 *
 * @param {string} platform - `process.platform`
 * @param {typeof execFile} [run] - injected by tests
 * @param {(name: string) => string | null} [resolveBin] - injected by tests
 * @returns {Promise<boolean>}
 */
function hasTray(platform, run = execFile, resolveBin = (name) => resolveSystemBinary(fs, path, name)) {
  if (platform === "win32") return Promise.resolve(true);
  if (platform !== "linux") return Promise.resolve(false);
  // Only from the fixed system directories, never PATH: this runs in the
  // un-sandboxed main process. Not found there reads as no tray.
  const dbusSend = resolveBin("dbus-send");
  if (!dbusSend) return Promise.resolve(false);
  return new Promise((resolve) => {
    run(dbusSend, STATUS_NOTIFIER_HOST_QUERY, { timeout: 2000 }, (err, stdout) => {
      resolve(!err && String(stdout).trim().endsWith("boolean true"));
    });
  });
}

/**
 * Whether the switch is offered, whether it can be turned on right now, and
 * whether it is on.
 *
 * Only an exact `true` turns it on, and only while it can be, so a missing or
 * hand-edited value never hides a window with no icon to bring it back.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {{trayAvailable: boolean, trayShown: boolean}} o
 * @returns {{on: boolean, offered: boolean, canEnable: boolean}}
 */
function getMinimizeToTray(store, { trayAvailable, trayShown }) {
  const canEnable = trayAvailable && trayShown;
  return { on: canEnable && store.get(MINIMIZE_TO_TRAY_KEY) === true, offered: trayAvailable, canEnable };
}

/**
 * Record the user's choice. Turning it on is refused while it cannot be, and
 * nothing is written then.
 *
 * @param {{get: (key: string) => unknown, set: (key: string, value: unknown) => void}} store
 * @param {unknown} on
 * @param {{trayAvailable: boolean, trayShown: boolean}} o
 * @returns {{on: boolean, offered: boolean, canEnable: boolean}} the state after the write
 */
function setMinimizeToTray(store, on, o) {
  const next = on === true;
  if (!next || getMinimizeToTray(store, o).canEnable) {
    // electron-store rewrites the whole file on every set; skip a no-op.
    if (store.get(MINIMIZE_TO_TRAY_KEY) !== next) store.set(MINIMIZE_TO_TRAY_KEY, next);
  }
  return getMinimizeToTray(store, o);
}

/**
 * The main window's `minimize` listener: hide the minimized window to the tray
 * when the user chose that and a tray icon is there to bring it back.
 *
 * @param {{
 *   win: {isDestroyed: () => boolean, isMinimized: () => boolean, hide: () => void},
 *   store: {get: (key: string) => unknown},
 *   trayShown: () => boolean,
 *   trayAvailable: () => Promise<boolean>,
 *   log: (line: string) => void,
 * }} o
 * @returns {() => Promise<void> | undefined} the promise is for tests; Electron ignores it
 */
function createMinimizeHandler({ win, store, trayShown, trayAvailable, log }) {
  return () => {
    // The desktop is asked only once the user turned the switch on.
    if (store.get(MINIMIZE_TO_TRAY_KEY) !== true) return undefined;
    return trayAvailable().then((available) => {
      if (!getMinimizeToTray(store, { trayAvailable: available, trayShown: trayShown() }).on) return;
      // A window restored while the tray was being checked stays on screen.
      if (win.isDestroyed() || !win.isMinimized()) return;
      log("minimize: hiding to tray");
      win.hide();
    });
  };
}

/**
 * The main process's side of the setting: the Settings facade and the main
 * window's `minimize` listener, both reading the tray live.
 *
 * @param {{
 *   store: {get: (key: string) => unknown, set: (key: string, value: unknown) => void},
 *   platform: string,
 *   trayShown: () => boolean,
 *   log: (line: string) => void,
 *   trayAvailable?: () => Promise<boolean>,
 * }} o
 */
function createMinimizeToTray({ store, platform, trayShown, log, trayAvailable = () => hasTray(platform) }) {
  const options = async () => ({ trayAvailable: await trayAvailable(), trayShown: trayShown() });
  return {
    get: async () => getMinimizeToTray(store, await options()),
    async set(on) {
      const state = setMinimizeToTray(store, on, await options());
      log(`minimizeToTray set to ${state.on}`);
      return state;
    },
    minimizeHandler: (win) => createMinimizeHandler({ win, store, trayShown, trayAvailable, log }),
  };
}

module.exports = {
  MINIMIZE_TO_TRAY_KEY,
  createMinimizeHandler,
  createMinimizeToTray,
  getMinimizeToTray,
  hasTray,
  setMinimizeToTray,
};
