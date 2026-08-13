/**
 * petOverlays.js — Mochi's pet overlay windows (multi-display model).
 *
 * PORTED from the original `src/main/petWindowManager.ts` (744 lines) plus the
 * overlay half of `broadcastService.ts`. The port is deliberately LINE-FOR-LINE
 * where it can be; see MOCHI_MIGRATION.md "petWindowManager.ts port inventory"
 * for the item-by-item PORT / DROP / FOLLOW-UP table.
 *
 * Only four MECHANICAL transforms were applied, so re-syncing with upstream is
 * re-running the same pass and diffing:
 *
 *   1. TypeScript annotations removed (this directory has no build step —
 *      package.json `main` is main.js and Electron loads CJS directly).
 *   2. `import` -> `require`.
 *   3. Channel prefix `pet:*` / `displays:*` -> `mochi-pet:*`. The builtin shares
 *      ONE ipcMain with the host, so the namespace is deliberate.
 *   4. Renderer/preload paths -> the gateway origin + this directory's preload.
 *
 * NOT ported, because the host already owns them: `createTray` /
 * `rebuildTrayMenu` (a second tray icon would appear beside KiroCrew's) and
 * `toggleHideAll`'s `app.hide()` (it would hide the dashboard too — main.js
 * hides Mochi's windows individually instead).
 *
 * THE ARCHITECTURE, in one sent: each display gets its own full-screen
 * transparent overlay, the pet lives in exactly one of them at a time, and the
 * MAIN process — not the renderer — owns the handoff, because mousemove events
 * stop at the window edge and only `screen.getCursorScreenPoint()` can follow a
 * drag across displays.
 */

const { app, BrowserWindow, ipcMain, screen } = require("electron");
const fs = require("fs");
const path = require("path");
const { mochiPageUrl } = require("./pageUrl");

/** ~60fps. Same value for the hitbox poll and the drag poll, as upstream. */
const POLL_MS = 16;

/**
 * Pet box, mirroring `src/shared/constants.ts` (PET_W/PET_H = 128).
 *
 * The hand-written predecessor used 120 here while the renderer used 128, so
 * every clamp in the drag path was 8px off. Keep these in sync with the shared
 * constants module the renderer imports.
 */
const PET_W = 128;
const PET_H = 128;

/** Force-stop a drag that never saw a mouseup (upstream value). */
const DRAG_SAFETY_MS = 10_000;

// ── Overlay error-page recovery ────────────────────────────────────────────
//
// A pet overlay covers a WHOLE display and is frameless, click-through and
// always-on-top. If its `pet.html` load is answered with a gateway error page
// (a token-required 401/403 — e.g. the session cookie expired while the machine
// slept and a reconnected display builds a fresh overlay — or any other
// 4xx/5xx), that opaque page blankets the entire display with no title bar to
// close and no click target: the only escape is force-quitting the whole app.
//
// An error page is a COMPLETED navigation (the gateway serves an HTML/JSON
// body), so `did-fail-load` never fires and `did-finish-load` DOES — meaning
// the overlay would happily show it. `did-navigate`'s httpResponseCode is the
// only signal that the page is an error, not the pet.
//
// Recovery is owned entirely by the host's 5s reconcile tick. The handler here
// only HIDES an error page and latches the overlay; the host re-arms it with a
// token it already resolved for the current target (rearmBlankedOverlays). One
// recovery path, living inside the loop that owns target/switch state — so there
// is no provider seam, no retry budget, and no superseded-window race to guard.

/** True for ANY gateway error page on the main frame (4xx/5xx) — the signal to
 * hide, since an error body is a completed navigation, not a did-fail-load. */
function isOverlayErrorPage(httpResponseCode) {
  return typeof httpResponseCode === "number" && httpResponseCode >= 400;
}

/**
 * Overlays hidden because their last main-frame navigation was a gateway error
 * page, so the load-finished handler knows NOT to reveal them and the reconcile
 * tick knows which to re-arm. A WeakSet so a closed/GC'd window drops out on its
 * own and never pins a dead BrowserWindow.
 * @type {WeakSet<object>}
 */
const overlayBlanked = new WeakSet();

/**
 * React to a completed main-frame navigation: an error page (>=400) latches the
 * overlay as blanked and hides it so it can never cover the display; any other
 * status clears the latch. Reloading is NOT done here — the reconcile tick owns
 * it (rearmBlankedOverlays), so there is no async work and no switch race.
 */
function handleOverlayNavigation(win, httpResponseCode) {
  if (win.isDestroyed()) return;
  if (isOverlayErrorPage(httpResponseCode)) {
    overlayBlanked.add(win);
    win.hide();
  } else {
    overlayBlanked.delete(win);
  }
}

/** True when any live overlay is currently hidden on an error page, so the host
 * only re-mints a token when there is actually one to heal. */
