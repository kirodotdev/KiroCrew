/**
 * petOverlay.js — the companion's transparent, always-on-top, click-through window.
 *
 * One overlay per display, each covering that display's full bounds. Full-bounds
 * rather than a small window because the companion moves around the screen and a
 * small window would need constant repositioning; covering everything and being
 * click-through is simpler and is what the reference implementation does.
 *
 * THE CLICK-THROUGH RULE IS LOAD-BEARING. The window sits over the entire desktop,
 * so one that accepted clicks would make the machine unusable. Input is refused by
 * default (`setIgnoreMouseEvents(true, { forward: true })`) and the renderer keeps
 * `pointer-events: none` on the body, enabling it only on the companion itself.
 * `forward: true` is what still lets the renderer SEE the cursor move, which is how
 * it knows when the pointer is over the sprite.
 */

const path = require("path");
const { BrowserWindow, screen, ipcMain } = require("electron");
const { companionPageUrl } = require("./pageUrl");

/** @type {Map<number, Electron.BrowserWindow>} display id -> overlay */
const overlays = new Map();

/**
 * Per-overlay cursor hitboxes: window -> { pet, bubble, menu }, each rect in that
 * overlay's local pixels or null. The renderer reports these; the poll below reads
 * them. Keyed by the window itself so a torn-down overlay drops out cleanly.
 * @type {Map<Electron.BrowserWindow, {pet: object|null, bubble: object|null, menu: object|null}>}
 */
const hitboxes = new Map();

/** Last ignore-mouse state applied per window, so the poll only toggles on change. */
const lastIgnore = new Map();

/** ~60fps, matching the desktop app's cursor poll. */
const HITBOX_POLL_MS = 16;

let pollTimer = null;
let ipcRegistered = false;

let baseUrl = "";
let credential = "";
let log = () => {};

function setOverlayLogger(fn) {
  if (typeof fn === "function") log = fn;
}

function setOverlayTarget(url, token) {
  baseUrl = url || "";
  credential = token || "";
}

function createOverlayFor(display) {
  const win = new BrowserWindow({
    x: display.bounds.x,
    y: display.bounds.y,
    width: display.bounds.width,
    height: display.bounds.height,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    enableLargerThanScreen: true,
    show: false,
    /*
     * Deliver the FIRST click to the page — a constructor option, the only place
     * this can be set.
     *
     * On macOS a click into an inactive window is consumed to activate it, and this
     * overlay is `setFocusable(false)` + `showInactive()`, so it never becomes the
     * active window: EVERY click is a first-mouse click. Without this the window
     * accepted `mousemove` (ignore-mouse is set with `forward: true`) but never the
     * `mousedown` behind it — so the bubble's hover-revealed ✕ appeared under the
     * cursor and did nothing when clicked, and the notification could not be
     * dismissed at all.
     *
     * It used to be attempted as `win.setAcceptFirstMouse?.(true)` after
     * construction. No such method exists on BrowserWindow — `acceptFirstMouse` is
     * a BaseWindow CONSTRUCTOR option only — and the optional call swallowed the
     * miss silently, which is why it read as done for so long.
     */
    acceptFirstMouse: true,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The companion animates continuously and the window is never focusable, so
      // without this Chromium throttles it to a stall for its whole lifetime.
      backgroundThrottling: false,
    },
  });

  win.setFocusable(false);
  // Refuse input by default; the renderer re-enables it over the sprite alone.
  win.setIgnoreMouseEvents(true, { forward: true });
  // INVISIBLE TO SCREEN CAPTURE (macOS NSWindowSharingNone, Windows
  // WDA_EXCLUDEFROMCAPTURE; no-op elsewhere). The overlay covers a whole display,
  // so without this it is the topmost window at EVERY point on the screen: the
  // macOS screenshot picker (Cmd+Shift+4 space / Cmd+Shift+5 window mode) offers
  // the overlay instead of the app the user is pointing at, and a region capture
  // or recording bakes the companion into the result. A decoration must not
  // appear in the user's screenshots, screen recordings, or screen shares — the
  // same reason computer use's cursor overlay sets NSWindowSharingNone.
  win.setContentProtection(true);
  // Follow the user across spaces and over full-screen apps — a companion that
  // vanished when you switched desktops would not be company.
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  win.loadURL(companionPageUrl(baseUrl, "pet.html", credential));
  win.once("ready-to-show", () => {
    if (!win.isDestroyed()) win.showInactive();
  });
  win.on("closed", () => {
    for (const [id, w] of overlays) if (w === win) overlays.delete(id);
    hitboxes.delete(win);
    lastIgnore.delete(win);
  });
  return win;
}

/** Open an overlay on every display. Idempotent per display. */
function openPetWindow() {
  if (!baseUrl) {
    log("crew-companion: no gateway origin yet, deferring overlay");
    return;
  }
  for (const display of screen.getAllDisplays()) {
    const existing = overlays.get(display.id);
    if (existing && !existing.isDestroyed()) continue;
    try {
      overlays.set(display.id, createOverlayFor(display));
      log(`crew-companion: overlay opened on display ${display.id}`);
    } catch (err) {
      log(`crew-companion: overlay failed on display ${display.id} — ${err && err.message}`);
    }
  }
}

/** Close every overlay. Idempotent. */
function closePetWindow() {
  for (const [id, win] of [...overlays]) {
    overlays.delete(id);
    if (win && !win.isDestroyed()) win.destroy();
  }
}

