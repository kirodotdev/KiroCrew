"use strict";

function setupWindowContents(deps, win, windowBackendUrl) {
    const windowPort = (0, deps.defaultedPort)(windowBackendUrl);
    let customName = null;
    // isGatewayLocalForWindow reads this.
    win._mcBackendUrl = windowBackendUrl;

    const view = new deps.WebContentsView({
      webPreferences: {
        preload: deps.path.join(deps.dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        // Launch-time opt-in; gateway locality is rechecked by the preload on each load.
        additionalArguments: [
          ...(deps.LINUX_FRAMELESS ? ["--kc-linux-frameless"] : []),
          ...(deps.connectedComputersBridge === true ? ["--kc-connected-computers"] : []),
        ],
      },
    });
    // The preload asks this on every load, so a crew configured or cleared for
    // the port applies from the next load. The ask is synchronous: the listener
    // is on this view's own WebContents before anything loads, and every path
    // answers, so the renderer never waits on a reply that does not come.
    view.webContents.ipc.on("dashboard:is-remote-gateway", (event) => {
      let remote = true;
      try {
        remote = !(0, deps.isGatewayLocalForWindow)(win);
      } catch (err) {
        (0, deps.glog)(`dashboard:is-remote-gateway: ${err && err.message}`);
      }
      event.returnValue = remote;
    });
    view.setBackgroundColor("#00000000");
    win.contentView.addChildView(view);

    // Arm before any handoff. Boot, reconnect and token-prompt pages share this
    // WebContents; leaving one in history lets mouse Back reach a dead-end shell.
    (0, deps.armSplashHistoryClear)(view.webContents, {
      isAlive: () => !win.isDestroyed() && !view.webContents.isDestroyed(),
      log: deps.glog,
    });
    // Mouse software such as Logitech's Options sends Back/Forward to macOS apps
    // as swipes; the dashboard, or the browser panel under the cursor, takes each
    // as a history step.
    (0, deps.armSwipeHistory)(win, view.webContents, {
      isAlive: () => !win.isDestroyed() && !view.webContents.isDestroyed(),
      panelUnderCursor: () => (0, deps.browserPanelUnderCursor)(win, deps.screen.getCursorScreenPoint()),
    });
    // The trackpad's real finger-down / finger-up, so a two-finger drag in the
    // dashboard releases on the lift-off instead of a guessed gap (macOS only).
    // It also tells the page the Natural scrolling setting, which decides
    // which way the fingers moved for a given wheel sign.
    (0, deps.armScrollTouch)(view.webContents, {
      platform: deps.platform,
      isAlive: () => !win.isDestroyed() && !view.webContents.isDestroyed(),
      readNatural: () => (0, deps.readNaturalScrolling)(deps.systemPreferences),
    });

    // Teardown order is load-bearing: no embedded view may outlive the page it
    // is composited over. Close the dashboard WebContents only after releasing
    // every embedded panel.
    win.on("closed", () => {
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

    // `menuBarOverlap` tells the renderer whether to keep its menu-bar band
    // clear: false when macOS already placed the fullscreen window below the
    // menu bar (a notched built-in display).
    const sendFullScreen = () => {
      if (win.isDestroyed() || view.webContents.isDestroyed()) return;
      view.webContents.send("fullscreen-changed", win.isFullScreen(), {
        menuBarOverlap: (0, deps.menuBarCanOverlap)(win, deps.screen),
      });
    };

    // Fullscreen events can precede the window manager's final reflow. Keep the
    // immediate recompute (no flash where reflow is synchronous) and bounded
    // deferred passes, including the late backstop slow Linux WMs need. Each
    // pass re-sends the fullscreen state too, because the menu-bar verdict reads
    // the window's position, which is only final once the reflow lands.
    let fullscreenSettleTimers = [];
    const scheduleFullscreenSettle = () => {
      for (const timer of fullscreenSettleTimers) clearTimeout(timer);
      fullscreenSettleTimers = [
        ...deps.FULLSCREEN_SETTLE_MS.map((ms) => setTimeout(updateViewBounds, ms)),
        ...deps.FULLSCREEN_SETTLE_MS.map((ms) => setTimeout(sendFullScreen, ms)),
      ];
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

    // Electron raises the BaseWindow when a page element goes DOM-fullscreen.
    // Retain who raised it, so persistence never mistakes a video-raised
    // transition for the user's launch preference.
    win._mcHtmlFullScreen = (0, deps.attachHtmlFullScreen)({
      win,
      webContents: view.webContents,
    });

    win.on("show", updateViewBounds);
    win.on("restore", updateViewBounds);
    win.on("move", updateViewBounds);

    // A display rearrange does not reliably fire a "resize"/"move" on the
    // window, yet it changes the backing scale factor and content bounds. Bind
    // the screen-level display events and re-run the reflow on a settle
    // schedule so the child view tracks the new geometry instead of ghosting on
    // the old display. Paired with an unbind in "closed" below — the bind/unbind
    // discipline the pet-overlay leak (#4673) established: a screen listener
    // that outlives its window keeps firing against a destroyed view.
    let displaySettleTimers = [];
    const onDisplayReflow = () => {
      if (win.isDestroyed()) return;
      for (const timer of displaySettleTimers) clearTimeout(timer);
      displaySettleTimers = deps.DISPLAY_SETTLE_MS.map((ms) =>
        ms === 0 ? (updateViewBounds(), null) : setTimeout(updateViewBounds, ms),
      ).filter(Boolean);
    };
    const DISPLAY_EVENTS = ["display-metrics-changed", "display-added", "display-removed"];
    if (deps.screen && typeof deps.screen.on === "function") {
      for (const event of DISPLAY_EVENTS) deps.screen.on(event, onDisplayReflow);
    }
    win.on("closed", () => {
      for (const timer of displaySettleTimers) clearTimeout(timer);
      displaySettleTimers = [];
      if (deps.screen && typeof deps.screen.removeListener === "function") {
        for (const event of DISPLAY_EVENTS) deps.screen.removeListener(event, onDisplayReflow);
      }
    });

    view.webContents.on("did-finish-load", () => {
      updateViewBounds();
      sendFullScreen();
      setTimeout(updateViewBounds, deps.DASHBOARD_SETTLE_MS);
    });

    // BaseWindow has no webContents. Preserve the shell's compatibility alias
    // before any caller can load the splash or dashboard into this window.
    win.webContents = view.webContents;

    function applyTitle() {
      // Resolve the name through the URL so a title an older version pinned under
      // the empty key on a scheme-default port still renders until the record is
      // retired onto the resolved key. Reading `getRemoteHostConfig(store,
      // windowPort)` alone would miss it and the suffix would revert to `[:80]`.
      const remoteName = (0, deps.getRemoteHostDefaultNameForUrl)(deps.store, windowBackendUrl);
      if (!deps.IS_WIN) {
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
    // The dashboard SPA is a capture surface (the chat composer's snip and the
    // web-preview crop). Registered against the gateway origin THIS window was
    // opened on, so a secondary window pointed at a remote gateway is bound to
    // its own origin and never to a sibling's.
    (0, deps.registerCaptureSurface)(view.webContents, windowBackendUrl);
    win._mcView = view;

    // One native Preview view per dashboard panel (runtime/window/browser-panels.js).
    const browserPanels = (0, deps.attachBrowserPanels)(win, view, {
      WebContentsView: deps.WebContentsView,
      shell: deps.shell,
      partition: deps.BROWSER_PARTITION,
    });

    // The dashboard's own webContents, so the link block also gets the origin --
    // which is what turns a `/abs/path` link into a bare-path copy instead of a
    // useless localhost URL. The embedded browser panel passes no origin: an
    // arbitrary site's same-origin pathname is not a local file.
    (0, deps.attachContextMenu)(view.webContents, { getAppOrigin: () => windowBackendUrl });

    (0, deps.trackZoomChrome)(win, view);

    // Frameless macOS exposes a native system context menu on the drag region.
    win.on("system-context-menu", (event, point) => {
      event.preventDefault();
      deps.Menu.buildFromTemplate([
        { label: "Rename Window…", click: () => (0, deps.renameCurrentWindow)() },
        { label: "Set Remote Host…", click: () => (0, deps.promptRemoteHost)() },
        { label: "Refresh Token", click: () => (0, deps.refreshToken)() },
        { type: "separator" },
        { label: "New Connection Window…", click: () => (0, deps.openNewConnectionWindow)() },
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
      if (deps.IS_MAC || deps.IS_WIN || deps.LINUX_FRAMELESS) {
        view.webContents.insertCSS(`
          #electron-drag-bar {
            position: fixed;
            top: 0; left: 0; right: ${deps.IS_WIN ? "var(--mc-win-drag-reserve, 138px)" : deps.LINUX_FRAMELESS ? "108px" : "0"};
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
      if (deps.LINUX_FRAMELESS) (0, deps.injectLinuxCaptionControls)(win, view);

      view.webContents.executeJavaScript(
        `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`,
      ).then((bg) => {
        if (bg && !win.isDestroyed()) win.setBackgroundColor(bg);
      }).catch(() => {});
      (0, deps.syncNativeTheme)(view, win);
    });

    // Native themeSource is process-global, so a focused connection window must
    // refresh it from its own dashboard before native chrome is painted.
    win.on("focus", () => (0, deps.syncNativeTheme)(view, win));
    // On re-activation the platform re-resolves which child view receives
    // keystrokes and may pick a hidden browser view again; each panel heals
    // that by handing focus back to the dashboard view (see browser-view.js
    // header note 3). A visible or unfocused panel is left alone.
    win.on("focus", () => {
      for (const entry of browserPanels.values()) entry.manager.reclaimFocus();
    });

    // Same-origin windows remain in-app. Cross-origin web URLs and the audited
    // custom-scheme allowlist go to the OS; every other target fails closed.
    const windowOpenHandler = (0, deps.createWindowOpenHandler)({
      openExternal: (url) => deps.shell.openExternal(url),
      getAppOrigin: () => windowBackendUrl,
      confirmOpen: async (url) => {
        const { response, checkboxChecked } = await deps.dialog.showMessageBox(win, {
          type: "question",
          buttons: ["Open", "Cancel"],
          defaultId: 0,
          cancelId: 1,
          message: "Open this link in your editor?",
          detail: url,
          checkboxLabel: "Don't ask again for this machine until Kiro Crew quits",
        });
        return { open: response === 0, remember: checkboxChecked === true };
      },
      rememberedEditorTargets: deps.rememberedEditorTargets,
      log: deps.glog,
    });
    // An allowed same-origin window.open (the session pop-out) is a child window
    // Electron creates on its own. Give it, and each of its own children, the
    // same handler, or its external links open yet another in-app window.
    // A pane iframe can top-navigate the dashboard (or a pop-out) to its own
    // document, which would then hold the dashboard's session grants. Refuse
    // every renderer-started navigation off this window's own origin.
    const navigationGuard = (0, deps.createNavigationGuard)({
      getAppOrigin: () => windowBackendUrl,
      log: deps.glog,
    });
    const redirectGuard = (0, deps.createRedirectGuard)({
      getAppOrigin: () => windowBackendUrl,
      log: deps.glog,
    });
    const armWindowOpen = (contents) => {
      contents.setWindowOpenHandler(windowOpenHandler);
      contents.on("will-navigate", navigationGuard);
      contents.on("will-redirect", redirectGuard);
      contents.on("did-create-window", (child) => armWindowOpen(child.webContents));
    };
    armWindowOpen(view.webContents);

    // Do not leak the dashboard URL/token as a Referer (dashboardRequestHeaders).
    // This listener remains attached at the same per-window setup point; moving
    // it to a one-time global policy would be a behavior change.
    view.webContents.session.webRequest.onBeforeSendHeaders((details, callback) => {
      callback({ requestHeaders: (0, deps.dashboardRequestHeaders)(details) });
    });

    // Give renderer-triggered downloads (e.g. the Portability "Download Export
    // (.zip)" button's <a download> blob) a real save path. The dashboard view
    // is a WebContentsView under a BaseWindow, which Chromium cannot parent a
    // native "Save As" dialog to, so without this the DownloadItem never
    // resolves a path and nothing lands on disk (issue #13047). Attached once
    // per session (every window shares the default session), so N windows do
    // not stack N handlers.
    //
    // Because the single handler is shared by every window, it must resolve the
    // dashboard origin for the window that OWNS each download rather than
    // capturing one window's URL: a later window on a different gateway (the
    // remote-host secondary windows main supports) would otherwise fail the
    // origin checks and its export would never land. `resolveDashboardUrl` maps
    // the download's own `webContents` back to its window's `_mcBackendUrl`;
    // `dashboardUrl` is the fallback for the first window / an unmatched sender.
    (0, deps.wireWillDownloadOnce)(view.webContents.session, {
      app: deps.app,
      log: deps.glog,
      dashboardUrl: windowBackendUrl,
      resolveDashboardUrl: (wc) => (0, deps.windowForWebContents)(wc)?._mcBackendUrl,
      pending: deps.downloadExpectations,
      platform: deps.platform,
    });

    return view;
  }

module.exports = { setupWindowContents };