function hasBlankedOverlay() {
  for (const win of overlays.values()) {
    if (!win.isDestroyed() && overlayBlanked.has(win)) return true;
  }
  return false;
}

/**
 * Re-arm every overlay hidden on an error page by reloading it with the
 * (baseUrl, token, viaCookie) the host ALREADY resolved for the current target this
 * reconcile tick. The host owns target resolution AND switches, so this takes
 * concrete values rather than resolving again — no provider seam, no retry
 * budget, and no superseded-window race (the reload is synchronous inside the
 * tick). A minted credential goes on the URL; a borrowed session uses the
 * overlay's default BrowserWindow session. An absent credential means stay
 * blank and let the next tick try. Recovering an expired cookie and a transient
 * 5xx share this one path.
 */
function rearmBlankedOverlays(baseUrl, token, viaCookie = false) {
  if (!baseUrl || (!token && !viaCookie)) return;
  const pageToken = viaCookie ? "" : token;
  for (const [, win] of overlays) {
    if (win.isDestroyed() || !overlayBlanked.has(win)) continue;
    // Refresh the shared target so overlays built LATER for other displays load
    // the same fresh origin + token.
    currentBaseUrl = baseUrl;
    currentToken = pageToken;
    win.loadURL(mochiPageUrl(currentBaseUrl, "pet.html", pageToken));
  }
}

// ── Overlay registry (broadcastService.ts, overlay half) ───────────────────

/** @type {Map<number, BrowserWindow>} displayId -> overlay */
const overlays = new Map();

function registerOverlay(displayId, win) {
  overlays.set(displayId, win);
  // Self-cleaning, exactly as upstream: a closed window must not linger in the
  // map, or a later broadcast targets a dead webContents.
  //
  // Identity-checked delete: a pet-instance switch runs closePetWindow() ->
  // openPetWindow() and may have already registered a REPLACEMENT window for the
  // same displayId before this old window's async `closed` fires. A bare
  // delete(displayId) would then evict the live replacement, leaking an
  // unreachable always-on-top full-screen window. Only remove ourselves.
  win.on("closed", () => {
    if (overlays.get(displayId) === win) overlays.delete(displayId);
  });
}

function removeOverlay(displayId) {
  overlays.delete(displayId);
}

function getOverlays() {
  return overlays;
}

/** Send to every live overlay (upstream broadcastToRenderers, overlay half). */
function broadcastToOverlays(channel, ...args) {
  for (const win of overlays.values()) {
    if (!win.isDestroyed()) {
      try {
        win.webContents.send(channel, ...args);
      } catch {
        /* window torn down mid-broadcast */
      }
    }
  }
}

// ── Module state ──────────────────────────────────────────────────────────

/** @type {number|null} which display currently hosts the pet */
let activeDisplayId = null;
/** Gateway origin, captured on open. */
let currentBaseUrl = "";
/** First-load token for a REMOTE instance; "" for the local gateway. */
let currentToken = "";
let ipcBound = false;
let displayListenersBound = false;

/** @type {{x:number,y:number,w:number,h:number}|null} */
let petHitbox = null;
/** @type {{x:number,y:number,w:number,h:number}|null} */
let bubbleHitbox = null;
/**
 * The open context menu's rectangle.
 *
 * Load-bearing: the menu is drawn INSIDE the click-through overlay, so without
 * its own hitbox every click on a menu row is forwarded to whatever sits behind
 * the pet. Upstream excluded it explicitly (`!inPet && !inBubble && !inMenu`).
 *
 * @type {{x:number,y:number,w:number,h:number}|null}
 */
let menuHitbox = null;
/**
 * While a menu is open EVERY overlay accepts clicks — that is how a click on
 * another screen dismisses it. Restoring click-through is left to the poll.
 */
let menuOpen = false;
let lastIgnoreState = true;

/** @type {ReturnType<typeof setInterval>|null} */
let hitPollTimer = null;
/** @type {ReturnType<typeof setInterval>|null} */
let dragPollTimer = null;
/** @type {ReturnType<typeof setTimeout>|null} */
let dragSafetyTimer = null;
let dragOffsetX = 0;
let dragOffsetY = 0;

// ── Position persistence ──────────────────────────────────────────────────

/**
 * Upstream wrote `mochi-pet-position.json` into Mochi's own data dir. The shell
 * cannot resolve the gateway's data dir, so it uses Electron's `userData` —
 * which is also where the host keeps its own window geometry (window-state.js).
 * DIVERGENCE IS THE LOCATION ONLY; the shape and the 0600 mode are upstream's.
 *
 * This was a no-op stub in the hand-written predecessor, which meant the pet's
 * position was never remembered across restarts.
 */
function petPosPath() {
  return path.join(app.getPath("userData"), "mochi-pet-position.json");
}

/** @type {{x:number,y:number,displayId?:number}|null} */
let savedPetPos = null;
try {
  savedPetPos = JSON.parse(fs.readFileSync(petPosPath(), "utf-8"));
} catch {
  /* first run, or unreadable — fall back to a computed start position */
}

