"use strict";

/**
 * app-window-host.js — the desktop windows and global shortcuts apps declare.
 *
 * An app's manifest may carry a `desktop` block (rules: app-window-host-rules.js,
 * mirrored from the gateway's `apps/desktop_manifest.py`). This module serves it,
 * for every app at once and naming none: a reconcile tick reads `/api/apps`, opens
 * the windows an enabled app declares `openOnEnable`, registers the shortcuts it
 * declares, and closes and releases everything for an app that is disabled, gone
 * or no longer declares it.
 *
 * NO APP CODE RUNS HERE. Every window is created by this module with fixed web
 * preferences (context isolation, sandbox, no Node), loads the host's own
 * `/popout/desktop/<app>/<id>` route from the gateway origin with the same
 * credential chain the dashboard window uses, and that route mounts the app's
 * declared ESM `entry` through AppHost. The page reaches the host only through
 * the generic preload (app-window-preload.js), and every message it sends is
 * resolved back to the window it came from: a page can open, close or shape only
 * its OWN app's declared windows, and only within what the declaration allows.
 *
 * Fail closed on the page: a window is revealed only when its page says `ready`,
 * and a main-frame load that ends on an error document or a transport failure is
 * latched hidden (app-window-error-latch.js) until a later tick whose probe the
 * gateway answered reloads it. A probe that cannot be answered leaves every
 * window and shortcut exactly as it is, so a gateway restart does not flicker.
 */

const { createAppWindowErrorLatch } = require("./app-window-error-latch");
const { desktopDeclarations, isAppListBody } = require("./app-window-host-rules");
const { shellShortcutReservations } = require("./shell-shortcut-reservations");

/** Document-path prefix of a hosted window. Keep in sync with website/src/desktopWindow/. */
const DESKTOP_WINDOW_PREFIX = "/popout/desktop";
/** The bridge level the preload advertises; raised when the bridge gains a call. */
const APP_WINDOW_BRIDGE_LEVEL = 1;
const TICK_MS = 5_000;
const HIT_POLL_MS = 16;
const PROBE_TIMEOUT_MS = 5_000;
const EMPTY_AUTH_RETRY_MS = 30_000;
const MAX_HIT_REGIONS = 16;
const MAX_REGION_EXTENT = 100_000;
const MAX_PENDING_SHORTCUTS = 8;

const CHANNELS = Object.freeze({
  ready: "app-window:ready",
  close: "app-window:close",
  open: "app-window:open",
  hitRegions: "app-window:set-hit-regions",
  focusable: "app-window:set-focusable",
  shortcut: "app-window:shortcut",
});

/** The page path a hosted window loads; the only document its navigation may hold. */
function desktopWindowPath(app, id) {
  return `${DESKTOP_WINDOW_PREFIX}/${encodeURIComponent(app)}/${encodeURIComponent(id)}`;
}

/**
 * Hit regions a page reported, kept only when every one is a finite box.
 * Anything else answers null, which the caller treats as "no regions": the overlay
 * stays click-through, which is the safe state for a window covering a display.
 */
function sanitizeHitRegions(raw) {
  if (!Array.isArray(raw) || raw.length > MAX_HIT_REGIONS) return null;
  const out = [];
  for (const r of raw) {
    if (!r || typeof r !== "object") return null;
    const { x, y, width, height } = r;
    const nums = [x, y, width, height];
    if (!nums.every((n) => typeof n === "number" && Number.isFinite(n))) return null;
    if (width < 0 || height < 0 || nums.some((n) => Math.abs(n) > MAX_REGION_EXTENT)) return null;
    out.push({ x, y, width, height });
  }
  return out;
}

function pointInRegions(regions, x, y) {
  return regions.some((r) => x >= r.x && x <= r.x + r.width && y >= r.y && y <= r.y + r.height);
}

/**
 * @param {{
 *   electron: {BrowserWindow: Function, screen: object, ipcMain: object,
 *              globalShortcut: object, app?: object},
 *   backendUrl: string,
 *   fetchGatewayAuth?: () => Promise<{value: string, viaCookie?: boolean}>,
 *   cookieHeader?: (backendUrl: string, value: string) => string,
 *   fetchImpl?: typeof fetch,
 *   preloadPath: string,
 *   log?: (line: string) => void,
 *   platform?: string,
 *   now?: () => number,
 *   reservations?: {isReserved: (a: string) => boolean, onChange: (fn: () => void) => () => void},
 * }} deps
 */
