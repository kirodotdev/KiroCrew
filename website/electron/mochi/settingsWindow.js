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
 * WHY NO CLOSE INTERCEPTOR
 * The original intercepted `close` to ask its renderer about unsaved edits (with
 * a 2s force-close fallback for a wedged renderer). Our panel persists every
 * control on change — there is no staged, unsaved state to lose — so a plain
 * close is correct and the interceptor would only add a way to get stuck.
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

/** @type {BrowserWindow|null} */
let settingsWindow = null;
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
  win.once("ready-to-show", reveal);
  win.webContents.once("did-finish-load", reveal);

  win.on("closed", () => {
    settingsWindow = null;
  });

  win.loadURL(mochiPageUrl(lastBaseUrl, "settings.html", lastToken));
  return win;
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

module.exports = {
  openSettingsWindow,
  closeSettingsWindow,
  isSettingsWindowOpen,
  setSettingsBaseUrl,
  WIN_W,
  WIN_H,
};
