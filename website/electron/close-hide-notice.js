"use strict";
//
// What tells the user that closing the main window left Kiro Crew running: the
// one-time notice the first close-to-background shows, and the tray menu whose
// quit item names the gateway quitting stops. Pure, injectable helpers like
// close-action.js, so both are testable without Electron.
//
// Main-process text is English-only, like the other main-process notices
// (website/docs/i18n-catalog.md, "Scope").

/** electron-store key set once the first close-to-background notice was shown. */
const CLOSE_HIDE_NOTICE_KEY = "closeHideNoticeShown";

/**
 * Where the close-button switch lives: Settings → Display → View, scrolled to and
 * highlighted (the route the settings registry generates for this row).
 */
const CLOSE_ACTION_SETTING_ROUTE =
  "/settings/display/view?highlight=display.keep-running-in-the-background-after-closing-the-window";

/**
 * The tray menu's quit item. It names the gateway when quitting stops one: the
 * Quit path stops only a gateway this app started, never one it connected to.
 *
 * @param {string} appName
 * @param {boolean} stopsGateway
 * @returns {string}
 */
function trayQuitLabel(appName, stopsGateway) {
  return stopsGateway ? `Quit ${appName} and Stop Its Gateway` : `Quit ${appName}`;
}

/**
 * The tray context menu template.
 *
 * @param {{appName: string, stopsGateway: boolean, onShow: Function,
 *   onNewConnection: Function, onOpenConfig: Function, onQuit: Function}} o
 * @returns {object[]}
 */
function trayMenuTemplate({ appName, stopsGateway, onShow, onNewConnection, onOpenConfig, onQuit }) {
  return [
    { label: `Show ${appName}`, click: onShow },
    { type: "separator" },
    { label: "New Connection Window…", click: onNewConnection },
    { type: "separator" },
    { label: "Open Config File", click: onOpenConfig },
    { type: "separator" },
    { label: trayQuitLabel(appName, stopsGateway), click: onQuit },
  ];
}

/**
 * Text of the notice. The body names the setting's place in words too, so the
 * way there does not depend on the notification being clickable (some Linux
 * notification servers offer no click action).
 *
 * @param {{appName: string, isMac: boolean, stopsGateway: boolean}} o
 * @returns {{title: string, body: string}}
 */
function closeHideNotice({ appName, isMac, stopsGateway }) {
  // On macOS the menu bar icon can be turned off; the Dock is always there.
  const where = isMac ? "the Dock or the menu bar icon" : "the tray icon";
  return {
    title: stopsGateway
      ? `${appName} and its gateway are still running`
      : `${appName} is still running`,
    body:
      `Quit it from ${where}. To make the close button quit, click here or open ` +
      "Settings → Display → View → Desktop app.",
  };
}

/**
 * Build the once-per-install notice for a close that hides the main window.
 *
 * The stored key is written once the notice is known to have been shown (the
 * notification's `show` event, or right after `show()` on Linux, whose
 * notifications emit no such event), or at once when `Notification.isSupported()`
 * is false, because no notice can show there. Without a Notification class it is
 * never written. A notice the system drops (notification
 * permission not granted yet, Focus Assist) therefore comes back on a later
 * launch. Within one run an in-memory flag keeps a burst of closes from stacking
 * copies; a Windows `failed` clears it so the next close tries again.
 *
 * @param {object} deps
 * @param {{get: Function, set: Function}} deps.store
 * @param {Function|undefined} deps.Notification  Electron's Notification class.
 * @param {string} deps.platform  process.platform.
 * @param {(line: string) => void} [deps.log]
 * @returns {(o: {notice: {title: string, body: string}, onClick: () => void}) => boolean}
 *   shows the notice when it is due; true when one was shown.
 */
function createCloseHideNotice({ store, Notification, platform, log = () => {} }) {
  let outThisRun = false;
  // Held so the click handler is not garbage-collected while the notice is up.
  let current = null;
  const remember = () => {
    if (store.get(CLOSE_HIDE_NOTICE_KEY) !== true) store.set(CLOSE_HIDE_NOTICE_KEY, true);
  };
  return ({ notice, onClick }) => {
    if (outThisRun || store.get(CLOSE_HIDE_NOTICE_KEY) === true) return false;
    try {
      if (typeof Notification !== "function") return false;
      if (typeof Notification.isSupported === "function" && !Notification.isSupported()) {
        remember(); // it can never show here, so stop trying on every close
        return false;
      }
      const notification = new Notification({ title: notice.title, body: notice.body });
      outThisRun = true;
      current = notification;
      notification.on("show", remember);
      notification.on("failed", () => {
        outThisRun = false;
        if (current === notification) current = null;
      });
      notification.on("click", onClick);
      notification.show();
      // shortcut: Linux emits no `show`, so a server that drops the notice (Do
      // Not Disturb) still marks it shown; listen for a delivery signal there if
      // Electron ever exposes one.
      if (platform === "linux") remember();
      return true;
    } catch (e) {
      // Optional: the window has already hidden, which is what the close asked for.
      outThisRun = false;
      log(`close: background notice failed: ${e && e.message}`);
      return false;
    }
  };
}

