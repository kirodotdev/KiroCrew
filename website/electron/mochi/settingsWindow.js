/**
 * settingsWindow.js — Mochi's Settings window.
 *
 * Its own window, mirroring the original (main/settingsWindowManager.ts):
 * 420x560, min 360x400, opaque #1e1e2e, alwaysOnTop at the "modal-panel" level,
 * and a singleton that focuses an existing window instead of opening a second.
 *
 * WHY NOT AN IN-PANEL OVERLAY
 * The port first rendered Settings inside the chat panel. That covered the
 * conversation and squeezed a form the original gave 420px into the 320px panel.
 * Settings is a utility window in the original and is one here again.
 *
 * WHY THE CLOSE INTERCEPTOR EXISTS
 * The Settings renderer stages every control locally until Save, so destroying
 * the window on the native close button would silently drop pending edits. The
 * `close` handler therefore asks the renderer first (it runs the Unsaved
 * Changes guard) and only destroys once the renderer decides — with a bounded
 * force-close fallback so a wedged or absent renderer can never leave an
 * unclosable always-on-top window. The explicit teardown paths (Save/Discard,
 * disable, shutdown, instance switching) call closeSettingsWindow(), whose
 * destroy() never emits `close`, so they bypass the guard by construction.
 *
 * The open channel is registered at MODULE LOAD, not on first open: registering
 * lazily inside the open function is what silently broke the Avatars window
 * (the pet's IPC had no listener until something had already opened it once).
 */

const { BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const { mochiPageUrl } = require("./pageUrl");

// Geometry from the original settings window (settingsWindowManager.ts:13-18).
// Sized for the two-column layout: a 148px section rail plus a content column
// that must still fit a labelled select and its description. At the old 360px
// floor the content column collapsed to ~210px and rows clipped mid-word.
const WIN_W = 580;
const WIN_H = 620;
const WIN_MIN_W = 480;
const WIN_MIN_H = 420;
// First-paint title; the renderer refines it per language via document.title,
// which Electron mirrors onto the window.
const SETTINGS_TITLE = "⚙️ Settings";
// How long the shell waits for the renderer to acknowledge a close request
// before force-destroying the window. Long enough for a healthy renderer's
// event loop to run the subscriber; short enough that a wedged renderer does
// not read as a window that refuses to close. Matches the original's fallback.
const CLOSE_ACK_TIMEOUT_MS = 2000;

/** @type {BrowserWindow|null} */
let settingsWindow = null;
/**
 * Force-close timer for the ONE close request that may be in flight. Doubles as
 * the pending marker: non-null means a request was sent and not yet
 * acknowledged, so repeated clicks on the native close button cannot queue
 * duplicate requests or stack timers. Cleared on acknowledgement and when the
 * window goes away.
 * @type {ReturnType<typeof setTimeout>|null}
 */
let closeAckTimer = null;
let lastBaseUrl = "";
/** First-load token for a REMOTE instance; "" for the local gateway. */
let lastToken = "";

/** Remember the gateway origin so the pet's IPC can open the window later. */
function setSettingsBaseUrl(baseUrl, token = "") {
  if (baseUrl) lastBaseUrl = baseUrl;
  lastToken = token || "";
}

function openSettingsWindow(baseUrl, token = "") {
  if (baseUrl) lastBaseUrl = baseUrl;
  if (token) lastToken = token;
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.show();
    settingsWindow.focus();
    return settingsWindow;
  }
  if (!lastBaseUrl) return null;

  const win = new BrowserWindow({
    width: WIN_W,
    height: WIN_H,
    minWidth: WIN_MIN_W,
    minHeight: WIN_MIN_H,
    title: SETTINGS_TITLE,
    center: true,
    resizable: true,
    minimizable: false,
    maximizable: false,
    backgroundColor: "#1e1e2e",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  settingsWindow = win;
  win.setAlwaysOnTop(true, "modal-panel");

  // Both events, because ready-to-show has proved unreliable on these Mochi
  // windows and a settings window that never appears reads as a dead menu item.
  const reveal = () => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  };
  // The close guard only makes sense once the renderer has a frame: before
  // did-finish-load nothing is staged and there is no subscriber, and a
  // webContents.send to a frameless renderer is dropped SILENTLY (not thrown),
  // which would stall the close for the full ack timeout instead of closing.
  let rendererLoaded = false;
  win.once("ready-to-show", reveal);
  win.webContents.once("did-finish-load", () => {
    rendererLoaded = true;
    reveal();
  });

  // Native close (the red x) asks the renderer first — see the module
  // docstring. The acknowledgement means "a subscriber ran the close guard";
  // until it arrives, the bounded timer is the only thing standing between a
  // wedged renderer and an unclosable always-on-top window.
  win.on("close", (event) => {
    // Pre-load there are no edits to guard — let the default close destroy.
    if (!rendererLoaded) return;
    event.preventDefault();
    // At most one request in flight: a second click while the renderer is
    // still deciding must not re-fire the guard or stack force-close timers.
    if (closeAckTimer !== null) return;
    try {
      win.webContents.send("mochi-settings:close-request");
    } catch {
      // The renderer is gone (webContents destroyed mid-close); there is
      // nobody to guard the edits, so an unclosable window is the only thing
      // left to prevent.
      win.destroy();
      return;
    }
    closeAckTimer = setTimeout(() => {
      closeAckTimer = null;
      if (!win.isDestroyed()) win.destroy();
    }, CLOSE_ACK_TIMEOUT_MS);
  });

  win.on("closed", () => {
    clearPendingCloseRequest();
    settingsWindow = null;
  });

  win.loadURL(mochiPageUrl(lastBaseUrl, "settings.html", lastToken));
  return win;
}

function clearPendingCloseRequest() {
  if (closeAckTimer !== null) {
    clearTimeout(closeAckTimer);
    closeAckTimer = null;
  }
}

function closeSettingsWindow() {
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.destroy();
  settingsWindow = null;
}

function isSettingsWindowOpen() {
  return settingsWindow !== null && !settingsWindow.isDestroyed();
}

// Eager registration — see the module docstring.
ipcMain.on("mochi-pet:open-settings", () => openSettingsWindow(lastBaseUrl));
ipcMain.on("mochi-settings:close", () => closeSettingsWindow());
// The acknowledgement is bound to the CURRENT Settings window's own renderer:
// a stale window's late ack (destroyed and replaced while a request was in
// flight) must not clear a request that belongs to its successor.
ipcMain.on("mochi-settings:close-request-ack", (event) => {
  if (
    settingsWindow &&
    !settingsWindow.isDestroyed() &&
    event.sender === settingsWindow.webContents
  ) {
    clearPendingCloseRequest();
  }
});

module.exports = {
  openSettingsWindow,
  closeSettingsWindow,
  isSettingsWindowOpen,
  setSettingsBaseUrl,
  WIN_W,
  WIN_H,
  CLOSE_ACK_TIMEOUT_MS,
};
