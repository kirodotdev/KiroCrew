"use strict";

const fs = require("fs");
const path = require("path");

const { createTokenRetryHandler, dashboardRetryPath } = require("./token-retry");
const { createRendererRecovery } = require("./renderer-recovery");
const { createHangRecovery } = require("./hang-recovery");
const { armSplashHistoryClear } = require("./splash-history");
const { hideToTray, cancelPendingTrayHide } = require("./hide-to-tray");
const { attachHtmlFullScreen } = require("./html-fullscreen");
const { createDisplayMediaHandler } = require("./display-media");
const { applyFocusModeChrome } = require("./focus-chrome");
const {
  createPermissionRequestHandler,
  createPermissionCheckHandler,
} = require("./permission-handler");
const { createWindowOpenHandler, openExternalSafely } = require("./external-scheme");
const { resolveThemeSource } = require("./native-theme");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const { clampZoomFactor, stepZoomFactor } = require("./zoom");
const { createBrowserViewManager, isUntrustedContents } = require("./browser-view");
const {
  canAgentControl,
  isLoopbackUrl,
  mayBootstrapView,
  createControlPlane,
  OWNER,
} = require("./browser-control");
const { createBrowserOps } = require("./browser-ops");
const { createAgentCommandChannel } = require("./browser-agent-channel");
const { attachContextMenu } = require("./context-menu");
const { validateRemoteSettings } = require("./validation");
const { getRemoteHostConfig, setRemoteHostConfig } = require("./host-config");
const { DEFAULT_REMOTE_BIN, DEFAULT_REMOTE_PATH } = require("./remote-token");
const { identityFamily } = require("./instance-guard");
const { decideLinuxFrame, applyWindowControl } = require("./linux-frame");
const { buildMenuTemplate } = require("./app-menu");
const { serializeMenuItems, executeMenuItem } = require("./windows-menu-model");
const {
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
  SYMBOL_DARK: WINDOWS_TITLEBAR_SYMBOL_DARK,
  SYMBOL_LIGHT: WINDOWS_TITLEBAR_SYMBOL_LIGHT,
  OVERLAY_BACKGROUND: WINDOWS_TITLEBAR_BACKGROUND,
} = require("./windows-titlebar");
const { attachFrameLoadLogging } = require("./frame-load-log");
const { createMemoryWatchLog } = require("./memory-watch-log");
const { createCageTrace } = require("./cage-trace");
const { profilingEnabled } = require("./perf-metrics");

const BROWSER_PARTITION = "persist:kirocrew-browser";
const HEADER_CSS_PX = 42;
const TRAFFIC_LIGHT_NATIVE_H = 12;
const TRAFFIC_LIGHT_Y_NUDGE = -4;
const FULLSCREEN_SETTLE_MS = [250, 1500];
const DASHBOARD_SETTLE_MS = 1500;
const WINDOW_SAVE_DEBOUNCE_MS = 400;
const WINDOWS_TITLEBAR_MENU_IDS = new Set([
  "file-menu",
  "edit-menu",
  "view-menu",
  "connection-menu",
  "window-menu",
  "help-menu",
]);

/**
 * Own every dashboard window and the security policy of the sessions they use.
 *
 * Electron is injected so node:test can load this module without a live
 * Electron runtime. Runtime collaborators stay deliberately narrow: gateway
 * boot/recovery remains outside and crosses this boundary through the single
 * connectWindow callback.
 */
