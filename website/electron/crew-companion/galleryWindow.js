/**
 * galleryWindow.js — the avatar gallery, in its own window.
 *
 * A fixed-size card rather than a resizable document, sized to fit three columns of
 * the 200px-minimum grid plus padding. Frameless and transparent to match the panel,
 * so it can have rounded corners and its own shadow instead of OS chrome.
 *
 * The cost of frameless is accepted deliberately: no traffic lights, so the window
 * supplies its own ✕ and Escape, and there is no minimise or zoom.
 *
 * Unlike the panel it does NOT close on blur. The panel is a transient card you
 * glance at; this is a surface you browse — importing, editing, recolouring — and
 * yanking it away on a stray click elsewhere would be hostile.
 */

const path = require("path");
const { BrowserWindow, ipcMain, shell } = require("electron");
const { companionPageUrl } = require("./pageUrl");

/** Transparent gutter for the card's own shadow. */
const GALLERY_PAD = 24;
const GALLERY_SIZE = {
  width: 760 + GALLERY_PAD * 2,
  height: 560 + GALLERY_PAD * 2,
};

let galleryWin = null;
let baseUrl = "";
let credential = "";
let log = () => {};

/** Told when the active avatar changes, so every overlay can be refreshed. */
let onAppearanceChanged = null;

/**
 * Told when the gallery opens / closes, so the companion overlay can hold still
 * while the user is browsing avatars and resume wandering afterwards. Mirrors the
 * panel's closed-handler wiring — the gallery is its own window with no other signal
 * back to the overlay.
 */
let onOpened = null;
let onClosed = null;

function setGalleryOpenedHandler(fn) {
  onOpened = typeof fn === "function" ? fn : null;
}

function setGalleryClosedHandler(fn) {
  onClosed = typeof fn === "function" ? fn : null;
}

function setGalleryTarget(url, token) {
  baseUrl = url || "";
  credential = token || "";
}

function setAppearanceChangedHandler(fn) {
  onAppearanceChanged = typeof fn === "function" ? fn : null;
}

function setGalleryLogger(fn) {
  if (typeof fn === "function") log = fn;
}

function openGalleryWindow() {
  if (!baseUrl) {
    log("crew-companion: no gateway origin yet, deferring gallery");
    return null;
  }

  if (galleryWin && !galleryWin.isDestroyed()) {
    // Already open — bring it forward rather than opening a second copy.
    galleryWin.show();
    galleryWin.focus();
    if (onOpened) onOpened();
    return galleryWin;
  }

  galleryWin = new BrowserWindow({
    ...GALLERY_SIZE,
    resizable: false,
    title: "Avatar gallery",
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    // The card draws its own CSS shadow.
    hasShadow: false,
    alwaysOnTop: true,
    // Painted only once the first frame is ready, so there is no flash of an
    // unstyled or wrongly-coloured window before the theme lands.
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  galleryWin.loadURL(companionPageUrl(baseUrl, "gallery.html", credential));
  galleryWin.once("ready-to-show", () => {
    if (galleryWin && !galleryWin.isDestroyed()) galleryWin.show();
    if (onOpened) onOpened();
  });
  galleryWin.setAlwaysOnTop(true, "modal-panel");
  galleryWin.on("closed", () => {
    galleryWin = null;
    if (onClosed) onClosed();
  });

  return galleryWin;
}

function closeGalleryWindow() {
  const was = Boolean(galleryWin && !galleryWin.isDestroyed());
  if (was) galleryWin.destroy();
  galleryWin = null;
  // `destroy()` does not always deliver 'closed' synchronously; the overlay must not
  // be left believing the gallery is still up (and stay parked forever).
  if (was && onClosed) onClosed();
}

function galleryIsOpen() {
  return Boolean(galleryWin && !galleryWin.isDestroyed());
}

function registerGalleryIpc() {
  ipcMain.on("crew-companion:gallery-open", () => openGalleryWindow());
  ipcMain.on("crew-companion:gallery-close", () => closeGalleryWindow());
  ipcMain.on("crew-companion:appearance-changed", () => {
    if (onAppearanceChanged) onAppearanceChanged();
  });
  ipcMain.on("crew-companion:open-external", (_event, url) => {
    // Only http(s) reaches the OS handler. A renderer is web content, so anything it
    // hands over is untrusted — and a `file://` or custom scheme here would ask the
    // system to open a local file or launch another app.
    if (typeof url !== "string" || !/^https:\/\//i.test(url)) {
      log(`crew-companion: refusing to open a non-HTTPS link: ${String(url).slice(0, 40)}`);
      return;
    }
    void shell.openExternal(url);
  });
}

module.exports = {
  GALLERY_PAD,
  GALLERY_SIZE,
  openGalleryWindow,
  closeGalleryWindow,
  galleryIsOpen,
  registerGalleryIpc,
  setGalleryTarget,
  setGalleryLogger,
  setGalleryOpenedHandler,
  setGalleryClosedHandler,
  setAppearanceChangedHandler,
};
