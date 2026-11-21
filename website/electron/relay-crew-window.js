"use strict";

const path = require("path");
const {
  createNavigationGuard,
  createRedirectGuard,
  openExternalSafely,
} = require("./external-scheme");
const { createReachabilityWatch } = require("./relay-crew-reachability");
const { normalizeBounds, viewHoldsFocus, computeVisible } = require("./browser-view");

/**
 * A window that shows a remote Crew's dashboard through the Kiro relay.
 *
 * The relay admits a viewer by a short-lived access token in the
 * `Kiro-Relay-Authorization` header. Only main can set request headers, so
 * main holds the token and adds it to every request (HTTP and the WebSocket
 * upgrade) bound for the Crew's own origin, and to nothing else. The page gets
 * no preload, so its JavaScript can never read the token.
 *
 * Each Crew gets its own in-memory partition: its cookies and storage stay
 * away from the dashboard's default session and from other Crews, and the
 * header listener is the only `onBeforeSendHeaders` listener on it (Electron
 * keeps only the last one per session).
 *
 * Not wired in yet: the caller supplies `getToken`, a synchronous
 * `() => string|null` that returns the newest access token.
 *
 * The window also watches the page's two retry loops (the `/api/ws` socket and
 * the `/api/status` probe) and reports the Crew unreachable once they have
 * failed for the cap in relay-crew-reachability.js (sooner when the relay says
 * it has no host connection for the Crew), or at once when the relay
 * answers the page load itself with a 5xx or the load fails. Its notice is a
 * local page in a second view over the Crew's: showing it unloads the Crew's
 * page, which is what stops that page's retries.
 */

const RELAY_HEADER = "Kiro-Relay-Authorization";
// Added to the Crew session's user agent so the Crew's dashboard can tell it is
// in this window (its offline wording). Not a credential: anyone can send it.
const RELAY_WINDOW_UA_MARKER = "kiroCrewRelayWindow";
// Added after it on a pane's page only: the page leaves out its own header.
const RELAY_PANE_UA_MARKER = "kiroCrewRelayPane";
const NOTICE_FILE = path.join(__dirname, "relay-crew-notice.html");
const NOTICE_PRELOAD = path.join(__dirname, "relay-crew-notice-preload.js");
const NOTICE_CHANNEL = "relay-crew-notice:act";
const NOTICE_ACTIONS = new Set(["check", "close"]);
// One in-memory session for every notice, apart from each Crew's own.
const NOTICE_PARTITION = "relay-crew-notice";
// The page's two retry loops, by path on the Crew's origin.
const RETRY_PATHS = new Set(["/api/ws", "/api/status"]);
// A request the page itself cancelled (a forced reconnect, an unload).
const ABORTED = "net::ERR_ABORTED";
// The relay's endpoint ID grammar: one lower-case DNS label of 20 to 63
// letters and digits.
const ENDPOINT_ID_MIN_LENGTH = 20;
const ENDPOINT_ID_MAX_LENGTH = 63;
const ENDPOINT_ID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789";

function isEndpointId(value) {
  if (typeof value !== "string") return false;
  if (value.length < ENDPOINT_ID_MIN_LENGTH || value.length > ENDPOINT_ID_MAX_LENGTH) return false;
  for (const ch of value) {
    if (!ENDPOINT_ID_CHARS.includes(ch)) return false;
  }
  return true;
}

// shortcut: one window per Crew. The view is built separately so the pane
// (openRelayCrewPane) builds the very same one.
const openWindows = new Map();

function createTokenHolder() {
  let token = null;
  return {
    current: () => token,
    set: (value) => { token = typeof value === "string" && value ? value : null; },
    clear: () => { token = null; },
  };
}

// endpointId -> the wipe of its partition still running after a close. The
// next page for that Crew, perhaps as another account, waits for it
// (`partitionCleared`), so it never starts on what the last one stored.
const clearing = new Map();
function partitionCleared(endpointId) {
  return clearing.get(endpointId) || Promise.resolve();
}

function relayPartitionName(endpointId) {
  return `relay-crew-${endpointId}`; // no "persist:" prefix: in memory only
}

