/**
 * Mochi's crop window — a full-screen surface to drag a region out of a captured
 * frame.
 *
 * Why it exists: the crop UI (`SnipOverlay`) is `position: fixed; inset: 0`, so it
 * fills WHATEVER WINDOW hosts it. Hosting it in the chat panel put a full-screen
 * frame inside a 320x470 window: the image was scaled to ~288px wide, so one pixel
 * of drag moved ~13 source pixels and the selection was unusable. The dashboard
 * gets away with the same component because its window is large. The fix is a
 * host the size of the screen, not a change to the component.
 *
 * How it differs from the pet overlay (petOverlays.js), which is the other
 * full-screen transparent window here:
 *
 *   - FOCUSABLE. The pet is `setFocusable(false)` so it never steals focus; this
 *     window must take focus to receive the Escape key that cancels the crop.
 *   - MOUSE-ACCEPTING. The pet is click-through (`setIgnoreMouseEvents`); the whole
 *     point of this one is to receive a drag.
 *   - ABOVE the pet ("screen-saver" level), so the pet cannot sit on top of the
 *     surface the user is dragging on.
 *   - Created HIDDEN. The capture runs first and macOS shows its own source
 *     picker; a visible full-screen window would cover that dialog. The renderer
 *     asks to be shown only once it holds a frame.
 */
"use strict";

const path = require("path");
const { BrowserWindow, screen } = require("electron");

const { mochiPageUrl } = require("./pageUrl");

/** The live crop window, or null. One at a time: a second would cover the first. */
let snipWin = null;

/**
 * Open the crop window, hidden, and load the crop entry.
 *
 * Returns the existing window when one is already up (the accelerator can fire
 * again while a crop is in progress) so the caller never stacks two surfaces.
 *
 * @param {string} baseUrl gateway origin serving the app windows
 * @param {string} [token] session token for the page URL
 * @returns {import('electron').BrowserWindow | null}
 */
function openSnipWindow(baseUrl, token = "") {
  if (snipWin !== null && !snipWin.isDestroyed()) return snipWin;

  // The display under the pointer is the one the user is looking at. Full
  // `bounds`, not `workArea`: the captured frame includes the menu bar and the
  // Dock, so the surface has to cover them for the crop to line up 1:1.
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const b = display.bounds;

  const win = new BrowserWindow({
    x: b.x,
    y: b.y,
    width: b.width,
    height: b.height,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    hasShadow: false,
    // Hidden until the renderer holds a frame — see the module header.
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The crop surface must keep painting while it is the foreground window,
      // and a throttled renderer drops drag frames.
      backgroundThrottling: false,
    },
  });

  snipWin = win;
  win.loadURL(mochiPageUrl(baseUrl, "snip.html", token));
  // Above the pet (which is "screen-saver" too but created earlier); the crop
  // surface must be the topmost thing on screen while it is up.
  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true);

  win.on("closed", () => {
    if (snipWin === win) snipWin = null;
  });

  return win;
}

/**
 * Reveal the crop surface. Called once the renderer has a frame to draw.
 *
 * `focus()` is not cosmetic: without focus the window never receives keydown, so
 * Escape would not cancel and the user would be stuck with a full-screen overlay.
 */
function showSnipWindow() {
  if (snipWin === null || snipWin.isDestroyed()) return;
  snipWin.show();
  snipWin.focus();
}

/** Tear the crop surface down (completed, cancelled, or capture refused). */
function closeSnipWindow() {
  if (snipWin === null || snipWin.isDestroyed()) {
    snipWin = null;
    return;
  }
  // Destroy rather than hide: the entry captures on mount, so the next crop
  // needs a fresh load anyway, and a lingering full-screen window that is only
  // hidden is a bad thing to leave behind if a later show() misfires.
  snipWin.destroy();
  snipWin = null;
}

/** Is a crop surface currently open? Used to keep the accelerator idempotent. */
function snipWindowIsOpen() {
  return snipWin !== null && !snipWin.isDestroyed();
}

module.exports = {
  openSnipWindow,
  showSnipWindow,
  closeSnipWindow,
  snipWindowIsOpen,
};
