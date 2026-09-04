/**
 * panelWindow.js — Mochi's chat panel window.
 *
 * PORTED from the original src/main/chatWindowManager.ts. Opened by clicking the
 * pet (`openChat` -> `mochi-pet:open-chat`), and toggled: a second click hides
 * it, matching the original's toggle-expand behavior.
 *
 * Deliberately NOT the pet's overlay:
 *  - opaque (`transparent: false` + a backgroundColor), because it holds real
 *    UI — a transparent chat panel would show the desktop through the text;
 *  - focusable and resizable, since the user types in it;
 *  - frameless with the traffic lights pushed off-screen
 *    (`trafficLightPosition: {x:-100,y:-100}`), which is how the original got a
 *    chromeless panel that still behaves like a window on macOS.
 *
 * `skipTaskbar: true` keeps it out of the window list — it is an accessory of
 * the pet, not a document window.
 *
 * Bounds are NOT persisted yet. The original saved them to its own config; as a
 * builtin that belongs in Mochi's settings store, and is a follow-up rather
 * than something to hand-roll in the shell.
 */

const { app, BrowserWindow, ipcMain, screen, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { mochiPageUrl } = require("./pageUrl");

// Returns the dashboard main window (a BaseWindow owned by main.js) or null.
// Wired at init via setMainWindowGetter so the panel-close path can keep the
// dashboard from being surfaced by macOS window promotion (see mochi-panel:close).
let getMainWindow = null;
function setMainWindowGetter(fn) {
  getMainWindow = typeof fn === "function" ? fn : null;
}

// macOS only. A mouse click on the panel's own X makes this non-activating
// panel the app's key window; hiding a key window makes macOS promote the app's
// next FOCUSABLE regular window (the dashboard) to key and bring the app
// forward -- resurfacing a dashboard the user had left behind another app. The
// pet overlay never triggers this because it is setFocusable(false), so it is
// never a promotion target; the pet-click and hotkey hides likewise don't,
// because their gesture doesn't land in the panel. Mirror the pet: make the
// dashboard briefly non-focusable across the hide so macOS finds no window to
// promote and returns activation to the previously-active app, then restore
// focusability so the user can click the dashboard again. No-op unless the
// dashboard exists and is not itself the focused window.
function hidePanelReleasingFocus() {
  if (!panelWindow || panelWindow.isDestroyed()) return;
  let mw = null;
  try {
    mw = process.platform === "darwin" && getMainWindow ? getMainWindow() : null;
  } catch {
    mw = null;
  }
  const shield =
    mw &&
    !mw.isDestroyed() &&
    typeof mw.isFocusable === "function" &&
    mw.isFocusable() &&
    !mw.isFocused();
  if (shield) {
    try {
      mw.setFocusable(false);
    } catch {
      /* BaseWindow shape changed */
    }
  }
  panelWindow.hide();
  if (shield) {
    // Restore after the window-server settles (empirically stable by ~250ms).
    setTimeout(() => {
      try {
        if (!mw.isDestroyed()) mw.setFocusable(true);
      } catch {
        /* torn down */
      }
    }, 300);
  }
}

/** Original defaults (chatWindowManager.ts:16). */
const PANEL_W = 320;
const PANEL_H = 470;
/** Inset from the work-area corner, matching the original's 20px. */
const PANEL_MARGIN = 20;
/**
 * Width clamp for the cross-agent `mochi-panel:set-width` channel. Floor mirrors
 * the window's own minWidth; the ceiling stops a runaway renderer value from
 * blowing the panel across the whole display.
 */
const PANEL_MIN_W = 260;
const PANEL_MAX_W = 1200;

/**
 * Where a width change should put the panel's left edge.
 *
 * Growing a panel that already sits near the right edge of the display would push
 * it off-screen, so the original's `applySidePanelResize` pulled X back to keep
 * the whole window inside the work area (and clamped at the left edge so a window
 * wider than the display starts flush rather than negative). Our previous handler
 * preserved X unconditionally, which is why opening a dock on a right-docked panel
 * pushed the rail past the screen.
 *
 * Pure so it can be unit-tested without a display.
 */
function panelLeftForWidth(x, width, workArea) {
  if (x + width <= workArea.x + workArea.width) return x;
  return Math.max(workArea.x, workArea.x + workArea.width - width);
}

/** @type {BrowserWindow|null} */
let panelWindow = null;
let ipcBound = false;
/**
 * The gateway the panel should open against, and its first-load token.
 *
 * MODULE-LEVEL, read at call time — NOT captured by bindPanelIpc. The IPC binding
 * is deliberately one-shot (re-registering would stack duplicate handlers), so a
 * handler that closed over the origin it was first given would keep opening the
 * OLD instance's panel forever after the user switched `petInstance`, and no
 * amount of tearing windows down would fix it. Kept current by setPanelTarget on
 * every reconcile tick instead.
 */
let currentBaseUrl = "";
let currentToken = "";

/**
 * Gateway log sink, injected by the shell (main.js `glog`) so panel-lifecycle
 * events — especially renderer crashes and forwarded page errors — land in
 * gateway-launch.log next to everything else, instead of a console the packaged
 * app throws away. Defaults to console.warn so the module is usable (and
 * testable) before the shell wires it.
 */
let logFn = (line) => console.warn(line);
function setPanelLogger(fn) {
  if (typeof fn === "function") logFn = fn;
}

/**
 * Crash-loop guard. A renderer that dies on every load (e.g. a poison message
 * replayed from restored chat state) must not be resurrected forever — that
 * just strobes a black window a few times a second. After MAX_PANEL_CRASHES
 * auto-recreates within PANEL_CRASH_WINDOW_MS we stop and leave the panel
 * closed, logging why, so the user gets a clean "no panel" rather than a
 * flickering zombie. Timestamps older than the window are pruned, so isolated
 * crashes hours apart each still self-heal.
 */
const PANEL_CRASH_WINDOW_MS = 60_000;
const MAX_PANEL_CRASHES = 3;
/** @type {number[]} */
let panelCrashTimes = [];

// Hidden-singleton contract, ported from the original chatWindowManager: a
// user close is intercepted into hide() so the WS session, chat history, and
// last position/size survive; only a real app quit destroys it. Mirrors the
// original's module-level isQuitting set on before-quit. Guarded because the
// unit test loads this module with a stub electron whose `app` may lack `on`.
let isQuitting = false;
if (app && typeof app.on === "function") {
  app.on("before-quit", () => { isQuitting = true; });
}

// Whether the panel was visible when the reconcile loop last HID it on disable,
// so re-enable can restore exactly that. Distinct from a user dismissal: a user
// close (close -> hide) clears this so re-enabling does NOT resurrect a panel
// the user deliberately put away.
let wasVisibleBeforeHide = false;

/** Clamp an arbitrary renderer-supplied width to a sane pixel range. */
function clampPanelWidth(w) {
  const n = Math.round(Number(w));
  if (!Number.isFinite(n)) return PANEL_W;
  return Math.max(PANEL_MIN_W, Math.min(PANEL_MAX_W, n));
}

/**
 * True when a window's renderer process is gone even though the window object
 * still exists. Guards every show()/focus() path: the panel is opaque and
 * always-on-top, so show()-ing a window whose renderer has died paints the last
 * (or a blank) framebuffer as a dead black rectangle — exactly the bug being
 * fixed. `isCrashed()` is absent on very old Electron and on the test stub's
 * bare webContents, so it is called defensively.
 */
function isRendererGone(win) {
  try {
    const wc = win && win.webContents;
    return !!(wc && typeof wc.isCrashed === "function" && wc.isCrashed());
  } catch {
    return false;
  }
}

function createPanelWindow(baseUrl, token = "") {
  const wa = screen.getPrimaryDisplay().workArea;
  const win = new BrowserWindow({
    x: wa.x + wa.width - PANEL_W - PANEL_MARGIN,
    y: wa.y + wa.height - PANEL_H - PANEL_MARGIN,
    width: PANEL_W,
    height: PANEL_H,
    // A NON-ACTIVATING panel, macOS only. There it adds
    // NSWindowStyleMaskNonactivatingPanel, so showing/focusing the panel gives
    // it keyboard focus WITHOUT activating the app -- the Spotlight/Alfred
    // behavior. That matters because the shell's `app.on("activate")` restores
    // the dashboard window, so without this a single click on the pet resurrects
    // a dashboard the user had deliberately hidden: they asked for the chat
    // panel, not the whole app. Fixing it here keeps the shell out of it,
    // instead of teaching the generic activate handler to recognize this one
    // window.
    //
    // Gated on darwin because `type` is not a macOS-only option that other
    // platforms ignore -- its legal VALUES differ per platform ("panel" is not
    // one of Linux's desktop/dock/toolbar/splash/notification, nor Windows'
    // toolbar). The panel is reachable off macOS via the CommandOrControl+Shift+M
    // shortcut even though the pet overlay itself is darwin-only, so this really
    // does run there.
    ...(process.platform === "darwin" ? { type: "panel" } : {}),
    frame: false,
    titleBarStyle: "hidden",
    // Off-screen traffic lights: frameless, but macOS still treats it as a
    // normal window (drag, resize) rather than a panel with no controls.
    trafficLightPosition: { x: -100, y: -100 },
    transparent: false,
    backgroundColor: "#0f1117",
    resizable: true,
    // Below ~300px the chat header's toggle row and the composer's send button
    // start overlapping; the docked side panels have their own widths on top.
    minWidth: 300,
    minHeight: 320,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "pet-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
    },
  });

  win.loadURL(mochiPageUrl(baseUrl, "panel.html", token));
  win.setVisibleOnAllWorkspaces(true);
  // "floating" (not "screen-saver"): the panel should sit above ordinary
  // windows but BELOW the pet, so the pet is never hidden behind its own panel.
  win.setAlwaysOnTop(true, "floating");

  // The panel never navigates away from the gateway origin — any link the user
  // clicks (a watch-list target, a chat link) goes to the system browser.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(baseUrl)) {
      event.preventDefault();
      if (/^https?:/.test(url)) shell.openExternal(url);
    }
  });

  win.webContents.on("did-fail-load", (_e, code, desc, url) => {
    // Strip the query string before logging: a failed remote panel load carries
    // the session token in the URL (?token=…), which would otherwise be written
    // verbatim to gateway-launch.log.
    const safeUrl = String(url || "").split("?")[0];
    logFn(`Mochi panel: load failed (${code} ${desc}) for ${safeUrl}`);
  });
  // Forward the panel page's own warnings/errors into the gateway log. The
  // panel is loaded from the gateway and runs the ported renderer; when it
  // crashes mid-stream the LAST thing it logged is the closest thing to a stack
  // trace we get, and a packaged app has no devtools console to read it from.
  // level: 0 verbose, 1 info, 2 warning, 3 error — forward 2+ only.
  win.webContents.on("console-message", (_e, level, message, line, sourceId) => {
    if (level >= 2) {
      const where = sourceId ? ` (${sourceId}:${line})` : "";
      logFn(`Mochi panel console [${level >= 3 ? "error" : "warn"}]: ${message}${where}`);
    }
  });
  // Renderer death (crash / OOM / GPU fault). The old handler merely destroyed
  // the window, which — because the reconcile restore path keys off
  // wasVisibleBeforeHide, never set on a crash — left the user with no way to
  // get the panel back short of an app restart (the reported "disable/enable
  // does not recover it"). Self-heal instead: log the cause and recreate.
  win.webContents.on("render-process-gone", (_e, details) => {
    recoverFromRendererGone(baseUrl, details && details.reason, details && details.exitCode);
  });
  // A hung (not dead) renderer: log and force a fresh load. reload() replaces
  // the wedged page rather than leaving a frozen opaque panel; if the hang is
  // terminal this later surfaces as render-process-gone and the crash path
  // takes over.
  win.webContents.on("unresponsive", () => {
    logFn("Mochi panel: renderer unresponsive — reloading");
    try {
      if (!win.isDestroyed() && !win.webContents.isDestroyed()) win.webContents.reload();
    } catch {
      /* window torn down between the hang and the reload */
    }
  });

  win.once("ready-to-show", () => win.show());
  // Opaque window, so ready-to-show is reliable here; still belt-and-braces.
  win.webContents.on("did-finish-load", () => {
    if (!win.isDestroyed() && !win.isVisible()) win.show();
  });

  // Hidden singleton: a user close (red traffic light / Cmd-W) hides the panel
  // instead of destroying it, so the chat's WebSocket, scroll position, and
  // in-flight turn survive and reopening restores the last geometry. Only a
  // real app quit lets the close through. A user dismissal also clears the
  // reconcile restore-intent: re-enabling Mochi must NOT resurrect a panel the
  // user deliberately closed.
  win.on("close", (e) => {
    if (isQuitting) return;
    e.preventDefault();
    hidePanelReleasingFocus();
    wasVisibleBeforeHide = false;
  });

  win.on("closed", () => {
    panelWindow = null;
  });

  return win;
}

