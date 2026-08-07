/**
 * panelWindow.js — the companion's panel, as its own window.
 *
 * Why a window rather than DOM inside the overlay: the panel is DRAGGABLE
 * independently of the companion, and that is the operating system moving a frameless
 * window via `-webkit-app-region: drag` on its heading. Rendering it inside the
 * full-display overlay makes those drag regions inert, because there is no window to
 * move — the overlay already covers everything.
 *
 * It is transparent and frameless with a gutter around the card, so the card can draw
 * its own CSS shadow. The gutter is deliberately small: it is invisible but still part
 * of the window, so it swallows clicks that would otherwise reach the desktop.
 *
 * Behaviours carried over from the desktop app because each one exists for a reason:
 *
 *   * **It does NOT close on click-away.** The card carries its own ✕ and answers to
 *     Escape, so dismissing it is deliberate. Treating it as a popover meant a stray
 *     click — or simply grabbing the companion to move it — threw away whatever the
 *     user was reading or typing.
 *   * **It follows the companion.** Placement is recomputed from the companion's
 *     current position, so the panel opens beside it wherever it has been dragged.
 */

const path = require("path");
const { BrowserWindow, ipcMain, screen } = require("electron");
const { companionPageUrl } = require("./pageUrl");
const { isPetWindow } = require("./petOverlay");

/** True when the window belongs to the companion (an overlay, or the gallery). */
function isCompanionWindow(win) {
  if (isPetWindow(win)) return true;
  const url = win.webContents ? win.webContents.getURL() : "";
  return url.includes("/app-windows/crew-companion/");
}
const { PANEL_PAD, placePanelNearPet } = require("./panelPlacement");

/** The card's own size, from CARD_SIZE in the desktop app. */
const CARD_SIZE = { width: 320, height: 340 };

let panelWin = null;
let baseUrl = "";
let credential = "";
let log = () => {};

/**
 * True while the breathing exercise is running.
 *
 * Kept because the renderer reports it and it stands the desktop companion aside, so
 * its copy and the overlay's are never on screen together.
 */
let breathingActive = false;

/** Told when the panel closes, so the companion can reset its own state. */
let onClosed = null;

/** True while a secondary view (full list / settings) is open. */
let holdOpen = false;

function setPanelTarget(url, token) {
  baseUrl = url || "";
  credential = token || "";
}

function setPanelLogger(fn) {
  if (typeof fn === "function") log = fn;
}

/** Register the callback that tells the companion the panel has gone. */
function setPanelClosedHandler(fn) {
  onClosed = typeof fn === "function" ? fn : null;
}

/**
 * Open (or re-place) the panel beside the companion.
 *
 * @param {{x:number,y:number,width:number,height:number}} petRect companion rect in
 *   SCREEN coordinates — the caller owns that conversion, since only the renderer
 *   knows where inside its overlay the companion currently is.
 */
function openPanelWindow(petRect) {
  if (!baseUrl) {
    log("crew-companion: no gateway origin yet, deferring panel");
    return null;
  }

  const display = screen.getDisplayNearestPoint({
    x: Math.round(petRect.x + petRect.width / 2),
    y: Math.round(petRect.y + petRect.height / 2),
  });
  const placement = placePanelNearPet(petRect, CARD_SIZE, display.workArea);

  // The window is the card plus its transparent gutter on every side.
  const bounds = {
    x: placement.rect.x - PANEL_PAD,
    y: placement.rect.y - PANEL_PAD,
    width: CARD_SIZE.width + PANEL_PAD * 2,
    height: CARD_SIZE.height + PANEL_PAD * 2,
  };

  if (panelWin && !panelWin.isDestroyed()) {
    // Re-place rather than rebuild: the companion may have moved since it opened.
    panelWin.setBounds(bounds);
    panelWin.showInactive();
    panelWin.focus();
    panelWin.webContents.send("crew-companion:panel-opened", placement.side);
    return panelWin;
  }

  panelWin = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    // The card draws its own CSS shadow; a native one would double up and clip
    // against the transparent gutter.
    hasShadow: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  panelWin.loadURL(companionPageUrl(baseUrl, "panel.html", credential));
  panelWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // "floating" rather than the companion's "modal-panel": the panel sits above
  // ordinary windows but below the companion, so the companion is never hidden
  // behind its own panel.
  panelWin.setAlwaysOnTop(true, "floating");

  panelWin.once("ready-to-show", () => {
    if (panelWin && !panelWin.isDestroyed()) {
      panelWin.show();
      panelWin.focus();
      panelWin.webContents.send("crew-companion:panel-opened", placement.side);
    }
  });


  panelWin.on("closed", () => {
    panelWin = null;
    breathingActive = false;
    holdOpen = false;
    if (onClosed) onClosed();
  });

  return panelWin;
}

function closePanelWindow() {
  const was = Boolean(panelWin && !panelWin.isDestroyed());
  if (was) panelWin.destroy();
  panelWin = null;
  breathingActive = false;
  holdOpen = false;
  // `destroy()` does not always deliver 'closed' synchronously, and the companion must
  // not be left believing the panel is still up.
  if (was && onClosed) onClosed();
}

function panelIsOpen() {
  return Boolean(panelWin && !panelWin.isDestroyed());
}

/** Register the renderer's requests. Called once from init. */
function registerPanelIpc() {
  ipcMain.on("crew-companion:panel-open", (_event, petRect) => {
    if (petRect && typeof petRect.x === "number") openPanelWindow(petRect);
  });
  ipcMain.on("crew-companion:panel-close", () => closePanelWindow());
  // The renderer tells us when the exercise starts and stops, because only it knows
  // — and without that the first click elsewhere would discard the session.
  ipcMain.on("crew-companion:panel-breathing", (_event, active) => {
    breathingActive = Boolean(active);
  });
  ipcMain.on("crew-companion:panel-hold", (_event, hold) => {
    holdOpen = Boolean(hold);
  });
}

module.exports = {
  CARD_SIZE,
  openPanelWindow,
  closePanelWindow,
  panelIsOpen,
  registerPanelIpc,
  setPanelTarget,
  setPanelLogger,
  setPanelClosedHandler,
};
