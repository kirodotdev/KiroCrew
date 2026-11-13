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
 * Three window kinds. An `overlay` covers a display and clicks through outside the
 * regions its page reports; one declared `displays: "all"` gets a copy on every
 * display with the app's avatar on exactly one, handed between them by drag
 * (app-window-span.js). A `panel` is a small always-on-top window, placed beside a
 * box when another of the app's windows opens it with an anchor
 * (app-window-placement.js). A `background` window is never shown: it owns work
 * that must outlive any one display, and it is reopened at once if its page dies.
 * The app's windows talk to each other only through this host (`send`,
 * `broadcast`), and each hears when another of them opens or closes.
 *
 * Capabilities. Beyond its own windows, a page may use what its app declares in
 * `permissions.desktopCapabilities` AND the owner approved (the row's
 * `desktopCapabilitiesGrant.approved`), nothing else: `screenCapture` registers the
 * window as a capture surface (capture-trust.js), so `getDisplayMedia` is granted
 * there and refused everywhere else; `localFiles` serves reveal-a-file and
 * open-an-image (app-local-files.js); `gatewayChoice` lists the operator's crews
 * and loads the app's windows from the one it picks (app-window-gateways.js). A
 * window whose app's approved set changes is closed, so a withdrawn capability
 * leaves no window that was opened with it.
 *
 * Fail closed on the page: a window is revealed only when its page says `ready`,
 * and a main-frame load that ends on an error document or a transport failure is
 * latched hidden (app-window-error-latch.js) until a later tick whose probe the
 * gateway answered reloads it. A probe that cannot be answered leaves every
 * window and shortcut exactly as it is, so a gateway restart does not flicker.
 */

const { createAppWindowErrorLatch } = require("./app-window-error-latch");
const { openLocalImage, revealLocalFile } = require("./app-local-files");
const {
  SELF_GATEWAY,
  createGatewayChoices,
  createGatewayResolver,
  memoryStore,
} = require("./app-window-gateways");
const { desktopDeclarations, isAppListBody } = require("./app-window-host-rules");
const { WORK_AREA_MARGIN, placeBesideAnchor } = require("./app-window-placement");
const { SPAN_CHANNELS, createDisplayMemory, createSpans, spanKey } = require("./app-window-span");
const { shellShortcutReservations } = require("./shell-shortcut-reservations");

/** Document-path prefix of a hosted window. Keep in sync with website/src/desktopWindow/. */
const DESKTOP_WINDOW_PREFIX = "/popout/desktop";
/** The bridge level the preload advertises; raised when the bridge gains a call. */
const APP_WINDOW_BRIDGE_LEVEL = 2;
const TICK_MS = 5_000;
const HIT_POLL_MS = 16;
const PROBE_TIMEOUT_MS = 5_000;
const EMPTY_AUTH_RETRY_MS = 30_000;
const MAX_HIT_REGIONS = 16;
const MAX_REGION_EXTENT = 100_000;
const MAX_PENDING_SHORTCUTS = 8;
/** Messages held for a window of the app that is open but not ready yet. */
const MAX_PENDING_MESSAGES = 32;
/** The longest message between an app's windows, as JSON text. */
const MAX_MESSAGE_CHARS = 64 * 1024;
const MAX_SLOT_KEY_LENGTH = 512;
const MAX_EXTERNAL_URL_LENGTH = 2048;
/** A background window whose page died is reopened at once, at most this often. */
const BACKGROUND_RESPAWN_MS = TICK_MS;
/** The size of the window a background page runs in; it is never shown. */
const BACKGROUND_SIZE = 16;
/** How long after a click or key press the shell saw in a window it counts as acted in. */
const USER_ACTION_MS = 2_000;
/** The native input types that start a press: its release never counts again. */
const USER_INPUT_TYPES = new Set(["mouseDown", "rawKeyDown"]);
/** How long a resolved remote target is reused before core is asked again. */
const TARGET_REUSE_MS = 60_000;
const MAX_PATH_LEN = 4096;