/**
 * Recover from the panel renderer dying (crash / OOM / GPU fault).
 *
 * Ordering is deliberate:
 *  1. Log the cause (reason + exitCode) so the next run — with console-message
 *     forwarding above — has a breadcrumb.
 *  2. DESTROY the dead window immediately and null the handle. Leaving it alive
 *     keeps isDestroyed()===false, so openPanelWindow/restorePanelOnEnable would
 *     hand it back and show() it — an opaque window with a dead renderer paints
 *     a black rectangle. This is also why a crashed renderer must not be parked
 *     by the close→hide hidden-singleton logic: we tear it down, not hide it.
 *  3. Recreate ONLY when the death was a genuine fault, the app is not quitting,
 *     and the panel was actually on screen — a hidden panel is recreated fresh
 *     on its next open with no surprise pop-up, and our own quit/disable
 *     teardown (which also ends the renderer) must never trigger a recreate.
 *  4. Respect the crash-loop guard so a page that dies on every load stops
 *     strobing.
 */
function recoverFromRendererGone(baseUrl, reason, exitCode) {
  logFn(`Mochi panel: renderer gone — reason=${reason} exitCode=${exitCode}`);

  const win = panelWindow;
  const wasVisible = !!(win && !win.isDestroyed() && win.isVisible());
  if (win && !win.isDestroyed()) win.destroy();
  panelWindow = null;

  if (isQuitting) return;
  // Reasons Electron reports for render-process-gone. 'clean-exit'/'killed' are
  // our own teardown; only these are faults worth healing.
  const RECOVERABLE = new Set([
    "crashed",
    "abnormal-exit",
    "oom",
    "integrity-failure",
    "launch-failed",
  ]);
  if (!RECOVERABLE.has(reason)) return;
  if (!wasVisible) return;

  const now = Date.now();
  panelCrashTimes = panelCrashTimes.filter((t) => now - t < PANEL_CRASH_WINDOW_MS);
  if (panelCrashTimes.length >= MAX_PANEL_CRASHES) {
    logFn(
      `Mochi panel: renderer crashed ${panelCrashTimes.length + 1}x within ` +
        `${PANEL_CRASH_WINDOW_MS}ms — not recreating (crash loop)`,
    );
    // Give up cleanly: also clear the reconcile restore-intent so a later
    // enable tick does not immediately re-trigger the loop.
    wasVisibleBeforeHide = false;
    return;
  }
  panelCrashTimes.push(now);
  logFn("Mochi panel: recreating after renderer crash");
  // The CURRENT target, not the `baseUrl` this recovery was handed: that value was
  // captured when the window was created, so on a remote instance it both DROPS the
  // first-load token (every request would then 403, and a crash-recovered panel
  // showing a 403 page cannot heal itself) and may point at an instance the user
  // has since switched away from.
  openPanelWindow(currentBaseUrl || baseUrl, currentToken);
}

