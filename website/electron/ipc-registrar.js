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
  requestedGlobalHotkey,
  setGlobalHotkeyLogger,
} = require("./global-hotkey");
const { initAutoUpdate } = require("./auto-update");
const { requiredUpdateChannelFor } = require("./runtime/update/policy-channel");
const { makeUpdaterLogger } = require("./update-logger");
const { createUpdateArm } = require("./runtime/update/update-arm");
const { createGatewayArm } = require("./runtime/update/gateway-arm");
const { detectWsl2 } = require("./wsl-detection");
const { crashNoticeSummary } = require("./crash-collector");
const { PREFIX: PANE_ASSETS_PREFIX, purgeableOrigin } = require("./pane-asset-journal");
const { safeFilename } = require("./download-handler");
const { saveThemeSplash } = require("./theme-splash");
const { createRelayCrewOpener, isEndpointId } = require("./relay-crew-open");
const { requestRelayCrewToken } = require("./relay-crew-client");
const { openRelayCrewWindow, openRelayCrewPane, createTokenHolder, partitionCleared } = require("./relay-crew-window");
const { normalizeBounds, deriveScale, scaleRect } = require("./browser-view");
const { createRelayCrewRenewal } = require("./relay-crew-renewal");
const { createRelayCrewHandoff } = require("./relay-crew-handoff");

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
  crashScan = null,
  glog,
  // Close/reopen the Crew Companion overlay around an update install so it does
  // not float orphaned over the vanished dashboard during the quit handoff.
  // Optional and no-op by default: an updater path must never depend on the
  // companion being wired.
  closeCrewCompanionForUpdate = () => {},
  reopenCrewCompanionAfterUpdate = () => {},
  // Shared single-use registry of downloads the renderer explicitly announced,
  // consumed by the will-download handler (see download-expectations.js). When
  // absent the download:expect channel is simply not registered.
  downloadExpectations = null,
  // The terminal-app allowlist and its grant-gated launcher. Injected so the
  // channel can be exercised without probing real install paths.
  externalTerminal = require("./external-terminal"),
  // Native consent for connected computers (29949); injected for tests.
  connectedComputers = require("./connected-computers"),
  // Whether that preview was on at launch. Off registers no channel for it.
  connectedComputersEnabled = false,
  // The gateway's internal secret, re-read per call (it rotates per boot). Used
  // only to redeem an external-terminal grant at this shell's own gateway.
  readInternalSecret = () => "",
  fetchFn = (url, init) => fetch(url, init),
  // The update channel this build must follow, "" when Settings decides (an oss
  // build). See runtime/update/policy-channel.js.
  getRequiredChannel = requiredUpdateChannelFor(),
  clipboardImageTimeoutMs = 1000,
  // The relay Crew opener behind `relay-crew:open`. Built on first use from the
  // injected Electron when absent; tests pass a fake.
  relayCrewOpenerOverride = null,
  // The credential chain main.js resolves for the gateway, and the cookie header
  // for a borrowed session. The updater uses them to tell a running gateway that
  // the user armed an update (best effort; see runtime/update/gateway-arm.js).
  fetchGatewayAuth = null,
  cookieHeader = null,
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
    dialog,
    Notification,
    ipcMain,
    shell,
    webContents,
    clipboard,
  } = electron;
  const log = typeof glog === "function" ? glog : (() => {});

  const RESTORABLE_TEXT_FORMATS = new Set(["text/plain", "text/html", "text/rtf"]);
  const RESTORABLE_IMAGE_FORMATS = new Set(["image/png", "image/jpeg", "image/tiff", "image/bmp"]);
  // An image format counts only when readImage() can capture it as a raster.
  const isRestorableFormat = (f, hasRasterImage) =>
    RESTORABLE_TEXT_FORMATS.has(f) || (hasRasterImage && RESTORABLE_IMAGE_FORMATS.has(f));

  function snapshotClipboard() {
    const image = clipboard.readImage();
    const saved = {
      text: clipboard.readText(),
      html: clipboard.readHTML(),
      rtf: clipboard.readRTF(),
    };
    if (!image.isEmpty()) saved.image = image;
    return saved;
  }

  function restoreClipboardIfEmpty(saved) {
    if (clipboard.availableFormats().length > 0) return;
    const data = {};
    for (const [key, value] of Object.entries(saved)) if (value) data[key] = value;
    if (Object.keys(data).length > 0) clipboard.write(data);
  }

  // `replaced` lists formats only a successful copy can remove, so an image
  // counts once they are gone and the answer never reflects the prior contents.
  async function clipboardHoldsImage(replaced = []) {
    const deadline = Date.now() + clipboardImageTimeoutMs;
    for (;;) {
      const formats = clipboard.availableFormats();
      if (formats.some((f) => f.startsWith("image/"))
          && !replaced.some((f) => formats.includes(f))) return true;
      if (Date.now() >= deadline) return false;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
  }

  let shellRegistered = false;
  let updaterRegistered = false;
  let updater = null;
  // True when the gateway reports that a security policy provider owns this
  // install's updates and the Electron feed lane must stand down. Set by the
  // renderer via update:notify-policy-governed once the gateway status lands.
  let policyGoverned = false;
  // True once the dashboard has reported a verdict through update:notify-policy-governed.
  // Until then `policyGoverned` is only its initial value, which is not a verdict.
  let policyVerdictLive = false;
  // Set of webContents IDs currently reporting governance=true. Governance is
  // active as long as at least one sender is in the set; removing a sender's id
  // (on explicit false or on destroyed) clears governance only when the set
  // empties, so a second window closing does not re-enable the feed lane while
  // another window on the same bundled gateway is still governed.
  const governanceSenders = new Set();
  // Per-sender sequence counter to discard out-of-order async completions.
  // Two notifications from the same sender (e.g. false then true at startup)
  // are probed concurrently; if the probes complete in reverse order the
  // earlier continuation would overwrite the later verdict. Recording the
  // latest seq before awaiting and checking after guards this race condition.
  const senderSeq = new Map();
  // True while a native folder chooser is on screen, so a second request
  // answers "no choice" instead of stacking another dialog.
  let folderPickOpen = false;

  // Rejection-log throttle. `glog` is an unrotated synchronous
  // `fs.appendFileSync` on the MAIN thread and the gate below writes one line
  // per rejection, so an unthrottled reject path converts a remote page's
  // loop into main-thread stalls plus unbounded disk growth. The channels
  // routed through this gate are reachable with NO backpressure:
  // `dev-mode-changed` is a fire-and-forget `ipcRenderer.send`, and while
  // `app-menu:execute` answers through `ipcRenderer.invoke`, a hostile page
  // need not AWAIT it — an un-awaited invoke fired in a loop is the same
  // flood. The pre-existing gated channels self-throttle only because THEIR
  // callers await, which is a property of those call sites rather than of the
  // channel shape. The single-flight coalescer in `registerShell()`
  // below does NOT cover this: each IPC message is its own main-thread task
  // and microtasks drain between tasks, so the in-flight entry is always
  // empty when the next message lands.
  //
  // This throttles the WRITE, never the VERDICT. Every call still runs all
  // three gates and still rejects, so there is no cached `false` that could
  // strand a legitimate local user on a permanently-greyed titlebar menu —
  // the failure mode the removal of negative caching exists to prevent. Same
  // shape as the coalescer: a third option satisfying two lanes whose
  // literal asks conflicted. See the F1 finding in GPT 5.6 Review on
  // 7234d97b9 (UPHOLD-FENCED, adjudication `flagged=0`).
  //
  // Scoped here rather than inside `registerShell()` because the gate it
  // serves is itself at this scope: `registerUpdater`'s
  // `update:notify-policy-governed` runs the same three gates and its
  // rejections need the same per-sender budget.
  const REJECTION_LOG_BURST = 5;
  const REJECTION_LOG_WINDOW_MS = 60_000;
  const rejectionLogState = new WeakMap();

  // A renderer-supplied value headed for a log line. `String(value)` runs the
  // sender's own `toString`, which a hostile page can make throw, and a throw
  // there skips the rejection throttle. Render by type instead: a string is
  // cut to 64 characters, a finite number prints as itself, anything else
  // shows only its type. Control and line-break characters become `?` after
  // the cut, so the value stays on one log line and within 64 characters.
  function boundedMenuArg(value) {
    if (typeof value === "string") {
      return value.slice(0, 64).replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]/g, "?");
    }
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    return `<${typeof value}>`;
  }

  // Keyed per WebContents so one flooding sender cannot spend another
  // window's diagnostic budget and blind an investigator to its rejects.
  function logGateRejection(wc, line) {
    // A torn-down or absent sender is a single event, not a flood vector —
    // never drop its diagnostic to a bookkeeping miss.
    if (!wc) {
      log(line);
      return;
    }
    const now = Date.now();
    let state = rejectionLogState.get(wc);
    if (!state) {
      state = { windowStart: now, written: 0, suppressed: 0 };
      rejectionLogState.set(wc, state);
    }
    if (now - state.windowStart >= REJECTION_LOG_WINDOW_MS) {
      // Report the scale before resetting, so a SUSTAINED flood — the actual
      // attack — is visible in the log at full magnitude once per window.
      // A flood that stops mid-window loses only its tail count, never the
      // fact of the rejections: the window's first `REJECTION_LOG_BURST`
      // lines were already written. The alternative, a flush timer, would
      // either hold the WebContents alive or keep the loop warm for a
      // diagnostic nicety.
      if (state.suppressed > 0) {
        log(
          `${state.suppressed} further local-dashboard gate rejections suppressed `
          + `for this sender in the last ${REJECTION_LOG_WINDOW_MS}ms`,
        );
      }
      state.windowStart = now;
      state.written = 0;
      state.suppressed = 0;
    }
    if (state.written < REJECTION_LOG_BURST) {
      state.written += 1;
      log(line);
      return;
    }
    state.suppressed += 1;
  }

  // Shared local-dashboard gate — used by both registerShell() and
  // registerUpdater() so the contract test finds a single spelling.
  // See the comment block inside registerShell() for the full rationale.
  const assertLocalDashboard = async (event, channel) => {
    let origin = "";
    try {
      origin = new URL(event.senderFrame.url).origin;
    } catch { /* about:blank, malformed URL, or torn-down frame */ }
    if (origin !== backendUrl) {
      logGateRejection(
        event.sender,
        `${channel} rejected for sender origin ${origin || "(unreadable)"}`,
      );
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const owner = windows.windowForWebContents(event.sender);
    if (!windows.security.isGatewayLocalForWindow(owner)) {
      logGateRejection(
        event.sender,
        `${channel} rejected for a sender window without a local gateway`,
      );
      throw new Error(`${channel} is restricted to the local dashboard`);
    }
    const portOwner = await gateway.probePrimaryPortOwner();
    if (portOwner !== "kirocrew" && portOwner !== "service") {
      logGateRejection(
        event.sender,
        `${channel} rejected: :${port} held by ${portOwner}, `
        + "not this shell's gateway",
      );
      // Every caller still sees a refusal. The classified owner rides along
      // only so the app-menu gate can tell an inconclusive probe from a
      // foreign holder; the other channels ignore it and stay fail-closed.
      const refusal = new Error(`${channel} is restricted to the local dashboard`);
      refusal.portOwner = portOwner;
      throw refusal;
    }
  };

  let relayOpener = relayCrewOpenerOverride;
  const relayCrewOpener = () => {
    if (!relayOpener) {
      const { BaseWindow, WebContentsView, session } = electron;
      const windowDeps = { BaseWindow, WebContentsView, session, shell, log };
      relayOpener = createRelayCrewOpener({
        requestToken: (endpointId, signInMarker) => requestRelayCrewToken({
          backendUrl, endpointId, signInMarker, getSecret: readInternalSecret, fetchFn,
        }),
        openWindow: (opts) => openRelayCrewWindow(opts, windowDeps),
        openPane: (opts) => openRelayCrewPane(opts, windowDeps),
        createRenewal: createRelayCrewRenewal,
        createTokenHolder,
        partitionCleared,
        log,
      });
    }
    return relayOpener;
  };

  setGlobalHotkeyLogger(log);
  const summonDashboard = createSummonHandler({
    getWindow: () => (
      typeof windows.getSummonWindow === "function"
        ? windows.getSummonWindow()
        : (windows.focusedDashboardWindow() || windows.getMainWindow() || null)
    ),
    createWindow: () => windows.createMainWindow(),
    // A global shortcut fires while another app is frontmost. A fullscreen
    // tray-close hides the whole application, so unhide it before win.show();
    // then steal activation after the window has been surfaced.
    showApp: () => {
      if (process.platform === "darwin" && typeof app.show === "function") app.show();
    },
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
    return require("./runtime/ipc/shell").registerShell({
      get app() { return app; },
      get assertLocalDashboard() { return assertLocalDashboard; },
      get backendUrl() { return backendUrl; },
      get boundedMenuArg() { return boundedMenuArg; },
      get clampBadgeCount() { return clampBadgeCount; },
      get clipboard() { return clipboard; },
      get clipboardHoldsImage() { return clipboardHoldsImage; },
      get connectedComputers() { return connectedComputers; },
      get connectedComputersEnabled() { return connectedComputersEnabled; },
      get crashNoticeSummary() { return crashNoticeSummary; },
      get crashScan() { return crashScan; },
      get createRelayCrewHandoff() { return createRelayCrewHandoff; },
      get currentGlobalHotkey() { return currentGlobalHotkey; },
      get DEFAULT_GLOBAL_HOTKEY() { return DEFAULT_GLOBAL_HOTKEY; },
      get deriveScale() { return deriveScale; },
      get detectWsl() { return detectWsl; },
      get dialog() { return dialog; },
      get downloadExpectations() { return downloadExpectations; },
      get externalTerminal() { return externalTerminal; },
      get fetchFn() { return fetchFn; },
      get folderPickOpen() { return folderPickOpen; }, set folderPickOpen(value) { folderPickOpen = value; },
      get gateway() { return gateway; },
      get ipcMain() { return ipcMain; },
      get isEndpointId() { return isEndpointId; },
      get isLocalGatewayEnabled() { return isLocalGatewayEnabled; },
      get isRestorableFormat() { return isRestorableFormat; },
      get log() { return log; },
      get logGateRejection() { return logGateRejection; },
      get normalizeBounds() { return normalizeBounds; },
      get PANE_ASSETS_PREFIX() { return PANE_ASSETS_PREFIX; },
      get purgeableOrigin() { return purgeableOrigin; },
      get readInternalSecret() { return readInternalSecret; },
      get relayCrewOpener() { return relayCrewOpener; },
      get relayOpener() { return relayOpener; }, set relayOpener(value) { relayOpener = value; },
      get requestedGlobalHotkey() { return requestedGlobalHotkey; },
      get restoreClipboardIfEmpty() { return restoreClipboardIfEmpty; },
      get safeFilename() { return safeFilename; },
      get saveThemeSplash() { return saveThemeSplash; },
      get scaleRect() { return scaleRect; },
      get setLocalGatewayEnabled() { return setLocalGatewayEnabled; },
      get shell() { return shell; },
      get shellRegistered() { return shellRegistered; }, set shellRegistered(value) { shellRegistered = value; },
      get snapshotClipboard() { return snapshotClipboard; },
      get store() { return store; },
      get summonDashboard() { return summonDashboard; },
      get windows() { return windows; },
    }, ...arguments);
  }

  /**
   * Bind only after createMainWindow(). Binding earlier lets a keypress during
   * boot race window creation and produce a second dashboard.
   */
  function bindGlobalHotkey() {
    return bindStoredGlobalHotkey(store.get("globalHotkey"), summonDashboard);
  }

  function broadcastUpdateState(payload) {
    // Include the main-process governance verdict in every state update so
    // the renderer can distinguish a local bundled gateway (governed) from
    // a remote one connected via SSH (not governed here, regardless of stamp).
    const enriched = { ...payload, policyGoverned };
    try {
      for (const contents of webContents.getAllWebContents()) {
        if (!contents.isDestroyed()) {
          try {
            contents.send("update-state", enriched);
          } catch {
            // The view disappeared between enumeration and dispatch.
          }
        }
      }
    } catch {
      // Updating is auxiliary; an unavailable WebContents registry is benign.
    }
  }

  // The armed-update record lives in the app's store: the gateway is down when
  // the apply runs, and an interrupted quit-time apply must survive a restart.
  const updateArm = createUpdateArm({ store });

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
        updateArm,
        gatewayArm: createGatewayArm({
          backendUrl,
          fetchGatewayAuth,
          cookieHeader,
          log: makeUpdaterLogger(log),
        }),
        // Only the gateway this app spawned is judged by this app's version: locality
        // proves nothing (another installation's gateway, or a tunnel, can answer on
        // loopback), so the kernel must name our own child as the listener.
        isOwnGateway: async () => {
          try {
            return windows.security.isGatewayLocalForWindow(windows.getMainWindow()) === true
              && typeof gateway.ownsLocalListener === "function"
              && (await gateway.ownsLocalListener()) === true;
          } catch { return false; }
        },
        // A start-time apply that failed (main.js runs it before the main window).
        startupApplyError: updateArm.takeApplyError(),
        // electron-updater's AppUpdater, NOT Electron's built-in autoUpdater:
        // it generates/verifies feed metadata and also supports Linux.
        autoUpdater: require("electron-updater").autoUpdater,
        // null until a live verdict arrives: the lane then keeps the last recorded
        // one instead of reading the initial false as "no provider governs".
        isPolicyGoverned: () => (policyVerdictLive ? policyGoverned : null),
        // On Windows, 'session-end' fires on BaseWindow (not App). The feed
        // lane uses this to register its pending-update notice on the window.
        getMainWindow: () => windows.getMainWindow(),
        dialog: electron.dialog,
        Notification,
        getFlavor: () => "stable",
        getChannelPreference: () => store.get("updateChannel", ""),
        getRequiredChannel,
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
        stopGateway: () => gateway.stopForUpdate(),
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
      policyGoverned,
    });

    // One-way notification from the renderer: the gateway status reported that
    // a security policy provider governs this install's updates. The feed lane
    // stands down until notified the opposite. Fire-and-forget: no response
    // expected; the lane reads policyGoverned on its next scheduled check.
    //
    // Three-gate guard (assertLocalDashboard): origin, window-locality, and
    // port-owner proof. The `lsof` binary is required on Linux for gate 3;
    // it is declared as a Debian package dependency in electron/package.json.
    ipcMain.on("update:notify-policy-governed", async (_event, governed) => {
      const srcId = _event?.sender?.id ?? null;
      // Per-sender sequence: bump before awaiting so if a later notification
      // from the same sender completes the probe first, this older continuation
      // is discarded and does not overwrite the newer verdict.
      const mySeq = (senderSeq.get(srcId) ?? 0) + 1;
      senderSeq.set(srcId, mySeq);

      // Three-gate check via the shared assertLocalDashboard helper.
      try {
        await assertLocalDashboard(_event, "update:notify-policy-governed");
      } catch {
        return;
      }
      // Gate 4 — current-bundle ownership (governance handler only).
      // A second copy of the app can also hold the port as "kirocrew"; its
      // policy provider would incorrectly suppress this copy's updater.
      // Unlike assertLocalDashboard's three gates which apply to every shell
      // channel, this check is only relevant for governance, and
      // isCurrentBundleGateway returns false on Windows/dev so must not
      // run for other channels. Skip when the function is unavailable.
      if (typeof gateway.isCurrentBundleGateway === "function") {
        let isCurrentBundle = false;
        try {
          isCurrentBundle = await gateway.isCurrentBundleGateway();
        } catch { /* probe failed — fail closed */ }
        if (!isCurrentBundle) {
          log("update:notify-policy-governed ignored: running gateway belongs to a different bundle");
          return;
        }
      }
      // Reject senders that were destroyed while the probe was awaited.
      // If the window closed during assertLocalDashboard's async port probe,
      // the srcId must not be added to governanceSenders: the destroyed event
      // already fired before the add, so no cleanup listener will fire.
      if (_event?.sender?.isDestroyed?.()) {
        log("update:notify-policy-governed: sender destroyed during probe — rejecting");
        return;
      }
      // Discard if a newer notification from the same sender already landed.
      if (senderSeq.get(srcId) !== mySeq) {
        log("update:notify-policy-governed: superseded notification discarded");
        return;
      }
      const prev = policyGoverned;
      policyVerdictLive = true;
      if (governed) {
        if (srcId !== null) {
          governanceSenders.add(srcId);
          // Remove from the set when the window is destroyed so governance is
          // lifted automatically once all governing windows close.
          // Uses the captured srcId (not a live sender reference) because the
          // sender object is gone by the time destroyed fires.
          if (_event?.sender) {
            _event.sender.once("destroyed", () => {
              governanceSenders.delete(srcId);
              // On macOS the app stays alive after all windows close
              // (Cmd+W vs Cmd+Q). If this was the last window, do not clear
              // governance: the user may reopen a window and the governance
              // state re-sent by the new window's hook will re-establish it.
              // On other platforms the app exits when the last window closes,
              // so clearing is safe. Only clear when another window is open
              // or we're not on macOS.
              // Dashboard windows are BaseWindows; BrowserWindow.getAllWindows() lists none of them.
              const { BaseWindow } = electron;
              const anyOpen = BaseWindow && BaseWindow.getAllWindows().length > 0;
              if (anyOpen || process.platform !== "darwin") {
                const wasGoverned = policyGoverned;
                policyGoverned = governanceSenders.size > 0;
                // Broadcast the cleared verdict to any remaining windows so
                // their useIsPolicyGoverned can stop treating the old
                // update-state cache value as authoritative (same as the
                // explicit-false path).
                if (wasGoverned && !policyGoverned) {
                  updater?.check?.();
                }
              }
            });
          }
        }
        policyGoverned = srcId !== null ? governanceSenders.size > 0 : true;
        if (!prev) {
          // Governance just became active: trigger a check immediately so the
          // feed lane emits not-available and clears any stale "found" state.
          updater?.check?.();
        }
      } else {
        // Lift governance for this sender. Other windows may still be governed.
        if (srcId !== null) governanceSenders.delete(srcId);
        else governanceSenders.clear();
        policyGoverned = governanceSenders.size > 0;
        // On a true-to-false transition: broadcast the cleared verdict by
        // triggering a check so the renderer's update-state cache updates
        // (useIsPolicyGoverned reads policyGoverned from the broadcast).
        if (prev && !policyGoverned) {
          updater?.check?.();
        }
      }
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
    // A plain quit and relaunch with no update command of its own (D-d). It
    // quits the app, so it answers only the local dashboard. An armed update is
    // applied by the quit it triggers, and the relaunch brings the app back.
    ipcMain.handle("update:relaunch", async (event) => {
      try {
        await assertLocalDashboard(event, "update:relaunch");
      } catch {
        return { ok: false, error: "relaunch is only available from the local dashboard" };
      }
      if (typeof updater.relaunch === "function") {
        updater.relaunch();
      } else {
        app.relaunch();
        app.quit();
      }
      return { ok: true };
    });
    // Whether `update:relaunch` would accept this sender. The same check, as a
    // bare boolean, so the dashboard offers Restart now only where it works.
    ipcMain.handle("update:can-relaunch", async (event) => {
      try {
        await assertLocalDashboard(event, "update:can-relaunch");
      } catch {
        return false;
      }
      return true;
    });
    ipcMain.handle("update:set-channel", (_event, channel) => {
      const next = typeof channel === "string" ? channel : "";
      if (next !== "" && next !== "insider" && next !== "stable") {
        return { ok: false, error: `invalid channel: ${next}` };
      }
      // A required channel replaces the preference, so storing another one
      // would be a switch that does nothing. The dashboard's
      // POST /api/update/channel answers the same way (409 locked_by_policy).
      const required = getRequiredChannel();
      if (required && next !== required) {
        return {
          ok: false,
          code: "locked_by_policy",
          error: `updates follow the ${required} channel set by your organization's policy`,
          info: updaterInfo(),
        };
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