function savePetPos(x, y, displayId) {
  savedPetPos = { x, y, displayId };
  try {
    fs.writeFileSync(petPosPath(), JSON.stringify(savedPetPos), { mode: 0o600 });
  } catch {
    /* a failed position write must never break the pet */
  }
}

function getSavedPetPos() {
  return savedPetPos;
}

// ── Display geometry helpers (upstream, verbatim logic) ────────────────────

function getAllDisplayInfo() {
  const primaryId = screen.getPrimaryDisplay().id;
  const all = screen.getAllDisplays();
  return all.map((d, i) => ({
    id: d.id,
    // 1-based, in the order the OS reports them — this is the number the user
    // means by "screen 2". Without it the pet has only opaque display ids and
    // has to guess which monitor it is standing on.
    index: i + 1,
    primary: d.id === primaryId,
    x: d.bounds.x,
    y: d.bounds.y,
    width: d.bounds.width,
    height: d.bounds.height,
    // EXTENSION over the original payload. Upstream computed walk targets in the
    // main process, where `workArea` was in scope; as a builtin the walk intent
    // arrives on the gateway's event bus, which the shell cannot subscribe to,
    // so the geometry moved to the renderer and needs the work area (bounds
    // minus Dock / menu bar / taskbar) to clamp against. Additive, so the
    // vendored useDisplayActivation is unaffected.
    workArea: {
      x: d.workArea.x,
      y: d.workArea.y,
      width: d.workArea.width,
      height: d.workArea.height,
    },
  }));
}

function findDisplayAtPoint(sx, sy) {
  return (
    screen
      .getAllDisplays()
      .find(
        (d) =>
          sx >= d.bounds.x &&
          sx < d.bounds.x + d.bounds.width &&
          sy >= d.bounds.y &&
          sy < d.bounds.y + d.bounds.height,
      ) || null
  );
}

/** Nearest display by squared edge distance — the fallback when the cursor sits
 *  in a gap between mismatched display bounds. */
function findNearestDisplay(sx, sy) {
  const displays = screen.getAllDisplays();
  let best = displays[0];
  let bestDist = Infinity;
  for (const d of displays) {
    const dx = Math.max(d.bounds.x - sx, 0, sx - (d.bounds.x + d.bounds.width));
    const dy = Math.max(d.bounds.y - sy, 0, sy - (d.bounds.y + d.bounds.height));
    const dist = dx * dx + dy * dy;
    if (dist < bestDist) {
      bestDist = dist;
      best = d;
    }
  }
  return best;
}

/** Clamp a local position the way the drag path does (upstream formula: the pet
 *  may hang half off the left/right edge, but never off the top/bottom). */
function clampLocal(localX, localY, bounds) {
  return {
    x: Math.max(-PET_W / 2, Math.min(bounds.width - PET_W / 2, localX)),
    y: Math.max(0, Math.min(bounds.height - PET_H, localY)),
  };
}

// ── The handoff ───────────────────────────────────────────────────────────

/**
 * Move the pet to another display's overlay.
 *
 * The old overlay is told it is no longer active (so it stops rendering the pet)
 * and returns to click-through — unless a drag is in flight, which manages its
 * own ignore-mouse state across every overlay.
 */
function transferPetToDisplay(targetDisplayId, localX, localY, isDragging = false) {
  if (activeDisplayId !== null && activeDisplayId !== targetDisplayId) {
    const oldWin = overlays.get(activeDisplayId);
    if (oldWin && !oldWin.isDestroyed()) {
      oldWin.webContents.send("mochi-pet:set-active", false);
      if (!isDragging) {
        oldWin.setIgnoreMouseEvents(true, { forward: true });
      }
    }
  }

  activeDisplayId = targetDisplayId;
  // The old owner's box no longer describes where the pet is, and the new owner
  // has not reported yet. Clearing hands the poll to its "no hitbox -> stay
  // click-through" safety net for those few frames instead of testing the cursor
  // against a stale rectangle on a different monitor.
  petHitbox = null;
  bubbleHitbox = null;
  const newWin = overlays.get(targetDisplayId);
  if (newWin && !newWin.isDestroyed()) {
    newWin.webContents.send("mochi-pet:set-active", true, localX, localY, isDragging);
  }
}

// ── Click-through decision ────────────────────────────────────────────────

function inRect(x, y, r) {
  return r !== null && r !== undefined && x >= r.x && x <= r.x + r.w && y >= r.y && y <= r.y + r.h;
}

/**
 * The whole click-through decision as one pure function.
 *
 * Extracted (upstream had it inline) so it is unit-testable: a missing term here
 * is INVISIBLE at runtime — the affected surface simply never receives clicks,
 * which is how the menu term went missing once already.
 */