/** Show the panel, creating it on first use. */
function openPanelWindow(baseUrl, token = "") {
  // Reuse the live window — but NEVER a window whose renderer has died: show()
  // on that paints a dead black rectangle. If the handle is stale (destroyed or
  // renderer-gone), tear it down and build a fresh one.
  if (panelWindow && !panelWindow.isDestroyed() && !isRendererGone(panelWindow)) {
    if (!panelWindow.isVisible()) panelWindow.show();
    panelWindow.focus();
    return panelWindow;
  }
  if (panelWindow && !panelWindow.isDestroyed()) {
    panelWindow.destroy();
    panelWindow = null;
  }
  panelWindow = createPanelWindow(baseUrl, token);
  return panelWindow;
}

/**
 * Toggle — this is what the pet click is wired to.
 *
 * Hides rather than closes so the chat keeps its scroll position and in-flight
 * turn; the WebSocket stays connected while hidden.
 */
function togglePanelWindow(baseUrl, token = "") {
  if (panelWindow && !panelWindow.isDestroyed() && panelWindow.isVisible()) {
    hidePanelReleasingFocus();
    return null;
  }
  return openPanelWindow(baseUrl, token);
}

function closePanelWindow() {
  if (panelWindow && !panelWindow.isDestroyed()) panelWindow.destroy();
  panelWindow = null;
}