function petWindowCount() {
  let n = 0;
  for (const win of overlays.values()) if (win && !win.isDestroyed()) n += 1;
  return n;
}

/**
 * Send a message to every companion overlay.
 *
 * There is one overlay per display, and all of them need state changes that happen
 * elsewhere — the panel closing, for instance — or a companion on a second monitor
 * would be left believing the panel is still open.
 */
function broadcastToPets(channel, ...args) {
  for (const win of overlays.values()) {
    if (win && !win.isDestroyed()) win.webContents.send(channel, ...args);
  }
}

/** True when this window is one of the companion overlays. */
function isPetWindow(win) {
  for (const w of overlays.values()) if (w === win) return true;
  return false;
}

/**
 * ── Cursor-hitbox authority ────────────────────────────────────────────────
 *
 * The overlay is click-through everywhere except a few small regions — the
 * companion, its bubble, and (while open) the context menu. The renderer reports
 * those rects; the main process polls the real cursor at ~60fps and toggles this
 * window's ignore-mouse itself. Doing the hit-test here rather than on a
 * pointer-enter/leave IPC round-trip is what stops a click on the companion body
 * falling through to the window behind it.
 *
 * `forward: true` on the ignore state keeps move events flowing even while the
 * window is click-through, which is what lets this poll keep seeing the cursor.
 */

/** True when the point is within the rect (inclusive of its edges). */
function pointInRect(rect, x, y) {
  return (
    !!rect &&
    x >= rect.x &&
    x <= rect.x + rect.w &&
    y >= rect.y &&
    y <= rect.y + rect.h
  );
}

/** True when the point falls inside ANY of this overlay's reported hitboxes. */
function cursorHitsWindow(boxes, localX, localY) {
  if (!boxes) return false;
  return (
    pointInRect(boxes.pet, localX, localY) ||
    pointInRect(boxes.bubble, localX, localY) ||
    pointInRect(boxes.menu, localX, localY)
  );
}

/** Merge a window's pet/bubble hitboxes, preserving any menu rect. */
function setWindowHitbox(win, pet, bubble) {
  if (!win) return;
  const cur = hitboxes.get(win) || { pet: null, bubble: null, menu: null };
  hitboxes.set(win, { pet: pet || null, bubble: bubble || null, menu: cur.menu || null });
}

/** Merge a window's menu hitbox, preserving its pet/bubble rects. */
function setWindowMenuHitbox(win, rect) {
  if (!win) return;
  const cur = hitboxes.get(win) || { pet: null, bubble: null, menu: null };
  hitboxes.set(win, { pet: cur.pet || null, bubble: cur.bubble || null, menu: rect || null });
}

/**
 * Decide one overlay's click-through state from a SCREEN cursor point and apply it.
 *
 * Converts the cursor to overlay-local coordinates using the window's OWN bounds —
 * ground truth versus display.bounds, which can drift with the macOS menu bar or a
 * display rearrangement. Toggles ignore-mouse only when it actually changes.
 * Returns the ignore state applied.
 */
function refreshOverlayInput(win, cursor) {
  if (!win || win.isDestroyed()) return true;
  const b = win.getBounds();
  const localX = cursor.x - b.x;
  const localY = cursor.y - b.y;
  const shouldIgnore = !cursorHitsWindow(hitboxes.get(win), localX, localY);
  if (lastIgnore.get(win) !== shouldIgnore) {
    lastIgnore.set(win, shouldIgnore);
    win.setIgnoreMouseEvents(shouldIgnore, { forward: true });
  }
  return shouldIgnore;
}

/** One poll iteration across every overlay. The interval calls this. */
function pollOverlayInputOnce() {
  let cursor;
  try {
    cursor = screen.getCursorScreenPoint();
  } catch {
    return; // no cursor available (e.g. under test) — nothing to do
  }
  for (const win of overlays.values()) {
    try {
      refreshOverlayInput(win, cursor);
    } catch {
      /* window torn down between checks — skip it */
    }
  }
}

function startHitboxPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(pollOverlayInputOnce, HITBOX_POLL_MS);
  // A background poll must never be the reason the process cannot exit.
  pollTimer.unref?.();
}

function stopHitboxPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/**
 * Register the overlay's cursor-hitbox IPC and start the poll. Called once from
 * `initCrewCompanion`. Each renderer reports its own window's rects; the sender is
 * resolved back to its overlay so one display's report cannot describe another's.
 */
function registerOverlayIpc() {
  if (ipcRegistered) return;
  ipcRegistered = true;

  ipcMain.on("crew-companion:update-hitbox", (event, pet, bubble) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win && isPetWindow(win)) setWindowHitbox(win, pet, bubble);
  });

  ipcMain.on("crew-companion:menu-hitbox", (event, rect) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win && isPetWindow(win)) setWindowMenuHitbox(win, rect);
  });

  startHitboxPoll();
}

module.exports = {
  broadcastToPets,
  isPetWindow,
  openPetWindow,
  closePetWindow,
  petWindowCount,
  setOverlayLogger,
  setOverlayTarget,
  registerOverlayIpc,
  stopHitboxPoll,
  // Exported for tests: they drive the toggle directly rather than through the
  // live cursor poll.
  pointInRect,
  cursorHitsWindow,
  refreshOverlayInput,
  pollOverlayInputOnce,
  setWindowHitbox,
  setWindowMenuHitbox,
};