function shouldIgnoreAt(x, y, boxes) {
  return !inRect(x, y, boxes.pet) && !inRect(x, y, boxes.bubble) && !inRect(x, y, boxes.menu);
}

// ── Cross-display drag polling ────────────────────────────────────────────

/**
 * While dragging, the renderer cannot follow the cursor: mousemove stops when
 * the pointer leaves the overlay. The main process polls the global cursor and
 * drives the position — and the transfer — itself.
 */
function startDragPolling(offsetX, offsetY) {
  stopDragPolling();
  dragOffsetX = offsetX;
  dragOffsetY = offsetY;

  // Every overlay must accept mouse events so whichever one the cursor ends up
  // over can report the mouseup.
  for (const win of overlays.values()) {
    if (!win.isDestroyed()) win.setIgnoreMouseEvents(false);
  }
  broadcastToOverlays("mochi-pet:drag-listen-mouseup");

  dragSafetyTimer = setTimeout(() => {
    if (dragPollTimer !== null) stopDragPolling();
  }, DRAG_SAFETY_MS);

  dragPollTimer = setInterval(() => {
    const cursor = screen.getCursorScreenPoint();
    const petScreenX = cursor.x - dragOffsetX;
    const petScreenY = cursor.y - dragOffsetY;

    const targetDisplay =
      findDisplayAtPoint(cursor.x, cursor.y) || findNearestDisplay(cursor.x, cursor.y);

    const localX = petScreenX - targetDisplay.bounds.x;
    const localY = petScreenY - targetDisplay.bounds.y;

    if (targetDisplay.id !== activeDisplayId) {
      // Note: upstream transfers with the UNCLAMPED local position — the pet is
      // mid-drag under the cursor, and clamping here would snap it away from the
      // pointer at the moment of handoff.
      transferPetToDisplay(targetDisplay.id, localX, localY, true);
      savePetPos(localX, localY, targetDisplay.id);
      return;
    }

    const clamped = clampLocal(localX, localY, targetDisplay.bounds);
    const win = overlays.get(activeDisplayId);
    if (win && !win.isDestroyed()) {
      win.webContents.send("mochi-pet:drag-update", clamped.x, clamped.y);
    }
  }, POLL_MS);
}

function stopDragPolling() {
  if (dragPollTimer === null) return; // not polling — nothing to do
  clearInterval(dragPollTimer);
  dragPollTimer = null;
  if (dragSafetyTimer !== null) {
    clearTimeout(dragSafetyTimer);
    dragSafetyTimer = null;
  }

  // Final position, so the renderer can run its edge-snap animation.
  const cursor = screen.getCursorScreenPoint();
  if (activeDisplayId !== null) {
    const display = screen.getAllDisplays().find((d) => d.id === activeDisplayId);
    if (display) {
      const clamped = clampLocal(
        cursor.x - dragOffsetX - display.bounds.x,
        cursor.y - dragOffsetY - display.bounds.y,
        display.bounds,
      );
      const win = overlays.get(activeDisplayId);
      if (win && !win.isDestroyed()) {
        win.webContents.send("mochi-pet:drag-ended", clamped.x, clamped.y);
      }
      savePetPos(clamped.x, clamped.y, activeDisplayId);
    }
  }

  // CRITICAL (upstream's word): restore ignore-mouse on ALL overlays, including
  // the active one. Hitbox data may be stale right after a drag, and leaving an
  // overlay interactive with no valid hitbox swallows every click on that
  // display. The hitbox poll switches the active overlay back once the renderer
  // reports a real rectangle.
  for (const win of overlays.values()) {
    if (!win.isDestroyed()) win.setIgnoreMouseEvents(true, { forward: true });
  }
  lastIgnoreState = true;
}

// ── Hitbox poll ───────────────────────────────────────────────────────────

function startHitPoll() {
  if (hitPollTimer !== null) return;
  hitPollTimer = setInterval(() => {
    if (dragPollTimer !== null) return; // drag polling owns ignore-mouse
    if (menuOpen) return; // all overlays accept clicks so a click elsewhere dismisses
    if (activeDisplayId === null) return;
    const win = overlays.get(activeDisplayId);
    if (!win || win.isDestroyed()) return;

    if (petHitbox === null) {
      // Safety net: an interactive overlay with no known hitbox swallows every
      // click on the display — the worst failure this module can produce.
      if (!lastIgnoreState) {
        win.setIgnoreMouseEvents(true, { forward: true });
        lastIgnoreState = true;
      }
      return;
    }

    const cursor = screen.getCursorScreenPoint();
    // `win.getBounds()`, NOT `display.bounds`: upstream learned the two drift
    // apart (macOS menu-bar offset, display rearrangement, scaling), and the
    // window's own bounds are the ground truth for where the overlay sits.
    const b = win.getBounds();
    const shouldIgnore = shouldIgnoreAt(cursor.x - b.x, cursor.y - b.y, {
      pet: petHitbox,
      bubble: bubbleHitbox,
      menu: menuHitbox,
    });

    if (shouldIgnore !== lastIgnoreState) {
      lastIgnoreState = shouldIgnore;
      if (shouldIgnore) win.setIgnoreMouseEvents(true, { forward: true });
      else win.setIgnoreMouseEvents(false);
    }
  }, POLL_MS);
}