// The Crew's origin, or null unless `crewUrl` is a bare https origin on the
// default port whose host is exactly `<endpointId>.<relayDomain>`. No relay
// domain matches no Crew: the token goes to the relay's own names only.
function crewOrigin(endpointId, crewUrl, relayDomain) {
  if (!isEndpointId(endpointId) || typeof relayDomain !== "string" || !relayDomain) return null;
  let url;
  try {
    url = new URL(crewUrl);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port
    || url.pathname !== "/" || url.search || url.hash
    || url.hostname !== `${endpointId}.${relayDomain}`) {
    return null;
  }
  return url.origin;
}

// A request URL's origin, with wss: read as https:, or null.
function requestOrigin(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (url.protocol === "wss:") url.protocol = "https:";
    return url.protocol === "https:" ? url.origin : null;
  } catch {
    return null;
  }
}

// Log lines name the endpoint and the outcome only: never a token or a URL.
function crewLog(endpointId, deps) {
  return (message) => {
    try {
      if (deps.log) deps.log(`relay crew ${endpointId}: ${message}`);
    } catch {
      /* a diagnostic must never be load-bearing */
    }
  };
}

function readToken(getToken) {
  try {
    const token = getToken();
    return typeof token === "string" && token ? token : null;
  } catch {
    return null;
  }
}

function installRelayHeaderRule(ses, origin, getToken) {
  if (typeof origin !== "string" || !origin.startsWith("https://")) {
    throw new Error("relay crew: the header rule needs an https origin");
  }
  // No URL filter: the session holds only this Crew, and every request is
  // checked, so a copy carried across a redirect to another origin is dropped.
  ses.webRequest.onBeforeSendHeaders((details, callback) => {
    const headers = { ...details.requestHeaders };
    // A page-set copy is never forwarded. Authorization is never touched.
    for (const name of Object.keys(headers)) {
      if (name.toLowerCase() === RELAY_HEADER.toLowerCase()) delete headers[name];
    }
    const token = requestOrigin(details.url) === origin ? readToken(getToken) : null;
    if (token) headers[RELAY_HEADER] = `Bearer ${token}`;
    callback({ requestHeaders: headers });
  });
}

function createRelayCrewView({ endpointId, crewUrl, relayDomain, getToken }, deps) {
  const origin = crewOrigin(endpointId, crewUrl, relayDomain);
  if (!origin || typeof getToken !== "function") {
    throw new Error("relay crew: invalid endpoint or crew URL");
  }
  const log = crewLog(endpointId, deps);
  const partition = relayPartitionName(endpointId);
  const ses = deps.session.fromPartition(partition);
  ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  const ua = ses.getUserAgent();
  if (!ua.split(" ").includes(RELAY_WINDOW_UA_MARKER)) ses.setUserAgent(`${ua} ${RELAY_WINDOW_UA_MARKER}`);
  installRelayHeaderRule(ses, origin, getToken);

  const view = new deps.WebContentsView({
    webPreferences: {
      partition,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  });
  const wc = view.webContents;
  const getAppOrigin = () => origin;
  wc.on("will-navigate", createNavigationGuard({ getAppOrigin, log }));
  wc.on("will-redirect", createRedirectGuard({ getAppOrigin, log }));
  // shortcut: same-origin popups are denied too, until a pane or tab model
  // exists to hold them.
  wc.setWindowOpenHandler(({ url }) => {
    let target = null;
    try {
      target = new URL(url);
    } catch {
      /* unparseable: deny */
    }
    if (target && (target.protocol === "https:" || target.protocol === "http:")
      && target.origin !== origin) {
      openExternalSafely(deps.shell.openExternal, target.href, log);
    } else {
      log("denied a window open");
    }
    return { action: "deny" };
  });
  return { view, session: ses, origin };
}

// A request on the Crew's origin as one of the page's retry attempts: its
// path, or null for anything else.
// The relay names why it could not serve a request in this header (relay
// protocol v1, section 7 typed reasons); `crew_not_connected` means it holds no
// host connection for the Crew. Never a Crew's own: the relay drops a Crew's
// x-kiro-relay-* response headers.
const RELAY_ERROR_HEADER = "x-kiro-relay-error";
const CREW_NOT_CONNECTED = "crew_not_connected";

function relayError(responseHeaders) {
  if (!responseHeaders || typeof responseHeaders !== "object") return null;
  for (const [name, value] of Object.entries(responseHeaders)) {
    if (name.toLowerCase() !== RELAY_ERROR_HEADER) continue;
    const first = Array.isArray(value) ? value[0] : value;
    return typeof first === "string" ? first.trim() : null;
  }
  return null;
}

function retryPath(origin, rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (url.protocol === "wss:") url.protocol = "https:";
    return url.origin === origin && RETRY_PATHS.has(url.pathname) ? url.pathname : null;
  } catch {
    return null;
  }
}

