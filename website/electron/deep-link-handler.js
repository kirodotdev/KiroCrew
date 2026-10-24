// kirocrew:// deep-link handler: the Electron-side half of deep-link.js.
//
// Owns the things the pure resolver cannot: registering the scheme with the
// OS, listening for the ways a URL arrives (`open-url` on macOS, argv on
// Windows/Linux), and HOLDING a session link until the dashboard document can
// take it. All Electron access goes through injected callbacks so the
// sequencing is testable without a live app (the same shape as hide-to-tray.js
// and global-hotkey.js).
//
// "Can take it" is reported by the dashboard itself. `did-finish-load` is not
// enough: the SPA's entry holds its first render for the language catalog and,
// on a fresh profile, for a UI-prefs fetch (which may reload the page), so the
// `navigate` listener can mount well after the load event and a route sent on
// it reaches nobody. The preload's `onNavigate` sends `navigate-ready` once it
// has registered that listener; a held link is delivered only after that ack,
// and a new main-frame document re-arms the wait.
"use strict";

const { SCHEME, deepLinkFromArgv, resolveDeepLink } = require("./deep-link");

/**
 * @param {object} deps
 * @param {(route: string) => boolean} deps.deliver raise the dashboard window
 *   and, for a non-empty route, send it there. An empty route only raises.
 *   False when there is no window that can take it (the route is kept).
 * @param {(msg: string) => void} [deps.log]
 * @returns {{ handleUrl: (raw: string) => boolean, handleArgv: (argv: ReadonlyArray<string>) => boolean,
 *   flushPending: () => boolean, navigateReady: () => boolean, navigationStarted: () => void,
 *   pending: () => (string | null) }}
 */
function createDeepLinkHandler({ deliver, log = () => {} }) {
  if (typeof deliver !== "function") throw new TypeError("deliver is required");
  // At most ONE link is held: a second arrival replaces the first, because the
  // user's latest click is the one they are waiting on.
  let pendingRoute = null;
  // Whether the CURRENT dashboard document has mounted its `navigate` listener.
  let ready = false;

  function apply(route) {
    if (route === "") {
      // A bare link only raises the window, which needs no listener; it also
      // supersedes any session link still waiting.
      pendingRoute = null;
      return deliver("");
    }
    if (ready && deliver(route)) {
      pendingRoute = null;
      return true;
    }
    pendingRoute = route;
    log(`deep link: dashboard not ready, holding route ${JSON.stringify(route)}`);
    return false;
  }

  /** Handle one URL from the OS. Returns true when it was applied now. */
  function handleUrl(raw) {
    const resolved = resolveDeepLink(raw);
    if (!resolved.ok) {
      // The URL came from another program; name the refusal, never the payload
      // verbatim (it is attacker-chosen text), and do nothing else.
      log(`deep link refused (${resolved.reason}); length=${String(raw || "").length}`);
      return false;
    }
    return apply(resolved.route);
  }

  /** Handle a process argv (launch or second-instance). True when a link was applied now. */
  function handleArgv(argv) {
    const raw = deepLinkFromArgv(argv);
    if (!raw) return false;
    return handleUrl(raw);
  }

  /** Deliver a held link if the current document has acked; otherwise keep it. */
  function flushPending() {
    if (pendingRoute === null || !ready) return false;
    return apply(pendingRoute);
  }

  /** The dashboard document has mounted its `navigate` listener. */
  function navigateReady() {
    ready = true;
    return flushPending();
  }

  /** The dashboard's main frame is loading a new document; wait for its ack. */
  function navigationStarted() {
    ready = false;
  }

  return { handleUrl, handleArgv, flushPending, navigateReady, navigationStarted, pending: () => pendingRoute };
}

/**
 * Wire the handler into an Electron `app`.
 *
 * - `setAsDefaultProtocolClient` is idempotent and only re-asserts a
 *   registration the installer already made; a failure (the OS refused, for
 *   example because another application holds the scheme) is logged and
 *   degraded, never thrown, because a scheme that cannot be claimed is a missing
 *   convenience, not a startup failure.
 * - A dev run (`process.defaultApp`: the stock electron binary launched with an
 *   app path) does not register at all. With no path argument the call registers
 *   `process.execPath`, which there is the bare electron binary, so on Windows it
 *   would overwrite the installer's per-user handler with one that starts
 *   Electron without this app.
 * - `open-url` MUST be registered before `app.whenReady()` resolves on macOS:
 *   a URL that launched the app is delivered during startup and is dropped if
 *   no listener is attached yet. The handler's pending buffer is what makes an
 *   early arrival survive until the dashboard can take it.
 * - `second-instance` (the Windows/Linux path for a later launch: the OS starts
 *   a second copy with the URL in argv and Electron hands that argv to the
 *   running primary) is deliberately NOT attached here. main.js owns that
 *   registration -- its ordering relative to the GPU policy is pinned by tests --
 *   and calls `handler.handleArgv(argv)` from inside it, falling back to its
 *   existing show-the-window behaviour when the argv carries no link.
 *
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {ReturnType<typeof createDeepLinkHandler>} deps.handler
 * @param {(msg: string) => void} [deps.log]
 * @param {string} [deps.platform]
 * @param {boolean} [deps.defaultApp] whether this is a dev run of the stock
 *   electron binary; defaults to `process.defaultApp`
 */
function installDeepLinks({
  app,
  handler,
  log = () => {},
  platform = process.platform,
  defaultApp = Boolean(process.defaultApp),
}) {
  let registered = false;
  if (defaultApp) {
    log(`deep link: dev run (process.defaultApp), not registering ${SCHEME}:// so the installed handler is kept`);
  } else {
    try {
      registered = app.setAsDefaultProtocolClient(SCHEME);
    } catch (err) {
      log(`deep link: setAsDefaultProtocolClient threw: ${err && err.message}`);
    }
    if (!registered) {
      log(`deep link: could not register ${SCHEME}:// (the OS refused; another application may hold it)`);
    }
  }

  if (platform === "darwin") {
    app.on("open-url", (event, url) => {
      event.preventDefault();
      handler.handleUrl(url);
    });
  }

  return { registered };
}

/**
 * Feed the dashboard's readiness into the handler.
 *
 * - `navigate-ready` (sent by preload.js once `onNavigate` has registered its
 *   listener) counts only from the dashboard view itself; any other renderer
 *   sending it is ignored.
 * - A main-frame, cross-document `did-start-navigation` on that view means the
 *   document that acked is going away, so the handler waits for the next ack.
 *   Same-document navigations (the SPA's own route changes) keep the listener.
 *
 * Identity is checked when each event fires, not when the webContents is
 * created: the dashboard view exists before the window that owns it is
 * recorded as the main window.
 *
 * @param {object} deps
 * @param {import('electron').App} deps.app
 * @param {import('electron').IpcMain} deps.ipcMain
 * @param {ReturnType<typeof createDeepLinkHandler>} deps.handler
 * @param {(contents: object) => boolean} deps.isDashboardContents
 */
function installNavigateReady({ app, ipcMain, handler, isDashboardContents }) {
  ipcMain.on("navigate-ready", (event) => {
    if (!event || !isDashboardContents(event.sender)) return;
    handler.navigateReady();
  });
  app.on("web-contents-created", (_event, contents) => {
    contents.on("did-start-navigation", (_navEvent, _url, isInPlace, isMainFrame) => {
      if (isMainFrame !== true || isInPlace) return;
      if (!isDashboardContents(contents)) return;
      handler.navigationStarted();
    });
  });
}

module.exports = { createDeepLinkHandler, installDeepLinks, installNavigateReady };