function stopHitPoll() {
  if (hitPollTimer !== null) {
    clearInterval(hitPollTimer);
    hitPollTimer = null;
  }
}

/**
 * Is this IPC frame from the overlay that currently owns the pet?
 *
 * There is ONE pet but N overlays (one per display), and every one of them runs
 * the same renderer. `petHitbox` is a single slot, so an inactive overlay that
 * reports its own position overwrites the real one — the poll then alternates
 * between two boxes and flips click-through on every tick, which reads as "the
 * pet ignores clicks" and, on the interactive half of the flip, as a full-screen
 * transparent window swallowing input meant for other apps.
 *
 * The renderer cannot police this: it has no way to know it lost the race, and a
 * "report anyway in case set-active hasn't arrived" fallback there is exactly how
 * the second reporter got in. The main process is the only party that knows which
 * overlay is active, so the authority check belongs here.
 */
function isActiveSender(event) {
  if (activeDisplayId === null) return false;
  const win = overlays.get(activeDisplayId);
  if (!win || win.isDestroyed()) return false;
  return event.sender.id === win.webContents.id;
}

// ── IPC ───────────────────────────────────────────────────────────────────

function bindIpc() {
  if (ipcBound) return;
  ipcBound = true;

  ipcMain.on("mochi-pet:update-hitbox", (e, pet, bubble) => {
    if (!isActiveSender(e)) return;
    petHitbox = pet || null;
    bubbleHitbox = bubble || null;
  });

  ipcMain.on("mochi-pet:menu-hitbox", (e, rect) => {
    if (!isActiveSender(e)) return;
    menuHitbox = rect || null;
  });

  ipcMain.on("mochi-pet:menu-open", () => {
    menuOpen = true;
    for (const win of overlays.values()) {
      if (!win.isDestroyed()) win.setIgnoreMouseEvents(false);
    }
    lastIgnoreState = false;
  });

  ipcMain.on("mochi-pet:menu-close", () => {
    menuOpen = false;
    menuHitbox = null;
    // Non-active overlays go back to click-through immediately; the active one
    // is left to the poll, which needs a state change to act on — hence the
    // false below.
    for (const [id, win] of overlays) {
      if (!win.isDestroyed() && id !== activeDisplayId) {
        win.setIgnoreMouseEvents(true, { forward: true });
      }
    }
    lastIgnoreState = false;
  });

  // Legacy direct setter, kept because upstream kept it for edge cases.
  ipcMain.on("mochi-pet:ignore-mouse", (_e, ignore) => {
    const win = activeDisplayId !== null ? overlays.get(activeDisplayId) : null;
    if (!win || win.isDestroyed()) return;
    if (ignore) win.setIgnoreMouseEvents(true, { forward: true });
    else win.setIgnoreMouseEvents(false);
    lastIgnoreState = !!ignore;
  });

  ipcMain.handle("mochi-pet:get-position", () => {
    if (savedPetPos !== null) return { x: savedPetPos.x, y: savedPetPos.y };
    const win = activeDisplayId !== null ? overlays.get(activeDisplayId) : null;
    if (!win || win.isDestroyed()) return { x: 0, y: 0 };
    const b = win.getBounds();
    return { x: b.x, y: b.y };
  });

  ipcMain.on("mochi-pet:save-position", (_e, x, y) => {
    savePetPos(x, y, activeDisplayId ?? undefined);
  });

  ipcMain.on("mochi-pet:transfer-display", (_e, displayId, x, y) => {
    void transferPetToDisplayById(displayId, x || 0, y || 0);
  });

  // Cross-display drag.
  ipcMain.on("mochi-pet:drag-start", (_e, offsetX, offsetY) => {
    startDragPolling(offsetX || 0, offsetY || 0);
  });
  ipcMain.on("mochi-pet:drag-end", () => {
    stopDragPolling();
  });
  // ANY overlay reporting mouseup ends the drag — that is the point of
  // broadcasting drag-listen-mouseup to all of them.
  ipcMain.on("mochi-pet:drag-mouseup", () => {
    if (dragPollTimer !== null) stopDragPolling();
  });

  // The panel is opened BY the pet, so its IPC is bound here — one place, once.
  require("./panelWindow").bindPanelIpc(currentBaseUrl);
}

// ── Overlay lifecycle ─────────────────────────────────────────────────────

/**
 * Keep the HOST app in the Dock.
 *
 * The original ran as a macOS accessory app (`LSUIElement`, `app.dock.hide()`)
 * and re-asserted that on every window show, because Electron flips a
 * window-owning app back to Regular when a window is SHOWN. As a builtin the
 * requirement inverts: the pet's window has the shape macOS associates with
 * accessory apps, so Regular is re-asserted after showing it.
 */