// The notice's own view: a local file, sandboxed, that can only report a press
// of its two buttons on its own channel.
function createNoticeView(deps, onAction) {
  const view = new deps.WebContentsView({
    webPreferences: {
      partition: NOTICE_PARTITION,
      preload: NOTICE_PRELOAD,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: false,
    },
  });
  const wc = view.webContents;
  wc.on("will-navigate", (event) => event.preventDefault());
  wc.setWindowOpenHandler(() => ({ action: "deny" }));
  wc.ipc.on(NOTICE_CHANNEL, (_event, action) => { if (NOTICE_ACTIONS.has(action)) onAction(action); });
  return view;
}

// What a Crew's view needs once it has a home, the same for the window and the
// pane: the reachability watch, the local notice over the page, the handle's
// listeners and the teardown. `host` puts a view on screen (`add`, `place`) and
// takes one off (`remove`).
function attachRelayCrew({ endpointId, view, session: ses, origin }, host, deps) {
  const log = crewLog(endpointId, deps);
  let noticeView = null;
  const statusListeners = [];
  const closedListeners = [];
  const visibilityListeners = [];
  const unreachableListeners = [];
  const noticeActionListeners = [];
  const watch = createReachabilityWatch({ now: deps.now });
  let closed = false;
  const notify = (listeners, ...args) => {
    for (const listener of listeners) {
      try {
        listener(...args);
      } catch {
        /* one listener's failure must not stop the others */
      }
    }
  };
  const unreachable = (why) => {
    log(`crew unreachable (${why})`);
    notify(unreachableListeners);
  };
  const failedAttempt = (hostGone = false) => {
    if (watch.fail(hostGone)) unreachable(hostGone ? "host not connected" : "retries ran out");
  };
  // The socket's upgrade is a wss: request, which an https: pattern does not match.
  // `origin` passed crewOrigin(), so it always starts with https:.
  const wssOrigin = "wss:" + origin.slice("https:".length);
  const watched = { urls: [`${origin}/*`, `${wssOrigin}/*`] };
  // Whether the current main-frame load was already reported, so a load seen by
  // both the network hook and `did-navigate`, in either order, raises one notice.
  let mainFrameReported = false;
  const reportMainFrame = (code) => {
    if (mainFrameReported) return;
    mainFrameReported = true;
    unreachable(`main frame ${code}`);
  };
  // The status the page on screen was served with: 0 before the first answer.
  let lastMainFrameStatus = 0;
  ses.webRequest.onCompleted(watched, (details) => {
    if (details.resourceType === "mainFrame") {
      lastMainFrameStatus = details.statusCode;
      if (details.statusCode >= 400) log(`main frame ${details.statusCode}`);
      notify(statusListeners, details.statusCode);
      if (details.statusCode >= 500) reportMainFrame(details.statusCode);
      return;
    }
    if (!retryPath(origin, details.url)) {
      // Any other read the Crew answered (not a cached copy) proves its host is there.
      if (details.statusCode >= 200 && details.statusCode < 300 && !details.fromCache) watch.ok();
      return;
    }
    // The relay accepts a socket upgrade even with no host and then closes it
    // with 4404 (`crew_not_connected`), so an upgrade's 101 proves nothing.
    if (details.statusCode === 101) return;
    if (details.statusCode >= 400) failedAttempt(relayError(details.responseHeaders) === CREW_NOT_CONNECTED);
    else watch.ok();
  });
  // A reload the page's service worker answers never reaches the network hook as a
  // main-frame request, so the relay's 503 would render as its bare JSON body.
  // `did-navigate` reports the status the document was served with either way.
  // Only a new document from the Crew starts a new load: the notice's own
  // about:blank, and a same-document navigation, keep the current report.
  view.webContents.on("did-start-navigation", (_event, url, inPlace, isMainFrame) => {
    if (isMainFrame !== false && !inPlace && String(url || "").startsWith(origin)) mainFrameReported = false;
  });
  view.webContents.on("did-navigate", (_event, url, code) => {
    if (!String(url || "").startsWith(origin)) return;
    if (code > 0) lastMainFrameStatus = code;
    if (!(code >= 500)) return;
    log(`main frame ${code}`);
    reportMainFrame(code);
  });
  ses.webRequest.onErrorOccurred(watched, (details) => {
    if (details.error === ABORTED) return;
    if (details.resourceType === "mainFrame") unreachable("main frame failed");
    else if (retryPath(origin, details.url)) failedAttempt();
  });

  const removeNotice = () => {
    if (!noticeView) return;
    const gone = noticeView;
    noticeView = null;
    try { host.remove(gone); } catch { /* mid-teardown */ }
    try { gone.webContents.close(); } catch { /* mid-teardown */ }
  };

  // Once. `what` names the host in the log line.
  const teardown = (what) => {
    if (closed) return;
    closed = true;
    ses.webRequest.onBeforeSendHeaders(null);
    ses.webRequest.onCompleted(null);
    ses.webRequest.onErrorOccurred(null);
    removeNotice();
    try {
      view.webContents.close();
    } catch {
      /* mid-teardown */
    }
    const cleared = Promise.resolve()
      .then(() => Promise.all([ses.clearStorageData(), ses.clearCache()]))
      .catch(() => log("could not clear the partition"))
      .then(() => { if (clearing.get(endpointId) === cleared) clearing.delete(endpointId); });
    clearing.set(endpointId, cleared);
    log(`${what} closed`);
    notify(closedListeners);
  };

  const handle = {
    endpointId,
    origin,
    // With a notice up the Crew's page is blank, so this loads the Crew again
    // rather than reloading the blank page. `ignoreCache` is Force Reload's.
    reload: ({ ignoreCache = false } = {}) => {
      if (closed) return;
      watch.reset();
      lastMainFrameStatus = 0;
      if (!noticeView) {
        if (ignoreCache) view.webContents.reloadIgnoringCache();
        else view.webContents.reload();
        return;
      }
      removeNotice();
      Promise.resolve(view.webContents.loadURL(`${origin}/`)).catch(() => log("load failed"));
    },
    // `text` is `{ title, body, check, close }` (relay-crew-notice.js); an
    // empty part is hidden. Unloads the Crew's page, so its retries stop.
    showNotice: (text) => {
      if (closed) return;
      watch.reset();
      Promise.resolve(view.webContents.loadURL("about:blank")).catch(() => {});
      if (!noticeView) {
        noticeView = createNoticeView(deps, (action) => notify(noticeActionListeners, action));
        host.add(noticeView);
        host.place(noticeView);
      }
      Promise.resolve(noticeView.webContents.loadFile(NOTICE_FILE, { query: { ...text } }))
        .catch(() => log("notice failed to load"));
    },
    isClosed: () => closed,
    // Whether the page on screen is an error the relay answered rather than the
    // Crew (a 4xx or 5xx document, or a notice), so opening it again reloads.
    showsError: () => noticeView !== null || lastMainFrameStatus >= 400,
    onMainFrameStatus: (cb) => { statusListeners.push(cb); },
    onClosed: (cb) => { closedListeners.push(cb); },
    onVisibilityChange: (cb) => { visibilityListeners.push(cb); },
    onUnreachable: (cb) => { unreachableListeners.push(cb); },
    onNoticeAction: (cb) => { noticeActionListeners.push(cb); },
  };
  return {
    handle,
    log,
    teardown,
    notify,
    // The Crew's views on screen: its page, and the notice while one is up.
    views: () => (noticeView ? [view, noticeView] : [view]),
    setHidden: (hidden) => { if (!closed) notify(visibilityListeners, hidden); },
  };
}

