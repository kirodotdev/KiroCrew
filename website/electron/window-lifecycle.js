"use strict";

const fs = require("fs");
const path = require("path");

const { createTokenRetryHandler, dashboardRetryPath } = require("./token-retry");
const { createRendererRecovery } = require("./renderer-recovery");
const { createHangRecovery } = require("./hang-recovery");
const { armSplashHistoryClear, fileShellPageBasename } = require("./splash-history");
const { hideToTray, cancelPendingTrayHide, shouldKeepAppHidden } = require("./hide-to-tray");
const { attachHtmlFullScreen } = require("./html-fullscreen");
const {
  watchFullScreenTransitions,
  repairStalledFullScreenExit,
} = require("./fullscreen-transition-watch");
const { createWindowOpenHandler } = require("./external-scheme");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const { stepZoomFactor } = require("./zoom");
const { registerCaptureSurface } = require("./capture-trust");
const { isLoopbackUrl } = require("./browser-control");
const { runAnnotateOp } = require("./browser-annotate");
const { attachContextMenu } = require("./context-menu");
const {
  TUNNEL_OPTION_LABEL,
  parseRemoteCrewFields,
  saveRemoteCrewConfig,
  tunnelOptionHint,
} = require("./remote-crew-setup");
const {
  getRemoteHostConfig,
  getRemoteHostConfigForUrl,
  getRemoteHostDefaultNameForUrl,
  isSelectablePort,
  retireLegacyEmptyPortHost,
  setRemoteHostConfig,
} = require("./host-config");
const { defaultedPort, portIsSchemeDefault } = require("./gateway-auth-hint");
const { openPathHardened } = require("./open-path");
const { DEFAULT_REMOTE_BIN, DEFAULT_REMOTE_PATH } = require("./remote-token");
const { identityFamily } = require("./instance-guard");
const { decideLinuxFrame, applyWindowControl } = require("./linux-frame");
const { buildMenuTemplate } = require("./app-menu");
const { serializeMenuItems, executeMenuItem } = require("./windows-menu-model");
const {
  SYMBOL_DARK: WINDOWS_TITLEBAR_SYMBOL_DARK,
  SYMBOL_LIGHT: WINDOWS_TITLEBAR_SYMBOL_LIGHT,
  OVERLAY_BACKGROUND: WINDOWS_TITLEBAR_BACKGROUND,
} = require("./windows-titlebar");
const { attachFrameLoadLogging } = require("./frame-load-log");
const { attachPaneAssetJournal } = require("./pane-asset-journal");
const { createMemoryWatchLog } = require("./memory-watch-log");
const { createCageTrace } = require("./cage-trace");
const { profilingEnabled } = require("./perf-metrics");
const {
  HEADER_CSS_PX,
  trafficLightPositionForZoom,
  createWindowChrome,
} = require("./runtime/window/chrome");
const { createWindowPrompts } = require("./runtime/window/prompts");
const { createSessionSecurity } = require("./runtime/window/session-security");
const { injectLinuxCaptionControls } = require("./runtime/window/linux-captions");
const { attachBrowserPanels, dispatchBrowserOp } = require("./runtime/window/browser-panels");