/**
 * The main window's side of both: the notice a close-to-background shows, with
 * the click that opens the setting, and the tray menu rebuilt from whether the
 * app runs its own gateway. Built from the lifecycle's effects so
 * window-lifecycle.js only calls it.
 *
 * @param {object} deps
 * @param {{name: string}} deps.app  read lazily, like Menu.
 * @param {{buildFromTemplate: Function}} deps.Menu
 * @param {Function|undefined} deps.Notification
 * @param {{get: Function, set: Function}} deps.store
 * @param {string} deps.platform
 * @param {(line: string) => void} [deps.log]
 * @param {() => boolean} deps.stopsGatewayOnQuit
 * @param {() => object|null} deps.getMainWindow
 * @param {() => object|null} deps.getTray
 * @param {(o: {focus: boolean}) => boolean} deps.showMainWindow
 * @param {Function} deps.onShow  the tray's Show item (shared with its icon click)
 * @param {Function} deps.onNewConnection
 * @param {Function} deps.onOpenConfig
 * @param {Function} deps.onQuit
 * @returns {{noticeOnHide: () => boolean, refreshTrayMenu: () => void,
 *   navigation: {navigateReady: () => void, navigationStarted: () => void}}}
 *   `navigation` takes the main dashboard's `navigate-ready` acks (main.js
 *   feeds it through installNavigateReady, as for deep links).
 */
function createCloseHideWiring({
  app, Menu, Notification, store, platform, log = () => {}, stopsGatewayOnQuit,
  getMainWindow, getTray, showMainWindow, onShow, onNewConnection, onOpenConfig, onQuit,
}) {
  const stopsGateway = () => stopsGatewayOnQuit() === true;
  const showNotice = createCloseHideNotice({ store, Notification, platform, log });

  // Whether the main dashboard document has mounted its `navigate` listener, and
  // whether a click is waiting for it. A click while the window still shows the
  // loading or gateway-error page is held, like a kirocrew:// link, and sent on
  // the next ack. Not deliverDeepLink: its origin check would drop the route on
  // a main window that serves a remote gateway.
  let ready = false;
  let held = false;
  function sendHeld() {
    if (!held || !ready) return;
    const win = getMainWindow();
    const wc = win && win._mcView && win._mcView.webContents;
    if (!wc || wc.isDestroyed()) return;
    held = false;
    wc.send("navigate", CLOSE_ACTION_SETTING_ROUTE);
  }

  // The notice's click: bring the main window back on the close-button setting.
  // The main window, not the focused one: connection windows have no such row.
  function openCloseActionSetting() {
    if (!showMainWindow({ focus: true })) return;
    held = true;
    sendHeld();
  }

  return {
    navigation: {
      navigateReady() { ready = true; sendHeld(); },
      navigationStarted() { ready = false; },
    },
    // Once per install: say the app is still running and how to change that.
    noticeOnHide: () => showNotice({
      notice: closeHideNotice({ appName: app.name, isMac: platform === "darwin", stopsGateway: stopsGateway() }),
      onClick: openCloseActionSetting,
    }),
    // Rebuilt, not built once: the quit label follows whether the app runs its
    // own gateway, and main.js calls this whenever the supervisor's child changes.
    refreshTrayMenu() {
      const tray = getTray();
      if (!tray) return;
      tray.setContextMenu(Menu.buildFromTemplate(trayMenuTemplate({
        appName: app.name, stopsGateway: stopsGateway(), onShow, onNewConnection, onOpenConfig, onQuit,
      })));
    },
  };
}

module.exports = {
  CLOSE_HIDE_NOTICE_KEY,
  CLOSE_ACTION_SETTING_ROUTE,
  trayQuitLabel,
  trayMenuTemplate,
  closeHideNotice,
  createCloseHideNotice,
  createCloseHideWiring,
};