function openRelayCrewWindow({ endpointId, crewUrl, relayDomain, title, getToken }, deps) {
  const existing = openWindows.get(endpointId);
  if (existing && !existing.isClosed()) {
    existing.focus();
    return existing;
  }
  const parts = createRelayCrewView({ endpointId, crewUrl, relayDomain, getToken }, deps);
  const win = new deps.BaseWindow({ width: 1200, height: 820, title: title || "Kiro Crew" });
  win.contentView.addChildView(parts.view);
  const place = (v) => {
    if (win.isDestroyed()) return;
    const { width, height } = win.getContentBounds();
    v.setBounds({ x: 0, y: 0, width, height });
  };
  const crew = attachRelayCrew({ endpointId, ...parts }, {
    add: (v) => win.contentView.addChildView(v),
    remove: (v) => win.contentView.removeChildView(v),
    place,
  }, deps);
  const fitView = () => { for (const v of crew.views()) place(v); };
  fitView();
  win.on("resize", fitView);

  // Renewal stops for a window nobody is looking at.
  for (const [event, hidden] of [["minimize", true], ["hide", true], ["restore", false], ["show", false]]) {
    win.on(event, () => crew.setHidden(hidden));
  }

  win.on("closed", () => {
    if (crew.handle.isClosed()) return;
    openWindows.delete(endpointId);
    crew.teardown("window");
  });

  const isClosed = crew.handle.isClosed;
  const handle = {
    ...crew.handle,
    focus: () => { if (isClosed()) return; if (win.isMinimized()) win.restore(); win.show(); win.focus(); },
    close: () => { if (!isClosed() && !win.isDestroyed()) win.close(); },
  };
  openWindows.set(endpointId, handle);
  Promise.resolve(parts.view.webContents.loadURL(`${parts.origin}/`))
    .catch(() => crew.log("first load failed"));
  crew.log("window opened");
  return handle;
}