/**
 * Plain hide for the hideAll hotkey. Returns whether the panel WAS visible, so
 * the caller can restore exactly that on toggle-back.
 *
 * Deliberately NOT hidePanelOnDisable: that records reconcile restore-intent
 * (wasVisibleBeforeHide), which the very next enabled reconcile tick would act
 * on by re-showing the panel — defeating a user hideAll within 5s. This touches
 * only window visibility and no reconcile state.
 */
function hidePanelWindow() {
  if (
    panelWindow &&
    !panelWindow.isDestroyed() &&
    !isRendererGone(panelWindow) &&
    panelWindow.isVisible()
  ) {
    hidePanelReleasingFocus();
    return true;
  }
  return false;
}

/**
 * Open (or focus) the panel, then tell its renderer to switch to a view.
 *
 * The pet overlay is a different window, so a pet-menu item like Memories has
 * to reach across: open the panel here, then send it a named view channel the
 * ChatPanel listens for. On a FIRST open the React app has not yet registered
 * its IPC listeners when did-finish-load fires, so — mirroring the pet
 * activation handshake in petWindow.js — we send on did-finish-load and re-send
 * once shortly after. After first use the panel only HIDES (never closes), so
 * on every later open its listeners are already live and one send suffices.
 */
function showPanelView(baseUrl, viewChannel, token = "") {
  const win = openPanelWindow(baseUrl, token);
  if (!win) return;
  const wc = win.webContents;
  const send = () => {
    try {
      if (!win.isDestroyed() && !wc.isDestroyed()) wc.send(viewChannel);
    } catch {
      /* window torn down between open and send */
    }
  };
  if (wc.isLoading()) {
    wc.once("did-finish-load", () => {
      send();
      setTimeout(send, 300);
    });
  } else {
    send();
  }
}