function assertHostStaysInDock() {
  if (process.platform !== "darwin") return;
  try {
    app.setActivationPolicy?.("regular");
    app.dock?.show?.();
  } catch {
    /* older Electron / already regular */
  }
}

function createOverlayForDisplay(display) {
  const win = new BrowserWindow({
    x: display.bounds.x,
    y: display.bounds.y,
    width: display.bounds.width,
    height: display.bounds.height,
    // A NON-ACTIVATING panel, macOS only (NSWindowStyleMaskNonactivatingPanel).
    // Without it, a click on the pet activates the app, and the shell's
    // app.on("activate") pulls a deliberately-hidden dashboard back up -- the
    // user petted the cat and the whole app resurfaced. It also removes the
    // knock-on where closing the chat panel revealed the dashboard: that only
    // happened because the app had already been activated by the pet click, so
    // hiding the panel handed key status to the dashboard window.
    //
    // Gated on darwin because `type` values are per-platform: "panel" is not
    // one of Linux's (desktop/dock/toolbar/splash/notification) nor Windows'
    // (toolbar) legal values, so passing it there would be rejected.
    ...(process.platform === "darwin" ? { type: "panel" } : {}),
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: true,
    hasShadow: false,
    enableLargerThanScreen: true,
    show: false,
    // Deliver the FIRST click to the page. This overlay is never focusable and is
    // shown inactive, so on macOS every click is a first-mouse click and would
    // otherwise be eaten activating a window that can never activate — leaving the
    // pet hoverable but unclickable. Constructor-only: there is no
    // `setAcceptFirstMouse()` on BrowserWindow, and the optional call that used to
    // sit below hid that fact.
    acceptFirstMouse: true,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The pet animates continuously and the window is never focusable, so
      // without this it stalls for its entire lifetime.
      backgroundThrottling: false,
    },
  });

  win.setFocusable(false);
  win.setIgnoreMouseEvents(true, { forward: true });
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  // INVISIBLE TO SCREEN CAPTURE (macOS NSWindowSharingNone, Windows
  // WDA_EXCLUDEFROMCAPTURE; no-op elsewhere). This window covers a whole display,
  // so without this it is the topmost window at EVERY point: the macOS screenshot
  // picker (Cmd+Shift+4 space / Cmd+Shift+5 window mode) offers the overlay
  // instead of the app the user is pointing at, and a region capture bakes the pet
  // into the image. Doubly necessary here because of the screen-saver level below.
  win.setContentProtection(true);
  // "screen-saver" — above ordinary always-on-top windows, so the pet is not
  // buried by other floating panels.
  win.setAlwaysOnTop(true, "screen-saver");
  win.loadURL(mochiPageUrl(currentBaseUrl, "pet.html", currentToken));

  // Diagnostics: a transparent overlay that failed to load looks identical to
  // one that loaded and drew nothing.
  win.webContents.on("did-fail-load", (_e, code, desc, url) => {
    // Strip the query string: the pet window URL carries the session token
    // (?token=…), which must not be written to the console/log on a load error.
    const safeUrl = String(url || "").split("?")[0];
    console.warn(`Mochi pet: load failed (${code} ${desc}) for ${safeUrl}`);
  });

  // A gateway error page (any status >= 400) is a COMPLETED navigation, not a
  // did-fail-load, so without this the frameless full-display overlay would
  // reveal it with no way to close it. Hide + latch here; the reconcile tick
  // re-arms the overlay with a fresh token (rearmBlankedOverlays).
  win.webContents.on("did-navigate", (_e, _url, httpResponseCode) => {
    handleOverlayNavigation(win, httpResponseCode);
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    console.warn("Mochi pet: renderer gone —", details && details.reason);
    // Tear down so the reconcile loop recreates it: isDestroyed() stays false
    // on a window with a dead renderer, so it would otherwise be handed back
    // forever and the pet would be gone until the app restarted.
    closePetWindow();
  });
  win.webContents.on("console-message", (_e, level, message) => {
    if (level >= 2) console.warn("Mochi pet page error:", message);
  });

  return win;
}

/** Where the pet should start when there is no saved position. */
function defaultStartPos(display) {
  return {
    x: Math.max(0, display.bounds.width - PET_W - 80),
    y: Math.max(0, display.bounds.height - PET_H - 120),
  };
}

/**
 * Activation handshake for one overlay.
 *
 * PetWidget renders NOTHING until it receives set-active(true) —
 * useDisplayActivation starts inactive — so this is what makes the pet appear.
 * The 300ms re-send is upstream's and is load-bearing: the page's React effects
 * may not have registered their listeners when the first event fires, and a
 * missed event means a permanently invisible pet.
 */