const BROWSER_PARTITION = "persist:kirocrew-browser";
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
    mintLocalToken,
    fetchRemoteToken,
    isQuitting = () => false,
    requestQuit,
    connectWindow,
    // Re-apply the launch port's managed-tunnel choice after a save here, so
    // ticking or un-ticking it takes effect without a relaunch.
    syncTunnel = () => {},
    platform = process.platform,
    env = process.env,
  } = options || {};

  if (!electron) throw new Error("createWindowLifecycle: electron is required");
  if (!store) throw new Error("createWindowLifecycle: store is required");
  if (!backendUrl) throw new Error("createWindowLifecycle: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createWindowLifecycle: port is required");
  if (typeof mintLocalToken !== "function") {
    throw new Error("createWindowLifecycle: mintLocalToken is required");
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
  let appMenu = null;
  // The fullscreen-transition watch for the current main window. Its `pending()`
  // is what keeps a close-to-tray exit from abandoning a transition AppKit is
  // still animating, which is the cause of the orphan overlay rather than a
  // symptom of it.
  let fullScreenWatch = null;

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

  // The cohesive owners this facade composes, in the order the process first
  // needs them. Each receives only what it reads; window state stays here.
  const {
    syncNativeTheme,
    positionTrafficLights,
    trackZoomChrome,
    setThemeAccent,
    handleFocusMode,
    handleWatchFocusCursor,
    setThemeMode,
    setTitlebarMode,
    getZoom,
    setZoom,
    stepZoom,
  } = createWindowChrome({
    BaseWindow,
    nativeTheme,
    screen,
    store,
    log: glog,
    isMac: IS_MAC,
    isWindows: IS_WINDOWS,
    windowForWebContents,
  });
  const { getModalCSS, promptConnectionPort, renameFocusedWindow } = createWindowPrompts({
    BaseWindow,
    BrowserWindow,
    nativeTheme,
    store,
    getMainWindow: () => mainWindow,
  });
  const { configureSessionSecurity, handleMicDenied } = createSessionSecurity({
    session,
    webContents,
    desktopCapturer,
    systemPreferences,
    dialog,
    shell,
    isMac: IS_MAC,
    partition: BROWSER_PARTITION,
  });

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
  //
  // The key comes from `defaultedPort`, not `URL.port`, because the URL API
  // leaves a scheme's default port empty: on `http://localhost:80` the raw
  // property is `""`, the lookup asks for `remoteHosts[""]`, misses, and a
  // tunnelled crew reads as a gateway on this machine -- after which the
  // heartbeat sends this machine's internal secret over that tunnel.
  //
  // The lookup goes through `getRemoteHostConfigForUrl` rather than the port
  // alone so a crew an older version recorded UNDER that empty key still reads
  // as remote. Resolving the key without honouring those records would turn a
  // store that was safe by accident into the exposure above.
  //
  // The store is the single source of truth for the remote verdict. On a
  // scheme-default port (:80/:443) the 'Set Remote Host…' Clear is REFUSED, so
  // the record cannot be dropped from under a live `ssh -L` tunnel and a later
  // default-port window still resolves REMOTE from the store. On a selectable
  // port a Clear that removes the record is the user's own reconfiguration, and
  // every window reads that same store, so no per-window latch is needed.
  function isGatewayLocalForWindow(win) {
    if (!win || win.isDestroyed() || !win._mcBackendUrl) return false;
    const url = win._mcBackendUrl;
    if (!isLoopbackUrl(url)) return false; // non-loopback: reached off-box, never local
    // The store is the source of truth. A crew record under this URL's resolved
    // key (including the legacy empty key) marks the window remote; its absence
    // marks it local. On a scheme-default port the Clear is REFUSED, so the
    // record cannot be dropped from under a live tunnel; on a selectable port a
    // Clear that removes the record is the user's own reconfiguration, and a
    // fresh window reads that same store, so no window-scoped latch is needed.
    return !getRemoteHostConfigForUrl(store, url)?.host;
  }

  function setupWindowContents(win, windowBackendUrl) {
    const windowPort = defaultedPort(windowBackendUrl);
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
      // Resolve the name through the URL so a title an older version pinned under
      // the empty key on a scheme-default port still renders until the record is
      // retired onto the resolved key. Reading `getRemoteHostConfig(store,
      // windowPort)` alone would miss it and the suffix would revert to `[:80]`.
      const remoteName = getRemoteHostDefaultNameForUrl(store, windowBackendUrl);
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
    // The dashboard SPA is a capture surface (the chat composer's snip and the
    // web-preview crop). Registered against the gateway origin THIS window was
    // opened on, so a secondary window pointed at a remote gateway is bound to
    // its own origin and never to a sibling's.
    registerCaptureSurface(view.webContents, windowBackendUrl);
    win._mcView = view;

    // One native browser view/control plane per dashboard panel, plus the agent
    // command channel that drives them (runtime/window/browser-panels.js).
    const browserPanels = attachBrowserPanels(win, view, {
      WebContentsView,
      shell,
      partition: BROWSER_PARTITION,
      readInternalSecret,
      isGatewayLocalForWindow,
    });

    // The dashboard's own webContents, so the link block also gets the origin --
    // which is what turns a `/abs/path` link into a bare-path copy instead of a
    // useless localhost URL. The embedded browser panel passes no origin: an
    // arbitrary site's same-origin pathname is not a local file.
    attachContextMenu(view.webContents, { getAppOrigin: () => windowBackendUrl });

    trackZoomChrome(win, view);

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

      // Frameless Linux has no OS caption controls, so the shell draws them.
      if (LINUX_FRAMELESS) injectLinuxCaptionControls(win, view);

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
    // On re-activation the platform re-resolves which child view receives
    // keystrokes and may pick a hidden browser view again; each panel heals
    // that by handing focus back to the dashboard view (see browser-view.js
    // header note 3). A visible or unfocused panel is left alone.
    win.on("focus", () => {
      for (const entry of browserPanels.values()) entry.manager.reclaimFocus();
    });

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

    // Journal the terminal events so a stalled transition is legible in
    // gateway-launch.log; until this existed a frozen fullscreen exit left no
    // evidence anywhere. The watch below is the only detector the main process
    // has for that stall (fullscreen-transition-watch.js explains why), and its
    // repair is the only thing that clears the AppKit overlay short of a quit.
    mainWindow.on("enter-full-screen", () => {
      glog(`fullscreen: entered bounds=${JSON.stringify(mainWindow.getBounds())}`);
    });
    mainWindow.on("leave-full-screen", () => {
      glog(`fullscreen: left bounds=${JSON.stringify(mainWindow.getBounds())}`);
    });
    fullScreenWatch = watchFullScreenTransitions(mainWindow, {
      isMac: IS_MAC,
      onStall: ({ target, fullScreen, visible, elapsedMs }) => {
        glog(
          `fullscreen: ${target ? "enter" : "exit"} transition did not complete` +
            ` after ${elapsedMs}ms (isFullScreen=${fullScreen} visible=${visible})`,
        );
        if (target) return; // an unfinished ENTER has no known overlay to clear
        const outcome = repairStalledFullScreenExit({
          app,
          win: mainWindow,
          isMac: IS_MAC,
          keepHidden: () => shouldKeepAppHidden(mainWindow),
        });
        glog(
          `fullscreen: stalled exit repair hidden=${outcome.hidden}` +
            ` unhideScheduled=${outcome.unhideScheduled}`,
        );
      },
      onArm: ({ target }) => {
        glog(`fullscreen: ${target ? "enter" : "exit"} transition started`);
      },
      // A transition abandoned mid-animation orphans its overlay just as a stall
      // does, and its replacement fires normally so nothing else notices. The
      // close path no longer causes this (hide-to-tray serialises its exit), but
      // a user toggling fullscreen twice inside one animation still can, and
      // AppKit gives no way to reach the overlay other than this repair.
      onAbort: ({ target, fullScreen, visible, elapsedMs }) => {
        const keepHiddenNow = shouldKeepAppHidden(mainWindow);
        glog(
          `fullscreen: ${target ? "enter" : "exit"} transition abandoned after ${elapsedMs}ms` +
            ` (isFullScreen=${fullScreen} visible=${visible} pendingTrayHide=${keepHiddenNow})`,
        );
        const outcome = repairStalledFullScreenExit({
          app,
          win: mainWindow,
          isMac: IS_MAC,
          keepHidden: () => shouldKeepAppHidden(mainWindow),
        });
        glog(
          `fullscreen: abandoned transition repair hidden=${outcome.hidden}` +
            ` unhideScheduled=${outcome.unhideScheduled}`,
        );
      },
    });

    // A 403 means the gateway secret may have rotated. Re-enter through the
    // same local-then-remote token order used at boot.
    const onNavigate = createTokenRetryHandler(async () => {
      let tokenValue = await mintLocalToken(backendUrl);
      if (!tokenValue) {
        ({ token: tokenValue } = await fetchRemoteToken(port));
      }
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
    // The pane's module graph is the one load stage no renderer-side line can
    // report: a stalled hashed-chunk fetch leaves the entry module unevaluated,
    // so nothing of ours runs in that frame to say so. The main process sees the
    // request either way. See pane-asset-journal.js.
    attachPaneAssetJournal(mainWindow.webContents.session, glog, backendUrl);

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
          let tokenValue = await mintLocalToken(backendUrl);
          if (!tokenValue) {
            ({ token: tokenValue } = await fetchRemoteToken(port));
          }
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
        // macOS must leave its native fullscreen Space before hiding or the Space
        // becomes an orphaned black surface, and the hide that follows is an
        // app-level one: AppKit may have left a full-display overlay on screen
        // that only `app.hide()` can reach (see hide-to-tray.js).
        glog(`close: hiding to tray (fullScreen=${mainWindow.isFullScreen()})`);
        hideToTray(mainWindow, {
          log: glog,
          // isFullScreen() already reports the target while AppKit is still
          // exiting. Carry the watch target so the helper attaches to that exit
          // instead of issuing another toggle or treating the window as stable.
          transitionTarget: fullScreenWatch ? fullScreenWatch.pending() : null,
          // The exit must not be issued while AppKit is still animating; the watch
          // is what knows how long the window has been still. Its terminal-exit
          // clock also covers AppKit's final order-in after pending() clears.
          quietFor: () => (fullScreenWatch ? fullScreenWatch.quietFor() : Infinity),
          exitSettlingFor: () => (
            fullScreenWatch ? fullScreenWatch.exitSettlingFor() : Infinity
          ),
        });
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

  // A tray hide out of fullscreen hides the whole APP (hide-to-tray.js explains
  // why: it is the only call that also orders out AppKit's abandoned overlay).
  // A hidden app ignores `win.show()`, so every user-intent show has to unhide
  // the app first. Harmless when the app was never hidden, and macOS-only
  // because `app.hide()` is.
  function unhideApp() {
    if (!IS_MAC || typeof app.show !== "function") return;
    try {
      app.show();
    } catch {
      /* best effort — the window show below is what the user asked for */
    }
  }

  function showMainWindow({ focus = false } = {}) {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    cancelPendingTrayHide(mainWindow);
    unhideApp();
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
    unhideApp();
    if (!mainWindow.isVisible()) mainWindow.show();
    return true;
  }

  function createTray() {
    const showFromTray = () => {
      showMainWindow({ focus: true });
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
      { label: "Open Config File", click: () => openPathHardened(shell, store.path) },
      { type: "separator" },
      { label: "Quit", click: requestQuit },
    ]));
    tray.on("click", showFromTray);
    return tray;
  }

  async function promptRemoteHost() {
    const focused = BaseWindow.getFocusedWindow() || mainWindow;
    if (!focused || focused.isDestroyed() || !focused._mcBackendUrl) return;
    // Keyed with `defaultedPort` so the entry this form WRITES lands under the
    // same key `isGatewayLocalForWindow` READS. A raw `URL.port` is "" on a
    // scheme-default port, so the two would disagree on :80 and a crew the user
    // configured here would classify as a gateway on this machine.
    const focusedPort = defaultedPort(focused._mcBackendUrl);
    // Read through the resolver so a crew an older version left under the empty
    // key pre-fills this form: stating it again is what moves the record onto
    // the resolved key, and the write below retires the old one.
    const config = getRemoteHostConfigForUrl(store, focused._mcBackendUrl);
    const currentHost = config?.host || "";
    const currentBin = config?.binPath || DEFAULT_REMOTE_BIN;
    const currentRemotePort = config?.remotePort || "";
    const currentRemotePath = config?.remotePath || "";
    const currentManageTunnel = config?.manageTunnel === true;
    // The app keeps a tunnel only for the port it launched on, so this form
    // offers the option there alone; for any other tab it carries the stored
    // choice over unchanged.
    const isLaunchPort = String(focusedPort) === String(port);
    const offerTunnel = isLaunchPort && !IS_WINDOWS;

    const css = await getModalCSS();
    const esc = (value) => value
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    const promptWin = new BrowserWindow({
      width: 480,
      height: offerTunnel ? 520 : 400,
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
      ${offerTunnel ? `<label style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="mt" style="width:auto"${currentManageTunnel ? " checked" : ""}> ${TUNNEL_OPTION_LABEL}</label>
      <div class="hint">${esc(tunnelOptionHint(focusedPort))}</div>` : ""}
      <div class="row"><button class="ok" onclick="save()">Save</button>
      <button class="cancel" onclick="window.close()">Cancel</button></div>
      <script>
        function save() {
          document.title = JSON.stringify({
            host: document.getElementById('h').value.trim(),
            binPath: document.getElementById('b').value.trim(),
            remotePort: document.getElementById('rp').value.trim(),
            remotePath: document.getElementById('pa').value.trim(),
            // Read from the checkbox where this form offers it; elsewhere the
            // stored choice rides through unchanged, never silently dropped.
            manageTunnel: ${offerTunnel ? "document.getElementById('mt').checked" : currentManageTunnel ? "true" : "false"},
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
        const fields = parseRemoteCrewFields(savedTitle);
        if (fields) {
          const { host } = fields;
          const parent = focused && !focused.isDestroyed() ? focused : null;
          // A record an older version left under the empty key is superseded by
          // what the user states here, but only ONCE that statement is durable.
          // Retiring first would erase the record on a write that is refused --
          // and `saveRemoteCrewConfig` refuses an unselectable port outright, so
          // on an http window whose port resolves to 80 no write can ever
          // succeed. The crew would then read as a gateway on this machine, with
          // no way back, which is the exposure this whole change closes.
          //
          // Only a URL whose port is its scheme's default could have produced
          // that record, so a window on any other port leaves it alone.
          const retireLegacy = () => {
            if (portIsSchemeDefault(focused._mcBackendUrl)) {
              retireLegacyEmptyPortHost(store, focusedPort);
            }
          };
          if (!host) {
            // A scheme-default port (:80/:443) can't hold a per-port record the
            // user can rewrite (saveRemoteCrewConfig refuses to WRITE such a
            // port), so a crew reached on one is marked remote either by the
            // empty-key legacy record OR by a legacy record left under the
            // resolved key "80"/"443" itself. Deleting the resolved-key record
            // AND retiring the legacy one here would leave a FRESH default-port
            // window later reading no crew at all, so isGatewayLocalForWindow
            // classifies the loopback window as a local gateway and the
            // host-presence heartbeat sends X-Internal-Secret through the
            // still-open ssh tunnel: the exact exposure this change closes.
            //
            // There is no in-app probe that a scheme-default port is positively
            // local (a :80 write is refused by saveRemoteCrewConfig), so the only
            // fail-closed answer is to REFUSE the Clear and keep the record
            // intact -- a future default-port window still resolves REMOTE from
            // the store itself, not merely from a window-scoped latch. Resolve
            // through getRemoteHostConfigForUrl (not the empty key alone) so a
            // host under the resolved "80"/"443" key is also caught. Direct the
            // user to reopen the crew on a selectable port and clear it there,
            // where a per-port record can be written and removed.
            if (
              portIsSchemeDefault(focused._mcBackendUrl)
              && getRemoteHostConfigForUrl(store, focused._mcBackendUrl)?.host
            ) {
              dialog.showMessageBox(parent, {
                type: "error",
                title: "Can't clear remote host from this tab",
                message:
                  `This crew was reached on a default port (:${focusedPort}), so the app can't change or clear its remote-host setting from this tab. The setting is kept on purpose so this window is never mistaken for a local gateway while the connection is open. To stop reaching this crew, close this tab (and stop the "ssh -L :${focusedPort}" tunnel if you started one). A crew opened on a selectable port (for example "ssh -L 7777:…") can be cleared from its own tab.`,
              });
              return;
            }
            // Clearing belongs to this surface: the shared writer stores a crew
            // and refuses an empty host. A scheme-default port with a host-bearing
            // legacy record was already intercepted by the refusal above, and on
            // any other port there is no empty-key record to retire, so no
            // retirement runs here.
            setRemoteHostConfig(store, focusedPort, {});
            if (isLaunchPort) syncTunnel();
            const cleared = `Remote host for :${focusedPort} cleared (using local token)`;
            console.log(cleared);
            dialog.showMessageBox(parent, { message: cleared, type: "info" });
            return;
          }
          const { saved, error } = saveRemoteCrewConfig(store, focusedPort, fields);
          if (!saved) {
            dialog.showMessageBox(parent, {
              type: "error",
              title: "Invalid Input",
              message: error,
            });
            return;
          }
          if (isLaunchPort) syncTunnel();
          retireLegacy();
          const message = `Remote host for :${focusedPort} set to ${host}`;
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
    const targetPort = defaultedPort(targetUrl);

    let tokenValue = await mintLocalToken(targetUrl);
    let sshError = null;
    if (!tokenValue) {
      ({ token: tokenValue, error: sshError } = await fetchRemoteToken(targetPort));
    }
    if (win.isDestroyed()) return;
    if (tokenValue) {
      win.webContents.loadURL(`${targetUrl}?token=${tokenValue}`);
      return;
    }
    // Resolver, not the port alone: a crew an older version left under the empty
    // key is still the crew this tab reaches, and saying none is configured
    // would send the reader to a form that shows the host they already set.
    const config = getRemoteHostConfigForUrl(store, targetUrl);
    dialog.showMessageBox(win, {
      type: "warning",
      title: "Token Refresh",
      message: "Could not fetch a fresh token.",
      // Three states, not two. `fetchRemoteToken` keys its own lookup by port, so
      // on a record this resolver reached under the empty key it returns without
      // running ssh at all -- and reporting that as "SSH failed" describes an
      // attempt that never happened and sends the reader to check a connection
      // nothing used.
      //
      // On the crew-configured branch the remedy depends on whether the tab's
      // own port can hold a crew at all. `isSelectablePort` gates on
      // `Number.isInteger`, and `defaultedPort` returns a STRING, so the port is
      // coerced with `Number(...)` before the test -- passing the raw string
      // makes the predicate always false and sends every port down the
      // unselectable branch.
      //
      // On an unselectable port (http :80) `saveRemoteCrewConfig` refuses every
      // Save, so re-entering the host cannot help. But the remedy must NOT be
      // Clear: on a live `ssh -L 80:` tunnel the crew record is the only thing
      // marking this window remote, and clearing it makes `isGatewayLocalForWindow`
      // read the still-open loopback window as a gateway on THIS machine -- after
      // which the idle heartbeat puts `X-Internal-Secret` through the tunnel,
      // which is the exact exposure this change closes. So direct the user to
      // reopen the crew on a selectable port instead, leaving the record intact.
      detail: sshError
        ? `SSH to ${config?.host || "the remote host"} failed.\n\n${sshError}`
        : config?.host
          ? isSelectablePort(Number(targetPort))
            ? `No token could be fetched for ${config.host}, and no SSH attempt was made.`
              + " Re-enter the host with 'Set Remote Host…' from the Tab menu so it's"
              + " saved for this tab."
            : `No token could be fetched for ${config.host}, and this tab's port (${targetPort})`
              + " can't be used to save one. Reconnect the crew on a different local"
              + " port (for example \"ssh -L 7777:…\" instead of :" + targetPort + "), then open that tab."
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
      let tokenValue = await mintLocalToken(connectionBackendUrl);
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
    unhideApp();
    mainWindow.show();

    await promptConnectionPort(() => mainWindow, async (connectionPort) => {
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
    renameFocusedWindow();
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
    unhideApp();
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
      // Match the sibling reload/devtools handlers: a BaseWindow has no
      // top-level webContents, so getFocusedWebContents() returns null here and
      // zoom would silently no-op. focusedDashboardWebContents() reaches the
      // dashboard view nested in the contentView.
      const wc = focusedDashboardWebContents();
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
      openConfigFile: () => openPathHardened(shell, store.path),
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

  function handleWindowControl(sender, action, senderFrame) {
    const win = windowForWebContents(sender);
    if (!win) return;
    if (LINUX_FRAMELESS) {
      applyWindowControl(win, action);
      return;
    }
    // Off Linux the OS draws the captions, so this channel stays closed to the
    // dashboard. The one admission is `close` from the splash (loading.html):
    // it is painted into this window with no chrome of its own, and on macOS
    // the window may have no reachable close control at that moment -- native
    // fullscreen hides the traffic lights, and focus mode hides them in
    // windowed mode with nothing left to restore them once the dashboard
    // document is gone. `close` runs the window's own close handler, which
    // hides to tray and leaves fullscreen first, exactly like the native
    // button. Admission uses the immutable URL of the top-level frame that sent
    // the IPC; the WebContents current URL may change before this handler runs.
    // The page is named by exactly one literal: the splash is the only shell
    // page that carries a close control. The token prompt is a transient shell
    // page for history pruning (splash-history.js) but sends nothing on this
    // channel, so it gets no admission on it. fileShellPageBasename yields ""
    // for anything that is not a file: URL, so a dashboard route that merely
    // mentions loading.html never matches, and junk fails closed.
    if (
      action !== "close"
      || fileShellPageBasename(sendingMainFrameUrl(sender, senderFrame)) !== "loading.html"
    ) return;
    applyWindowControl(win, "close");
  }

  function sendingMainFrameUrl(sender, senderFrame) {
    try {
      if (!senderFrame || senderFrame !== sender?.mainFrame) return "";
      return typeof senderFrame.url === "string" ? senderFrame.url : "";
    } catch {
      return ""; // missing, malformed, or torn down: fail closed
    }
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

  // Human-initiated element annotation on the page the user is looking at.
  // Served through executeJavaScript/capturePage, never the agent control
  // plane: it needs no CDP owner and Browser Mode may be off. Closed op set.
  // Native pointer input seen by the browser view, per WebContents. The
  // annotate focus hand-back keys off THIS (a signal the page cannot forge),
  // never off the page-controlled poll reply alone. WebContents 'input-event'
  // is a documented Electron event, present in the pinned v43 line, whose
  // InputEvent.type covers mouseDown/mouseUp:
  // https://www.electronjs.org/docs/latest/api/web-contents#event-input-event
  const annotateInputArmed = new WeakSet();
  const ANNOTATE_FOCUS_WINDOW_MS = 2000;
  function focusAnnotateSender(panel) {
    try {
      const s = panel.annotateSender;
      if (s && !s.isDestroyed()) s.focus();
    } catch {
      // Focus is a courtesy; the editor still works after a click.
    }
  }
  function armAnnotateInput(panel, wc) {
    if (!wc || annotateInputArmed.has(wc)) return;
    annotateInputArmed.add(wc);
    try {
      wc.on("input-event", (_e, input) => {
        if (!input || (input.type !== "mouseDown" && input.type !== "mouseUp")) return;
        panel.lastNativeInput = Date.now();
        // While picking, the mouse-up that completes a pick hands focus back
        // to the panel RIGHT HERE -- on the native input path, before the
        // ~150 ms poll that reports the pick -- so a note typed immediately
        // after the click lands in the panel's editor, never in the page.
        // Nothing the page can do triggers this: it is real input, and the
        // picking flag is written only from this process's own op results.
        if (input.type === "mouseUp" && panel.annotatePicking) focusAnnotateSender(panel);
      });
    } catch {
      // No native input feed: the hand-back simply never fires.
    }
  }
  async function browserAnnotate(sender, panelId, op, args) {
    const panel = panelForSender(sender, panelId, { create: false });
    if (!panel) return { ok: false, code: "no_view", error: "no native browser panel" };
    const wc = panel.manager.getWebContents();
    if (op === "start") { panel.annotateSender = sender; armAnnotateInput(panel, wc); }
    const res = await runAnnotateOp(wc, op, args);
    // Pick-mode flag for the native input path above -- from this process's
    // own view of the ops (start/stop/teardown) and the sanitized poll reply.
    if (res && res.ok) {
      if (op === "start") panel.annotatePicking = true;
      else if (op === "stop" || op === "teardown") panel.annotatePicking = false;
      else if (op === "poll" && typeof res.picking === "boolean") panel.annotatePicking = res.picking;
    } else if (res && !res.ok && (res.code === "no_overlay" || res.code === "no_view")) {
      panel.annotatePicking = false;
    }
    // The click that picked an element (or a marker) landed in the native
    // view, so keyboard focus is there. The note is typed in the PANEL -- hand
    // focus back to the dashboard renderer so its editor can take it without
    // a second click. Poll-only, one-shot (the flags are cleared on read).
    // The page owns the reply, so it is never enough on its own: the id must
    // name a pick the sanitizer kept AND a real mouse press must have reached
    // the view (Electron's input-event, which page script cannot synthesize)
    // within the last two seconds; that press is then consumed. A hostile
    // page re-reporting `picked` every poll moves focus zero times, while
    // EVERY real pick -- however quick the previous one -- gets focus back,
    // so the next keystrokes land in the panel's editor, never in the page.
    if (op === "poll" && res && res.ok && (res.picked !== undefined || res.edit !== undefined)) {
      const id = res.picked !== undefined ? res.picked : res.edit;
      const known = Array.isArray(res.items) && res.items.some((it) => it && it.id === id);
      const now = Date.now();
      const native = panel.lastNativeInput && now - panel.lastNativeInput <= ANNOTATE_FOCUS_WINDOW_MS;
      if (known && native) {
        panel.lastNativeInput = 0;
        focusAnnotateSender(panel);
      }
    }
    return res;
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
      watchFocusCursor: handleWatchFocusCursor,
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
      annotate: browserAnnotate,
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