function isPanelWindowOpen() {
  return (
    panelWindow !== null &&
    !panelWindow.isDestroyed() &&
    !isRendererGone(panelWindow) &&
    panelWindow.isVisible()
  );
}

/**
 * Reconcile: HIDE the panel when Mochi is disabled.
 *
 * The panel is opaque and always-on-top, so leaving it up after disable orphans
 * a near-black rectangle floating over the desktop (the original bug). We hide
 * rather than destroy to keep the session, and record that it WAS visible so
 * re-enable can bring it back. Guarded on isVisible() so repeated disabled ticks
 * cannot clobber the remembered state to false after the first hide.
 */
function hidePanelOnDisable() {
  if (panelWindow && !panelWindow.isDestroyed() && panelWindow.isVisible()) {
    wasVisibleBeforeHide = true;
    panelWindow.hide();
  }
}

/**
 * Drop the panel because the gateway it is attached to is CHANGING.
 *
 * DESTROY, not hide — and that distinction is the whole point of this function
 * existing next to hidePanelOnDisable. A hidden panel is still a live window, so
 * the next openPanelWindow reuses it with a plain show(), and it would keep
 * rendering the OLD instance's chat slot after the user switched. Only a destroyed
 * window forces a fresh load against the new origin.
 *
 * `wasVisibleBeforeHide` is set exactly as on disable, so restorePanelOnEnable
 * brings the panel back on the NEW instance if it was open on the old one —
 * switching instance should not silently close a panel the user had open.
 */
function dropPanelForInstanceSwitch() {
  if (!panelWindow || panelWindow.isDestroyed()) return;
  if (panelWindow.isVisible()) wasVisibleBeforeHide = true;
  panelWindow.destroy();
  panelWindow = null;
}

/**
 * Reconcile: RESTORE the panel when Mochi is re-enabled, but only if it was
 * visible when we hid it on disable. Idempotent: the isPanelWindowOpen() guard
 * makes every later enabled tick a no-op once the panel is back up, so the 5s
 * loop cannot re-focus it every cycle.
 */
function restorePanelOnEnable(baseUrl, token = "") {
  if (!wasVisibleBeforeHide) return;
  if (isPanelWindowOpen()) return;
  // ONE-SHOT: clear the flag as we act on it. It used to survive the restore, so
  // every later tick that found the panel not visible re-opened it — including
  // when the USER had just hidden it. isPanelWindowOpen() checks visibility, so a
  // hidden-but-alive window looks closed to this check and cannot distinguish the
  // two cases on its own.
  wasVisibleBeforeHide = false;
  openPanelWindow(baseUrl, token);
}

/**
 * Bind the pet -> panel IPC. Idempotent (the pet window can be recreated by the
 * reconcile loop, and re-registering would stack duplicate handlers).
 */
/**
 * Point the panel (and the pet's menu actions) at a gateway. Called every
 * reconcile tick so a `petInstance` switch reaches the one-shot IPC handlers.
 */
function setPanelTarget(baseUrl, token = "") {
  if (baseUrl) currentBaseUrl = baseUrl;
  currentToken = token || "";
}