// The Crew-switch chord the dashboard binds: Cmd on macOS or Ctrl elsewhere,
// with digit 1 to 6 and no other modifier. The pane holds the keyboard while it
// is in use, so main reads the chord before the page does. Answers the 0-based
// tab index, or -1.
// shortcut: the default chords only; a rebound shortcut is not followed until
// the dashboard hands main its bindings.
const PANE_CHORD_DIGITS = 6;
function paneChordIndex(input, platform) {
  if (!input || typeof input !== "object" || input.type !== "keyDown") return -1;
  const primary = platform === "darwin" ? input.meta && !input.control : input.control && !input.meta;
  if (!primary || input.alt || input.shift) return -1;
  const match = /^Digit([1-9])$/.exec(typeof input.code === "string" ? input.code : "");
  const digit = match ? Number(match[1]) : 0;
  return digit >= 1 && digit <= PANE_CHORD_DIGITS ? digit - 1 : -1;
}

// A relay Crew's page has no preload to say it has drawn, so after its load
// the shell asks it every PANE_ROOT_PROBE_MS: once its root holds text and its
// theme is applied (`data-mode`, which the app sets in an effect after its first
// render; before that the stylesheet's dark fallback shows), the probe answers
// that mode and the pane shows. Any host answers this, an older build's too.
// A page that never does shows PANE_PAINT_BACKSTOP_MS after its load. The same
// bound holds the first show of a page that does not say it has drawn (PANE_DRAWN).
const PANE_PAINT_BACKSTOP_MS = 3000;
const PANE_ROOT_PROBE_MS = 100;
const PANE_ROOT_PROBE = "(() => { const r = document.getElementById(\"root\"); "
  + "if (!r || r.childElementCount === 0 || r.textContent.trim() === \"\") return \"\"; "
  + "return document.documentElement.dataset.mode || \"\"; })()";