function wireHandshake(win, displayId, pos) {
  win.webContents.on("did-finish-load", () => {
    const send = () => {
      if (win.isDestroyed()) return;
      win.webContents.send(
          "mochi-pet:displays-info",
          getAllDisplayInfo(),
          displayId,
          activeDisplayId,
        );
      if (displayId === activeDisplayId) {
        win.webContents.send("mochi-pet:set-active", true, pos.x, pos.y, false);
      } else {
        win.webContents.send("mochi-pet:set-active", false);
      }
    };
    send();
    setTimeout(send, 300);
    // Never reveal an overlay currently hidden on a gateway error page (see the
    // did-navigate latch): showing it would blanket the display with an
    // uncloseable page. A healed reload clears the latch and re-fires
    // did-finish-load, which then shows the pet.
    if (!overlayBlanked.has(win) && !win.isVisible()) win.showInactive();
    startHitPoll();
    assertHostStaysInDock();
  });
}

/**
 * Rebuild overlays when displays are added, removed or rearranged.
 *
 * Upstream's version contains a duplicated `if (isHidden)` block (a copy/paste
 * slip); it is ported once here.
 */
function onDisplayChange() {
  const newDisplays = screen.getAllDisplays();
  const newIds = new Set(newDisplays.map((d) => d.id));

  // Displays that went away.
  for (const [id, win] of [...overlays]) {
    if (newIds.has(id)) continue;
    if (!win.isDestroyed()) win.close();
    removeOverlay(id);
    if (activeDisplayId !== id) continue;
    // The pet was on the display that vanished — move it to primary, clamped.
    const primary = screen.getPrimaryDisplay();
    activeDisplayId = primary.id;
    const pos = savedPetPos ?? defaultStartPos(primary);
    const x = Math.max(0, Math.min(primary.bounds.width - PET_W, pos.x));
    const y = Math.max(0, Math.min(primary.bounds.height - PET_H, pos.y));
    savePetPos(x, y, primary.id);
    const pWin = overlays.get(primary.id);
    if (pWin && !pWin.isDestroyed()) {
      pWin.webContents.send("mochi-pet:set-active", true, x, y, false);
    }
  }

  // Displays that appeared, and bounds updates for the rest.
  for (const d of newDisplays) {
    const existing = overlays.get(d.id);
    if (existing === undefined) {
      const win = createOverlayForDisplay(d);
      registerOverlay(d.id, win);
      wireHandshake(win, d.id, savedPetPos ?? defaultStartPos(d));
    } else if (!existing.isDestroyed()) {
      existing.setBounds(d.bounds);
    }
  }

  // Everyone needs the new geometry for edge detection.
  const info = getAllDisplayInfo();
  for (const [id, win] of overlays) {
    if (!win.isDestroyed()) {
      win.webContents.send("mochi-pet:displays-info", info, id, activeDisplayId);
    }
  }
}

/**
 * Open the pet overlays — one per display (upstream `createAllOverlays`).
 * Idempotent: returns the active overlay if they already exist.
 *
 * @param {string} baseUrl gateway origin, e.g. http://localhost:6777
 */
function openPetWindow(baseUrl, token = "") {
  if (overlays.size > 0) return getActiveOverlay();

  currentBaseUrl = baseUrl;
  currentToken = token || "";
  bindIpc();

  const primary = screen.getPrimaryDisplay();
  // The saved position is local to whatever display it was on; upstream also
  // starts from primary and lets a later transfer correct it.
  activeDisplayId = savedPetPos?.displayId ?? primary.id;
  if (!screen.getAllDisplays().some((d) => d.id === activeDisplayId)) {
    activeDisplayId = primary.id; // saved display is no longer connected
  }

  const activeDisplay =
    screen.getAllDisplays().find((d) => d.id === activeDisplayId) || primary;
  const startPos = savedPetPos ?? defaultStartPos(activeDisplay);

  // Pre-seed the hitbox so the active overlay is interactive before the
  // renderer's first updateHitbox arrives (upstream did the same).
  petHitbox = { x: startPos.x, y: startPos.y, w: PET_W, h: PET_H };

  for (const d of screen.getAllDisplays()) {
    const win = createOverlayForDisplay(d);
    registerOverlay(d.id, win);
    wireHandshake(win, d.id, startPos);
  }

  if (!displayListenersBound) {
    displayListenersBound = true;
    screen.on("display-added", onDisplayChange);
    screen.on("display-removed", onDisplayChange);
    screen.on("display-metrics-changed", onDisplayChange);
  }

  return getActiveOverlay();
}

function closePetWindow() {
  stopHitPoll();
  stopDragPolling();
  for (const win of [...overlays.values()]) {
    if (!win.isDestroyed()) win.close();
  }
  overlays.clear();
  petHitbox = null;
  bubbleHitbox = null;
  menuHitbox = null;
  menuOpen = false;
}