function createWindowLifecycle(options) {
  const {
    electron,
    store,
    backendUrl,
    port,
    glog = () => {},
    readInternalSecret = () => "",
    fetchLocalToken,
    fetchRemoteToken,
    isQuitting = () => false,
    requestQuit,
    connectWindow,
    platform = process.platform,
    env = process.env,
  } = options || {};

  if (!electron) throw new Error("createWindowLifecycle: electron is required");
  if (!store) throw new Error("createWindowLifecycle: store is required");
  if (!backendUrl) throw new Error("createWindowLifecycle: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createWindowLifecycle: port is required");
  if (typeof fetchLocalToken !== "function") {
    throw new Error("createWindowLifecycle: fetchLocalToken is required");
  }
  if (typeof fetchRemoteToken !== "function") {
    throw new Error("createWindowLifecycle: fetchRemoteToken is required");
  }
  if (typeof requestQuit !== "function") {
    throw new Error("createWindowLifecycle: requestQuit is required");
  }
  if (typeof connectWindow !== "function") {
    throw new Error("createWindowLifecycle: connectWindow is required");
  }

  const {
    app,
    BaseWindow,
    BrowserWindow,
    WebContentsView,
    shell,
    dialog,
    Tray,
    Menu,
    nativeImage,
    nativeTheme,
    webContents,
    session,
    desktopCapturer,
    systemPreferences,
    screen,
    contentTracing,
  } = electron;

  const IS_MAC = platform === "darwin";
  const IS_WINDOWS = platform === "win32";
  const IS_WIN = IS_WINDOWS;
  const IS_LINUX = platform === "linux";
  // Decide once: every window in the process must agree about native versus
  // client-side decorations.
  const LINUX_FRAME_DECISION = IS_LINUX
    ? decideLinuxFrame({ env, override: store.get("linuxFrameless") })
    : null;
  const LINUX_FRAMELESS = !!(LINUX_FRAME_DECISION && LINUX_FRAME_DECISION.frameless);

  let mainWindow = null;
  let tray = null;
  let micDialogOpen = false;
  let sessionSecurityConfigured = false;
  let appMenu = null;

  // The primary window owns both the cheap memory trajectory and the bounded
  // process-wide cage trace. Keeping record, crash flush, and quit stop behind
  // one façade prevents sibling-window samples from being attributed to this
  // renderer and prevents another owner from stopping contentTracing.
  const memoryWatchLog = createMemoryWatchLog();
  const cageTrace = createCageTrace({
    contentTracing,
    // Fixed ordinal slots bound disk use across launches; gateway-launch.log
    // carries the timestamp that correlates a slot with a renderer death.
    tracePath: (slot) => path.join(app.getPath("logs"), `cage-trace-${slot}.json`),
    log: glog,
  });

  async function getDashboardThemeVars() {
    const win = BaseWindow.getFocusedWindow() || mainWindow;
    if (!win || win.isDestroyed()) return null;
    try {
      return await win.webContents.executeJavaScript(`
        (() => {
          const s = getComputedStyle(document.documentElement);
          return {
            bg: s.getPropertyValue('--bg').trim(),
            card: s.getPropertyValue('--card').trim(),
            text: s.getPropertyValue('--text').trim(),
            muted: s.getPropertyValue('--muted').trim(),
            border: s.getPropertyValue('--border').trim(),
            accent: s.getPropertyValue('--accent').trim(),
            accentHover: s.getPropertyValue('--accent-hover').trim(),
            bgAccent: s.getPropertyValue('--bg-accent').trim(),
          };
        })()
      `);
    } catch {
      return null;
    }
  }

  function modalCSSForMode(dark) {
    return `* { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:-apple-system,sans-serif; padding:24px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${dark ? "#e2e8f0" : "#1e293b"}; }
      label { display:block; margin-bottom:8px; font-size:13px; color:${dark ? "#94a3b8" : "#64748b"}; }
      input { width:100%; padding:10px; border-radius:6px; border:1px solid ${dark ? "#475569" : "#cbd5e1"};
        background:${dark ? "#0f172a" : "#ffffff"}; color:${dark ? "#e2e8f0" : "#1e293b"}; font-size:14px; outline:none; margin-bottom:12px; }
      input:focus { border-color:#f97316; }
      .hint { font-size:11px; color:${dark ? "#64748b" : "#94a3b8"}; margin-bottom:12px; }
      .row { display:flex; gap:8px; }
      button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
      .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
      .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; } .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }`;
  }

  function modalCSSFromVars(v) {
    return `* { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:-apple-system,sans-serif; padding:24px; background:${v.bg}; color:${v.text}; }
      label { display:block; margin-bottom:8px; font-size:13px; color:${v.muted}; }
      input { width:100%; padding:10px; border-radius:6px; border:1px solid ${v.border};
        background:${v.card}; color:${v.text}; font-size:14px; outline:none; margin-bottom:12px; }
      input:focus { border-color:${v.accent}; }
      .hint { font-size:11px; color:${v.muted}; margin-bottom:12px; }
      .row { display:flex; gap:8px; }
      button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
      .ok { background:${v.accent}; color:#fff; } .ok:hover { background:${v.accentHover || v.accent}; }
      .cancel { background:${v.bgAccent || v.card}; color:${v.muted}; } .cancel:hover { background:${v.border}; }`;
  }

  async function getModalCSS() {
    const vars = await getDashboardThemeVars();
    if (vars && vars.bg) return modalCSSFromVars(vars);
    return modalCSSForMode(nativeTheme.shouldUseDarkColors);
  }

  // Reads the MODE PREFERENCE, not only the resolved mode. Setting themeSource
  // to dark/light also overrides prefers-color-scheme in renderers; feeding the
  // resolved value back would freeze the dashboard's Auto mode.
  function syncNativeTheme(view, win) {
    if (win.isDestroyed()) return;
    view.webContents.executeJavaScript(
      `JSON.stringify({`
        + `pref: document.documentElement.dataset.modePref || "",`
        + `mode: document.documentElement.dataset.mode || ""`
        + `})`,
    ).then((raw) => {
      let pref = "";
      let mode = "";
      try {
        const parsed = JSON.parse(raw);
        pref = parsed.pref || "";
        mode = parsed.mode || "";
      } catch {
        return;
      }
      nativeTheme.themeSource = resolveThemeSource(pref, mode);
      if (mode === "dark" || mode === "light") updateWindowsTitleBarOverlay(win, mode);
    }).catch(() => {});
  }

  function updateWindowsTitleBarOverlay(win, mode) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode || (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    paintTitleBarOverlay(win, resolvedMode, HEADER_CSS_PX);
  }

  function trafficLightPositionForZoom(zoomFactor) {
    const stripPx = Math.round(HEADER_CSS_PX * zoomFactor);
    return {
      x: Math.round(16 * zoomFactor),
      y: Math.max(
        4,
        Math.round((stripPx - TRAFFIC_LIGHT_NATIVE_H) / 2) + TRAFFIC_LIGHT_Y_NUDGE,
      ),
    };
  }

  function positionTrafficLights(win) {
    if (!IS_MAC || !win || win.isDestroyed()) return;
    try {
      const zoom = win._mcView ? win._mcView.webContents.getZoomFactor() : 1;
      win.setWindowButtonPosition(trafficLightPositionForZoom(zoom));
    } catch {
      // Window is mid-teardown.
    }
  }

  // BaseWindow.fromWebContents does not exist. Match the dashboard view
  // explicitly so every window-scoped IPC action targets its sender's window.
  function windowForWebContents(wc) {
    for (const win of BaseWindow.getAllWindows()) {
      try {
        if (win._mcView && win._mcView.webContents === wc) return win;
      } catch {
        // Window is mid-teardown.
      }
    }
    return null;
  }

  // Is this window's gateway genuinely on THIS machine? A loopback URL is
  // necessary but NOT sufficient: a remote gateway reached over a tunnel also
  // presents as localhost, so additionally require that no remote host is
  // configured for the window's OWN port (each window carries its backendUrl;
  // the factory's port is only the primary window's, so a secondary remote
  // window must not read the primary's config). Shared by the host-presence
  // heartbeat and the wsl:detect sender gate so the two security decisions
  // cannot drift apart.
  function isGatewayLocalForWindow(win) {
    if (!win || win.isDestroyed() || !win._mcBackendUrl) return false;
    const url = win._mcBackendUrl;
    return isLoopbackUrl(url) && !getRemoteHostConfig(store, new URL(url).port)?.host;
  }

  function syncLinuxMaximizeState(win, view) {
    const push = () => {
      if (win.isDestroyed() || view.webContents.isDestroyed()) return;
      const maxed = win.isMaximized();
      view.webContents.executeJavaScript(`
        {
          const wrap = document.getElementById('electron-linux-controls');
          if (wrap) {
            wrap.classList.toggle('is-maximized', ${maxed});
            const b = wrap.querySelector('button.maximize');
            if (b) b.setAttribute('aria-label', ${maxed} ? 'Restore' : 'Maximize');
          }
        }
      `).catch(() => {});
    };
    // did-finish-load re-fires on reload. Window listeners must be armed once.
    if (!win._mcLinuxMaximizeSyncArmed) {
      win._mcLinuxMaximizeSyncArmed = true;
      win.on("maximize", push);
      win.on("unmaximize", push);
    }
    push();
  }

  function hardenBrowserPartition(sessionApi) {
    const browserSession = sessionApi.fromPartition(BROWSER_PARTITION);
    browserSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    browserSession.setPermissionCheckHandler(() => false);
    return browserSession;
  }

  function browserOpsFor(entry) {
    if (entry._ops) return entry._ops;
    entry._ops = createBrowserOps({
      sendCommand: (method, params) => entry.control.send(method, params),
      // The debugger object is stable for one WebContents, so this subscription
      // survives attach/detach and its bounded console buffer persists.
      subscribe: (handler) => {
        const wc = entry.manager.getWebContents();
        const dbg = wc && wc.debugger;
        if (dbg && typeof dbg.on === "function") {
          dbg.on("message", (_event, method, params) => handler(method, params));
        }
      },
    });
    return entry._ops;
  }

  async function dispatchBrowserOp(entry, op, args) {
    return browserOpsFor(entry).run(op, args);
  }

  function setupWindowContents(win, windowBackendUrl) {
    const windowPort = new URL(windowBackendUrl).port;
    let customName = null;

    const view = new WebContentsView({
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        // Frameless Linux is a launch-time decision, not a platform constant.
        // The preload reads this argument to reserve caption-control space.
        additionalArguments: LINUX_FRAMELESS ? ["--kc-linux-frameless"] : [],
      },
    });
    view.setBackgroundColor("#00000000");
    win.contentView.addChildView(view);

    // Arm before any handoff. Boot, reconnect and token-prompt pages share this
    // WebContents; leaving one in history lets mouse Back reach a dead-end shell.
    armSplashHistoryClear(view.webContents, {
      isAlive: () => !win.isDestroyed() && !view.webContents.isDestroyed(),
      log: glog,
    });

    // Teardown order is load-bearing: no command poller or CDP owner may outlive
    // the page it targets. Close the dashboard WebContents only after releasing
    // every embedded panel.
    win.on("closed", () => {
      if (win._mcAgentChannel) void win._mcAgentChannel.stop();
      if (win._mcBrowserPanels) {
        for (const id of [...win._mcBrowserPanels.keys()]) win._mcDestroyBrowserPanel(id);
      }
      view.webContents.close();
    });

    function updateViewBounds() {
      if (win.isDestroyed()) return;
      const { width, height } = win.getContentBounds();
      view.setBounds({ x: 0, y: 0, width, height });
      // Embedded browser panels are partial rectangles in the same content
      // area, so a host-window resize invalidates their clamp too.
      if (win._mcBrowserPanels) {
        for (const entry of win._mcBrowserPanels.values()) entry.manager.refreshBounds();
      }
    }

    updateViewBounds();
    win.on("resize", updateViewBounds);

    const sendFullScreen = () => {
      if (win.isDestroyed() || view.webContents.isDestroyed()) return;
      view.webContents.send("fullscreen-changed", win.isFullScreen());
    };

    // Fullscreen events can precede the window manager's final reflow. Keep the
    // immediate recompute (no flash where reflow is synchronous) and bounded
    // deferred passes, including the late backstop slow Linux WMs need.
    let fullscreenSettleTimers = [];
    const scheduleFullscreenSettle = () => {
      for (const timer of fullscreenSettleTimers) clearTimeout(timer);
      fullscreenSettleTimers = FULLSCREEN_SETTLE_MS.map(
        (ms) => setTimeout(updateViewBounds, ms),
      );
    };
    win.on("closed", () => {
      for (const timer of fullscreenSettleTimers) clearTimeout(timer);
      fullscreenSettleTimers = [];
    });
    win.on("enter-full-screen", () => {
      updateViewBounds();
      sendFullScreen();
      scheduleFullscreenSettle();
    });
    win.on("leave-full-screen", () => {
      updateViewBounds();
      sendFullScreen();
      scheduleFullscreenSettle();
    });

    // DOM fullscreen is separate from native window fullscreen. Bridge it so a
    // video can grow the BaseWindow, and retain provenance so persistence never
    // mistakes a video-raised transition for the user's launch preference.
    win._mcHtmlFullScreen = attachHtmlFullScreen({
      win,
      webContents: view.webContents,
    });

    win.on("show", updateViewBounds);
    win.on("restore", updateViewBounds);
    win.on("move", updateViewBounds);
    view.webContents.on("did-finish-load", () => {
      updateViewBounds();
      sendFullScreen();
      setTimeout(updateViewBounds, DASHBOARD_SETTLE_MS);
    });

    // BaseWindow has no webContents. Preserve the shell's compatibility alias
    // before any caller can load the splash or dashboard into this window.
    win.webContents = view.webContents;

    function applyTitle() {
      const remoteName = getRemoteHostConfig(store, windowPort)?.defaultName;
      if (!IS_WIN) {
        const suffix = customName || remoteName || `[:${windowPort}]`;
        win.setTitle(`Kiro Crew ${suffix}`);
        return;
      }
      // Windows omits the default local port; secondary/remote windows retain a
      // suffix so they remain distinguishable in the taskbar.
      let suffix = customName || remoteName || "";
      if (!suffix && windowPort && String(windowPort) !== "5476") {
        suffix = `[:${windowPort}]`;
      }
      win.setTitle(suffix ? `Kiro Crew ${suffix}` : "Kiro Crew");
    }

    win._mcSetCustomName = (name) => {
      customName = name;
      applyTitle();
    };
    win._mcGetCustomName = () => customName;
    win._mcBackendUrl = windowBackendUrl;
    win._mcView = view;

    // One native browser view/control plane per dashboard panel. The renderer
    // owns layout; this process owns the WebContents and every privilege.
    const browserPanels = new Map();

    function browserPanel(panelId, { create = true } = {}) {
      const id = typeof panelId === "string" ? panelId.trim() : "";
      if (!id) return null;
      const existing = browserPanels.get(id);
      if (existing || !create) return existing || null;

      const entry = { id, agentAct: false };
      entry.manager = createBrowserViewManager({
        createView: () => new WebContentsView({
          webPreferences: {
            // Persistent for ordinary browser logins, but isolated from the
            // dashboard's host-scoped mc_token_<port> cookie jar.
            partition: BROWSER_PARTITION,
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            webviewTag: false,
          },
        }),
        getContentBounds: () => win.getContentBounds(),
        addView: (child) => win.contentView.addChildView(child),
        removeView: (child) => win.contentView.removeChildView(child),
        // Chrome the embedded page needs but the module must not import Electron
        // for: the shared right-click menu (spelling suggestions, cut/copy/paste,
        // Look Up, Copy Link Address). Safe for untrusted content — every item is
        // a plain edit role or a clipboard write, none reaches app state. No
        // origin is passed: an arbitrary site's same-origin pathname that happens
        // to exist on disk is not a local file.
        onCreate: (child) => attachContextMenu(child.webContents),
        onEvent: (name, payload) => {
          if (name === "open-external") {
            if (payload && payload.url) {
              openExternalSafely(
                shell.openExternal,
                payload.url,
                (message) => console.warn(`[browser-panel] ${message}`),
              );
            }
            return;
          }
          if (!view.webContents.isDestroyed()) {
            view.webContents.send(
              `browser:${name}`,
              { ...(payload || {}), panelId: id },
            );
          }
        },
      });

      // Display and CDP ownership are independent. At most one agent owner may
      // hold LIGHT, and all transitions are audited in this process.
      entry.control = createControlPlane({
        getWebContents: () => entry.manager.getWebContents(),
        onAudit: (event, detail) => {
          console.warn(`[browser-control] ${id} ${event} ${JSON.stringify(detail)}`);
        },
      });

      // Browser Mode is the authorization; the native-view existence check is
      // the remaining per-panel precondition. Do not reintroduce a second
      // session consent gate here.
      entry.gate = () => canAgentControl({
        agentActEnabled: true,
        viewOpen: entry.manager.getState().open,
      });

      browserPanels.set(id, entry);
      return entry;
    }

    function destroyBrowserPanel(id) {
      const entry = browserPanels.get(id);
      if (!entry) return;
      browserPanels.delete(id);
      try {
        void entry.control.release();
      } catch {
        // Mid-teardown.
      }
      try {
        entry.manager.close();
      } catch {
        // Mid-teardown.
      }
    }

    win._mcBrowserPanel = browserPanel;
    win._mcBrowserPanels = browserPanels;
    win._mcDestroyBrowserPanel = destroyBrowserPanel;

    // Reachability is distinct from mounted panels: a declared chat slot must
    // be polled so its first navigate can bootstrap the native view.
    const reachableSessions = new Set();
    win._mcReachableSessions = reachableSessions;

    win._mcAgentChannel = createAgentCommandChannel({
      fetchFn: (url, init) => fetch(url, init),
      getGatewayUrl: () => win._mcBackendUrl,
      // Re-read for every call; the secret rotates with each gateway boot.
      getSecret: () => readInternalSecret(),
      // The idle host-presence heartbeat must fire ONLY when the gateway is truly
      // on this machine — see isGatewayLocalForWindow for why loopback alone is
      // not sufficient and why the port must be the window's own.
      isGatewayLocal: () => isGatewayLocalForWindow(win),
      listPanelIds: () => {
        // Preserve the existing predicate exactly. In particular, do not
        // mechanically fold isGatewayLocal into this branch during extraction:
        // that is a policy change, not a module move.
        if (!isLoopbackUrl(win._mcBackendUrl)) return [];
        return [...new Set([...browserPanels.keys(), ...reachableSessions])];
      },
      dispatch: async (sessionKey, op, args) => {
        console.warn(`[browser-cmdbus] dispatch op=${op} session=${sessionKey}`);
        const bootstrapping = op === "navigate";
        const entry = browserPanel(sessionKey, { create: bootstrapping });
        if (!entry) {
          throw new Error(`no native browser panel for session ${sessionKey}`);
        }

        // Navigate is the one op allowed to satisfy an absent-view precondition.
        // The order is essential: opening after acquiring LIGHT would refuse the
        // first command before there was any view to acquire.
        if (bootstrapping && !entry.manager.getWebContents()) {
          const pre = entry.gate();
          if (!mayBootstrapView(pre)) {
            throw new Error(`browser control refused: ${pre.reason}`);
          }
          const opened = entry.manager.navigate(String((args && args.url) || ""));
          if (opened && opened.refused) {
            return {
              ok: false,
              code: "bad_url",
              error: `refused non-web URL: ${args && args.url}`,
            };
          }
          try {
            view.webContents.send("browser:agent-opened", {
              panelId: sessionKey,
              url: (opened && opened.url) || String((args && args.url) || ""),
            });
          } catch {
            // A torn-down dashboard must not fail the navigation itself.
          }
          const takenAfterOpen = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
          if (takenAfterOpen.refused) {
            throw new Error(`browser control refused: ${takenAfterOpen.refused}`);
          }
          return {
            ok: true,
            url: (opened && opened.url) || String((args && args.url) || ""),
          };
        }

        const taken = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
        if (taken.refused) {
          throw new Error(`browser control refused: ${taken.refused}`);
        }
        return dispatchBrowserOp(entry, op, args);
      },
      onError: (error, context) => {
        console.warn(`[browser-agent-channel] ${context}: ${error && error.message}`);
      },
    });
    win._mcAgentChannel.start();

    // The dashboard's own webContents, so the link block also gets the origin --
    // which is what turns a `/abs/path` link into a bare-path copy instead of a
    // useless localhost URL. The embedded browser panel passes no origin: an
    // arbitrary site's same-origin pathname is not a local file.
    attachContextMenu(view.webContents, { getAppOrigin: () => windowBackendUrl });

    if (IS_MAC) {
      positionTrafficLights(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => positionTrafficLights(win), 0);
      });
    }
    if (IS_WINDOWS) {
      updateWindowsTitleBarOverlay(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => updateWindowsTitleBarOverlay(win), 0);
      });
    }

    // Frameless macOS exposes a native system context menu on the drag region.
    win.on("system-context-menu", (event, point) => {
      event.preventDefault();
      Menu.buildFromTemplate([
        { label: "Rename Window…", click: () => renameCurrentWindow() },
        { label: "Set Remote Host…", click: () => promptRemoteHost() },
        { label: "Refresh Token", click: () => refreshToken() },
        { type: "separator" },
        { label: "New Connection Window…", click: () => openNewConnectionWindow() },
      ]).popup({ window: win, x: point.x, y: point.y });
    });

    view.webContents.on("did-finish-load", applyTitle);
    view.webContents.on("page-title-updated", (event) => {
      event.preventDefault();
      applyTitle();
    });

    view.webContents.on("did-finish-load", () => {
      // Every frameless platform needs a drag region. It collapses with the
      // focus-mode header: app-region hit-testing happens before DOM pointer
      // hit-testing, so pointer-events:none alone cannot stop an invisible drag
      // strip from swallowing the transcript's mouse input.
      if (IS_MAC || IS_WIN || LINUX_FRAMELESS) {
        view.webContents.insertCSS(`
          #electron-drag-bar {
            position: fixed;
            top: 0; left: 0; right: ${IS_WIN ? "138px" : LINUX_FRAMELESS ? "108px" : "0"};
            height: 42px;
            -webkit-app-region: drag;
            z-index: 99999;
            pointer-events: none;
          }
          a, button, input, select, textarea,
          [role="button"], [tabindex], iframe {
            -webkit-app-region: no-drag;
          }
          body.mc-focus-mode #electron-drag-bar {
            height: 0;
          }
          body.mc-focus-mode.mc-focus-chrome #electron-drag-bar {
            height: 42px;
          }
        `);
        view.webContents.executeJavaScript(`
          if (!document.getElementById('electron-drag-bar')) {
            const bar = document.createElement('div');
            bar.id = 'electron-drag-bar';
            document.body.prepend(bar);
          }
        `);
      }

      // Frameless Linux has no OS caption controls. CSS-drawn marks avoid
      // distro-font glyph drift; the actions cross the allowlisted preload IPC.
      if (LINUX_FRAMELESS) {
        view.webContents.insertCSS(`
          #electron-linux-controls {
            position: fixed;
            top: 0; right: 0;
            height: 42px;
            display: flex;
            align-items: stretch;
            z-index: 100000;
            -webkit-app-region: no-drag;
          }
          #electron-linux-controls button {
            position: relative;
            width: 36px;
            border: 0;
            background: transparent;
            color: var(--text, #e2e8f0);
            opacity: 0.55;
            cursor: default;
            -webkit-app-region: no-drag;
          }
          #electron-linux-controls button:hover {
            opacity: 1;
            background: rgba(128,128,128,0.18);
          }
          #electron-linux-controls button.close:hover {
            background: #e81123;
            color: #fff;
          }
          #electron-linux-controls button::before {
            content: "";
            position: absolute;
            top: 50%; left: 50%;
            transform: translate(-50%, -50%);
          }
          #electron-linux-controls button.minimize::before {
            width: 10px; height: 0;
            border-top: 1px solid currentColor;
          }
          #electron-linux-controls button.maximize::before {
            width: 9px; height: 9px;
            border: 1px solid currentColor;
          }
          #electron-linux-controls.is-maximized button.maximize::before {
            width: 7px; height: 7px;
            transform: translate(-70%, -30%);
          }
          #electron-linux-controls.is-maximized button.maximize::after {
            content: "";
            position: absolute;
            top: 50%; left: 50%;
            width: 7px; height: 7px;
            transform: translate(-30%, -70%);
            border: 1px solid currentColor;
            border-bottom: 0;
            border-left: 0;
          }
          #electron-linux-controls button.close::before {
            width: 12px; height: 0;
            border-top: 1px solid currentColor;
            transform: translate(-50%, -50%) rotate(45deg);
          }
          #electron-linux-controls button.close::after {
            content: "";
            position: absolute;
            top: 50%; left: 50%;
            width: 12px; height: 0;
            border-top: 1px solid currentColor;
            transform: translate(-50%, -50%) rotate(-45deg);
          }
        `);
        view.webContents.executeJavaScript(`
          if (!document.getElementById('electron-linux-controls')) {
            const wrap = document.createElement('div');
            wrap.id = 'electron-linux-controls';
            const mk = (cls, label, action) => {
              const button = document.createElement('button');
              button.className = cls;
              button.setAttribute('aria-label', label);
              // Native caption controls are not in the tab order either.
              button.tabIndex = -1;
              button.addEventListener(
                'click',
                () => window.kirocrew?.windowControl?.(action),
              );
              return button;
            };
            wrap.append(
              mk('minimize', 'Minimize', 'minimize'),
              mk('maximize', 'Maximize', 'maximize-toggle'),
              mk('close', 'Close', 'close'),
            );
            document.body.prepend(wrap);
          }
        `);
        syncLinuxMaximizeState(win, view);
      }

      view.webContents.executeJavaScript(
        `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`,
      ).then((bg) => {
        if (bg && !win.isDestroyed()) win.setBackgroundColor(bg);
      }).catch(() => {});
      syncNativeTheme(view, win);
    });

    // Native themeSource is process-global, so a focused connection window must
    // refresh it from its own dashboard before native chrome is painted.
    win.on("focus", () => syncNativeTheme(view, win));

    // Same-origin windows remain in-app. Cross-origin web URLs and the audited
    // custom-scheme allowlist go to the OS; every other target fails closed.
    view.webContents.setWindowOpenHandler(
      createWindowOpenHandler({
        openExternal: (url) => shell.openExternal(url),
        getAppOrigin: () => windowBackendUrl,
        log: glog,
      }),
    );

    // Do not leak the dashboard URL/token as a Referer to resources it embeds.
    // This listener remains attached at the same per-window setup point; moving
    // it to a one-time global policy would be a behavior change.
    view.webContents.session.webRequest.onBeforeSendHeaders((details, callback) => {
      delete details.requestHeaders.Referer;
      callback({ requestHeaders: details.requestHeaders });
    });

    return view;
  }

  function applyDashboardChrome(opts, { includeIcon = false } = {}) {
    if (IS_MAC) {
      opts.titleBarStyle = "hidden";
      opts.trafficLightPosition = trafficLightPositionForZoom(1);
    }
    if (IS_WINDOWS) {
      opts.titleBarStyle = "hidden";
      opts.autoHideMenuBar = true;
      opts.titleBarOverlay = {
        color: WINDOWS_TITLEBAR_BACKGROUND,
        symbolColor: nativeTheme.shouldUseDarkColors
          ? WINDOWS_TITLEBAR_SYMBOL_DARK
          : WINDOWS_TITLEBAR_SYMBOL_LIGHT,
        height: HEADER_CSS_PX,
      };
    }
    if (LINUX_FRAMELESS) {
      opts.frame = false;
      // Keep the application menu reachable with Alt without stacking a native
      // menu bar above the dashboard's own header.
      opts.autoHideMenuBar = true;
    }
    if (includeIcon && (IS_WIN || IS_LINUX)) {
      const iconFile = identityFamily(app.getVersion()) === "nightly"
        && fs.existsSync(path.join(__dirname, "icon-nightly.png"))
        ? "icon-nightly.png"
        : "icon.png";
      opts.icon = path.join(__dirname, iconFile);
    }
    return opts;
  }

  function persistMainWindowState() {
    const state = captureWindowState(mainWindow, {
      // DOM fullscreen belongs to the playing element, not the user's launch
      // preference. The bridge is the only owner that can identify it.
      transientFullScreen:
        mainWindow?._mcHtmlFullScreen?.raisedWindow() === true,
    });
    if (state) store.set("windowState", state);
  }

  function createWindow() {
    const state = sanitizeWindowState(store.get("windowState"), {
      displays: screen.getAllDisplays().map((display) => ({
        workArea: display.workArea,
      })),
      defaults: { width: 1280, height: 860 },
      minSize: { width: 550, height: 600 },
    });

    const opts = applyDashboardChrome({
      width: state.width,
      height: state.height,
      minWidth: 550,
      minHeight: 600,
      backgroundColor: "#0f1117",
    }, { includeIcon: true });

    // Restore fullscreen/always-on-top as constructor options so the window
    // never flashes in the wrong state. Normal bounds remain the restore frame.
    if (state.fullScreen) opts.fullscreen = true;
    if (state.alwaysOnTop) opts.alwaysOnTop = true;
    if (typeof state.x === "number" && typeof state.y === "number") {
      opts.x = state.x;
      opts.y = state.y;
    }

    mainWindow = new BaseWindow(opts);
    if (IS_WINDOWS && typeof mainWindow.setMenuBarVisibility === "function") {
      mainWindow.setMenuBarVisibility(false);
    }
    setupWindowContents(mainWindow, backendUrl);

    // Persist continuously (debounced), then synchronously on real quit so the
    // final geometry cannot be lost behind a pending timer.
    let saveTimer = null;
    const persist = persistMainWindowState;
    const persistDebounced = () => {
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(persist, WINDOW_SAVE_DEBOUNCE_MS);
    };
    mainWindow.on("resize", persistDebounced);
    mainWindow.on("move", persistDebounced);
    mainWindow.on("enter-full-screen", persist);
    mainWindow.on("leave-full-screen", persist);

    // A 403 means the gateway secret may have rotated. Re-enter through the
    // same local-then-remote token order used at boot.
    const onNavigate = createTokenRetryHandler(async () => {
      let tokenValue = await fetchLocalToken(backendUrl);
      if (!tokenValue) ({ token: tokenValue } = await fetchRemoteToken(port));
      if (tokenValue && !mainWindow.isDestroyed()) {
        mainWindow.webContents.loadURL(`${backendUrl}?token=${tokenValue}`);
      }
    });
    mainWindow.webContents.on("did-navigate", (_event, _url, httpCode) => {
      onNavigate(httpCode).catch((error) => {
        console.error("Token retry failed:", error);
      });
    });

    // Journal frame-level load outcomes into gateway-launch.log. The remote-crew
    // panes are iframes of THIS webContents, and until this existed a pane that
    // never became a live document left no evidence anywhere: the remote gateway
    // keeps no HTTP access log and a packaged app has no devtools console. The
    // navigation lines are what separate "never requested" from "requested and
    // refused" — the two failures that look identical on screen.
    //
    // `backendUrl` is passed as the trusted origin: it is the ONE document whose
    // `[pane]` journal lines are the dashboard's own. Being the top frame is not
    // enough on its own, because a pane can navigate the top-level window to a
    // remote document and inherit that position.
    attachFrameLoadLogging(mainWindow.webContents, glog, backendUrl);

    const rendererRecovery = createRendererRecovery({
      isQuitting,
      log: glog,
      describeProcesses: () => {
        const metrics = app.getAppMetrics() || [];
        let totalCpu = 0;
        let totalMb = 0;
        let worst = null;
        for (const metric of metrics) {
          const cpu = (metric.cpu && metric.cpu.percentCPUUsage) || 0;
          const mb = ((metric.memory && metric.memory.workingSetSize) || 0) / 1024;
          totalCpu += cpu;
          totalMb += mb;
          if (!worst || mb > worst.mb) {
            worst = { type: metric.type, pid: metric.pid, mb, cpu };
          }
        }
        const parts = [
          `procs=${metrics.length}`,
          `totalCpu=${totalCpu.toFixed(1)}%`,
          `totalWorkingSet=${Math.round(totalMb)}MB`,
        ];
        if (worst) {
          parts.push(
            `largest=${worst.type}:${worst.pid}@${Math.round(worst.mb)}MB/${worst.cpu.toFixed(1)}%`,
          );
        }
        return parts.join(" ");
      },
      reload: () => {
        if (mainWindow.isDestroyed()) return;
        (async () => {
          let tokenValue = await fetchLocalToken(backendUrl);
          if (!tokenValue) ({ token: tokenValue } = await fetchRemoteToken(port));
          if (mainWindow.isDestroyed()) return;
          mainWindow.webContents.loadURL(
            tokenValue ? `${backendUrl}?token=${tokenValue}` : backendUrl,
          );
        })().catch((error) => {
          glog(`renderer recovery reload failed: ${error && error.message}`);
        });
      },
      onGiveUp: ({ reason }) => {
        glog(`renderer recovery exhausted (reason=${reason}); leaving the window as-is`);
      },
    });

    // A renderer that HANGS never reaches the `render-process-gone` handler
    // below: it emits `unresponsive` instead, and with no listener the window
    // stayed frozen until the user rebooted (#8264). Convert a sustained hang
    // into the crash the bounded recovery already heals; `responsive` within
    // the grace window cancels the kill so a transient stall keeps its state.
    const hangRecovery = createHangRecovery({
      isQuitting,
      log: glog,
      forceCrash: () => {
        if (mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) return;
        mainWindow.webContents.forcefullyCrashRenderer();
      },
    });
    mainWindow.webContents.on("unresponsive", () => hangRecovery.handleUnresponsive());
    mainWindow.webContents.on("responsive", () => hangRecovery.handleResponsive());

    mainWindow.webContents.on("render-process-gone", (_event, details) => {
      // Flush the trajectory before the terminal event so the log stays causal.
      // An in-flight content trace is write-or-lose: stopRecording is the only
      // operation that lands it, and a renderer death is its most useful end.
      for (const line of memoryWatchLog.flush()) glog(line);
      void cageTrace.stopForCrash();
      // A hung renderer can die on its own inside the grace window; the armed
      // force-crash must not survive into the reloaded replacement renderer.
      hangRecovery.handleGone();
      rendererRecovery.handleGone(details || {});
    });

    mainWindow.on("close", (event) => {
      if (!isQuitting()) {
        event.preventDefault();
        // macOS must leave its native fullscreen Space before hiding or the
        // Space becomes an orphaned black surface.
        hideToTray(mainWindow);
        return;
      }
      if (saveTimer) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      persist();
    });

    return mainWindow;
  }

  function showMainWindow({ focus = false } = {}) {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    cancelPendingTrayHide(mainWindow);
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    if (focus) mainWindow.focus();
    return true;
  }

  function activateMainWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    // An activate racing a fullscreen-exit hide must win before isVisible is
    // consulted, otherwise the deferred handler hides the window afterwards.
    cancelPendingTrayHide(mainWindow);
    if (!mainWindow.isVisible()) mainWindow.show();
    return true;
  }

  function createTray() {
    const showFromTray = () => {
      cancelPendingTrayHide(mainWindow);
      mainWindow?.show();
    };
    const nightly = identityFamily(app.getVersion()) === "nightly";
    const iconFile = nightly && fs.existsSync(path.join(__dirname, "icon-nightly.png"))
      ? "icon-nightly.png"
      : "icon.png";
    let icon;
    const templatePath = path.join(__dirname, "trayTemplate.png");
    if (IS_MAC && fs.existsSync(templatePath)) {
      // AppKit recolors template images for light/dark/tinted menu bars. The
      // filename supplies @2x automatically; keep the explicit flag too.
      icon = nativeImage.createFromPath(templatePath);
      icon.setTemplateImage(true);
    } else {
      if (IS_MAC) {
        console.warn("tray: trayTemplate.png missing, falling back to colour icon");
      }
      icon = nativeImage
        .createFromPath(path.join(__dirname, iconFile))
        .resize({ width: 18, height: 18 });
    }
    tray = new Tray(icon);
    tray.setToolTip(app.name);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: `Show ${app.name}`, click: showFromTray },
      { type: "separator" },
      { label: "New Connection Window…", click: () => openNewConnectionWindow() },
      { type: "separator" },
      { label: "Open Config File", click: () => shell.openPath(store.path) },
      { type: "separator" },
      { label: "Quit", click: requestQuit },
    ]));
    tray.on("click", showFromTray);
    return tray;
  }

  async function promptRemoteHost() {
    const focused = BaseWindow.getFocusedWindow() || mainWindow;
    if (!focused || focused.isDestroyed() || !focused._mcBackendUrl) return;
    const focusedPort = new URL(focused._mcBackendUrl).port;
    const config = getRemoteHostConfig(store, focusedPort);
    const currentHost = config?.host || "";
    const currentBin = config?.binPath || DEFAULT_REMOTE_BIN;
    const currentRemotePort = config?.remotePort || "";
    const currentRemotePath = config?.remotePath || "";

    const css = await getModalCSS();
    const esc = (value) => value
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    const promptWin = new BrowserWindow({
      width: 480,
      height: 400,
      resizable: false,
      useContentSize: true,
      parent: focused,
      modal: true,
      backgroundColor: "#00000000",
      webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    const html = `<!DOCTYPE html><html><head><style>
      ${css}
    </style></head><body>
      <label>Remote host for :${focusedPort}</label>
      <input id="h" value="${esc(currentHost)}" placeholder="myhost.corp.example.com" autofocus>
      <div class="hint">Leave empty to use local token (no SSH).</div>
      <label>kirocrew binary path</label>
      <input id="b" value="${esc(currentBin)}" placeholder="${DEFAULT_REMOTE_BIN}">
      <label>Remote port <span style="font-weight:normal;opacity:0.6">(default: same as tab = ${focusedPort})</span></label>
      <input id="rp" value="${esc(currentRemotePort)}" placeholder="${focusedPort}">
      <label>Remote PATH <span style="font-weight:normal;opacity:0.6">(default: ${DEFAULT_REMOTE_PATH})</span></label>
      <input id="pa" value="${esc(currentRemotePath)}" placeholder="${DEFAULT_REMOTE_PATH}">
      <div class="row"><button class="ok" onclick="save()">Save</button>
      <button class="cancel" onclick="window.close()">Cancel</button></div>
      <script>
        function save() {
          document.title = JSON.stringify({
            host: document.getElementById('h').value.trim(),
            bin: document.getElementById('b').value.trim(),
            remotePort: document.getElementById('rp').value.trim(),
            remotePath: document.getElementById('pa').value.trim(),
          });
          window.close();
        }
        document.addEventListener('keydown', event => {
          if (event.key === 'Enter') save();
          if (event.key === 'Escape') window.close();
        });
      </script>
    </body></html>`;
    promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    promptWin.setMenu(null);

    let savedTitle = null;
    promptWin.on("page-title-updated", (_event, title) => {
      savedTitle = title;
    });
    promptWin.on("closed", () => {
      try {
        if (savedTitle && savedTitle.startsWith("{")) {
          const {
            host,
            bin,
            remotePort: remotePortValue,
            remotePath,
          } = JSON.parse(savedTitle);
          if (host) {
            const error = validateRemoteSettings(
              host,
              bin,
              remotePortValue,
              remotePath,
            );
            const parent = focused && !focused.isDestroyed() ? focused : null;
            if (error) {
              dialog.showMessageBox(parent, {
                type: "error",
                title: "Invalid Input",
                message: error,
              });
              return;
            }
          }
          setRemoteHostConfig(store, focusedPort, {
            host,
            binPath: bin,
            remotePort: remotePortValue,
            remotePath,
          });
          const parent = focused && !focused.isDestroyed() ? focused : null;
          const message = host
            ? `Remote host for :${focusedPort} set to ${host}`
            : `Remote host for :${focusedPort} cleared (using local token)`;
          console.log(message);
          dialog.showMessageBox(parent, { message, type: "info" });
        }
      } catch (error) {
        console.error("Failed to parse remote host settings:", error.message);
      }
    });
  }

  async function refreshToken() {
    const win = BaseWindow.getFocusedWindow() || mainWindow;
    if (!win || win.isDestroyed() || !win._mcBackendUrl) return;
    const targetUrl = win._mcBackendUrl;
    const targetPort = new URL(targetUrl).port;

    let tokenValue = await fetchLocalToken(targetUrl);
    let sshError = null;
    if (!tokenValue) {
      ({ token: tokenValue, error: sshError } = await fetchRemoteToken(targetPort));
    }
    if (win.isDestroyed()) return;
    if (tokenValue) {
      win.webContents.loadURL(`${targetUrl}?token=${tokenValue}`);
      return;
    }
    const config = getRemoteHostConfig(store, targetPort);
    dialog.showMessageBox(win, {
      type: "warning",
      title: "Token Refresh",
      message: "Could not fetch a fresh token.",
      detail: config?.host
        ? `SSH to ${config.host} failed.\n\n${sshError || "Check your connection."}`
        : "No remote host configured for this tab. Use 'Set Remote Host…' from the Tab menu.",
    });
  }

  function createConnectionWindow(
    connectionBackendUrl,
    connectionPort,
    initialPath = "",
  ) {
    const connOpts = applyDashboardChrome({
      width: 1280,
      height: 860,
      minWidth: 550,
      minHeight: 600,
      backgroundColor: "#0f1117",
    });
    const connWin = new BaseWindow(connOpts);
    if (IS_WINDOWS && typeof connWin.setMenuBarVisibility === "function") {
      connWin.setMenuBarVisibility(false);
    }
    setupWindowContents(connWin, connectionBackendUrl);

    // initialPath can carry a one-shot intent (/chat?new=1). The retry target
    // must follow the URL that actually failed before token retry runs, or a 403
    // would replay the consumed intent and mint a second blank session.
    let retryTarget = initialPath;
    const onNavigate = createTokenRetryHandler(async () => {
      let tokenValue = await fetchLocalToken(connectionBackendUrl);
      if (!tokenValue) {
        ({ token: tokenValue } = await fetchRemoteToken(connectionPort));
      }
      if (tokenValue && !connWin.isDestroyed()) {
        const target = new URL(retryTarget || "", connectionBackendUrl);
        target.searchParams.set("token", tokenValue);
        connWin.webContents.loadURL(target.toString());
      }
    });
    connWin.webContents.on("did-navigate", (_event, url, httpCode) => {
      retryTarget = dashboardRetryPath(
        url,
        connectionBackendUrl,
        retryTarget,
      );
      onNavigate(httpCode).catch((error) => {
        console.error("Token retry failed:", error);
      });
    });
    return connWin;
  }

  async function openNewSessionWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    const win = createConnectionWindow(backendUrl, port, "/chat?new=1");
    await connectWindow(win, backendUrl, { initialPath: "/chat?new=1" });
  }

  async function openNewConnectionWindow() {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    // The tray reaches this during a deferred fullscreen hide; showing a modal
    // is user intent and must cancel that pending hide first.
    cancelPendingTrayHide(mainWindow);
    mainWindow.show();

    const css = await getModalCSS();
    const promptWin = new BrowserWindow({
      width: 400,
      height: 180,
      resizable: false,
      useContentSize: true,
      parent: mainWindow,
      modal: true,
      backgroundColor: "#00000000",
      webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    const html = `<!DOCTYPE html><html><head><style>
      ${css}
    </style></head><body>
      <label>Gateway port</label>
      <input id="p" type="number" value="7778" min="1" max="65535" autofocus>
      <div class="hint">Connect to a Kiro Crew gateway running on another port</div>
      <div class="row"><button class="ok" onclick="go()">Connect</button>
      <button class="cancel" onclick="window.close()">Cancel</button></div>
      <script>
        function go() {
          document.title = document.getElementById('p').value.trim();
          window.close();
        }
        document.addEventListener('keydown', event => {
          if (event.key === 'Enter') go();
          if (event.key === 'Escape') window.close();
        });
      </script>
    </body></html>`;
    promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    promptWin.setMenu(null);

    let savedTitle = null;
    promptWin.on("page-title-updated", (_event, title) => {
      savedTitle = title;
    });
    promptWin.on("closed", async () => {
      if (!savedTitle) return;
      const connectionPort = parseInt(savedTitle, 10);
      if (
        Number.isNaN(connectionPort)
        || connectionPort < 1
        || connectionPort > 65535
      ) return;
      if (!mainWindow || mainWindow.isDestroyed()) return;

      const connectionBackendUrl = `http://localhost:${connectionPort}`;
      const connWin = createConnectionWindow(
        connectionBackendUrl,
        connectionPort,
      );
      await connectWindow(connWin, connectionBackendUrl);
    });
  }

  function renameCurrentWindow() {
    const focused = BaseWindow.getFocusedWindow();
    if (!focused || !focused._mcSetCustomName) return;

    const currentTitle = focused.getTitle();
    const focusedPort = focused._mcBackendUrl
      ? new URL(focused._mcBackendUrl).port
      : "";
    const esc = (value) => value
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

    getDashboardThemeVars().then((vars) => {
      const css = vars && vars.bg
        ? modalCSSFromVars(vars)
        : modalCSSForMode(nativeTheme.shouldUseDarkColors);
      const promptWin = new BrowserWindow({
        width: 400,
        height: 200,
        resizable: false,
        useContentSize: true,
        parent: focused,
        modal: true,
        backgroundColor: "#00000000",
        webPreferences: { nodeIntegration: false, contextIsolation: true },
      });
      const html = `<!DOCTYPE html><html><head><style>
        ${css}
        .check-row { display:flex; align-items:center; gap:6px; margin-top:8px; }
        .check-row input { width:auto; margin:0; }
        .check-row label { margin:0; font-size:12px; }
      </style></head><body>
        <label>Window name</label>
        <input id="n" value="${esc(currentTitle.replace(/^Kiro ?Crew /g, ""))}" autofocus>
        <div class="row"><button class="ok" onclick="go()">Rename</button>
        <button class="cancel" onclick="window.close()">Cancel</button></div>
        <div class="check-row"><input type="checkbox" id="d"><label for="d">Set as default name for :${focusedPort} windows</label></div>
        <script>
          function go() {
            document.title = JSON.stringify({
              name: document.getElementById('n').value.trim(),
              setDefault: document.getElementById('d').checked,
            });
            window.close();
          }
          document.addEventListener('keydown', event => {
            if (event.key === 'Enter') go();
            if (event.key === 'Escape') window.close();
          });
        </script>
      </body></html>`;
      promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
      promptWin.setMenu(null);

      let savedTitle = null;
      promptWin.on("page-title-updated", (_event, title) => {
        savedTitle = title;
      });
      promptWin.on("closed", () => {
        if (!savedTitle || !focused || focused.isDestroyed()) return;
        try {
          const { name, setDefault } = JSON.parse(savedTitle);
          if (name) {
            focused._mcSetCustomName(name);
            if (setDefault && focusedPort) {
              const hosts = store.get("remoteHosts") || {};
              const key = String(focusedPort);
              hosts[key] = { ...(hosts[key] || {}), defaultName: name };
              store.set("remoteHosts", hosts);
            }
          }
        } catch {
          // Legacy plain-text fallback.
          if (savedTitle) focused._mcSetCustomName(savedTitle);
        }
      });
    });
  }

  function showScreenPermissionDialog() {
    const pane =
      "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
    dialog.showMessageBox({
      type: "info",
      title: "Screen Recording permission needed",
      message: "Allow Kiro Crew to capture the screen",
      detail:
        "The screen-snip tool needs macOS Screen Recording permission. "
        + "Open System Settings › Privacy & Security › Screen Recording, "
        + "enable Kiro Crew, then try the snip again.",
      buttons: ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) shell.openExternal(pane);
    }).catch(() => {});
  }

  function showMicPermissionDialog(status = "denied") {
    // Dictation, streaming STT, settings test and meetings can race the same
    // denial. Latch one recovery dialog rather than stacking modal copies.
    if (micDialogOpen) return;
    micDialogOpen = true;
    const pane =
      "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
    const restricted = status === "restricted";
    dialog.showMessageBox({
      type: "info",
      title: "Microphone permission needed",
      message: restricted
        ? "Microphone access is blocked by a policy"
        : "Allow Kiro Crew to use the microphone",
      detail: restricted
        ? "Voice input needs macOS Microphone permission, but access is "
          + "restricted by a device-management policy on this Mac. Contact "
          + "whoever manages it to allow microphone access for Kiro Crew."
        : "Voice input needs macOS Microphone permission, and macOS will not "
          + "ask again once it has been denied. Open System Settings › Privacy "
          + "& Security › Microphone, enable Kiro Crew, then try the mic again.",
      buttons: restricted ? ["OK"] : ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: restricted ? 0 : 1,
    }).then(({ response }) => {
      if (!restricted && response === 0) shell.openExternal(pane);
    }).catch(() => {}).then(() => {
      micDialogOpen = false;
    });
  }

  function configureSessionSecurity() {
    if (sessionSecurityConfigured) return;

    // Screen capture has its own handler. Prefer the native system picker when
    // available and fall back to desktopCapturer elsewhere.
    session.defaultSession.setDisplayMediaRequestHandler(
      createDisplayMediaHandler({
        getSources: () => desktopCapturer.getSources({
          types: ["screen", "window"],
        }),
        getScreenAccessStatus: () => (
          IS_MAC
            ? systemPreferences.getMediaAccessStatus("screen")
            : "granted"
        ),
        onPermissionNeeded: (reason) => {
          if (reason === "denied") showScreenPermissionDialog();
        },
      }),
      { useSystemPicker: true },
    );

    // The dashboard receives only the media grant it needs. Untrusted browser
    // WebContents fail closed by identity before any localhost-origin heuristic
    // can grant them access.
    session.defaultSession.setPermissionRequestHandler(
      createPermissionRequestHandler({
        isUntrusted: isUntrustedContents,
        ...(IS_MAC
          ? {
              getMicAccessStatus: () =>
                systemPreferences.getMediaAccessStatus("microphone"),
              // Ask only on the user's mic gesture. Asking at launch spends
              // macOS TCC's one-shot prompt before the action has context.
              askForMicAccess: () =>
                systemPreferences.askForMediaAccess("microphone"),
              onMicBlocked: () => showMicPermissionDialog(),
            }
          : {}),
      }),
    );
    session.defaultSession.setPermissionCheckHandler(
      createPermissionCheckHandler({ isUntrusted: isUntrustedContents }),
    );

    // Permission handlers are per-session. Embedded pages use a separate
    // persistent partition, so the default-session policy above cannot cover
    // them; deny every permission explicitly before any such view can exist.
    hardenBrowserPartition(session);
    sessionSecurityConfigured = true;
  }

  function handleMicDenied() {
    if (!IS_MAC) return;
    try {
      const status = systemPreferences.getMediaAccessStatus("microphone");
      if (status === "denied" || status === "restricted") {
        showMicPermissionDialog(status);
      }
    } catch {
      // Stay silent when the OS status probe itself is unavailable.
    }
  }

  function focusedDashboardWebContents() {
    const win = BaseWindow.getFocusedWindow();
    if (win) {
      const views = win.contentView && win.contentView.children;
      if (views && views.length > 0) {
        const mainView = views.find((candidate) => {
          try {
            return !!(
              candidate.webContents
              && candidate.webContents.getURL()
            );
          } catch {
            return false;
          }
        });
        if (mainView) return mainView.webContents;
      }
      // Plain BrowserWindows (modal prompts) retain their ordinary WebContents.
      if (win.webContents) return win.webContents;
    }
    return webContents.getFocusedWebContents();
  }

  function focusedDashboardWindow() {
    return [BaseWindow.getFocusedWindow(), mainWindow].find(
      (win) => win && !win.isDestroyed() && win._mcView,
    );
  }

  function openSettingsPage(tab) {
    const win = focusedDashboardWindow();
    if (!win) return;
    cancelPendingTrayHide(win);
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    const wc = win._mcView.webContents;
    if (wc && !wc.isDestroyed()) {
      wc.send("navigate", tab ? `/settings/${tab}` : "/settings");
    }
  }

  function toggleAlwaysOnTop() {
    const win = focusedDashboardWindow();
    if (!win) return;
    try {
      win.setAlwaysOnTop(!win.isAlwaysOnTop());
      const menu = Menu.getApplicationMenu();
      const item = menu && menu.getMenuItemById("keep-on-top");
      if (item) item.checked = win.isAlwaysOnTop();
    } catch {
      // Window is mid-teardown.
    }
    // The preference belongs to the main window state, matching the existing
    // single persisted record even when the command came from a connection.
    persistMainWindowState();
  }

  function zoomMenuItem(apply) {
    return () => {
      const wc = webContents.getFocusedWebContents();
      if (!wc) return;
      apply(wc);
      // Chromium applies zoom per-origin, so same-origin sibling windows move
      // together and every traffic-light inset must be reconciled.
      for (const win of BaseWindow.getAllWindows()) {
        if (win._mcView) positionTrafficLights(win);
      }
    };
  }

  function buildApplicationMenu() {
    appMenu = Menu.buildFromTemplate(buildMenuTemplate({
      isMac: IS_MAC,
      appName: app.name,
      openSettings: () => openSettingsPage(),
      openAbout: () => openSettingsPage("about"),
      reload: () => {
        const wc = focusedDashboardWebContents();
        if (wc) wc.reload();
      },
      forceReload: () => {
        const wc = focusedDashboardWebContents();
        if (wc) wc.reloadIgnoringCache();
      },
      toggleDevTools: () => {
        const wc = focusedDashboardWebContents();
        if (wc) wc.toggleDevTools();
      },
      zoomActualSize: zoomMenuItem((wc) => wc.setZoomFactor(1)),
      zoomIn: zoomMenuItem((wc) => {
        wc.setZoomFactor(stepZoomFactor(wc.getZoomFactor(), +1));
      }),
      zoomOut: zoomMenuItem((wc) => {
        wc.setZoomFactor(stepZoomFactor(wc.getZoomFactor(), -1));
      }),
      alwaysOnTop: !!(store.get("windowState") || {}).alwaysOnTop,
      toggleAlwaysOnTop,
      openNewSessionWindow: () => openNewSessionWindow(),
      openNewConnectionWindow: () => openNewConnectionWindow(),
      renameCurrentWindow: () => renameCurrentWindow(),
      promptRemoteHost: () => promptRemoteHost(),
      refreshToken: () => refreshToken(),
      openConfigFile: () => shell.openPath(store.path),
    }));
    Menu.setApplicationMenu(appMenu);
    return appMenu;
  }

  function menuItems(sender, id) {
    if (!IS_WINDOWS || !WINDOWS_TITLEBAR_MENU_IDS.has(id)) return [];
    const menu = appMenu || Menu.getApplicationMenu();
    const item = menu && menu.getMenuItemById(id);
    const win = windowForWebContents(sender);
    if (!item || !item.submenu || !win || win.isDestroyed()) return [];
    return serializeMenuItems(item.submenu);
  }

  function executeMenu(sender, id, index) {
    if (
      !IS_WINDOWS
      || !WINDOWS_TITLEBAR_MENU_IDS.has(id)
      || !Number.isInteger(index)
    ) return;
    const menu = appMenu || Menu.getApplicationMenu();
    const topLevelItem = menu && menu.getMenuItemById(id);
    const win = windowForWebContents(sender);
    if (!win || win.isDestroyed()) return;
    // sender is the focused dashboard WebContents: titlebar menu interaction
    // itself gives it focus, which is what Electron role items expect.
    executeMenuItem(topLevelItem, index, win, sender);
  }

  function setDevMode(enabled) {
    const menu = Menu.getApplicationMenu();
    const item = menu && menu.getMenuItemById("devtools-toggle");
    if (item) item.visible = !!enabled;
  }

  function setThemeAccent(hex) {
    if (
      typeof hex === "string"
      && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex)
    ) {
      store.set("themeAccent", hex);
    }
  }

  function handleFocusMode(sender, visible) {
    if (!IS_MAC) return;
    const win = windowForWebContents(sender);
    if (!win) return;
    // AppKit drops declared drag regions when button visibility mutates;
    // applyFocusModeChrome re-declares them after changing the native chrome.
    applyFocusModeChrome(win, visible, { positionTrafficLights });
  }

  function handleWindowControl(sender, action) {
    if (!LINUX_FRAMELESS) return;
    const win = windowForWebContents(sender);
    if (win) applyWindowControl(win, action);
  }

  function setThemeMode(pref) {
    if (pref === "system" || pref === "dark" || pref === "light") {
      nativeTheme.themeSource = resolveThemeSource(pref, "");
    }
  }

  function setTitlebarMode(mode) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode === "dark" || mode === "light"
      ? mode
      : (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    // Continue past framed modal windows that cannot accept an overlay; the
    // helper catches per-window so one dialog cannot strand siblings.
    paintAllTitleBarOverlays(
      BaseWindow.getAllWindows(),
      resolvedMode,
      HEADER_CSS_PX,
    );
  }

  function applyZoom(sender, factor) {
    sender.setZoomFactor(factor);
    for (const win of BaseWindow.getAllWindows()) {
      if (win._mcView) positionTrafficLights(win);
    }
    return factor;
  }

  function getZoom(sender) {
    return sender.getZoomFactor();
  }

  function setZoom(sender, factor) {
    return applyZoom(sender, clampZoomFactor(factor));
  }

  function stepZoom(sender, direction) {
    return applyZoom(
      sender,
      stepZoomFactor(sender.getZoomFactor(), direction > 0 ? +1 : -1),
    );
  }

  function panelForSender(sender, panelId, opts) {
    const owner = windowForWebContents(sender);
    if (!owner || !owner._mcBrowserPanel) return null;
    return owner._mcBrowserPanel(panelId, opts);
  }

  function browserOpen(sender, panelId, url) {
    const panel = panelForSender(sender, panelId);
    return panel ? panel.manager.open(url) : null;
  }

  function browserNavigate(sender, panelId, url) {
    const panel = panelForSender(sender, panelId);
    return panel ? panel.manager.navigate(url) : null;
  }

  function browserSetBounds(sender, panelId, rect, viewport) {
    // Layout reports must never create a native page by themselves.
    const panel = panelForSender(sender, panelId, { create: false });
    return panel ? panel.manager.setPanelBounds(rect, viewport) : null;
  }

  function browserSetOverlay(sender, panelId, active) {
    const panel = panelForSender(sender, panelId, { create: false });
    return panel ? panel.manager.setOverlayActive(active) : null;
  }

  function browserSetInactive(sender, panelId, value) {
    // Inactive hides without destroying; tab switches must preserve page state.
    const panel = panelForSender(sender, panelId, { create: false });
    return panel ? panel.manager.setInactive(value) : null;
  }

  function browserClose(sender, panelId) {
    const owner = windowForWebContents(sender);
    const panel = panelForSender(sender, panelId, { create: false });
    if (!panel) return null;
    const state = panel.manager.getState();
    // Release CDP ownership before the view disappears.
    if (owner && owner._mcDestroyBrowserPanel) {
      owner._mcDestroyBrowserPanel(panel.id);
    }
    return { ...state, open: false, visible: false };
  }

  function browserGetState(sender, panelId) {
    const panel = panelForSender(sender, panelId, { create: false });
    return panel ? panel.manager.getState() : null;
  }

  function browserTrackSession(sender, panelId, tracked) {
    const owner = windowForWebContents(sender);
    const sessions = owner && owner._mcReachableSessions;
    const id = typeof panelId === "string" ? panelId.trim() : "";
    if (!sessions || !id) return { ok: false };
    if (tracked) sessions.add(id);
    else sessions.delete(id);
    // Interrupt idle/backoff so a newly declared session is registered inside
    // submit's short native-panel wait instead of after a 25s long poll.
    if (owner._mcAgentChannel) owner._mcAgentChannel.poke();
    return { ok: true };
  }

  async function browserSetAgentAct(sender, panelId, enabled) {
    const panel = panelForSender(sender, panelId);
    if (!panel) return { ok: false };
    panel.agentAct = !!enabled;
    // Revocation must detach an already-held debugger immediately.
    if (!panel.agentAct) await panel.control.release();
    return { ok: true };
  }

  async function browserSetControlOwner(sender, panelId, requested) {
    const panel = panelForSender(sender, panelId, { create: false });
    if (!panel) return null;
    return panel.control.setOwner(requested, panel.gate());
  }

  function browserGetControl(sender, panelId) {
    const panel = panelForSender(sender, panelId, { create: false });
    if (!panel) return null;
    return {
      owner: panel.control.getOwner(),
      attached: panel.control.isAttached(),
      gate: panel.gate(),
    };
  }

  async function browserControl(sender, panelId, op, args) {
    const panel = panelForSender(sender, panelId, { create: false });
    if (!panel) return null;
    // dispatchBrowserOp owns the closed wire vocabulary; never accept raw CDP.
    return dispatchBrowserOp(panel, op, args);
  }

  function recordMemorySample(sender, payload) {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (sender !== mainWindow.webContents) return;
    if (!memoryWatchLog.record(payload)) return;
    // The cheap always-on series arms the expensive authoritative trace only
    // when committed external memory grows enough to justify the cost.
    void cageTrace.considerArming(
      memoryWatchLog.oldestExternalKB(),
      memoryWatchLog.latestExternalKB(),
    );
    if (!profilingEnabled(env)) return;
    const line = memoryWatchLog.lastLine();
    if (line) glog(line);
  }

  function getSummonWindow() {
    return [
      BaseWindow.getFocusedWindow(),
      mainWindow,
      ...BaseWindow.getAllWindows(),
    ].find((win) => win && !win.isDestroyed() && win._mcView) || null;
  }

  return {
    createMainWindow: createWindow,
    createConnectionWindow,
    createTray,
    openNewSessionWindow,
    openNewConnectionWindow,
    promptRemoteHost,
    refreshToken,
    renameCurrentWindow,
    setupWindowContents,
    getMainWindow: () => mainWindow,
    getTray: () => tray,
    getSummonWindow,
    focusedDashboardWindow,
    focusedDashboardWebContents,
    showMainWindow,
    activateMainWindow,
    windowForWebContents,
    positionTrafficLights,
    persistMainWindowState,
    platform: {
      isMac: IS_MAC,
      isWindows: IS_WINDOWS,
      isLinux: IS_LINUX,
      linuxFrameless: LINUX_FRAMELESS,
      linuxFrameDecision: LINUX_FRAME_DECISION,
    },
    menu: {
      buildApplicationMenu,
      items: menuItems,
      execute: executeMenu,
      setDevMode,
      openSettings: openSettingsPage,
      toggleAlwaysOnTop,
    },
    chrome: {
      setThemeAccent,
      focusMode: handleFocusMode,
      windowControl: handleWindowControl,
      setThemeMode,
      setTitlebarMode,
      getZoom,
      setZoom,
      stepZoom,
    },
    browser: {
      open: browserOpen,
      navigate: browserNavigate,
      setBounds: browserSetBounds,
      setOverlay: browserSetOverlay,
      setInactive: browserSetInactive,
      close: browserClose,
      getState: browserGetState,
      trackSession: browserTrackSession,
      setAgentAct: browserSetAgentAct,
      setControlOwner: browserSetControlOwner,
      getControl: browserGetControl,
      control: browserControl,
    },
    security: {
      configureSession: configureSessionSecurity,
      micDenied: handleMicDenied,
      isGatewayLocalForWindow,
    },
    diagnostics: {
      memorySample: recordMemorySample,
      stopForQuit: () => cageTrace.stopForQuit(),
    },
  };
}

module.exports = {
  BROWSER_PARTITION,
  HEADER_CSS_PX,
  WINDOWS_TITLEBAR_MENU_IDS,
  createWindowLifecycle,
};