// Resolves, with the number of animation frames it took, once the page has drawn
// its own theme while on screen: two frames in a row whose background is settled.
// The background is the body's, or the root's when the body has none. Settled is
// one that agrees with `data-mode` (the app sets the mode first and its palette a
// moment later, so until then a light Crew is still the boot page's dark). A page
// with no mode, or no background at all, is settled at once.
// The compositor gives a page nobody can see about three frames and then one a
// second, so the check has to settle in its first frames. It finishes the page's
// CSS transitions on each one: the palette's fade, which a hidden page holds at
// its start, would otherwise take a dozen frames to turn light. And a background
// that has not changed for PANE_SETTLE_MS settles on a timer, not on a frame: a
// palette whose background and mode disagree, or whose colour is not an `rgb()`
// this reads, comes up that way. The check ends with the shell's own wait
// (PANE_PAINT_BACKSTOP_MS) at the latest, so it never goes on in a page that is
// by then on top: a background that never rests (a keyframe animation) would
// otherwise keep it finishing that page's transitions for good.
const PANE_SETTLE_MS = 600;
const PANE_DRAWN = "new Promise((resolve) => { "
  + "const read = (el) => (el ? getComputedStyle(el).backgroundColor : \"\"); "
  + "const clear = (c) => c === \"\" || /^rgba\\(\\d+, \\d+, \\d+, 0\\)$/.test(c); "
  + "let frames = 0; let good = 0; let last = null; let timer = 0; let over = false; "
  + "const done = () => { over = true; clearTimeout(timer); resolve(frames); }; "
  + "const tick = () => { if (over) return; frames += 1; "
  + "for (const a of document.getAnimations()) { if (a.transitionProperty) { try { a.finish(); } catch { /* not one that ends */ } } } "
  + "let bg = read(document.body); "
  + "if (clear(bg)) bg = read(document.documentElement); "
  + "if (bg !== last) { last = bg; clearTimeout(timer); timer = setTimeout(done, " + PANE_SETTLE_MS + "); } "
  + "const mode = document.documentElement.dataset.mode; "
  + "const m = /^rgba?\\((\\d+), (\\d+), (\\d+)/.exec(bg); "
  + "const light = m ? 0.2126 * m[1] + 0.7152 * m[2] + 0.0722 * m[3] > 127 : null; "
  + "const settled = (mode !== \"dark\" && mode !== \"light\") || clear(bg) || light === (mode === \"light\"); "
  + "good = settled ? good + 1 : 0; "
  + "if (good >= 2) done(); else requestAnimationFrame(tick); }; "
  + "setTimeout(done, " + PANE_PAINT_BACKSTOP_MS + "); "
  + "requestAnimationFrame(tick); })";

// The page's applied mode, read again whenever its theme colour changes: the
// colour is the palette's background, which says nothing reliable about the mode.
const PANE_MODE_READ = "document.documentElement.dataset.mode || \"\"";

// shortcut: one pane per Crew, and none while the Crew is open in a window:
// both would use the Crew's one partition.
const openPanes = new Map();

