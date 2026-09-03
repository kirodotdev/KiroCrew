"use strict";

const { clampBadgeCount } = require("./badge");
const {
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
} = require("./local-gateway");
const {
  DEFAULT_GLOBAL_HOTKEY,
  createSummonHandler,
  bindGlobalHotkey: bindStoredGlobalHotkey,
  unregisterGlobalHotkey,
  currentGlobalHotkey,
  setGlobalHotkeyLogger,
} = require("./global-hotkey");
const { initAutoUpdate } = require("./auto-update");
const { makeUpdaterLogger } = require("./update-logger");
const { detectWsl2 } = require("./wsl-detection");

/**
 * Register the Electron shell's renderer bridges without taking ownership of
 * either window or gateway state. Electron is injected so this boundary can be
 * exercised with a small fake runtime rather than importing the main process.
 */
function createIpcRegistrar({
  electron,
  store,
  windows,
  gateway,
  backendUrl,
  port,
  detectWsl = detectWsl2,
  glog,
  // Close/reopen the Crew Companion overlay around an update install so it does
  // not float orphaned over the vanished dashboard during the quit handoff.
  // Optional and no-op by default: an updater path must never depend on the
  // companion being wired.
  closeCrewCompanionForUpdate = () => {},
  reopenCrewCompanionAfterUpdate = () => {},
} = {}) {
  if (!electron) throw new Error("createIpcRegistrar: electron is required");
  if (!store) throw new Error("createIpcRegistrar: store is required");
  if (!windows) throw new Error("createIpcRegistrar: windows is required");
  if (!gateway) throw new Error("createIpcRegistrar: gateway is required");
  if (!backendUrl) throw new Error("createIpcRegistrar: backendUrl is required");
  if (!Number.isInteger(port)) throw new Error("createIpcRegistrar: port is required");
  if (typeof detectWsl !== "function") {
    throw new Error("createIpcRegistrar: detectWsl must be a function");
  }

  const {
    app,
    Notification,
    ipcMain,
    webContents,
  } = electron;
  const log = typeof glog === "function" ? glog : (() => {});

  let shellRegistered = false;
  let updaterRegistered = false;
  let updater = null;

  setGlobalHotkeyLogger(log);
  const summonDashboard = createSummonHandler({
    getWindow: () => (
      typeof windows.getSummonWindow === "function"
        ? windows.getSummonWindow()
        : (windows.focusedDashboardWindow() || windows.getMainWindow() || null)
    ),
    createWindow: () => windows.createMainWindow(),
    // A global shortcut fires while another app is frontmost. On macOS the
    // window rises without keyboard focus unless the app steals activation.
    focusApp: () => {
      if (process.platform === "darwin") app.focus({ steal: true });
    },
  });

  /**
   * Build the application menu, harden both Electron sessions, and register
   * every renderer bridge except update:*.
   *
   * This must run after app.whenReady() but before createMainWindow(): session
   * permission handlers need to exist before an untrusted view can be created.
   */
  function registerShell() {
    if (shellRegistered) return summonDashboard;

    // Session policy is window-owned because it knows which WebContents are
    // untrusted and which persistent partition hosts embedded browser pages.
    if (windows.security && typeof windows.security.configureSession === "function") {
      windows.security.configureSession();
    } else {
      // Transitional alias for callers composed against the flat façade name.
      windows.configureSessionSecurity();
    }

    // buildApplicationMenu also installs the resulting native Menu. Its click
    // callbacks stay inside the window façade, where focused-window resolution
    // and traffic-light/zoom reconciliation live.
    windows.menu.buildApplicationMenu();

    ipcMain.handle("app-menu:items", (event, id) =>
      windows.menu.items(event.sender, id));
    ipcMain.on("app-menu:execute", (event, id, index) =>
      windows.menu.execute(event.sender, id, index));
    ipcMain.on("dev-mode-changed", (_event, enabled) =>
      windows.menu.setDevMode(enabled));

    // The shortcuts UI reports what is ACTUALLY bound. Registration may have
    // fallen back to the default or degraded to no shortcut at all.
    ipcMain.handle("global-hotkey:get", () => ({
      accelerator: currentGlobalHotkey(),
      default: DEFAULT_GLOBAL_HOTKEY,
    }));

    ipcMain.on("theme-accent-changed", (_event, hex) =>
      windows.chrome.setThemeAccent(hex));
    ipcMain.on("focus-mode-chrome", (event, visible) =>
      windows.chrome.focusMode(event.sender, visible));
    ipcMain.on("window-control", (event, action) =>
      windows.chrome.windowControl(event.sender, action));
    ipcMain.on("theme-mode-changed", (_event, pref) =>
      windows.chrome.setThemeMode(pref));
    ipcMain.on("titlebar-overlay-theme", (_event, mode) =>
      windows.chrome.setTitlebarMode(mode));

    // Electron no-ops setBadgeCount on unsupported platforms. Keep the clamp in
    // the trusted process so a malformed renderer value cannot set an absurd
    // badge on platforms which do support it.
    ipcMain.on("badge:set", (_event, count) => {
      app.setBadgeCount(clampBadgeCount(count));
    });

    // This setting deliberately has next-launch semantics. Toggling it never
    // starts or stops the gateway which owns the current session.
    ipcMain.handle("local-gateway:get", () => isLocalGatewayEnabled(store));
    ipcMain.handle("local-gateway:set", (_event, enabled) =>
      setLocalGatewayEnabled(store, enabled));

    // WSL2 host-runtime readout, rendered read-only by the Host runtime card.
    // Sender-restricted ON PURPOSE: the discovery result is a fact about THIS
    // machine, so only WebContents served by this shell's own local gateway may
    // enumerate it. Connection windows pointed at a remote gateway share this
    // preload and must get a rejection instead of the host's distro inventory.
    ipcMain.handle("wsl:detect", async (event) => {
      // Gate 1 — document origin: the page must have been served from this
      // shell's own fixed primary gateway URL.
      let origin = "";
      try {
        origin = new URL(event.senderFrame.url).origin;
      } catch {
        // about:blank, a malformed URL, or a torn-down frame is not a dashboard.
      }
      if (origin !== backendUrl) {
        // Logged, not silent: the UI renders both a sender rejection and a
        // missing WSL install as unavailable, so diagnostics must distinguish
        // those two causes.
        log(`wsl:detect rejected for sender origin ${origin || "(unreadable)"}`);
        throw new Error("wsl:detect is restricted to the local dashboard");
      }

      // Gate 2 — the sending window's gateway must genuinely be on this
      // machine. Loopback is necessary but insufficient because a configured
      // remote gateway reached through an SSH tunnel also presents as localhost.
      const owner = windows.windowForWebContents(event.sender);
      if (!windows.security.isGatewayLocalForWindow(owner)) {
        log("wsl:detect rejected for a sender window without a local gateway");
        throw new Error("wsl:detect is restricted to the local dashboard");
      }

      // Gate 3 — positive listener ownership. A MANUAL SSH tunnel can occupy
      // the primary local port without appearing in the remote-host config, so
      // only this shell's gateway or its service manager is accepted. Foreign
      // holders, an unbound port, and an unavailable owner probe all fail
      // closed. Gate 1 already fixed the sender to backendUrl, which is why the
      // supervisor's fixed primary-port probe is also probing the sender's port.
      const portOwner = await gateway.probePrimaryPortOwner();
      if (portOwner !== "kirocrew" && portOwner !== "service") {
        log(
          `wsl:detect rejected: :${port} held by ${portOwner}, `
          + "not this shell's gateway",
        );
        throw new Error("wsl:detect is restricted to the local dashboard");
      }

      return detectWsl();
    });

    ipcMain.handle("zoom:get", (event) =>
      windows.chrome.getZoom(event.sender));
    ipcMain.handle("zoom:set", (event, factor) =>
      windows.chrome.setZoom(event.sender, factor));
    ipcMain.handle("zoom:step", (event, direction) =>
      windows.chrome.stepZoom(event.sender, direction));

    // The window façade resolves every request from event.sender and keeps the
    // native panel/control-plane internals private to that owning window.
    ipcMain.handle("browser:open", (event, panelId, url) =>
      windows.browser.open(event.sender, panelId, url));
    ipcMain.handle("browser:navigate", (event, panelId, url) =>
      windows.browser.navigate(event.sender, panelId, url));
    ipcMain.handle("browser:set-bounds", (event, panelId, rect, viewport) =>
      windows.browser.setBounds(event.sender, panelId, rect, viewport));
    ipcMain.handle("browser:set-overlay", (event, panelId, active) =>
      windows.browser.setOverlay(event.sender, panelId, active));
    ipcMain.handle("browser:set-inactive", (event, panelId, value) =>
      windows.browser.setInactive(event.sender, panelId, value));
    ipcMain.handle("browser:close", (event, panelId) =>
      windows.browser.close(event.sender, panelId));
    ipcMain.handle("browser:get-state", (event, panelId) =>
      windows.browser.getState(event.sender, panelId));
    ipcMain.handle("browser:track-session", (event, panelId, tracked) =>
      windows.browser.trackSession(event.sender, panelId, tracked));
    ipcMain.handle("browser:set-agent-act", (event, panelId, enabled) =>
      windows.browser.setAgentAct(event.sender, panelId, enabled));
    ipcMain.handle("browser:set-control-owner", (event, panelId, requested) =>
      windows.browser.setControlOwner(event.sender, panelId, requested));
    ipcMain.handle("browser:get-control", (event, panelId) =>
      windows.browser.getControl(event.sender, panelId));
    // Deliberately remains a closed verb dispatcher inside window-lifecycle;
    // this bridge never exposes a raw CDP method to the renderer.
    ipcMain.handle("browser:control", (event, panelId, op, args) =>
      windows.browser.control(event.sender, panelId, op, args));

    ipcMain.on("mic:denied", () => windows.security.micDenied());

    // Diagnostics accept only the sender identity and payload. The window
    // owner enforces the primary-renderer attribution rule before buffering.
    ipcMain.on("memory-sample", (event, payload) =>
      windows.diagnostics.memorySample(event.sender, payload));

    shellRegistered = true;
    return summonDashboard;
  }

  /**
   * Bind only after createMainWindow(). Binding earlier lets a keypress during
   * boot race window creation and produce a second dashboard.
   */
  function bindGlobalHotkey() {
    return bindStoredGlobalHotkey(store.get("globalHotkey"), summonDashboard);
  }

  function broadcastUpdateState(payload) {
    try {
      for (const contents of webContents.getAllWebContents()) {
        if (!contents.isDestroyed()) {
          try {
            contents.send("update-state", payload);
          } catch {
            // The view disappeared between enumeration and dispatch.
          }
        }
      }
    } catch {
      // Updating is auxiliary; an unavailable WebContents registry is benign.
    }
  }

  /**
   * Initialize auto-update and register update:* IPC.
   *
   * Call this after the main window exists but BEFORE awaiting gateway.start()
   * or gateway.connect(). preload exposes updateAPI immediately, so a slow or
   * failed boot must not leave a visible button with no IPC handler behind it.
   */
  function registerUpdater() {
    if (updaterRegistered) return updater;

    // FAIL-OPEN: updater initialization is auxiliary and must never gate the
    // gateway. Because this runs before the awaited boot, any thrown require,
    // feed parse, or updater construction error is converted to a disabled
    // handle; all update:* handlers still register against that handle.
    try {
      updater = initAutoUpdate({
        app,
        // electron-updater's AppUpdater, NOT Electron's built-in autoUpdater:
        // it generates/verifies feed metadata and also supports Linux.
        autoUpdater: require("electron-updater").autoUpdater,
        dialog: electron.dialog,
        Notification,
        getFlavor: () => "stable",
        getChannelPreference: () => store.get("updateChannel", ""),
        getAutoDownloadPreference: () =>
          store.get("autoDownloadUpdates", true) !== false,
        notifyUpdateFound: (version, { autoDownload = false } = {}) => {
          if (!version || store.get("lastNudgedVersion", "") === version) return;
          store.set("lastNudgedVersion", version);
          try {
            const notification = new Notification({
              title: `${app.name} update available`,
              body: autoDownload
                ? `Version ${version} is downloading and will install the next time you quit. `
                  + "Manage in Settings > About."
                : `Version ${version} is ready. Open Settings > About to download and install.`,
            });
            notification.on("click", () => {
              // Reveal through the façade so a pending fullscreen/tray hide is
              // cancelled before restore/show/focus; otherwise the click can
              // appear to work and then immediately hide the window again.
              windows.showMainWindow({ focus: true });
            });
            notification.show();
          } catch {
            // Native notifications are optional; discovery still succeeds.
          }
        },
        stopGateway: () => gateway.stopGracefully(),
        // WHY this hook precedes stopGateway: it closes the watchdog race before
        // the intentional shutdown and disarms probing throughout bundle swap.
        // auto-update invokes onInstallDispatched before awaiting stopGateway;
        // keep these as separate callbacks so that ordering remains explicit.
        // Closing the companion overlay here — before the gateway stops — keeps a
        // reconcile tick from reopening it in the window where it is still up.
        onInstallDispatched: () => {
          gateway.onInstallDispatched();
          closeCrewCompanionForUpdate();
        },
        // WHY failure recovery is active: dispatch stopped both the watchdog and
        // gateway. A failed swap does not quit, so nothing else can restore the
        // dashboard; the supervisor clears the flag, respawns, reconnects, and
        // re-arms liveness in that order. The companion closed at dispatch is
        // reopened to match — its loop self-heals once the restored gateway answers.
        onInstallFailed: () => {
          gateway.onInstallFailed();
          reopenCrewCompanionAfterUpdate();
        },
        onUpdateState: broadcastUpdateState,
        log: makeUpdaterLogger(log),
      });
    } catch (error) {
      log(
        "auto-update init failed — continuing WITHOUT auto-update: "
        + ((error && error.stack) || error),
      );
      updater = {
        check: () => {},
        download: async () => {},
        install: async () => {},
        getInfo: () => ({
          version: app.getVersion(),
          packaged: !!app.isPackaged,
        }),
        disabled: "init-failed",
      };
    }

    // disabled is a sibling on every updater handle, not part of getInfo().
    // Merge it once so Settings > About can render why updates are unavailable.
    const updaterInfo = () => ({
      ...updater.getInfo(),
      disabled: updater.disabled,
    });

    ipcMain.handle("update:get-info", () => updaterInfo());
    ipcMain.handle("update:check", () => {
      updater.check();
      return { ok: true };
    });
    ipcMain.handle("update:download", () => {
      updater.download();
      return { ok: true };
    });
    ipcMain.handle("update:install", async () => {
      await updater.install();
      return { ok: true };
    });
    ipcMain.handle("update:set-channel", (_event, channel) => {
      const next = typeof channel === "string" ? channel : "";
      if (next !== "" && next !== "insider" && next !== "stable") {
        return { ok: false, error: `invalid channel: ${next}` };
      }
      store.set("updateChannel", next);
      updater.check();
      return { ok: true, info: updaterInfo() };
    });
    ipcMain.handle("update:set-auto-download", (_event, enabled) => {
      if (typeof enabled !== "boolean") {
        return { ok: false, error: `invalid value: ${typeof enabled}` };
      }
      store.set("autoDownloadUpdates", enabled);
      if (enabled) updater.check();
      return { ok: true, info: updaterInfo() };
    });

    updaterRegistered = true;
    return updater;
  }

  return Object.freeze({
    registerShell,
    bindGlobalHotkey,
    unregisterGlobalHotkey,
    registerUpdater,
    summonDashboard,
  });
}

module.exports = { createIpcRegistrar };
