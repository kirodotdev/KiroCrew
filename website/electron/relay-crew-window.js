"use strict";

const path = require("path");
const {
  createNavigationGuard,
  createRedirectGuard,
  openExternalSafely,
} = require("./external-scheme");
const { createReachabilityWatch } = require("./relay-crew-reachability");

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
 * failed for the cap in relay-crew-reachability.js, or at once when the relay
 * answers the page load itself with a 5xx or the load fails. Its notice is a
 * local page in a second view over the Crew's: showing it unloads the Crew's
 * page, which is what stops that page's retries.
 */

const RELAY_HEADER = "Kiro-Relay-Authorization";
// Added to the Crew session's user agent so the Crew's dashboard can tell it is
// in this window (its offline wording). Not a credential: anyone can send it.
const RELAY_WINDOW_UA_MARKER = "kiroCrewRelayWindow";
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

// shortcut: one window per Crew. The view is built separately so it can move
// into a dashboard pane later.
const openWindows = new Map();

function createTokenHolder() {
  let token = null;
  return {
    current: () => token,
    set: (value) => { token = typeof value === "string" && value ? value : null; },
    clear: () => { token = null; },
  };
}

function relayPartitionName(endpointId) {
  return `relay-crew-${endpointId}`; // no "persist:" prefix: in memory only
}

// The Crew's origin, or null unless `crewUrl` is a bare https origin whose
// host is `<endpointId>.<relay domain>`.
function crewOrigin(endpointId, crewUrl) {
  if (!isEndpointId(endpointId)) return null;
  let url;
  try {
    url = new URL(crewUrl);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password
    || url.pathname !== "/" || url.search || url.hash
    || !url.hostname.startsWith(`${endpointId}.`)) {
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

function createRelayCrewView({ endpointId, crewUrl, getToken }, deps) {
  const origin = crewOrigin(endpointId, crewUrl);
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

function openRelayCrewWindow({ endpointId, crewUrl, title, getToken }, deps) {
  const existing = openWindows.get(endpointId);
  if (existing && !existing.isClosed()) {
    existing.focus();
    return existing;
  }
  const { view, session: ses, origin } = createRelayCrewView(
    { endpointId, crewUrl, getToken },
    deps,
  );
  const log = crewLog(endpointId, deps);
  const win = new deps.BaseWindow({ width: 1200, height: 820, title: title || "Kiro Crew" });
  win.contentView.addChildView(view);
  let noticeView = null;
  const fitView = () => {
    if (win.isDestroyed()) return;
    const { width, height } = win.getContentBounds();
    view.setBounds({ x: 0, y: 0, width, height });
    if (noticeView) noticeView.setBounds({ x: 0, y: 0, width, height });
  };
  fitView();
  win.on("resize", fitView);

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
  const failedAttempt = () => { if (watch.fail()) unreachable("retries ran out"); };
  // The socket's upgrade is a wss: request, which an https: pattern does not match.
  // `origin` passed crewOrigin(), so it always starts with https:.
  const wssOrigin = "wss:" + origin.slice("https:".length);
  const watched = { urls: [`${origin}/*`, `${wssOrigin}/*`] };
  ses.webRequest.onCompleted(watched, (details) => {
    if (details.resourceType === "mainFrame") {
      if (details.statusCode >= 400) log(`main frame ${details.statusCode}`);
      notify(statusListeners, details.statusCode);
      if (details.statusCode >= 500) unreachable(`main frame ${details.statusCode}`);
      return;
    }
    if (!retryPath(origin, details.url)) return;
    if (details.statusCode >= 400) failedAttempt();
    else watch.ok();
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
    try { win.contentView.removeChildView(gone); } catch { /* mid-teardown */ }
    try { gone.webContents.close(); } catch { /* mid-teardown */ }
  };

  // Renewal stops for a window nobody is looking at.
  for (const [event, hidden] of [["minimize", true], ["hide", true], ["restore", false], ["show", false]]) {
    win.on(event, () => { if (!closed) notify(visibilityListeners, hidden); });
  }

  win.on("closed", () => {
    if (closed) return;
    closed = true;
    openWindows.delete(endpointId);
    ses.webRequest.onBeforeSendHeaders(null);
    ses.webRequest.onCompleted(null);
    ses.webRequest.onErrorOccurred(null);
    removeNotice();
    try {
      view.webContents.close();
    } catch {
      /* mid-teardown */
    }
    Promise.resolve()
      .then(() => Promise.all([ses.clearStorageData(), ses.clearCache()]))
      .catch(() => log("could not clear the partition"));
    log("window closed");
    notify(closedListeners);
  });

  const handle = {
    endpointId,
    origin,
    // With a notice up the Crew's page is blank, so this loads the Crew again
    // rather than reloading the blank page.
    reload: () => {
      if (closed) return;
      watch.reset();
      if (!noticeView) { view.webContents.reload(); return; }
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
        win.contentView.addChildView(noticeView);
        fitView();
      }
      Promise.resolve(noticeView.webContents.loadFile(NOTICE_FILE, { query: { ...text } }))
        .catch(() => log("notice failed to load"));
    },
    focus: () => { if (closed) return; if (win.isMinimized()) win.restore(); win.show(); win.focus(); },
    close: () => { if (!closed && !win.isDestroyed()) win.close(); },
    isClosed: () => closed,
    onMainFrameStatus: (cb) => { statusListeners.push(cb); },
    onClosed: (cb) => { closedListeners.push(cb); },
    onVisibilityChange: (cb) => { visibilityListeners.push(cb); },
    onUnreachable: (cb) => { unreachableListeners.push(cb); },
    onNoticeAction: (cb) => { noticeActionListeners.push(cb); },
  };
  openWindows.set(endpointId, handle);
  Promise.resolve(view.webContents.loadURL(`${origin}/`))
    .catch(() => log("first load failed"));
  log("window opened");
  return handle;
}

module.exports = {
  isEndpointId,
  createTokenHolder,
  relayPartitionName,
  installRelayHeaderRule,
  createRelayCrewView,
  openRelayCrewWindow,
};