// The window's Crew view, as a child of the main window instead. `parent` is
// the main BaseWindow and `dashboard` its dashboard view, which gets the
// keyboard back whenever the pane leaves the screen holding it. The caller owns
// the rect, when the pane shows and how many stay warm: a pane starts hidden,
// and only `setInactive(false)` from the dashboard shows it, so an answer that
// lands after the person chose something else never paints over it.
function openRelayCrewPane({ endpointId, crewUrl, relayDomain, getToken, parent, dashboard, bounds, menuReload, platform = process.platform }, deps) {
  const existing = openPanes.get(endpointId);
  if (existing && !existing.isClosed()) return existing;
  const inWindow = openWindows.get(endpointId);
  if (inWindow && !inWindow.isClosed()) throw new Error("relay crew: already open in a window");
  const parts = createRelayCrewView({ endpointId, crewUrl, relayDomain, getToken }, deps);
  const { view } = parts;
  const wc = view.webContents;
  // Both whole tokens: the page keeps its relay window wording and leaves out
  // its own header. Layout only, for this page alone; not a credential.
  wc.setUserAgent(`${parts.session.getUserAgent()} ${RELAY_PANE_UA_MARKER}`);
  let rect = null;
  let shown = false;
  // A local menu or dialog over the pane, and another tab in front of it: either
  // one hides it, as the Preview panel's computeVisible decides.
  let overlay = false;
  let inactive = true;
  let parentAway = false;
  let reportedHidden = false;
  view.setVisible(false);
  parent.contentView.addChildView(view);
  const chordListeners = [];
  const themeListeners = [];
  const loadedListeners = [];
  // The page has drawn the app (or failed to load, or a notice took its place).
  // Until then the pane stays hidden, so the dashboard's connecting cover shows
  // instead of the empty view's black.
  let loaded = false;
  let backstop = null;
  let probe = null;
  // Whether the page has been on screen in this window, and the reveal in
  // progress when it has not (`stage`).
  let drawn = false;
  let staging = null;
  // The notice view (which focuses its own button) reads the chord too, so
  // the keyboard can leave a tab that shows one.
  const readChord = (event, input) => {
    const index = paneChordIndex(input, platform);
    if (index < 0) return;
    event.preventDefault();
    crew.notify(chordListeners, index);
  };
  const crew = attachRelayCrew({ endpointId, ...parts }, {
    // A notice that comes up during the page's first show ends it: the notice shows now.
    add: (v) => { parent.contentView.addChildView(v); markLoaded(); if (staging) sync(); v.setVisible(shown); v.webContents.on("before-input-event", readChord); },
    remove: (v) => parent.contentView.removeChildView(v),
    place: (v) => { if (rect) v.setBounds(rect); },
  }, deps);
  const isClosed = crew.handle.isClosed;
  // The Crew's page or its notice, whichever is up, may hold the keyboard.
  const holdsFocus = () => crew.views().some(viewHoldsFocus);
  const handBack = () => {
    try { if (!dashboard.webContents.isDestroyed()) dashboard.webContents.focus(); } catch { /* mid-teardown */ }
  };
  const setBounds = (next) => {
    const clean = normalizeBounds(next, parent.getContentBounds());
    if (!clean) return;
    rect = clean;
    for (const v of crew.views()) v.setBounds(rect);
  };
  // Renewal parks while the pane is hidden or the main window is. A page drawing
  // under the dashboard is on screen to itself, so it counts as showing.
  const reportHidden = () => {
    const hidden = !(shown || staging) || parentAway;
    if (hidden !== reportedHidden) { reportedHidden = hidden; crew.setHidden(hidden); }
  };
  // A page that has never been on screen has no frame to show: made visible, its
  // area is the colour the page last reported as its background (often still the
  // boot page's dark one: a hidden page does not report a change reliably) until
  // its first frame arrives.
  // So the first time the pane should show, it goes on screen under the dashboard
  // view, where nobody sees it, and comes to the top once its page has drawn.
  const stage = () => {
    if (staging) return;
    const mine = { timer: null };
    staging = mine;
    const raise = () => {
      if (staging !== mine || isClosed()) return;
      sync(true);
    };
    parent.contentView.addChildView(view, 0);
    view.setVisible(true);
    mine.timer = setTimeout(raise, PANE_PAINT_BACKSTOP_MS);
    Promise.resolve(wc.executeJavaScript(PANE_DRAWN)).then(raise, raise);
  };
  // The page goes back on top, under its notice when one is up; `sync` then
  // shows or hides it.
  const endStage = () => {
    if (!staging) return;
    clearTimeout(staging.timer);
    staging = null;
    try { for (const v of crew.views()) parent.contentView.addChildView(v); } catch { /* mid-teardown */ }
  };
  // As for the Preview panel (browser-view.js): decide the hand-back before
  // the hide, and make it after.
  // `staged`: the reveal `stage` started is over, so the pane shows now.
  const sync = (staged = false) => {
    const was = shown;
    const want = loaded && computeVisible({ open: true, overlayActive: overlay, inactive, bounds: rect });
    // A notice up is a local page over the Crew's: nothing to wait for.
    if (want && !drawn && !staged && crew.views().length === 1) { stage(); reportHidden(); return; }
    endStage();
    shown = want;
    // The dashboard's cover stays until the pane is on top: a pane loaded behind
    // another tab says `loaded` when it first shows, not when its page loaded.
    if (shown && !drawn) { drawn = true; crew.notify(loadedListeners); }
    const reclaim = !shown && holdsFocus();
    for (const v of crew.views()) v.setVisible(shown);
    if (reclaim) handBack();
    // A pane that comes to the front takes the keyboard, as its window did, so
    // typing cannot reach the local dashboard underneath; a notice up takes it.
    if (shown && !was && parent.isFocused()) crew.views().at(-1).webContents.focus();
    reportHidden();
  };
  const parentEvents = [["minimize", true], ["hide", true], ["restore", false], ["show", false]]
    .map(([event, away]) => [event, () => { parentAway = away; sync(); }]);
  for (const [event, fn] of parentEvents) parent.on(event, fn);
  // A new document in the dashboard (a reload, or its renderer gone) starts with
  // no relay tabs, so nothing could steer or close this pane: it goes too.
  const dashboardEvents = ["did-navigate", "render-process-gone"];
  const close = () => {
    if (isClosed()) return;
    const reclaim = holdsFocus();
    for (const [event, fn] of parentEvents) parent.removeListener(event, fn);
    parent.removeListener("closed", close);
    for (const event of dashboardEvents) dashboard.webContents.removeListener(event, close);
    openPanes.delete(endpointId);
    for (const timer of [backstop, probe, staging && staging.timer]) if (timer) clearTimeout(timer);
    backstop = probe = staging = null;
    try { parent.contentView.removeChildView(view); } catch { /* mid-teardown */ }
    crew.teardown("pane");
    if (reclaim) handBack();
  };
  parent.on("closed", close);
  for (const event of dashboardEvents) dashboard.webContents.on(event, close);
  // Focus the page takes while off screen goes back, a tick later, and only
  // while the main window is the active one.
  wc.on("focus", () => setImmediate(() => {
    if (!isClosed() && !shown && parent.isFocused()) handBack();
  }));
  wc.on("before-input-event", readChord);
  // The page's theme, so the local top bar above it can match (the page has no
  // preload to say so itself).
  const markLoaded = () => {
    if (backstop) { clearTimeout(backstop); backstop = null; }
    if (probe) { clearTimeout(probe); probe = null; }
    if (loaded || isClosed()) return;
    loaded = true;
    // Only a pane the dashboard already asked to show has anything to change.
    if (!inactive) sync();
  };
  // A page could answer early, and all that does is show its own pane sooner;
  // only "dark" or "light" leaves main, as the theme it paints.
  const probeRoot = () => {
    probe = null;
    if (loaded || isClosed()) return;
    Promise.resolve(wc.executeJavaScript(PANE_ROOT_PROBE)).then((mode) => {
      if (loaded || isClosed()) return;
      if (mode === "dark" || mode === "light") {
        crew.notify(themeListeners, mode === "dark");
        markLoaded();
      } else probe = setTimeout(probeRoot, PANE_ROOT_PROBE_MS);
    }, () => {});
  };
  wc.once("did-finish-load", () => {
    if (loaded) return;
    backstop = setTimeout(markLoaded, PANE_PAINT_BACKSTOP_MS);
    probeRoot();
  });
  wc.once("did-fail-load", markLoaded);
  // Before the page has drawn, its theme colour is the boot page's dark one.
  // Only the latest read counts, so an earlier answer arriving late cannot win.
  let modeRead = 0;
  wc.on("did-change-theme-color", () => {
    if (!loaded) return;
    const read = ++modeRead;
    Promise.resolve(wc.executeJavaScript(PANE_MODE_READ)).then((mode) => {
      if (read !== modeRead || isClosed()) return;
      if (mode === "dark" || mode === "light") crew.notify(themeListeners, mode === "dark");
    }, () => {});
  });
  setBounds(bounds);

  const handle = {
    ...crew.handle,
    // The dashboard decides which tab is in front, and a window comes forward
    // only in the click that asked for it, so this raises and shows nothing.
    focus: () => {},
    parent,
    // The tab in front, also while a local menu or dialog hides it for a moment.
    isFront: () => !isClosed() && !inactive,
    // The app menu's Reload and Force Reload while this pane is on screen: the
    // opener's, which mints first when renewal stopped. Plain reload otherwise.
    menuReload: (opts) => {
      if (isClosed()) return;
      if (typeof menuReload === "function") menuReload(opts);
      else crew.handle.reload(opts);
    },
    close,
    setBounds: (next) => { if (!isClosed()) setBounds(next); },
    setOverlay: (active) => { if (isClosed()) return; overlay = active === true; sync(); },
    setInactive: (value) => { if (isClosed()) return; inactive = value === true; sync(); },
    onChord: (cb) => { chordListeners.push(cb); },
    onThemeDark: (cb) => { themeListeners.push(cb); },
    onLoaded: (cb) => { loadedListeners.push(cb); },
  };
  openPanes.set(endpointId, handle);
  Promise.resolve(wc.loadURL(`${parts.origin}/`)).catch(() => crew.log("first load failed"));
  crew.log("pane opened");
  return handle;
}

// The relay tab in front in `win`, or null: the app menu's Reload target.
function shownRelayPane(win) {
  if (!win) return null;
  for (const pane of openPanes.values()) {
    if (pane.parent === win && pane.isFront()) return pane;
  }
  return null;
}

module.exports = {
  isEndpointId,
  partitionCleared,
  shownRelayPane,
  createTokenHolder,
  relayPartitionName,
  installRelayHeaderRule,
  createRelayCrewView,
  openRelayCrewWindow,
  paneChordIndex,
  PANE_ROOT_PROBE,
  PANE_DRAWN,
  openRelayCrewPane,
};
