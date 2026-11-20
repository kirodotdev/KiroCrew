"use strict";

const fs = require("fs");
const path = require("path");

const {
  createTokenRetryHandler,
  dashboardRetryPath,
  dashboardUrlFor,
  loadDashboardWithToken,
} = require("./token-retry");
const { createRendererRecovery, withSafeReload, hasSafeReload } = require("./renderer-recovery");
const { createRendererExhaustedNotice } = require("./renderer-exhausted-notice");
const { createHangRecovery, endHungRenderer } = require("./hang-recovery");
const { armSplashHistoryClear, fileShellPageBasename } = require("./splash-history");
const { armSwipeHistory, browserPanelUnderCursor } = require("./swipe-history");
const { armScrollTouch, readNaturalScrolling } = require("./scroll-touch");
const { hideToTray, cancelPendingTrayHide, shouldKeepAppHidden } = require("./hide-to-tray");
const {
  createCloseHandler,
  getCloseAction,
  setCloseAction,
} = require("./close-action");
const { getTrayIcon, setTrayIcon } = require("./tray-icon");
const { createMinimizeToTray } = require("./minimize-to-tray");
const { attachHtmlFullScreen } = require("./html-fullscreen");
const { menuBarCanOverlap } = require("./fullscreen-top-reserve");
const {
  watchFullScreenTransitions,
  repairStalledFullScreenExit,
} = require("./fullscreen-transition-watch");
const { createNavigationGuard, createRedirectGuard, createWindowOpenHandler } = require("./external-scheme");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const { stepZoomFactor } = require("./zoom");
const { registerCaptureSurface } = require("./capture-trust");
const { isLoopbackUrl } = require("./browser-view");
const { shownRelayPane } = require("./relay-crew-window");
const { runAnnotateOp } = require("./browser-annotate");
const { attachContextMenu } = require("./context-menu");
const {
  TUNNEL_OPTION_LABEL,
  parseRemoteCrewFields,
  remoteCrewDraft,
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
const { buildMenuTemplate, applySharingVisibility } = require("./app-menu");
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
const { wireWillDownloadOnce } = require("./download-handler");
const { attachBrowserPanels } = require("./runtime/window/browser-panels");

const BROWSER_PARTITION = "persist:kirocrew-browser";
const FULLSCREEN_SETTLE_MS = [250, 1500];
const DASHBOARD_SETTLE_MS = 1500;
// A display rearrange (monitor hot-plug, or dragging the window to a screen
// with a different backing scale factor) changes the window's content bounds,
// but macOS reports the new geometry slightly AFTER the display event fires. An
// immediate recompute plus bounded deferred passes settle the WebContentsView
// onto the new scale — the same rationale as FULLSCREEN_SETTLE_MS. Without it
// the child view keeps its old-scale bounds until the next resize/move/paint,
// which surfaces as the dashboard briefly ghosting across both displays.
const DISPLAY_SETTLE_MS = [0, 250, 1500];
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
 * The dashboard session's request headers with the Referer cut down.
 *
 * The dashboard URL can carry its sign-in token, so no request sends it. A frame
 * the page embeds keeps the referring page's origin, never its path or query:
 * an embed host such as YouTube refuses to play without one (its Error 153), and
 * the dashboard CSP `frame-src` already limits frames to the origins it admits
 * (an app's are the `ui.frameSources` the owner approved). Every other request
 * sends no Referer. Chromium never sends one a page's policy withholds, so this
 * only ever narrows.
 */
function dashboardRequestHeaders(details) {
  const headers = { ...details.requestHeaders };
  const referer = headers.Referer;
  delete headers.Referer;
  if (referer && details.resourceType === "subFrame") {
    let origin = "null";
    try {
      origin = new URL(referer).origin;
    } catch {
      /* no usable referrer: send none */
    }
    if (origin !== "null") headers.Referer = `${origin}/`;
  }
  return headers;
}

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
    // The launch log's path, named in the dead-renderer error dialog so the
    // user can attach it to a report.
    logPath = () => "",
    // Whether a dashboard document has finished loading in this run (the
    // gpu-crash-fallback latch). Tells the dead-renderer dialog a renderer that
    // never started from one that crashed later.
    dashboardLoaded = () => false,
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
    // Shared single-use registry of downloads the renderer explicitly asked
    // for; passed to the will-download handler so only announced downloads
    // auto-save (see download-expectations.js).
    downloadExpectations = null,
    // Connected computers' native menu actions (connected-computers-desktop.js),
    // or null when this launch runs a local gateway that owns sharing, or when
    // the preview is off.
    desktopSharing = null,
    // Whether the connected-computers preview was on at launch: a local
    // gateway's window then gets the preload's native-consent bridge.
    connectedComputersBridge = false,
  } = options || {};

  if (!electron) throw new Error("createWindowLifecycle: electron is required");
  if (!store) throw new Error("createWindowLifecycle: store is required");
  if (!backendUrl) throw new Error("createWindowLifecycle: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createWindowLifecycle: port is required");
  if (typeof mintLocalToken !== "function") {
    throw new Error("createWindowLifecycle: mintLocalToken is required");
  }
  // Shared by every window, and kept in memory only, so a "Don't ask again"
  // ends when the app quits.
  const rememberedEditorTargets = new Set();
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
    reconcileZoomChrome,
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
    return require("./runtime/window/contents").setupWindowContents({
      get app() { return app; },
      get armScrollTouch() { return armScrollTouch; },
      get armSplashHistoryClear() { return armSplashHistoryClear; },
      get armSwipeHistory() { return armSwipeHistory; },
      get attachBrowserPanels() { return attachBrowserPanels; },
      get attachContextMenu() { return attachContextMenu; },
      get attachHtmlFullScreen() { return attachHtmlFullScreen; },
      get BROWSER_PARTITION() { return BROWSER_PARTITION; },
      get browserPanelUnderCursor() { return browserPanelUnderCursor; },
      get connectedComputersBridge() { return connectedComputersBridge; },
      get dirname() { return __dirname; },
      get createNavigationGuard() { return createNavigationGuard; },
      get createRedirectGuard() { return createRedirectGuard; },
      get createWindowOpenHandler() { return createWindowOpenHandler; },
      get DASHBOARD_SETTLE_MS() { return DASHBOARD_SETTLE_MS; },
      get dashboardRequestHeaders() { return dashboardRequestHeaders; },
      get defaultedPort() { return defaultedPort; },
      get dialog() { return dialog; },
      get DISPLAY_SETTLE_MS() { return DISPLAY_SETTLE_MS; },
      get downloadExpectations() { return downloadExpectations; },
      get FULLSCREEN_SETTLE_MS() { return FULLSCREEN_SETTLE_MS; },
      get getRemoteHostDefaultNameForUrl() { return getRemoteHostDefaultNameForUrl; },
      get glog() { return glog; },
      get injectLinuxCaptionControls() { return injectLinuxCaptionControls; },
      get IS_MAC() { return IS_MAC; },
      get IS_WIN() { return IS_WIN; },
      get isGatewayLocalForWindow() { return isGatewayLocalForWindow; },
      get LINUX_FRAMELESS() { return LINUX_FRAMELESS; },
      get Menu() { return Menu; },
      get menuBarCanOverlap() { return menuBarCanOverlap; },
      get openNewConnectionWindow() { return openNewConnectionWindow; },
      get path() { return path; },
      get platform() { return platform; },
      get promptRemoteHost() { return promptRemoteHost; },
      get readNaturalScrolling() { return readNaturalScrolling; },
      get refreshToken() { return refreshToken; },
      get registerCaptureSurface() { return registerCaptureSurface; },
      get rememberedEditorTargets() { return rememberedEditorTargets; },
      get renameCurrentWindow() { return renameCurrentWindow; },
      get screen() { return screen; },
      get shell() { return shell; },
      get store() { return store; },
      get syncNativeTheme() { return syncNativeTheme; },
      get systemPreferences() { return systemPreferences; },
      get trackZoomChrome() { return trackZoomChrome; },
      get WebContentsView() { return WebContentsView; },
      get windowForWebContents() { return windowForWebContents; },
      get wireWillDownloadOnce() { return wireWillDownloadOnce; },
    }, ...arguments);
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
    // Entering fullscreen is persisted after the debounce, not inside the emit:
    // whether a page or the user raised the window is only known once both of
    // Electron's events for the raise have landed, and their order differs by
    // platform.
    mainWindow.on("enter-full-screen", persistDebounced);
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
    // same local-then-remote token order used at boot. A recovery reload keeps
    // its `safe=1` through the retry: a bare URL here reopens the remembered
    // chat, and the crash that chat caused repeats.
    let retrySafe = false;
    const onNavigate = createTokenRetryHandler(async () => {
      let tokenValue = await mintLocalToken(backendUrl);
      if (!tokenValue) {
        ({ token: tokenValue } = await fetchRemoteToken(port));
      }
      if (tokenValue && !mainWindow.isDestroyed()) {
        const target = retrySafe ? withSafeReload(backendUrl) : backendUrl;
        await loadDashboardWithToken(mainWindow.webContents, target, tokenValue);
      }
    });
    mainWindow.webContents.on("did-navigate", (_event, url, httpCode) => {
      retrySafe = hasSafeReload(url);
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

    function reloadDashboard() {
      if (mainWindow.isDestroyed()) return;
      (async () => {
        let tokenValue = await mintLocalToken(backendUrl);
        if (!tokenValue) {
          ({ token: tokenValue } = await fetchRemoteToken(port));
        }
        if (mainWindow.isDestroyed()) return;
        // `safe=1`: do not reopen the chat that may have caused the crash.
        await loadDashboardWithToken(mainWindow.webContents, withSafeReload(backendUrl), tokenValue);
      })().catch((error) => {
        glog(`renderer recovery reload failed: ${error && error.message}`);
      });
    }

    const rendererExhaustedNotice = createRendererExhaustedNotice({
      dialog,
      openExternal: (url) => shell.openExternal(url),
      requestQuit,
      // "Try again" in the dialog: a fresh reload budget, then one more load.
      // `rendererRecovery` is created just below; this runs only after the
      // dialog it belongs to, long after both exist.
      retry: () => {
        rendererRecovery.reset();
        reloadDashboard();
      },
      dashboardLoaded,
      parentWindow: () => mainWindow,
      isQuitting,
      platform,
      logPath,
      log: glog,
    });

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
      reload: reloadDashboard,
      // The budget is spent: say so on screen. A dead renderer leaves the
      // window black with no text, so this is the only readable error the
      // user gets (renderer-exhausted-notice.js). Never awaited here: the
      // dialog outlives this event handler.
      onGiveUp: (info) => {
        void rendererExhaustedNotice.show(info);
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
        const outcome = endHungRenderer(mainWindow.webContents, {
          underTranslation: process.platform === "darwin" && app.runningUnderARM64Translation === true,
        });
        if (outcome !== "force-crashed") glog(`hung renderer under Rosetta: ${outcome} by pid`);
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

    mainWindow.on("close", createCloseHandler({
      isQuitting,
      getAction: () => getCloseAction(store),
      cancelPendingHide: () => {
        glog("close: quitting (closeAction=quit)");
        cancelPendingTrayHide(mainWindow);
      },
      requestQuit,
      hide: () => {
        // macOS must leave its native fullscreen Space before hiding or the Space
        // becomes an orphaned black surface, and the hide that follows is an
        // app-level one: AppKit may have left a full-display overlay on screen
        // that only `app.hide()` can reach (see hide-to-tray.js).
        glog(`close: hiding to tray (fullScreen=${mainWindow.isFullScreen()})`);
        // A hand-edited config can switch the close button to hide while the
        // icon is off; bring the icon back before the window goes, so it is
        // never hidden with no way back.
        syncTray();
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
      },
      persist: () => {
        if (saveTimer) {
          clearTimeout(saveTimer);
          saveTimer = null;
        }
        persist();
      },
    }));
    mainWindow.on("minimize", minimizeToTray.minimizeHandler(mainWindow));

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

  // A focused show answers an explicit user gesture (a relaunch, the tray's
  // Show item, a notification click) that usually reaches the app while another
  // app is frontmost. On macOS 14+ `win.focus()` alone orders the window in
  // without activating the app, so the other app keeps the keyboard and stays
  // in front; steal activation the way the summon hotkey and the gateway-error
  // reveal do. A show without `focus` never activates the app.
  function activateApp() {
    if (!IS_MAC || typeof app.focus !== "function") return;
    try {
      app.focus({ steal: true });
    } catch {
      /* best effort — the window is already shown and focused */
    }
  }

  function showMainWindow({ focus = false } = {}) {
    if (!mainWindow || mainWindow.isDestroyed()) return false;
    cancelPendingTrayHide(mainWindow);
    unhideApp();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    if (focus) {
      mainWindow.focus();
      activateApp();
    }
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

  // Create or remove the tray icon to match the stored choice (tray-icon.js).
  function syncTray() {
    const { show } = getTrayIcon(store, { isMac: IS_MAC });
    if (show && !tray) {
      createTray();
    } else if (!show && tray) {
      tray.destroy();
      tray = null;
    }
  }

  // Returns the stored value, which the Settings switch renders. The close
  // button decides whether the tray icon may be hidden, so the icon follows.
  function setCloseActionAndLog(value) {
    const written = setCloseAction(store, value);
    glog(`closeAction set to ${written}`);
    syncTray();
    return written;
  }

  function setTrayIconAndLog(show) {
    const state = setTrayIcon(store, show, { isMac: IS_MAC });
    glog(`showTrayIcon set to ${state.show}`);
    syncTray();
    return state;
  }

  // Minimize to tray needs a tray to bring the window back (minimize-to-tray.js).
  const minimizeToTray = createMinimizeToTray({ store, platform, trayShown: () => tray !== null, log: glog });

  async function promptRemoteHost() {
    return require("./runtime/window/remote-host").promptRemoteHost({
      get BaseWindow() { return BaseWindow; },
      get BrowserWindow() { return BrowserWindow; },
      get DEFAULT_REMOTE_BIN() { return DEFAULT_REMOTE_BIN; },
      get DEFAULT_REMOTE_PATH() { return DEFAULT_REMOTE_PATH; },
      get defaultedPort() { return defaultedPort; },
      get dialog() { return dialog; },
      get getModalCSS() { return getModalCSS; },
      get getRemoteHostConfigForUrl() { return getRemoteHostConfigForUrl; },
      get IS_WINDOWS() { return IS_WINDOWS; },
      get mainWindow() { return mainWindow; }, set mainWindow(value) { mainWindow = value; },
      get parseRemoteCrewFields() { return parseRemoteCrewFields; },
      get port() { return port; },
      get portIsSchemeDefault() { return portIsSchemeDefault; },
      get remoteCrewDraft() { return remoteCrewDraft; },
      get retireLegacyEmptyPortHost() { return retireLegacyEmptyPortHost; },
      get saveRemoteCrewConfig() { return saveRemoteCrewConfig; },
      get setRemoteHostConfig() { return setRemoteHostConfig; },
      get store() { return store; },
      get syncTunnel() { return syncTunnel; },
      get TUNNEL_OPTION_LABEL() { return TUNNEL_OPTION_LABEL; },
      get tunnelOptionHint() { return tunnelOptionHint; },
    }, ...arguments);
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
      await loadDashboardWithToken(win.webContents, targetUrl, tokenValue);
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
      // which a tunnelled crew must never allow. So direct the user to
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
        const target = dashboardUrlFor(connectionBackendUrl, retryTarget);
        await loadDashboardWithToken(connWin.webContents, target, tokenValue);
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
      // together and every native caption inset must be reconciled — the macOS
      // traffic lights AND the Windows overlay height, both of which track zoom.
      for (const win of BaseWindow.getAllWindows()) {
        reconcileZoomChrome(win);
      }
    };
  }

  // The last sharing-menu visibility the connected-computers host asked for.
  let sharingMenu = null;
  function buildApplicationMenu() {
    appMenu = Menu.buildFromTemplate(buildMenuTemplate({
      isMac: IS_MAC,
      appName: app.name,
      openSettings: () => openSettingsPage(),
      openAbout: () => openSettingsPage("about"),
      // A relay Crew tab in front takes Reload, as its own window did,
      // instead of the dashboard reloading and closing every tab.
      reload: () => {
        const pane = shownRelayPane(BaseWindow.getFocusedWindow());
        if (pane) { pane.menuReload({ ignoreCache: false }); return; }
        const wc = focusedDashboardWebContents();
        if (wc) wc.reload();
      },
      forceReload: () => {
        const pane = shownRelayPane(BaseWindow.getFocusedWindow());
        if (pane) { pane.menuReload({ ignoreCache: true }); return; }
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
      toggleFocusMode: () => {
        const win = focusedDashboardWindow();
        const wc = win && win._mcView.webContents;
        if (wc && !wc.isDestroyed()) wc.send("focus-mode:toggle");
      },
      openNewSessionWindow: () => openNewSessionWindow(),
      openNewConnectionWindow: () => openNewConnectionWindow(),
      renameCurrentWindow: () => renameCurrentWindow(),
      promptRemoteHost: () => promptRemoteHost(),
      refreshToken: () => refreshToken(),
      openConfigFile: () => openPathHardened(shell, store.path),
      ...(desktopSharing ? {
        shareThisComputer: () => { void desktopSharing.shareThisComputer(); },
        showSharingStatus: () => { void desktopSharing.showSharingStatus(); },
        turnOffSharing: () => { void desktopSharing.turnOffSharing(); },
      } : {}),
    }));
    Menu.setApplicationMenu(appMenu);
    if (sharingMenu) setSharingMenu(sharingMenu);
    return appMenu;
  }

  function setSharingMenu(visibility) {
    sharingMenu = visibility;
    applySharingVisibility(appMenu || Menu.getApplicationMenu(), visibility);
  }

  // The registrar passes the checked sender locality to keep remote menu items disabled.
  function menuItems(sender, id, senderIsLocal) {
    if (!(IS_WINDOWS || LINUX_FRAMELESS) || !WINDOWS_TITLEBAR_MENU_IDS.has(id)) return [];
    const menu = appMenu || Menu.getApplicationMenu();
    const item = menu && menu.getMenuItemById(id);
    const win = windowForWebContents(sender);
    if (!item || !item.submenu || !win || win.isDestroyed()) return [];
    return serializeMenuItems(item, senderIsLocal === true);
  }

  // Returns whether the click DISPATCHED. `app-menu:execute` answers the
  // renderer with it, and the renderer refetches its rows on a `false` — so a
  // bare `return` here (reporting `undefined` for a guard refusal) would
  // silently become "dispatched" at the strict check on the IPC boundary. Every
  // exit says so explicitly.
  function executeMenu(sender, id, index, senderIsLocal) {
    if (
      !(IS_WINDOWS || LINUX_FRAMELESS)
      || !WINDOWS_TITLEBAR_MENU_IDS.has(id)
      || !Number.isInteger(index)
    ) return false;
    const menu = appMenu || Menu.getApplicationMenu();
    const topLevelItem = menu && menu.getMenuItemById(id);
    const win = windowForWebContents(sender);
    if (!win || win.isDestroyed()) return false;
    // sender is the focused dashboard WebContents: titlebar menu interaction
    // itself gives it focus, which is what Electron role items expect.
    //
    // `senderIsLocal` is computed once at the IPC boundary in
    // ipc-registrar.js via `assertLocalDashboard` (origin + local-gateway +
    // primary-port-owner). Forwarded here with strict `=== true` so any
    // non-boolean (undefined from a caller that forgot the arg, a truthy
    // proxy, a stringy value) drops to the LOCAL_ONLY refusal branch.
    return executeMenuItem(topLevelItem, index, win, sender, senderIsLocal === true);
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
    if (owner && owner._mcDestroyBrowserPanel) {
      owner._mcDestroyBrowserPanel(panel.id);
    }
    return { ...state, open: false, visible: false };
  }

  function browserGetState(sender, panelId) {
    const panel = panelForSender(sender, panelId, { create: false });
    return panel ? panel.manager.getState() : null;
  }

  // Human-initiated element annotation on the page the user is looking at.
  // Served through executeJavaScript/capturePage; no debugger attachment.
  // Closed op set.
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
    openNewSessionWindow,
    openNewConnectionWindow,
    promptRemoteHost,
    refreshToken,
    renameCurrentWindow,
    setupWindowContents,
    getMainWindow: () => mainWindow,
    getTray: () => tray,
    closeAction: {
      get: () => getCloseAction(store),
      set: setCloseActionAndLog,
    },
    syncTray,
    trayIcon: {
      get: () => getTrayIcon(store, { isMac: IS_MAC }),
      set: setTrayIconAndLog,
    },
    minimizeToTray: { get: minimizeToTray.get, set: minimizeToTray.set },
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
      setSharingMenu,
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
  dashboardRequestHeaders,
};