function bindPanelIpc(baseUrl) {
  // SEED the origin only, and never touch the token: this runs from the pet's
  // one-shot bindIpc, so passing through setPanelTarget (whose token parameter
  // defaults to "") would CLEAR a token the reconcile tick had just set, leaving
  // the pet-menu channels loading a remote panel unauthenticated until the next
  // tick restored it.
  if (baseUrl && !currentBaseUrl) currentBaseUrl = baseUrl;
  if (ipcBound) return;
  ipcBound = true;
  ipcMain.on("mochi-pet:open-chat", () => togglePanelWindow(currentBaseUrl, currentToken));
  // Pet-menu → panel views. The pet is a separate window, so it opens/focuses
  // the panel and asks it to show the view (see showPanelView + pet-preload).
  ipcMain.on("mochi-pet:open-memories", () =>
    showPanelView(currentBaseUrl, "mochi-panel:show-memories", currentToken));
  // The pet's menu is the SAME menu as the panel's (see panel/mochiMenu.ts), but
  // two of its actions live in the panel's React state. Rather than duplicate
  // them in the pet, the pet asks the panel to run its own code path.
  ipcMain.on("mochi-pet:clear-screen", () =>
    showPanelView(currentBaseUrl, "mochi-panel:clear-screen", currentToken));
  ipcMain.on("mochi-pet:delete-history", () =>
    showPanelView(currentBaseUrl, "mochi-panel:delete-history", currentToken));
  // NO "mochi-pet:open-settings" here: Settings is its own window, owned by
  // pet/settingsWindow.js (which registers that channel at module load). Two
  // listeners on one channel would open the window AND flip a panel view.
  // Dashboard = the gateway origin itself, opened in the system browser. A
  // dedicated no-arg channel (not the renderer passing a URL) keeps the shell
  // from ever being handed an arbitrary external URL by page content.
  ipcMain.on("mochi-panel:open-dashboard", () => {
    if (/^https?:/.test(currentBaseUrl)) shell.openExternal(currentBaseUrl);
  });
  ipcMain.on("mochi-panel:close", () => {
    hidePanelReleasingFocus();
  });

  /**
   * Reveal a file in the OS file manager.
   *
   * VALIDATED, not forwarded: the argument comes from page content, so an
   * unchecked `showItemInFolder` would let chat content point the shell at any
   * path on disk. Only an existing regular file is accepted, and the failure is
   * silent — a renderer must not learn from the shell whether a path exists.
   */
  ipcMain.on("mochi-panel:reveal-file", (_e, filePath) => {
    if (typeof filePath !== "string" || filePath === "") return;
    try {
      if (!fs.statSync(filePath).isFile()) return;
    } catch {
      return;
    }
    shell.showItemInFolder(filePath);
  });

  /**
   * Open a link in the default browser.
   *
   * http(s) ONLY. Other schemes (file:, and on macOS anything registered by
   * another app) turn "open a link" into "launch an arbitrary handler with an
   * attacker-chosen argument", which is exactly what the panel's deny-by-default
   * navigation handler exists to prevent.
   */
  ipcMain.on("mochi-panel:open-external", (_e, url) => {
    if (typeof url !== "string") return;
    if (!/^https?:\/\//i.test(url)) return;
    shell.openExternal(url);
  });
  // Cross-agent contract: a renderer control adjusts the panel WIDTH only
  // (opening/closing the Pins or Watchlist dock). Height stays; X moves only when
  // the new width would otherwise overflow the display, matching the original's
  // side-panel resize. The width is clamped so a bad value can't blow the panel
  // across the display.
  ipcMain.on("mochi-panel:set-width", (_e, w) => {
    if (!panelWindow || panelWindow.isDestroyed()) return;
    const b = panelWindow.getBounds();
    const width = clampPanelWidth(w);
    const workArea = screen.getDisplayMatching(b).workArea;
    panelWindow.setBounds({
      x: panelLeftForWidth(b.x, width, workArea),
      y: b.y,
      width,
      height: b.height,
    });
  });
}

module.exports = {
  dropPanelForInstanceSwitch,
  setPanelTarget,
  openPanelWindow,
  togglePanelWindow,
  showPanelView,
  closePanelWindow,
  isPanelWindowOpen,
  hidePanelOnDisable,
  restorePanelOnEnable,
  bindPanelIpc,
  clampPanelWidth,
  panelLeftForWidth,
  setPanelLogger,
  setMainWindowGetter,
  hidePanelWindow,
  PANEL_W,
  PANEL_H,
  PANEL_MARGIN,
};