/**
 * Hide every overlay without destroying it (hideAll hotkey). Returns whether any
 * WAS visible, so the caller can restore the prior state.
 *
 * alwaysOnTop is lowered first, mirroring upstream: an overlay pinned at the
 * screen-saver level can stay above the menu bar even when hidden.
 */
function hidePetWindow() {
  let wasVisible = false;
  for (const win of overlays.values()) {
    if (win.isDestroyed()) continue;
    if (win.isVisible()) wasVisible = true;
    win.setAlwaysOnTop(false);
    win.hide();
  }
  return wasVisible;
}

function showPetWindow() {
  for (const win of overlays.values()) {
    if (win.isDestroyed()) continue;
    win.setAlwaysOnTop(true, "screen-saver");
    // Honor the error-page latch: the hide-all restore must not re-reveal an
    // overlay currently hidden on a gateway error page (same guard as the
    // load-finished handler), or CMD+SHIFT+H would bring the uncloseable page
    // back after a persisted auth failure.
    if (!overlayBlanked.has(win) && !win.isVisible()) win.showInactive();
  }
}

function isPetWindowOpen() {
  for (const win of overlays.values()) {
    if (!win.isDestroyed()) return true;
  }
  return false;
}

// ── Query surface ─────────────────────────────────────────────────────────

/** The overlay currently hosting the pet (upstream getMainWindow). */
function getActiveOverlay() {
  if (activeDisplayId !== null) {
    const win = overlays.get(activeDisplayId);
    if (win && !win.isDestroyed()) return win;
  }
  return null;
}

function getActiveDisplayId() {
  return activeDisplayId;
}

/** Screen-coordinate origin of the active display — local<->global conversion. */
function getOverlayOrigin() {
  if (activeDisplayId !== null) {
    const display = screen.getAllDisplays().find((d) => d.id === activeDisplayId);
    if (display) return { x: display.bounds.x, y: display.bounds.y };
  }
  const primary = screen.getPrimaryDisplay();
  return { x: primary.bounds.x, y: primary.bounds.y };
}

/** Display list for the pet-action tool. */
function getDisplaysForMcp() {
  const primary = screen.getPrimaryDisplay();
  return screen.getAllDisplays().map((d, i) => ({
    id: d.id,
    width: d.bounds.width,
    height: d.bounds.height,
    label: d.id === primary.id ? "Primary" : `Display ${i + 1}`,
    primary: d.id === primary.id,
  }));
}

/**
 * Move the pet to a specific display (tool-driven, no drag).
 *
 * Creates the overlay on demand: a display connected after startup may not have
 * one yet, and upstream waited 300ms after load before transferring so the
 * renderer's listeners exist.
 */
async function transferPetToDisplayById(displayId, localX, localY) {
  const display = screen.getAllDisplays().find((d) => d.id === displayId);
  if (!display) return false;

  if (!overlays.has(displayId)) {
    const win = createOverlayForDisplay(display);
    registerOverlay(displayId, win);
    return new Promise((resolve) => {
      win.webContents.on("did-finish-load", () => {
        if (win.isDestroyed()) {
          resolve(false);
          return;
        }
        win.webContents.send(
          "mochi-pet:displays-info",
          getAllDisplayInfo(),
          displayId,
          activeDisplayId,
        );
        setTimeout(() => {
          if (win.isDestroyed()) {
            resolve(false);
            return;
          }
          transferPetToDisplay(displayId, localX, localY);
          savePetPos(localX, localY, displayId);
          resolve(true);
        }, 300);
      });
      // Do not reveal an overlay hidden on a gateway error page (see the
      // did-navigate latch); a healed reload clears the latch and shows it.
      if (!overlayBlanked.has(win) && !win.isVisible()) win.showInactive();
    });
  }

  transferPetToDisplay(displayId, localX, localY);
  savePetPos(localX, localY, displayId);
  return true;
}

module.exports = {
  openPetWindow,
  closePetWindow,
  hidePetWindow,
  showPetWindow,
  isPetWindowOpen,
  getActiveOverlay,
  getActiveDisplayId,
  getOverlayOrigin,
  getDisplaysForMcp,
  transferPetToDisplayById,
  getSavedPetPos,
  broadcastToOverlays,
  rearmBlankedOverlays,
  hasBlankedOverlay,
  // Exported for tests: the pure geometry decisions, no Electron needed.
  _inRect: inRect,
  _shouldIgnoreAt: shouldIgnoreAt,
  _clampLocal: clampLocal,
  _findNearestDisplay: findNearestDisplay,
  // Exported for tests: the error-page recovery policy + navigation handler.
  _isOverlayErrorPage: isOverlayErrorPage,
  _handleOverlayNavigation: handleOverlayNavigation,
  // Exported for tests: overlay-map lifecycle (identity-checked cleanup).
  _registerOverlay: registerOverlay,
  _getOverlays: getOverlays,
  PET_W,
  PET_H,
  POLL_MS,
  DRAG_SAFETY_MS,
};