const CHANNELS = Object.freeze({
  ready: "app-window:ready",
  close: "app-window:close",
  open: "app-window:open",
  hitRegions: "app-window:set-hit-regions",
  focusable: "app-window:set-focusable",
  shortcut: "app-window:shortcut",
  closeAll: "app-window:close-all",
  send: "app-window:send",
  broadcast: "app-window:broadcast",
  message: "app-window:message",
  windowChange: "app-window:window-change",
  placed: "app-window:placed",
  beginDrag: "app-window:begin-drag",
  endDrag: "app-window:end-drag",
  active: SPAN_CHANNELS.active,
  drag: SPAN_CHANNELS.drag,
  showSession: "app-window:show-session",
  openExternal: "app-window:open-external",
  revealFile: "app-window:reveal-file",
  openImage: "app-window:open-image",
  listGateways: "app-window:list-gateways",
  chooseGateway: "app-window:choose-gateway",
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

/** A message between an app's windows, as a fresh JSON copy, or undefined when refused. */
function messagePayload(raw) {
  let text;
  try {
    text = JSON.stringify(raw === undefined ? null : raw);
  } catch {
    return undefined;
  }
  if (typeof text !== "string" || text.length > MAX_MESSAGE_CHARS) return undefined;
  return JSON.parse(text);
}

/** True for a spanning overlay: one copy per display, the avatar on one. */
function isSpanning(decl) {
  return decl.kind === "overlay" && decl.displays === "all";
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
 *   displayMemoryPath?: string,
 *   showDashboardSession?: (slotKey: string) => boolean,
 *   openExternal?: (url: string) => void,
 *   timers?: {setInterval?: Function, clearInterval?: Function, setTimeout?: Function, clearTimeout?: Function},
 *   registerCaptureSurface?: (wc: object, url: string) => boolean,
 *   localFiles?: {fs: object, path: object, shell: object, openPathHardened: Function},
 *   gatewayStore?: {get: (key: string) => unknown, set: (key: string, value: unknown) => void},
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
  const timers = deps.timers || {};
  const registerCaptureSurface = deps.registerCaptureSurface
    || ((wc, url) => require("./capture-trust").registerCaptureSurface(wc, url));
  const gatewayChoices = createGatewayChoices(deps.gatewayStore || memoryStore());
  const gateways = createGatewayResolver({ fetchHost: (p, init) => fetchHost(p, init), fetchImpl, now });
  let stopWatchingReservations = null;
  const backendOrigin = (() => {
    try {
      return new URL(backendUrl).origin;
    } catch {
      return "";
    }
  })();

  /** app -> {instanceId, baseUrl, token, choice, at}: what its stored choice resolved to; absent = self. */
  const targets = new Map();
  /** Remote page loads waiting on their crew's port being confirmed. */
  const pendingLoads = new Set();
  /**
   * record key -> {key, app, id, decl, win, ready, regions, lastIgnore, lastInput, pending,
   * inbox, span, displayId, placement, closing, respawn, caps, target, origin}. The key is
   * "<app>/<id>", or "<app>/<id>@<display>" for one copy of a spanning overlay.
   */
  const records = new Map();
  /** webContents id -> record: how every page message is resolved to its window. */
  const bySender = new Map();
  /** canonical chord -> {app, shortcut} for what THIS host registered. */
  const liveShortcuts = new Map();
  /** chords already reported as unavailable, so a taken chord logs once. */
  const reportedTaken = new Set();
  /** "<app>/<id>" of every window its app's other windows were told is open. */
  const announced = new Set();
  /** "<app>/<id>" -> when its background page was last reopened after dying. */
  const respawnedAt = new Map();
  /** "<app>/<id>" of background pages that died again too soon, reopened by the next tick. */
  const respawnDue = new Set();
  let declarations = new Map();
  let pageToken = "";
  let cookieAuth = false;
  let cachedAuth = { value: "", viaCookie: false };
  let emptyAuthAt = 0;
  let timer = null;
  let hitTimer = null;
  let inflight = null;
  let suspended = false;
  let started = false;
  const latch = createAppWindowErrorLatch({ onBlank: (win) => blank(win) });
  const spans = createSpans({
    screen,
    memory: createDisplayMemory(deps.displayMemoryPath || ""),
    copies: (key) => [...records.values()].filter((r) => r.span === key && isLive(r)),
    createCopy: (app, decl, display) => createWindow(app, decl, {
      bounds: display.bounds, displayId: display.id, focus: false,
    }),
    destroyCopy: (rec) => closeRecord(rec),
    send: (rec, channel, ...args) => sendTo(rec, channel, ...args),
    setIgnore: (rec, ignore) => {
      if (isLive(rec)) setIgnore(rec, ignore);
    },
    setTimer: timers.setInterval,
    clearTimer: timers.clearInterval,
    setTimeoutFn: timers.setTimeout,
    clearTimeoutFn: timers.clearTimeout,
  });

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

  /** The URL and headers that carry *auth* to this shell's gateway, or null. */
  function withAuth(path, auth) {
    let url = `${backendUrl}${path}`;
    const headers = {};
    if (auth.viaCookie) {
      const cookie = deps.cookieHeader ? deps.cookieHeader(backendUrl, auth.value) : "";
      if (!cookie) return null;
      headers.Cookie = cookie;
    } else {
      url += `${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(auth.value)}`;
    }
    return { url, headers };
  }

  /** One request to this shell's gateway with its credential; null on any transport failure. */
  async function fetchHost(path, { method = "GET", timeoutMs = PROBE_TIMEOUT_MS } = {}) {
    if (!backendOrigin || typeof fetchImpl !== "function") return null;
    const auth = await authFor(false);
    const req = auth.value ? withAuth(path, auth) : null;
    if (!req) return null;
    try {
      const res = await fetchImpl(req.url, {
        method,
        headers: req.headers,
        redirect: "manual",
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (res.status === 401 || res.status === 403) {
        // 401 is a stale credential; re-resolved on the next call. 403 is an answer.
        if (res.status === 401) cachedAuth = { value: "", viaCookie: false };
        return { status: res.status, body: null };
      }
      if (res.status !== 200) return { status: res.status, body: null };
      return { status: 200, body: await res.json() };
    } catch {
      return null;
    }
  }

  /** @returns {Promise<{state: "ok"|"unauthorized"|"unknown", body?: unknown}>} */
  async function probe(auth) {
    if (!backendOrigin || typeof fetchImpl !== "function") return { state: "unknown" };
    const req = withAuth("/api/apps", auth);
    if (!req) return { state: "unknown" };
    try {
      const res = await fetchImpl(req.url, {
        headers: req.headers,
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

  /** Where *app*'s windows load from now. */
  function targetOf(app) {
    return targets.get(app) || { instanceId: SELF_GATEWAY, baseUrl: "", token: "" };
  }

  function baseOf(target) {
    return target.baseUrl || backendUrl;
  }

  /**
   * What a window of an app holding *local* (declared and approved on this
   * computer's gateway) is served when it loads from *target*: all of it from
   * self; from a crew only what the app also holds there, so a bundle another
   * gateway serves never gets a capability that gateway did not approve.
   */
  function servedCapabilities(local, target) {
    if (!target.baseUrl) return [...local];
    const there = Array.isArray(target.capabilities) ? target.capabilities : [];
    return local.filter((cap) => there.includes(cap));
  }

  function pageUrl(rec) {
    const url = `${baseOf(rec.target)}${desktopWindowPath(rec.app, rec.id)}`;
    // A remote's first load carries the link token core minted for it; self the shell's own.
    // A refreshed token for the SAME crew is used; never another gateway's.
    const current = targetOf(rec.app);
    const token = !rec.target.baseUrl ? pageToken
      : sameTarget(current, rec.target) ? current.token : rec.target.token;
    return token ? `${url}?token=${encodeURIComponent(token)}` : url;
  }

  function sameTarget(a, b) {
    return !!a && !!b && a.instanceId === b.instanceId && a.baseUrl === b.baseUrl;
  }

  /**
   * Load (or reload) *rec*'s page. A window on a crew loads only once core's
   * registry still shows that crew connected on the port it was resolved to: a
   * disconnect frees the port for the next crew to connect, which must never be
   * sent this crew's credential. Otherwise the window closes; on a definite no its
   * app's target is dropped and re-resolved at once, and a non-answer leaves the
   * target for the next tick to try again. A tick waits for the loads it started.
   */
  function loadPage(rec) {
    if (!rec.target.baseUrl) {
      rec.win.loadURL(pageUrl(rec));
      return;
    }
    const loading = gateways.confirm(rec.target).catch(() => ({ known: false })).then((check) => {
      if (records.get(rec.key) !== rec || !isLive(rec) || rec.closing) return;
      if (check.known && check.ok) {
        rec.win.loadURL(pageUrl(rec));
        return;
      }
      log(`app-window-host: ${rec.key}: its crew is no longer on that port; not loaded`);
      closeRecord(rec);
      if (check.known && sameTarget(targets.get(rec.app), rec.target)) {
        targets.delete(rec.app);
        void reconcileOnce();
      }
    }).finally(() => pendingLoads.delete(loading));
    pendingLoads.add(loading);
  }

  // ── Windows ────────────────────────────────────────────────────────────────

  function isLive(rec) {
    return !!rec && !!rec.win && !rec.win.isDestroyed();
  }

  function declared(app, id) {
    return declarations.get(app)?.windows.find((w) => w.id === id) || null;
  }

  /** Every live record of one declared window: one, or one per display for a span. */
  function windowRecords(app, id) {
    return [...records.values()].filter((r) => r.app === app && r.id === id && isLive(r));
  }

  function sendTo(rec, channel, ...args) {
    if (isLive(rec)) rec.win.webContents.send(channel, ...args);
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

  /**
   * A panel's bounds beside a box in screen coordinates. The panel's gutter is the
   * transparent margin around the card it draws, so the CARD, not the window, is
   * what lines up with the anchor.
   */
  function anchoredPanel(decl, anchor) {
    let display;
    try {
      display = screen.getDisplayNearestPoint({
        x: Math.round(anchor.x + anchor.width / 2),
        y: Math.round(anchor.y + anchor.height / 2),
      });
    } catch {
      display = displayAtCursor();
    }
    const gutter = decl.gutter || 0;
    const work = display.workArea;
    // The window is no bigger than the work area, as an unanchored panel, and the
    // card it draws fits inside the margin the placement keeps from every edge.
    const fit = (declared, available) => Math.max(
      0,
      Math.min(Math.min(declared, available) - gutter * 2, available - WORK_AREA_MARGIN * 2),
    );
    const card = { width: fit(decl.width, work.width), height: fit(decl.height, work.height) };
    const { rect, side } = placeBesideAnchor(anchor, card, work);
    return {
      bounds: { x: rect.x - gutter, y: rect.y - gutter, width: card.width + gutter * 2, height: card.height + gutter * 2 },
      side,
    };
  }

  /** A box in *rec*'s page pixels, in screen coordinates. */
  function toScreen(rec, box) {
    const bounds = rec.win.getBounds();
    const zoom = rec.win.webContents.getZoomFactor() || 1;
    return {
      x: bounds.x + box.x * zoom,
      y: bounds.y + box.y * zoom,
      width: box.width * zoom,
      height: box.height * zoom,
    };
  }

  function windowOptions(app, decl, bounds, caps) {
    const overlay = decl.kind === "overlay";
    const background = decl.kind === "background";
    return {
      ...bounds,
      frame: false,
      transparent: overlay || decl.transparent === true,
      alwaysOnTop: !background,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      // A transparent window draws its own shadow, inside its gutter.
      hasShadow: decl.kind === "panel" && decl.transparent !== true,
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
        // An overlay is never focused and a background page is never shown, so
        // Chromium would throttle either to a stall.
        backgroundThrottling: decl.kind === "panel",
        additionalArguments: [
          `--kc-app-window=${app}/${decl.id}/${decl.kind}/${APP_WINDOW_BRIDGE_LEVEL}/${caps.join(",")}`,
        ],
      },
    };
  }

  /** True for the one document a hosted window may hold. */
  function isOwnDocument(rec, url) {
    try {
      const parsed = new URL(url);
      return parsed.origin === rec.origin && parsed.pathname === desktopWindowPath(rec.app, rec.id);
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
   * user's work. `anchor` (screen coordinates) places a panel beside that box.
   */
  function openWindow(app, decl, { focus = false, anchor = null } = {}) {
    if (!backendOrigin || (!pageToken && !cookieAuth)) return null;
    if (isSpanning(decl)) return spans.open(app, decl);
    const existing = records.get(`${app}/${decl.id}`);
    if (isLive(existing)) {
      if (focus) bringForward(existing, anchor);
      return existing;
    }
    let bounds;
    let placement = null;
    if (decl.kind === "panel" && anchor) {
      const placed = anchoredPanel(decl, anchor);
      bounds = placed.bounds;
      placement = { side: placed.side };
    } else {
      const display = displayAtCursor();
      if (decl.kind === "overlay") bounds = display.bounds;
      else if (decl.kind === "panel") bounds = panelBounds(decl, display.workArea);
      else bounds = { x: display.bounds.x, y: display.bounds.y, width: BACKGROUND_SIZE, height: BACKGROUND_SIZE };
    }
    return createWindow(app, decl, { bounds, focus, placement });
  }

  function createWindow(app, decl, { bounds, focus, displayId = null, placement = null }) {
    const key = displayId === null ? `${app}/${decl.id}` : `${app}/${decl.id}@${displayId}`;
    const target = targetOf(app);
    const caps = servedCapabilities(declarations.get(app)?.capabilities || [], target);
    let origin;
    try {
      origin = new URL(baseOf(target)).origin;
    } catch {
      return null;
    }
    let win;
    try {
      win = new BrowserWindow(windowOptions(app, decl, bounds, caps));
    } catch (err) {
      log(`app-window-host: ${key}: window failed — ${err && err.message}`);
      return null;
    }
    const wc = win.webContents;
    const rec = {
      key,
      app,
      id: decl.id,
      decl,
      win,
      wcId: wc.id,
      focus,
      ready: false,
      regions: [],
      lastIgnore: null,
      lastInput: 0,
      pending: [],
      inbox: [],
      span: displayId === null ? null : spanKey(app, decl.id),
      displayId,
      placement,
      closing: false,
      closedHandled: false,
      respawn: false,
      caps,
      target: {
        instanceId: target.instanceId,
        baseUrl: target.baseUrl,
        token: target.token,
        capabilities: [...(target.capabilities || [])],
      },
      origin,
    };
    // Only a window of an app holding the capability may be granted a screen.
    if (caps.includes("screenCapture")) registerCaptureSurface(wc, baseOf(target));
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
    } else if (decl.kind === "panel") {
      win.setAlwaysOnTop(true, "floating");
      // A panel opened beside a box belongs to it, and the box (an avatar on an
      // overlay) is on every workspace.
      if (placement) win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    } else {
      win.setContentProtection(true);
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
    // A background page owns the app's unattended work, so it is reopened at once.
    wc.on("render-process-gone", () => {
      rec.respawn = decl.kind === "background";
      closeRecord(rec);
    });
    // Native input, which page script cannot synthesize: what "a person is acting
    // in this window" is read from, never the regions or focus a page can set.
    try {
      wc.on("input-event", (_e, input) => {
        if (input && USER_INPUT_TYPES.has(input.type) && !input.isAutoRepeat) rec.lastInput = now();
      });
    } catch {
      /* no native input feed: the window never counts as acted in */
    }
    win.on("closed", () => onClosed(rec));
    loadPage(rec);
    log(`app-window-host: ${key}: opened`);
    return rec;
  }

  function onClosed(rec) {
    // Once: a 'closed' event may follow a destroy() this host already accounted for.
    if (rec.closedHandled) return;
    rec.closedHandled = true;
    bySender.delete(rec.wcId);
    if (records.get(rec.key) === rec) records.delete(rec.key);
    if (rec.span) spans.copyClosed(rec);
    if (windowRecords(rec.app, rec.id).length === 0) {
      if (rec.span) spans.closed(rec.span);
      announceClosed(rec.app, rec.id);
    }
    // A background page that died, or closed itself, comes back while declared.
    if (rec.decl.kind === "background" && (rec.respawn || !rec.closing)) respawnBackground(rec);
    syncHitPoll();
  }

  function respawnBackground(rec) {
    if (suspended || !started) return;
    const decl = declared(rec.app, rec.id);
    if (!decl || JSON.stringify(decl) !== JSON.stringify(rec.decl)) return;
    const key = `${rec.app}/${rec.id}`;
    const last = respawnedAt.get(key);
    // A page that dies again at once is left to the tick, so a crash loop runs
    // at tick cadence rather than as fast as the page can die. The tick reopens
    // it whether or not it is declared `openOnEnable`.
    if (last !== undefined && now() - last < BACKGROUND_RESPAWN_MS) {
      respawnDue.add(key);
      return;
    }
    respawnDue.delete(key);
    respawnedAt.set(key, now());
    log(`app-window-host: ${key}: background page gone, reopening`);
    openWindow(rec.app, decl);
  }

  function closeRecord(rec) {
    if (!rec) return;
    rec.closing = true;
    if (!rec.win.isDestroyed()) rec.win.destroy();
    // `destroy()` does not always deliver 'closed' synchronously.
    if (records.get(rec.key) === rec) onClosed(rec);
  }

  /** Close every copy of one of an app's windows. */
  function closeWindow(app, id) {
    const key = spanKey(app, id);
    // Closed on purpose: a pending reopen of its background page is dropped.
    respawnDue.delete(key);
    // Dropped first, so closing the copies does not re-elect among them.
    spans.closed(key);
    for (const rec of windowRecords(app, id)) closeRecord(rec);
  }

  function closeApp(app) {
    const ids = new Set([...records.values()].filter((r) => r.app === app).map((r) => r.id));
    for (const id of ids) closeWindow(app, id);
    // A background page waiting to be reopened has no window to close.
    for (const key of [...respawnDue]) if (key.startsWith(`${app}/`)) respawnDue.delete(key);
  }

  function closeAll() {
    const apps = new Set([...records.values()].map((r) => r.app));
    for (const app of apps) closeApp(app);
    spans.closeAll();
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

  /** A user asked for a panel that is already open: bring it forward, beside its anchor. */
  function bringForward(rec, anchor) {
    if (rec.decl.kind !== "panel") return;
    if (anchor) {
      const placed = anchoredPanel(rec.decl, anchor);
      rec.win.setBounds(placed.bounds);
      if (!rec.placement) rec.win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
      rec.placement = { side: placed.side };
    }
    rec.focus = true;
    if (!rec.ready) return;
    rec.win.show();
    rec.win.focus();
    keepHostInDock();
    if (anchor) sendTo(rec, CHANNELS.placed, rec.placement);
    announceOpen(rec.app, rec.id, true);
  }

  function reveal(rec) {
    if (rec.decl.kind === "overlay") {
      if (!rec.win.isVisible()) rec.win.showInactive();
      keepHostInDock();
    } else if (rec.decl.kind === "panel") {
      if (rec.focus) {
        rec.win.show();
        rec.win.focus();
      } else {
        rec.win.showInactive();
      }
      keepHostInDock();
      if (rec.placement) sendTo(rec, CHANNELS.placed, rec.placement);
    }
    // Which of the app's other windows are open, for a page that just loaded.
    for (const key of announced) {
      const [app, id] = key.split("/");
      if (app === rec.app && id !== rec.id) sendTo(rec, CHANNELS.windowChange, id, true);
    }
    if (rec.span) spans.copyReady(rec);
    const pending = rec.pending.splice(0);
    for (const id of pending) sendTo(rec, CHANNELS.shortcut, id);
    for (const args of rec.inbox.splice(0)) sendTo(rec, ...args);
    announceOpen(rec.app, rec.id, false);
  }

  // ── Between an app's windows ───────────────────────────────────────────────

  /** Tell the app's other ready windows that *id* opened (again, when `again`). */
  function announceOpen(app, id, again) {
    const key = `${app}/${id}`;
    if (announced.has(key) && !again) return;
    announced.add(key);
    for (const rec of records.values()) {
      if (rec.app === app && rec.id !== id && rec.ready && isLive(rec)) {
        sendTo(rec, CHANNELS.windowChange, id, true);
      }
    }
  }

  function announceClosed(app, id) {
    if (!announced.delete(`${app}/${id}`)) return;
    for (const rec of records.values()) {
      if (rec.app === app && rec.id !== id && rec.ready && isLive(rec)) {
        sendTo(rec, CHANNELS.windowChange, id, false);
      }
    }
  }

  /**
   * Deliver one channel's message to the app's window *id*: to the copy hosting
   * the avatar for a spanning overlay, held in order until the page is ready.
   * A window that is not open receives nothing.
   */
  function deliver(app, id, channel, ...args) {
    const decl = declared(app, id);
    if (!decl) return;
    if (isSpanning(decl)) {
      if (windowRecords(app, id).length > 0) spans.deliver(app, id, channel, ...args);
      return;
    }
    const rec = records.get(`${app}/${id}`);
    if (!isLive(rec)) return;
    if (rec.ready) sendTo(rec, channel, ...args);
    else if (rec.inbox.length < MAX_PENDING_MESSAGES) rec.inbox.push([channel, ...args]);
  }

  // ── Overlay click-through ──────────────────────────────────────────────────

  /**
   * True while a drag owns this spanning copy's pointer state (app-window-span.js).
   * Nothing else writes it then: its regions wait for the poll after the release.
   */
  function dragOwnsPointer(rec) {
    return !!rec.span && spans.isDragging(rec.span);
  }

  function pollHitRegionsOnce() {
    let cursor;
    try {
      cursor = screen.getCursorScreenPoint();
    } catch {
      return;
    }
    for (const rec of records.values()) {
      if (rec.decl.kind !== "overlay" || !isLive(rec)) continue;
      if (dragOwnsPointer(rec)) continue;
      // A copy that does not host the avatar never takes the pointer.
      if ((rec.span && !spans.isActive(rec)) || !rec.ready || rec.regions.length === 0) {
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
      if (rec.decl.kind !== "overlay" || rec.span || !isLive(rec)) continue;
      const b = rec.win.getBounds();
      let display;
      try {
        display = screen.getDisplayNearestPoint({ x: b.x + b.width / 2, y: b.y + b.height / 2 });
      } catch {
        continue;
      }
      rec.win.setBounds(display.bounds);
    }
    if (!suspended) spans.displaysChanged((app, id) => declared(app, id));
  }

  // ── Shortcuts ──────────────────────────────────────────────────────────────

  function deliverShortcut(app, shortcut) {
    const decl = declared(app, shortcut.window);
    if (!decl || suspended) return;
    if (isSpanning(decl)) {
      if (openWindow(app, decl, { focus: true })) spans.deliver(app, decl.id, CHANNELS.shortcut, shortcut.id);
      return;
    }
    const rec = records.get(`${app}/${decl.id}`) || openWindow(app, decl, { focus: true });
    if (!isLive(rec)) return;
    if (rec.ready) sendTo(rec, CHANNELS.shortcut, shortcut.id);
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
        const declaredBinding = declarations.get(live.app)?.shortcuts.some((s) => sameBinding(s, live.shortcut));
        if (declaredBinding && !reservations.isReserved(live.shortcut.accelerator)) desired.set(chord, live);
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
    /** Window keys whose declaration or served capabilities changed: closed. */
    const stale = new Set();
    /** Windows closed only because their app now loads from another gateway: reopened there. */
    const moved = new Map();
    for (const rec of records.values()) {
      const entry = next.get(rec.app);
      const decl = entry?.windows.find((w) => w.id === rec.id);
      const key = `${rec.app}/${rec.id}`;
      const current = targetOf(rec.app);
      const here = sameTarget(current, rec.target);
      // On the same gateway, a re-resolved crew's own approvals count as well as this computer's.
      const served = entry ? servedCapabilities(entry.capabilities, here ? current : rec.target) : [];
      if (!decl || JSON.stringify(decl) !== JSON.stringify(rec.decl)
        || JSON.stringify(served) !== JSON.stringify(rec.caps)) {
        stale.add(key);
      } else if (!here) {
        moved.set(key, { app: rec.app, decl });
      }
    }
    for (const key of stale) moved.delete(key);
    for (const key of [...stale, ...moved.keys()]) {
      const rec = [...records.values()].find((r) => `${r.app}/${r.id}` === key);
      if (rec) closeWindow(rec.app, rec.id);
    }
    declarations = next;
    syncShortcuts();
    for (const [app, decl] of next) {
      for (const win of decl.windows) if (win.openOnEnable) openWindow(app, win);
    }
    for (const key of [...respawnDue]) {
      respawnDue.delete(key);
      const [app, id] = key.split("/");
      const decl = declared(app, id);
      if (decl && decl.kind === "background" && windowRecords(app, id).length === 0) openWindow(app, decl);
    }
    for (const { app, decl } of moved.values()) openWindow(app, decl);
    latch.rearm(
      [...records.values()].map((rec) => rec.win),
      (win) => {
        const rec = [...records.values()].find((r) => r.win === win);
        if (rec) loadPage(rec);
      },
    );
  }

  /**
   * Settle where each app's windows load from before *next* is applied.
   *
   * Self unless the app holds `gatewayChoice` and the operator picked a crew. A
   * resolved remote is reused for TARGET_REUSE_MS, and only while core still shows
   * that crew connected on the same port and while the app declares the same windows
   * here (a window added here is checked against the crew's manifest before it loads
   * from there); re-resolving it re-reads what the app holds there, so a grant
   * withdrawn on the crew closes the windows served it within that time. A non-answer
   * changes nothing: the app keeps the target it already has, this
   * computer's gateway until a crew answers.
   */
  async function resolveTargets(next) {
    for (const app of [...targets.keys()]) if (!next.has(app)) targets.delete(app);
    await Promise.all([...next].map(async ([app, decl]) => {
      const choice = decl.capabilities.includes("gatewayChoice") ? gatewayChoices.get(app) : SELF_GATEWAY;
      if (choice === SELF_GATEWAY) {
        targets.delete(app);
        return;
      }
      const windowIds = decl.windows.map((w) => w.id);
      const windowKey = JSON.stringify(windowIds);
      const held = targets.get(app);
      if (held && held.choice === choice && held.windowKey === windowKey && now() - held.at < TARGET_REUSE_MS) {
        if (!held.baseUrl) return;
        const check = await gateways.confirm(held);
        if (!check.known || check.ok) return;
      }
      const resolved = await gateways.resolve(app, choice, windowIds);
      if (resolved.keep) return;
      // A fall-back to self is reused too, so a crew with the app off is not asked every tick.
      targets.set(app, { ...resolved, choice, windowKey, at: now() });
    }));
  }

  /** One tick; a call while one runs joins it rather than starting a second. */
  function reconcileOnce() {
    if (suspended || !started) return Promise.resolve();
    if (!inflight) inflight = reconcileTick().finally(() => { inflight = null; });
    return inflight;
  }

  /** A tick that STARTS after any in flight, so it reads a choice written just now. */
  async function reconcileAfterCurrent() {
    if (inflight) await inflight.catch(() => {});
    return reconcileOnce();
  }

  async function reconcileTick() {
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
      const next = desktopDeclarations(result.body, log);
      await resolveTargets(next);
      if (suspended || !started) return;
      apply(next);
      await Promise.all([...pendingLoads]);
    } catch (err) {
      log(`app-window-host: reconcile failed: ${err && err.message}`);
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

  /** True within USER_ACTION_MS of a click or key press the shell saw in *rec*'s window. */
  function userActed(rec) {
    return rec.lastInput > 0 && now() - rec.lastInput <= USER_ACTION_MS;
  }

  /**
   * A sender a person is acting in right now: one the shell saw a click or key
   * press in within USER_ACTION_MS. Never a background page, which nobody is
   * looking at. What it may ask for (raising the dashboard, opening a browser)
   * moves the user's focus, so it must follow their action rather than a page's
   * timer, and one press answers one request.
   */
  function actingSender(event) {
    const rec = senderRecord(event);
    if (!rec || rec.decl.kind === "background" || !userActed(rec)) return null;
    rec.lastInput = 0;
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
    [CHANNELS.close]: (event, windowId) => {
      const rec = senderRecord(event);
      if (!rec) return;
      if (windowId === undefined || windowId === null) {
        closeWindow(rec.app, rec.id);
      } else if (typeof windowId === "string" && declared(rec.app, windowId)) {
        // Only a window THIS page's app declares; never another app's.
        closeWindow(rec.app, windowId);
      }
    },
    [CHANNELS.closeAll]: (event) => {
      const rec = senderRecord(event);
      if (rec) closeApp(rec.app);
    },
    [CHANNELS.open]: (event, windowId, options) => {
      const rec = senderRecord(event);
      if (!rec || typeof windowId !== "string" || suspended) return;
      // Only a window THIS page's app declares; never another app's.
      const decl = declared(rec.app, windowId);
      if (!decl) return;
      const box = options && typeof options === "object" ? sanitizeHitRegions([options.anchor])?.[0] : null;
      // A background page has no pixels on screen to anchor to, and opens a
      // window without taking the user's focus.
      const background = rec.decl.kind === "background";
      openWindow(rec.app, decl, { focus: !background, anchor: box && !background ? toScreen(rec, box) : null });
    },
    [CHANNELS.hitRegions]: (event, raw) => {
      const rec = senderRecord(event);
      if (!rec || rec.decl.kind !== "overlay") return;
      rec.regions = sanitizeHitRegions(raw) || [];
      if (rec.regions.length === 0 && !dragOwnsPointer(rec)) setIgnore(rec, true);
      syncHitPoll();
    },
    [CHANNELS.focusable]: (event, focusable) => {
      const rec = senderRecord(event);
      if (!rec || rec.decl.kind !== "overlay" || typeof focusable !== "boolean") return;
      if (platform !== "win32") rec.win.setFocusable(focusable);
      if (focusable) rec.win.focus();
      else rec.win.blur();
    },
    [CHANNELS.send]: (event, windowId, raw) => {
      const rec = senderRecord(event);
      if (!rec || typeof windowId !== "string" || suspended) return;
      const message = messagePayload(raw);
      if (message === undefined) return;
      deliver(rec.app, windowId, CHANNELS.message, rec.id, message);
    },
    [CHANNELS.broadcast]: (event, raw) => {
      const rec = senderRecord(event);
      if (!rec || suspended) return;
      const message = messagePayload(raw);
      if (message === undefined) return;
      for (const other of records.values()) {
        if (other !== rec && other.app === rec.app && other.ready && isLive(other)) {
          sendTo(other, CHANNELS.message, rec.id, message);
        }
      }
    },
    [CHANNELS.beginDrag]: (event, box) => {
      const rec = senderRecord(event);
      // Only right after a press in this copy, so a page's timer cannot make
      // every display's copy take the pointer.
      if (rec && rec.span && userActed(rec) && !suspended) spans.beginDrag(rec, box);
    },
    [CHANNELS.endDrag]: (event) => {
      const rec = senderRecord(event);
      if (rec && rec.span) spans.endDrag(rec);
    },
    [CHANNELS.openExternal]: (event, url) => {
      const rec = actingSender(event);
      if (!rec || typeof url !== "string" || url.length > MAX_EXTERNAL_URL_LENGTH) return;
      let parsed;
      try {
        parsed = new URL(url);
      } catch {
        return;
      }
      // A page is web content, and anything else here would ask the OS to open a
      // local file or launch another program.
      if (parsed.protocol !== "https:") {
        log(`app-window-host: ${rec.app}: refused a non-HTTPS link`);
        return;
      }
      if (deps.openExternal) deps.openExternal(parsed.href);
    },
    [CHANNELS.revealFile]: (event, filePath) => {
      const rec = holding(event, "localFiles");
      if (rec && isPathArg(filePath)) revealLocalFile(localFileDeps(), filePath);
    },
  };

  /** Answered calls (`ipcMain.handle`): the page awaits the result. */
  const invokeHandlers = {
    [CHANNELS.showSession]: (event, slotKey) => {
      const rec = actingSender(event);
      if (!rec || suspended || typeof slotKey !== "string" || slotKey.length > MAX_SLOT_KEY_LENGTH) return false;
      if (!deps.showDashboardSession) return false;
      try {
        return deps.showDashboardSession(slotKey) === true;
      } catch {
        return false;
      }
    },
    [CHANNELS.openImage]: async (event, filePath) => {
      const rec = holding(event, "localFiles");
      if (!rec || !isPathArg(filePath)) return false;
      return openLocalImage(
        { ...localFileDeps(), onRefused: (err) => log(`app-window-host: ${rec.app}: open-image refused: ${err && err.message}`) },
        filePath,
      );
    },
    [CHANNELS.listGateways]: async (event) => {
      const rec = holding(event, "gatewayChoice");
      if (!rec) return null;
      const listed = await gateways.list(rec.app, (declarations.get(rec.app)?.windows || []).map((w) => w.id));
      return { ...listed, chosen: gatewayChoices.get(rec.app), showing: rec.target.instanceId };
    },
    [CHANNELS.chooseGateway]: async (event, id) => {
      const rec = holding(event, "gatewayChoice");
      if (!rec || typeof id !== "string" || suspended) return { ok: false };
      const { app } = rec;
      const chosen = gatewayChoices.set(app, id);
      gateways.forget(app);
      const held = targets.get(app);
      if (held) held.at = 0;
      await reconcileAfterCurrent();
      // Where the windows actually load from now, not merely that the tick ran.
      const showing = targetOf(app).instanceId;
      return { ok: showing === chosen, chosen, showing };
    },
  };

  /**
   * The sender's record when its window is served *capability*: declared and approved
   * on this computer's gateway and, for a window a crew serves, on that crew too.
   */
  function holding(event, capability) {
    const rec = senderRecord(event);
    if (!rec || !rec.caps.includes(capability)) return null;
    const local = declarations.get(rec.app)?.capabilities || [];
    return servedCapabilities(local, rec.target).includes(capability) ? rec : null;
  }

  function isPathArg(value) {
    return typeof value === "string" && value !== "" && value.length <= MAX_PATH_LEN && !value.includes("\0");
  }

  function localFileDeps() {
    if (deps.localFiles) return deps.localFiles;
    return {
      fs: require("fs"),
      path: require("path"),
      shell: electron.shell,
      openPathHardened: require("./open-path").openPathHardened,
    };
  }

  function onDisplaysChanged() {
    refitOverlays();
  }

  /** Start the loop; the returned promise settles when the first tick has. */
  function start() {
    if (started) return Promise.resolve();
    started = true;
    for (const [channel, handler] of Object.entries(handlers)) ipcMain.on(channel, handler);
    if (typeof ipcMain.handle === "function") {
      for (const [channel, handler] of Object.entries(invokeHandlers)) {
        ipcMain.removeHandler?.(channel);
        ipcMain.handle(channel, handler);
      }
    }
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
    for (const channel of Object.keys(invokeHandlers)) ipcMain.removeHandler?.(channel);
    if (typeof screen.removeListener === "function") {
      screen.removeListener("display-removed", onDisplaysChanged);
      screen.removeListener("display-added", onDisplaysChanged);
      screen.removeListener("display-metrics-changed", onDisplaysChanged);
    }
    releaseAllShortcuts();
    closeAll();
    declarations = new Map();
    respawnDue.clear();
    targets.clear();
    cachedAuth = { value: "", viaCookie: false };
    emptyAuthAt = 0;
  }

  /** Close everything for an update install, keeping the loop and handlers for a resume. */
  function suspend() {
    suspended = true;
    respawnDue.clear();
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
    _targets: targets,
    _pendingLoads: pendingLoads,
    _liveShortcuts: liveShortcuts,
    _handlers: handlers,
    _invokeHandlers: invokeHandlers,
  };
}

module.exports = {
  APP_WINDOW_BRIDGE_LEVEL,
  CHANNELS,
  DESKTOP_WINDOW_PREFIX,
  MAX_MESSAGE_CHARS,
  createAppWindowHost,
  desktopWindowPath,
  sanitizeHitRegions,
};
