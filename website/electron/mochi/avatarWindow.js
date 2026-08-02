/**
 * avatarWindow.js — the Avatars window.
 *
 * This is ONE window with two jobs, deliberately not two:
 *  - on first enable it is the picker (Kiro ghost or Mochi cat);
 *  - afterwards it is the Avatars surface the user re-opens to change their
 *    mind, and where imported packs will live.
 * The original shipped these as a separate first-run flow and a "Gallery"
 * window; folding them together avoids two divergent sets of avatar cards. The
 * name "Gallery" survives only in the `gallery:*` IPC channel ids, which are
 * wire protocol shared with the ported data files.
 *
 * WHY A WINDOW AND NOT A DASHBOARD PAGE
 * The choice is made the moment Mochi is enabled, before any pet exists, and a
 * user who enables it from the App Store may never open the Mochi page at all.
 * A dashboard page would leave them with an invisible companion and no prompt.
 * The original made the same call: its appearance Gallery was its own window
 * (gallery.html + galleryEntry.tsx).
 *
 * Deliberately unlike the pet overlay:
 *  - opaque, because it holds real UI (text over the desktop is unreadable);
 *  - focusable and centered — this is a modal decision, not an accessory;
 *  - alwaysOnTop at the "modal-panel" level, matching the original gallery
 *    window (galleryWindowManager.ts): the companion choice is a modal decision
 *    that should stay in front until it is made or dismissed.
 *
 * Opened by the shell's reconcile loop when the app is enabled but
 * `settings.avatar` is still null; closes itself once a choice is saved. It is
 * shown ONCE per unset avatar — re-picking later goes through the pet's
 * right-click menu or the settings page, not through this window.
 */

const { BrowserWindow, ipcMain } = require("electron");
const path = require("path");
const { mochiPageUrl } = require("./pageUrl");

// Geometry aligned to the original gallery window (galleryWindowManager.ts):
// 800x600, min 600x400. The earlier 620x680 was invented for the port.
//
// WHY ONE GEOMETRY FOR BOTH MODES
// This single window serves first-run onboarding AND the returning-user
// gallery, but it CANNOT know its mode at creation: the mode is decided by the
// renderer after it reads settings.avatar (see AvatarsView). So the window is
// sized to the larger surface — the gallery's 800x600 (the original's size) —
// and the compact onboarding chooser simply centers within it. Resizing after
// the renderer resolves the mode would make the window visibly jump, which is
// worse than a roomy onboarding. The mode-specific TITLE, which is cheap and
// jump-free, IS reflected: the renderer sets document.title and Electron
// mirrors it (page-title-updated), so the window reads as onboarding on first
// run and as "Avatars" for a returning user. PICKER_TITLE is only the
// first-paint default (the window's usual reason to open is first run).
const WIN_W = 800;
const WIN_H = 600;
const WIN_MIN_W = 600;
const WIN_MIN_H = 400;
// First-paint title; the renderer refines it per mode via document.title.
const PICKER_TITLE = "Choose your companion";

/** @type {BrowserWindow|null} */
let avatarWindow = null;
let ipcBound = false;
/** Set once the user has chosen, so the reconcile loop stops re-opening. */
let choiceMade = false;
/**
 * True when the CURRENT window was opened by an explicit user action
 * (right-click > Avatars). The reconcile loop must never close a window it did
 * not open, so its close path checks this. Reset when the window closes.
 */
let openedByUser = false;
/** Remembered so a user-initiated re-open needs no argument from the renderer. */
let lastBaseUrl = "";
/** First-load token for a REMOTE instance; "" for the local gateway. */
let lastToken = "";
/**
 * The user closed the picker WITHOUT choosing.
 *
 * Without this the 5s reconcile loop re-opens the window on the very next tick,
 * so dismissing it just makes it flash back — the app pesters the user every
 * five seconds until they pick. Treating a manual close as "not now" is the only
 * way to decline. Cleared by an explicit re-open (right-click > Avatars) and by
 * disabling the app.
 */
let dismissedByUser = false;

/**
 * Register the avatar-window IPC EAGERLY at module load, mirroring the
 * original's module-scope gallery:* handlers (ipcHandlers.ts). Previously this
 * ran only inside openAvatarWindow(), so when an avatar was already set at
 * startup the window was never opened and the 'mochi-avatar:open' channel had
 * NO listener — the right-click > Avatars menu silently did nothing.
 */
function bindIpc() {
  if (ipcBound) return;
  ipcBound = true;
  // The renderer signals a saved choice; the shell then closes the picker and
  // lets the reconcile loop bring up the pet on its next tick.
  ipcMain.on("mochi-avatar:chosen", () => {
    choiceMade = true;
    closeAvatarWindow();
  });
  // User-initiated re-open (right-click > Avatars). Bypasses the choiceMade
  // guard on purpose: that guard exists to stop the 5s reconcile loop from
  // re-summoning the window, not to stop the user from changing their mind.
  ipcMain.on("mochi-avatar:open", () => {
    openAvatarWindow(lastBaseUrl, { userInitiated: true });
  });
}