function createAppWindowHost(deps) {
  const { electron, preloadPath } = deps;
  const { BrowserWindow, screen, ipcMain, globalShortcut } = electron;
  const backendUrl = String(deps.backendUrl || "").replace(/\/$/, "");
  const log = typeof deps.log === "function" ? deps.log : () => {};
  const platform = deps.platform || process.platform;
  const now = deps.now || Date.now;
  const fetchImpl = deps.fetchImpl || globalThis.fetch;
  const reservations = deps.reservations || shellShortcutReservations;
  let stopWatchingReservations = null;
  const backendOrigin = (() => {
    try {
      return new URL(backendUrl).origin;
    } catch {
      return "";
    }
  })();

  /** "<app>/<id>" -> {app, id, decl, win, ready, regions, lastIgnore, pending} */
  const records = new Map();
  /** webContents id -> record: how every page message is resolved to its window. */
  const bySender = new Map();
  /** canonical chord -> {app, shortcut} for what THIS host registered. */
  const liveShortcuts = new Map();
  /** chords already reported as unavailable, so a taken chord logs once. */
  const reportedTaken = new Set();
  let declarations = new Map();
  let pageToken = "";
  let cookieAuth = false;
  let cachedAuth = { value: "", viaCookie: false };
  let emptyAuthAt = 0;
  let timer = null;
  let hitTimer = null;
  let reconciling = false;
  let suspended = false;
  let started = false;
  const latch = createAppWindowErrorLatch({ onBlank: (win) => blank(win) });

  // ── Credentials and the probe ──────────────────────────────────────────────

  async function authFor(force) {
    if (!force && cachedAuth.value) return cachedAuth;
    if (!force && emptyAuthAt && now() - emptyAuthAt < EMPTY_AUTH_RETRY_MS) return cachedAuth;
    let auth = null;
    try {
      auth = deps.fetchGatewayAuth ? await deps.fetchGatewayAuth() : null;
    } catch {
      auth = null;
    }
    cachedAuth = auth && typeof auth.value === "string" && auth.value
      ? { value: auth.value, viaCookie: Boolean(auth.viaCookie) }
      : { value: "", viaCookie: false };
    emptyAuthAt = cachedAuth.value ? 0 : now();
    return cachedAuth;
  }

  /** @returns {Promise<{state: "ok"|"unauthorized"|"unknown", body?: unknown}>} */
  async function probe(auth) {
    if (!backendOrigin || typeof fetchImpl !== "function") return { state: "unknown" };
    let url = `${backendUrl}/api/apps`;
    const headers = {};
    if (auth.viaCookie) {
      const cookie = deps.cookieHeader ? deps.cookieHeader(backendUrl, auth.value) : "";
      if (!cookie) return { state: "unknown" };
      headers.Cookie = cookie;
    } else {
      url += `?token=${encodeURIComponent(auth.value)}`;
    }
    try {
      const res = await fetchImpl(url, {
        headers,
        redirect: "manual",
        signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      });
      if (res.status === 401 || res.status === 403) return { state: "unauthorized" };
      if (res.status !== 200) return { state: "unknown" };
      const body = await res.json();
      return isAppListBody(body) ? { state: "ok", body } : { state: "unknown" };
    } catch {
      return { state: "unknown" };
    }
  }

  function target(auth) {
    pageToken = auth.viaCookie ? "" : auth.value;
    cookieAuth = auth.viaCookie;
  }

  function pageUrl(app, id) {
    const url = `${backendUrl}${desktopWindowPath(app, id)}`;
    return pageToken ? `${url}?token=${encodeURIComponent(pageToken)}` : url;
  }

  // ── Windows ────────────────────────────────────────────────────────────────

  function isLive(rec) {
    return !!rec && !!rec.win && !rec.win.isDestroyed();
  }

  function displayAtCursor() {
    try {
      return screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    } catch {
      return screen.getPrimaryDisplay();
    }
  }

  function panelBounds(decl, workArea) {
    const width = Math.min(decl.width, workArea.width);
    const height = Math.min(decl.height, workArea.height);
    const margin = 16;
    const right = workArea.x + workArea.width - width - margin;
    if (decl.position === "center") {
      return {
        x: Math.round(workArea.x + (workArea.width - width) / 2),
        y: Math.round(workArea.y + (workArea.height - height) / 2),
        width,
        height,
      };
    }
    const top = workArea.y + margin;
    const bottom = workArea.y + workArea.height - height - margin;
    return {
      x: Math.max(workArea.x, right),
      y: Math.max(workArea.y, decl.position === "top-right" ? top : bottom),
      width,
      height,
    };
  }

  function windowOptions(app, decl) {
    const display = displayAtCursor();
    const overlay = decl.kind === "overlay";
    const bounds = overlay ? display.bounds : panelBounds(decl, display.workArea);
    return {
      ...bounds,
      frame: false,
      transparent: overlay || decl.transparent,
      alwaysOnTop: true,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      hasShadow: !overlay,
      show: false,
      // An overlay is never the active window on macOS, so every click is a
      // first-mouse click; without this the page sees moves but no mousedown.
      acceptFirstMouse: overlay,
      enableLargerThanScreen: overlay,
      webPreferences: {
        preload: preloadPath,
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        nodeIntegrationInSubFrames: false,
        webviewTag: false,
        // A transparent overlay is never focused, so Chromium would throttle it.
        backgroundThrottling: !overlay,
        additionalArguments: [
          `--kc-app-window=${app}/${decl.id}/${decl.kind}/${APP_WINDOW_BRIDGE_LEVEL}`,
        ],
      },
    };
  }

  /** True for the one document a hosted window may hold. */
  function isOwnDocument(rec, url) {
    try {
      const parsed = new URL(url);
      return parsed.origin === backendOrigin && parsed.pathname === desktopWindowPath(rec.app, rec.id);
    } catch {
      return false;
    }
  }

  function setIgnore(rec, ignore) {
    if (rec.lastIgnore === ignore) return;
    rec.lastIgnore = ignore;
    if (ignore) rec.win.setIgnoreMouseEvents(true, { forward: true });
    else rec.win.setIgnoreMouseEvents(false);
  }

  function blank(win) {
    for (const rec of records.values()) {
      if (rec.win !== win) continue;
      rec.ready = false;
      rec.regions = [];
      if (rec.decl.kind === "overlay") setIgnore(rec, true);
      win.hide();
    }
    // A blanked overlay has no regions left, so the poll may have nothing to do.
    syncHitPoll();
  }

  /**
   * Open *decl* for *app* unless it is open. `focus` is true only for a user's
   * own action (a shortcut press, a click in another of the app's windows): a
   * panel the reconcile opens for `openOnEnable` must not take focus from the
   * user's work.
   */
  function openWindow(app, decl, { focus = false } = {}) {
    const key = `${app}/${decl.id}`;
    const existing = records.get(key);
    if (isLive(existing)) return existing;
    if (!backendOrigin || (!pageToken && !cookieAuth)) return null;
    let win;
    try {
      win = new BrowserWindow(windowOptions(app, decl));
    } catch (err) {
      log(`app-window-host: ${key}: window failed — ${err && err.message}`);
      return null;
    }
    const wc = win.webContents;
    const rec = {
      app, id: decl.id, decl, win, wcId: wc.id, focus, ready: false, regions: [], lastIgnore: null, pending: [],
    };
    records.set(key, rec);
    bySender.set(wc.id, rec);
    if (decl.kind === "overlay") {
      // Windows keeps the overlay activatable: setFocusable(false) there also
      // flips skipTaskbar. Elsewhere the overlay never takes focus by default.
      if (platform !== "win32") win.setFocusable(false);
      setIgnore(rec, true);
      // Not in the user's screenshots, recordings or screen shares.
      win.setContentProtection(true);
      win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    } else {
      win.setAlwaysOnTop(true, "floating");
    }
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    const holdDocument = (event, url) => {
      if (!isOwnDocument(rec, url)) event.preventDefault();
    };
    wc.on("will-navigate", holdDocument);
    wc.on("will-redirect", holdDocument);
    wc.on("will-attach-webview", (event) => event.preventDefault());
    wc.on("did-fail-load", (_e, code, _desc, _url, isMainFrame) => {
      latch.handleLoadFailure(win, code, isMainFrame);
    });
    wc.on("did-navigate", (_e, _url, httpResponseCode) => {
      latch.handleNavigation(win, httpResponseCode);
    });
    // A crashed page is closed rather than respawned here; an `openOnEnable`
    // window is reopened by the next tick, so a crash loop runs at tick cadence.
    wc.on("render-process-gone", () => closeRecord(rec));
    win.on("closed", () => {
      bySender.delete(rec.wcId);
      if (records.get(key) === rec) records.delete(key);
      syncHitPoll();
    });
    win.loadURL(pageUrl(app, decl.id));
    log(`app-window-host: ${key}: opened`);
    return rec;
  }

  function closeRecord(rec) {
    if (!rec) return;
    bySender.delete(rec.wcId);
    if (records.get(`${rec.app}/${rec.id}`) === rec) records.delete(`${rec.app}/${rec.id}`);
    if (!rec.win.isDestroyed()) rec.win.destroy();
    syncHitPoll();
  }

  function closeApp(app) {
    for (const rec of [...records.values()]) if (rec.app === app) closeRecord(rec);
  }

  function closeAll() {
    for (const rec of [...records.values()]) closeRecord(rec);
  }

  /** macOS demotes an app showing a window shaped like an overlay to a Dock-less accessory. */
  function keepHostInDock() {
    if (platform !== "darwin" || !electron.app) return;
    try {
      electron.app.setActivationPolicy?.("regular");
      electron.app.dock?.show?.();
    } catch {
      /* already regular */
    }
  }

  function reveal(rec) {
    if (rec.decl.kind === "overlay") {
      if (!rec.win.isVisible()) rec.win.showInactive();
      keepHostInDock();
    } else if (rec.focus) {
      rec.win.show();
    } else {
      rec.win.showInactive();
    }
    const pending = rec.pending.splice(0);
    for (const id of pending) rec.win.webContents.send(CHANNELS.shortcut, id);
  }

  // ── Overlay click-through ──────────────────────────────────────────────────

  function pollHitRegionsOnce() {
    let cursor;
    try {
      cursor = screen.getCursorScreenPoint();
    } catch {
      return;
    }
    for (const rec of records.values()) {
      if (rec.decl.kind !== "overlay" || !isLive(rec)) continue;
      if (!rec.ready || rec.regions.length === 0) {
        setIgnore(rec, true);
        continue;
      }
      const bounds = rec.win.getBounds();
      const zoom = rec.win.webContents.getZoomFactor() || 1;
      const x = (cursor.x - bounds.x) / zoom;
      const y = (cursor.y - bounds.y) / zoom;
      setIgnore(rec, !pointInRegions(rec.regions, x, y));
    }
  }

  function syncHitPoll() {
    const wanted = [...records.values()].some(
      (rec) => rec.decl.kind === "overlay" && rec.ready && rec.regions.length > 0,
    );
    if (wanted && !hitTimer) {
      hitTimer = setInterval(pollHitRegionsOnce, HIT_POLL_MS);
      hitTimer.unref?.();
    } else if (!wanted && hitTimer) {
      clearInterval(hitTimer);
      hitTimer = null;
    }
  }

  function refitOverlays() {
    for (const rec of records.values()) {
      if (rec.decl.kind !== "overlay" || !isLive(rec)) continue;
      const b = rec.win.getBounds();
      let display;
      try {
        display = screen.getDisplayNearestPoint({ x: b.x + b.width / 2, y: b.y + b.height / 2 });
      } catch {
        continue;
      }
      rec.win.setBounds(display.bounds);
    }
  }

  // ── Shortcuts ──────────────────────────────────────────────────────────────

  function deliverShortcut(app, shortcut) {
    const decl = declarations.get(app)?.windows.find((w) => w.id === shortcut.window);
    if (!decl || suspended) return;
    const rec = records.get(`${app}/${decl.id}`) || openWindow(app, decl, { focus: true });
    if (!isLive(rec)) return;
    if (rec.ready) rec.win.webContents.send(CHANNELS.shortcut, shortcut.id);
    else if (rec.pending.length < MAX_PENDING_SHORTCUTS) rec.pending.push(shortcut.id);
  }

  function releaseShortcut(chord) {
    const live = liveShortcuts.get(chord);
    liveShortcuts.delete(chord);
    if (!live) return;
    try {
      globalShortcut.unregister(live.shortcut.accelerator);
    } catch {
      /* already gone */
    }
  }

  function sameBinding(a, b) {
    return a.id === b.id && a.window === b.window && a.accelerator === b.accelerator;
  }

  function syncShortcuts() {
    const desired = new Map();
    if (!suspended) {
      // The app holding a chord keeps it while it still declares that binding,
      // so an app enabled later that declares the same chord never takes it.
      for (const [chord, live] of liveShortcuts) {
        const declared = declarations.get(live.app)?.shortcuts.some((s) => sameBinding(s, live.shortcut));
        if (declared && !reservations.isReserved(live.shortcut.accelerator)) desired.set(chord, live);
      }
      for (const [app, decl] of declarations) {
        for (const shortcut of decl.shortcuts) {
          if (desired.has(shortcut.chord)) continue;
          // A chord a shell owner is configured to hold stays theirs even while
          // they do not hold it (disabled, or mid-rebind).
          if (reservations.isReserved(shortcut.accelerator)) {
            reportTaken(app, shortcut);
            continue;
          }
          desired.set(shortcut.chord, { app, shortcut });
        }
      }
    }
    for (const [chord, live] of [...liveShortcuts]) {
      const want = desired.get(chord);
      if (!want || want.app !== live.app || !sameBinding(want.shortcut, live.shortcut)) releaseShortcut(chord);
    }
    for (const [chord, want] of desired) {
      if (liveShortcuts.has(chord)) continue;
      const { app, shortcut } = want;
      let ok = false;
      try {
        // Never take a chord another owner (this process, another app) already holds.
        ok = !globalShortcut.isRegistered(shortcut.accelerator)
          && globalShortcut.register(shortcut.accelerator, () => deliverShortcut(app, shortcut));
      } catch {
        ok = false;
      }
      if (ok) {
        liveShortcuts.set(chord, want);
        reportedTaken.delete(chord);
      } else {
        reportTaken(app, shortcut);
      }
    }
  }

  function reportTaken(app, shortcut) {
    if (reportedTaken.has(shortcut.chord)) return;
    reportedTaken.add(shortcut.chord);
    log(`app-window-host: ${app}: shortcut ${shortcut.id} unavailable`);
  }

  function releaseAllShortcuts() {
    for (const chord of [...liveShortcuts.keys()]) releaseShortcut(chord);
  }

  // ── Reconcile ──────────────────────────────────────────────────────────────

  function apply(next) {
    for (const rec of [...records.values()]) {
      const decl = next.get(rec.app)?.windows.find((w) => w.id === rec.id);
      if (!decl || JSON.stringify(decl) !== JSON.stringify(rec.decl)) closeRecord(rec);
    }
    declarations = next;
    syncShortcuts();
    for (const [app, decl] of next) {
      for (const win of decl.windows) if (win.openOnEnable) openWindow(app, win);
    }
    latch.rearm(
      [...records.values()].map((rec) => rec.win),
      (win) => {
        const rec = [...records.values()].find((r) => r.win === win);
        if (rec) win.loadURL(pageUrl(rec.app, rec.id));
      },
    );
  }

  async function reconcileOnce() {
    if (suspended || reconciling || !started) return;
    reconciling = true;
    try {
      let auth = await authFor(false);
      if (!auth.value) return;
      let result = await probe(auth);
      if (result.state === "unauthorized") {
        auth = await authFor(true);
        if (!auth.value) return;
        result = await probe(auth);
        if (result.state === "unauthorized") cachedAuth = { value: "", viaCookie: false };
      }
      // Re-checked after the awaits: a suspend that landed mid-probe wins.
      if (suspended || !started || result.state !== "ok") return;
      target(auth);
      apply(desktopDeclarations(result.body, log));
    } finally {
      reconciling = false;
    }
  }

  // ── Page messages ──────────────────────────────────────────────────────────

  /** The record a page message came from, or null for any sender this host did not create. */
  function senderRecord(event) {
    const sender = event && event.sender;
    if (!sender) return null;
    const rec = bySender.get(sender.id);
    if (!isLive(rec)) return null;
    if (event.senderFrame && sender.mainFrame && event.senderFrame !== sender.mainFrame) return null;
    if (!isOwnDocument(rec, sender.getURL())) return null;
    return rec;
  }

  const handlers = {
    [CHANNELS.ready]: (event) => {
      const rec = senderRecord(event);
      if (!rec || rec.ready || latch.isBlanked(rec.win)) return;
      rec.ready = true;
      reveal(rec);
      syncHitPoll();
    },
    [CHANNELS.close]: (event) => {
      closeRecord(senderRecord(event));
    },
    [CHANNELS.open]: (event, windowId) => {
      const rec = senderRecord(event);
      if (!rec || typeof windowId !== "string" || suspended) return;
      // Only a window THIS page's app declares; never another app's.
      const decl = declarations.get(rec.app)?.windows.find((w) => w.id === windowId);
      if (decl) openWindow(rec.app, decl, { focus: true });
    },
    [CHANNELS.hitRegions]: (event, raw) => {
      const rec = senderRecord(event);
      if (!rec || rec.decl.kind !== "overlay") return;
      rec.regions = sanitizeHitRegions(raw) || [];
      if (rec.regions.length === 0) setIgnore(rec, true);
      syncHitPoll();
    },
    [CHANNELS.focusable]: (event, focusable) => {
      const rec = senderRecord(event);
      if (!rec || rec.decl.kind !== "overlay" || typeof focusable !== "boolean") return;
      if (platform !== "win32") rec.win.setFocusable(focusable);
      if (focusable) rec.win.focus();
      else rec.win.blur();
    },
  };

  function onDisplaysChanged() {
    refitOverlays();
  }

  /** Start the loop; the returned promise settles when the first tick has. */
  function start() {
    if (started) return Promise.resolve();
    started = true;
    for (const [channel, handler] of Object.entries(handlers)) ipcMain.on(channel, handler);
    if (typeof screen.on === "function") {
      screen.on("display-removed", onDisplaysChanged);
      screen.on("display-added", onDisplaysChanged);
      screen.on("display-metrics-changed", onDisplaysChanged);
    }
    // Synchronous: a shell owner reserving a chord an app holds gets it back
    // before its own register() runs.
    stopWatchingReservations = reservations.onChange(() => syncShortcuts());
    timer = setInterval(() => void reconcileOnce(), TICK_MS);
    timer.unref?.();
    return reconcileOnce();
  }

  function stop() {
    if (!started) return;
    started = false;
    if (timer) clearInterval(timer);
    timer = null;
    stopWatchingReservations?.();
    stopWatchingReservations = null;
    for (const [channel, handler] of Object.entries(handlers)) {
      ipcMain.removeListener?.(channel, handler);
    }
    if (typeof screen.removeListener === "function") {
      screen.removeListener("display-removed", onDisplaysChanged);
      screen.removeListener("display-added", onDisplaysChanged);
      screen.removeListener("display-metrics-changed", onDisplaysChanged);
    }
    releaseAllShortcuts();
    closeAll();
    declarations = new Map();
    cachedAuth = { value: "", viaCookie: false };
    emptyAuthAt = 0;
  }

  /** Close everything for an update install, keeping the loop and handlers for a resume. */
  function suspend() {
    suspended = true;
    releaseAllShortcuts();
    closeAll();
  }

  function resume() {
    if (!suspended) return Promise.resolve();
    suspended = false;
    return reconcileOnce();
  }

  return {
    start,
    stop,
    suspend,
    resume,
    reconcileOnce,
    // Exported for tests: they drive the poll and inspect state directly.
    _pollHitRegionsOnce: pollHitRegionsOnce,
    _records: records,
    _liveShortcuts: liveShortcuts,
    _handlers: handlers,
  };
}

module.exports = {
  APP_WINDOW_BRIDGE_LEVEL,
  CHANNELS,
  DESKTOP_WINDOW_PREFIX,
  createAppWindowHost,
  desktopWindowPath,
  sanitizeHitRegions,
};