/**
 * Seed the gateway origin without opening the window. The reconcile loop calls
 * this every tick so a user-initiated 'mochi-avatar:open' has a base URL to
 * load even when the picker was never opened at startup (avatar already set).
 */
function setAvatarBaseUrl(baseUrl, token = "") {
  if (baseUrl) lastBaseUrl = baseUrl;
  lastToken = token || "";
}

// Eager registration — see bindIpc's docstring. Guarded internally by ipcBound.
bindIpc();

/**
 * @param {string} baseUrl gateway origin
 * @param {{userInitiated?: boolean}} [opts] `userInitiated` bypasses both the
 *   already-chosen and the dismissed guards — those exist to restrain the
 *   reconcile loop, never the user.
 */
function openAvatarWindow(baseUrl, opts = {}) {
  if (baseUrl) lastBaseUrl = baseUrl;
  if (opts.userInitiated) {
    choiceMade = false;
    dismissedByUser = false;
    // The user now owns this window; the reconcile loop must not close it.
    openedByUser = true;
  } else if (choiceMade || dismissedByUser) {
    return null;
  }
  if (avatarWindow && !avatarWindow.isDestroyed()) {
    avatarWindow.focus();
    return avatarWindow;
  }
  // Fresh window on the reconcile path: the reconcile loop opened it and may
  // later close it. (A user-initiated open above already set this true.)
  if (!opts.userInitiated) openedByUser = false;
  bindIpc();

  const win = new BrowserWindow({
    width: WIN_W,
    height: WIN_H,
    minWidth: WIN_MIN_W,
    minHeight: WIN_MIN_H,
    center: true,
    resizable: true,
    title: PICKER_TITLE,
    // Opaque: it holds real UI over the desktop.
    backgroundColor: "#0f1117",
    // Matches the original gallery — a modal decision that stays in front.
    alwaysOnTop: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // "modal-panel" level, mirroring galleryWindowManager.ts: above ordinary
  // windows so the choice is not lost behind other work.
  win.setAlwaysOnTop(true, "modal-panel");

  // `?token=` rather than a cookie: the auth cookie name is port-scoped
  // (mc_token_<port>) and can't be reliably constructed from the shell.
  const url = mochiPageUrl(baseUrl, "avatar.html", lastToken);
  win.loadURL(url);

  // Reveal on either signal — `ready-to-show` has proven unreliable for these
  // Mochi windows, and a picker that never appears blocks the whole flow.
  const reveal = () => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  };
  win.once("ready-to-show", reveal);
  win.webContents.once("did-finish-load", reveal);

  win.on("closed", () => {
    avatarWindow = null;
    openedByUser = false;
    // Closed with no choice saved => the user declined for now. `choiceMade` is
    // already true when WE closed it after a save, so this only fires for a
    // genuine dismissal.
    if (!choiceMade) dismissedByUser = true;
  });

  // A crashed picker must not wedge the flow — drop the handle so the reconcile
  // loop can create a fresh one.
  win.webContents.on("render-process-gone", () => {
    avatarWindow = null;
    openedByUser = false;
    if (!win.isDestroyed()) win.destroy();
  });

  // Forward renderer warnings/errors to the terminal, as the pet and panel
  // windows already do. This window had no forwarder, so everything the gallery
  // reported about art it could not render (a pack whose clip loads but paints
  // nothing, a slot with no usable content) was only visible to someone who
  // thought to open DevTools on it — which meant "the thumbnails are blank" came
  // with no evidence attached.
  // level: 0 verbose, 1 info, 2 warning, 3 error — forward 2+ only.
  win.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    if (level >= 2) {
      const where = sourceId ? ` (${sourceId}:${line})` : "";
      console.warn(
        `Mochi avatars console [${level >= 3 ? "error" : "warn"}]: ${message}${where}`,
      );
    }
  });

  avatarWindow = win;
  return win;
}

function closeAvatarWindow() {
  if (avatarWindow && !avatarWindow.isDestroyed()) avatarWindow.destroy();
  avatarWindow = null;
}

/**
 * Close the avatar window ONLY if the reconcile loop opened it.
 *
 * A window the user opened themselves (right-click > Avatars) must survive the
 * reconcile loop: the old code called closeAvatarWindow() unconditionally on
 * every 5s enabled+avatar-set tick, so a user-opened window died within 5s.
 */
function closeAvatarWindowFromReconcile() {
  if (openedByUser) return;
  closeAvatarWindow();
}

function isAvatarWindowOpen() {
  return !!(avatarWindow && !avatarWindow.isDestroyed());
}

/**
 * Reset the once-per-session guard.
 *
 * Called when Mochi is disabled, so re-enabling it with no avatar set shows the
 * picker again instead of silently starting a pet the user never chose.
 */
function resetAvatarChoiceGuard() {
  choiceMade = false;
  dismissedByUser = false;
  openedByUser = false;
}

module.exports = {
  openAvatarWindow,
  closeAvatarWindow,
  closeAvatarWindowFromReconcile,
  setAvatarBaseUrl,
  isAvatarWindowOpen,
  resetAvatarChoiceGuard,
  WIN_W,
  WIN_H,
};
