"use strict";

const defaultFs = require("fs");
const defaultOs = require("os");
const defaultPath = require("path");
const defaultHttp = require("http");
const {
  spawn: defaultSpawn,
  spawnSync: defaultSpawnSync,
  execFile: defaultExecFile,
  execFileSync: defaultExecFileSync,
} = require("child_process");

const {
  buildGatewayEnvironment,
  bundledKiroCliEnvironment,
  bundledKasEnvironment,
  gatewayBytecodeEnvironment,
} = require("./gateway-env");
const {
  launchBlockingBundleParts,
  describeIncompleteBundle,
  INSTALLING_DIALOG_TITLE,
  PRUNED_BACKEND_MESSAGE,
  PRUNED_DIALOG_TITLE,
  shouldReclassifyAsInstalling,
  currentAttemptLog,
  SPAWN_MARKER,
} = require("./bundle-integrity");
const { isPathFallback } = require("./find-bin");
const { classifyAuthBlock, defaultedPort, appImageBundledCliPath } = require("./gateway-auth-hint");
const {
  shouldRetryLocalTokenMint,
  tokenMintRetryDelayMs,
  TOKEN_MINT_MAX_RETRIES,
} = require("./token-acquire");
const {
  classifyPortOwner,
  probePortBinding,
} = require("./gateway-stop");
const {
  windowsGatewayExecutablePaths,
  windowsProcessCommand,
} = require("./windows-port");
const {
  gatewayWaitTimeoutMs,
  waitForGateway,
  tailLines,
  isPortInUse,
} = require("./gateway-wait");
const { createLivenessMonitor, createBackendProbe } = require("./gateway-liveness");
const {
  chooseRecoveryStrategy,
  revealWindowForConnect,
  gatewayCliInvocation,
  shouldReresolveBackend,
  isStaleBundleSignal,
} = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const { getRemoteHostConfig } = require("./host-config");
const { createTunnelKeeper } = require("./tunnel-keeper");
const {
  remoteCrewAction,
  remoteCrewDraft,
  saveRemoteCrewConfig,
} = require("./remote-crew-setup");
const { resolveHome, canonicalHome } = require("./home-dir");
const {
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
} = require("./local-gateway");
const { createLaunchPreflight } = require("./runtime/gateway/launch-preflight");
const { createPortHolders } = require("./runtime/gateway/port-holders");
const { createFamilyTakeover } = require("./runtime/gateway/family-takeover");
const { createTokenSources } = require("./runtime/gateway/token-sources");
const { createRemoteCrewPrompt } = require("./runtime/gateway/remote-crew-prompt");
const { createHomeLock } = require("./runtime/gateway/home-lock");
const { createSuccessorHandoff } = require("./runtime/gateway/successor-handoff");
const { createFailureDialog } = require("./runtime/gateway/failure-dialog");
const { createHolderDecision } = require("./runtime/gateway/holder-decision");
const { createShutdown } = require("./runtime/gateway/shutdown");
const { createReconnect } = require("./runtime/gateway/reconnect");

const DEFAULT_THEME_ACCENT = "#8E48FF";
const THEME_ACCENT_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const INSTALLING_STATUS = "Finishing installation…";
// The splash line behind the pruned-backend dialog: nothing is installing, so
// it must not say so, and it points at the dialog's Quit, the control that
// carries the action.
const PRUNED_STATUS = "Some app files are missing. Use Quit in the dialog, then reopen Kiro Crew.";
// relaunchViaConfirmedSuccessor's splash line defaults to the update wording
// (successor-handoff.js). The handoff also serves a caller that is not
// updating anything, and the splash is the only surface the user is looking at
// while it runs, so the reason is the caller's to name.
const RESTARTING_FOR_LOCAL_GATEWAY_STATUS = "Restarting Kiro Crew to start a local gateway…";
const POLL_INTERVAL_MS = 500;
// loadFile query that tells loading.html it is being painted by a reconnect
// path rather than a cold boot, so it can offer its exit control at once
// (loading.html reads `reconnect=1`). A query, not an IPC send: the splash
// loads asynchronously and a message sent right after loadFile can be missed.
const SPLASH_RECONNECT_QUERY = Object.freeze({ reconnect: "1" });
// loadFile query that tells loading.html it is painted into the main window:
// the one window whose close hides to tray and keeps the connect loop alive.
// A connection window is destroyed by close, so the page words its close hint
// from this flag (loading.html reads `primary=1`; absent means not primary).
const SPLASH_PRIMARY_QUERY = Object.freeze({ primary: "1" });

/**
 * Own the embedded gateway's complete lifecycle without owning the Electron
 * application's window or quit lifecycle. Electron objects and the few shared
 * window operations are injected so this module remains loadable in node:test.
 *
 * The state below is deliberately private and mutually consistent: callers can
 * start/connect/stop the gateway, but cannot independently mutate its child,
 * ownership classification, start failure, liveness monitor, or update handoff.
 */
function createGatewaySupervisor({
  app,
  store,
  BrowserWindow,
  nativeTheme,
  dialog,
  shell,
  ipcMain,
  port,
  backendUrl = `http://localhost:${port}`,
  home,
  getMainWindow,
  isQuitting,
  requestQuit,
  cancelPendingTrayHide,
  exitImmersiveModes,
  log,
  warn,
  error,
  logPath,
  predictLocalPort = () => port,
  fsMod = defaultFs,
  osMod = defaultOs,
  pathMod = defaultPath,
  httpMod = defaultHttp,
  spawnFn = defaultSpawn,
  execFileFn = defaultExecFile,
  execFileSyncFn = defaultExecFileSync,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  processRef = process,
  dirname = __dirname,
} = {}) {
  const fs = fsMod;
  const os = osMod;
  const path = pathMod;
  const http = httpMod;
  const spawn = spawnFn;
  const execFile = execFileFn;
  const execFileSync = execFileSyncFn;
  const processObj = processRef;
  const PORT = port;
  const BACKEND_URL = backendUrl;
  const HEALTH_URL = `${BACKEND_URL}/api/status`;
  const KIROCREW_HOME = home || resolveHome();
  const IS_MAC = processObj.platform === "darwin";
  const IS_WIN = processObj.platform === "win32";

  const glog = typeof log === "function" ? log : (() => {});
  const userWarn = typeof warn === "function" ? warn : glog;
  const userError = typeof error === "function" ? error : userWarn;
  const gatewayLogPath = typeof logPath === "function" ? logPath : (() => "");
  const mainWindow = () => (typeof getMainWindow === "function" ? getMainWindow() : null);
  const quitting = () => (typeof isQuitting === "function" ? isQuitting() : false);
  const quitApp = () => {
    if (typeof requestQuit === "function") requestQuit();
    else app.quit();
  };
  const cancelTrayHide = typeof cancelPendingTrayHide === "function"
    ? cancelPendingTrayHide : (() => {});
  const leaveImmersiveModes = typeof exitImmersiveModes === "function"
    ? exitImmersiveModes : (() => {});

  // Read ONCE at launch. startGateway() is also the recovery path for a gateway
  // that died mid-session, so re-reading the store there would let a setting
  // changed minutes ago refuse to replace the gateway this session still uses.
  // The error dialog's explicit "Start Local Gateway" action is the exception.
  let runLocalGateway = isLocalGatewayEnabled(store);
  // A "Start Local Gateway" re-exec was attempted and the successor never
  // served. Offering the button again would repeat a step this process has
  // shown it cannot finish, so it is offered once.
  let localStartRelaunchFailed = false;
  // The port a local-start refusal found already served, held here rather than
  // only on the failure record: a Retry discards that record and startGateway
  // rebuilds it, so a field written only onto the object is lost and the message
  // falls back to saying this app is set not to start a gateway -- which the
  // click has already made false. 0 means no refusal has named a port.
  let localStartPortBusy = 0;
  // The port whose holder could not be established after a local-start re-exec.
  // Held here for the same reason as the busy port -- a Retry discards the record
  // -- and kept separate from it because the two states have opposite remedies:
  // a busy port is freed and the button clicked again, while an unreadable probe
  // repeats identically, so the route out is quitting and reopening. 0 means no
  // attempt has ended this way.
  let localStartUnverifiable = 0;
  let gatewayProcess = null;
  // Exactly one ownership state is authoritative:
  //   none            external/unknown; never kill or respawn
  //   spawned         this app owns the child; recovery may kill and respawn
  //   reused-local    adopted local same-family process; bounded recovery
  //   reused-service  adopted service process; allow a manager rebind grace
  let gatewayOwnership = "none";
  let livenessMonitor = null;
  // Terminal exit of the child we spawned. Only primary own-port boot waits
  // consult this record; connection windows must never observe cross-talk.
  let gatewayStartFailure = null;
  // Set before updater shutdown begins. It keeps an intentional stop from being
  // read as a wedge and resurrected while the bundle is being replaced.
  let installingUpdate = false;
  // Re-resolves spent on the current stale-bundle incident (see
  // shouldReresolveBackend). Reset whenever a fresh gateway is asked for,
  // whenever one reaches handoff, and whenever a monitored backend answers
  // again, so each incident gets its own budget.
  let reresolveAttempts = 0;
  // Set when a packaged Windows app refuses the PATH fallback after a
  // stale-bundle incident: the backend tree it ran from was removed, so the
  // dialog says to quit and reopen rather than to wait for an install. Kept
  // across a Retry (which resets reresolveAttempts) and cleared once a launcher
  // resolves to a real backend again.
  let backendPruned = false;
  // Executables the CURRENT child was actually spawned from. findKirocrewBin
  // re-probes on every call, so after a Toolbox `current` junction is repointed
  // at a newer version it names a backend this shell never started; the child
  // still running is the one spawned before the repoint, and it must keep
  // classifying as ours (stop, liveness, port-owner) until it exits.
  let spawnedExecutablePaths = [];

  // The cohesive owners this facade composes. Each receives only what it
  // reads; the lifecycle state above stays in this closure and reaches them
  // through getters, and an owner that changes a slot is handed a setter for
  // that slot alone, so no owner can write any other.
  const {
    canRelaunchThisApp,
    resolveGatewayBin,
    resolveProjectDir,
    probeLaunchBlockingParts,
    warnSandboxProfileNeed,
    recoverLaunchdPath,
  } = createLaunchPreflight({
    fs,
    os,
    path,
    execFileSync,
    processObj,
    dirname,
    isWindows: IS_WIN,
    log: glog,
    warn: userWarn,
  });
  const {
    windowsRealpath,
    isTrustedWindowsGatewayCommand,
    posixDescendantPids,
    winListenPids,
    lsofListenPids,
    lsofHoldsFile,
    psCommand,
    psPpid,
    snapshotGatewayPortPids,
    unverifiedIncumbent,
    waitForIncumbentExit,
    forceStopGatewayPort,
  } = createPortHolders({
    fs,
    os,
    path,
    execFile,
    processObj,
    dirname,
    isWindows: IS_WIN,
    log: glog,
    getSpawnedExecutablePaths: () => spawnedExecutablePaths,
  });
  const {
    holdsHomeGatewayLock,
    sameHomeGatewayPid,
    runGatewayStop,
    probeLockHolderPid,
  } = createHomeLock({
    fs, path, execFile, app, processObj, home: KIROCREW_HOME, port: PORT, isWindows: IS_WIN, log: glog,
    lsofHoldsFile, snapshotGatewayPortPids, resolveGatewayBin,
  });
  // The SSH forward a client-only launch reaches its crew through, when the
  // crew opted in. Its PATH is recovered the same way the gateway's is, because
  // ssh runs the user's ProxyCommand (an SSM helper, typically) by name.
  const tunnelKeeper = createTunnelKeeper({
    store,
    port: PORT,
    spawn,
    resolveBin: resolveGatewayBin,
    // Built per spawn, not here: recovering the launchd PATH shells out, and
    // a launch whose crew never opted in must not pay for it.
    getEnv: () => {
      const { KIROCREW_PORT: _unused, ...tunnelEnv } = processObj.env;
      const recovered = recoverLaunchdPath(tunnelEnv.PATH || "");
      return recovered ? { ...tunnelEnv, PATH: recovered.path } : tunnelEnv;
    },
    isWindows: IS_WIN,
    log: glog,
    setTimeoutFn,
    clearTimeoutFn,
  });

  // How long a launch waits for a managed forward to come up before asking the
  // port. ssh's own connect budget is 15s (DEFAULT_CONNECT_TIMEOUT_SECS on the
  // Python side); a little over it lets one full attempt land.
  const TUNNEL_READY_TIMEOUT_MS = 20000;
  const TUNNEL_READY_POLL_MS = 500;

  /**
   * Start the managed forward when this port opted in, and give it one connect
   * budget to answer. Never rejects: a forward that is still down leaves the
   * ordinary "no gateway is answering" path to explain it, and the keeper goes
   * on retrying behind that dialog, so Retry works once the crew is back.
   */
  async function ensureManagedTunnel() {
    if (!tunnelKeeper.start()) return;
    sendStatus("Opening the tunnel to your crew…");
    // Counted in polls rather than read off the clock, so the injected timer
    // fully decides how long this waits.
    for (let waited = 0; waited < TUNNEL_READY_TIMEOUT_MS; waited += TUNNEL_READY_POLL_MS) {
      try { await checkBackend(); return; }
      catch { /* not up yet */ }
      await new Promise((resolve) => setTimeoutFn(resolve, TUNNEL_READY_POLL_MS));
    }
    glog(`tunnel: :${PORT} still not answering after ${TUNNEL_READY_TIMEOUT_MS}ms; keeper keeps retrying`);
  }

  const { resolveFamilyConflict } = createFamilyTakeover({
    dialog,
    execFile,
    processObj,
    port: PORT,
    log: glog,
    sendStatus,
    snapshotGatewayPortPids,
    waitForPortFree,
    waitForIncumbentExit,
  });
  const { fetchRemoteToken, mintLocalToken } = createTokenSources({
    store,
    port: PORT,
    backendUrl: BACKEND_URL,
    execFile,
    fs,
    path,
    http,
    isWindows: IS_WIN,
    log: glog,
    sendStatus,
    snapshotGatewayPortPids,
    getGatewayProcess: () => gatewayProcess,
    psPpid,
    holdsHomeGatewayLock,
  });
  const { promptRemoteCrew } = createRemoteCrewPrompt({ BrowserWindow, nativeTheme, isWindows: IS_WIN });

  // Accessors more than one owner below reads or changes; one that a single
  // owner needs is written at its composition. Each owner gets only the slots
  // it uses.
  const getGatewayOwnership = () => gatewayOwnership;
  const setGatewayOwnership = (ownership) => { gatewayOwnership = ownership; };
  const getRunLocalGateway = () => runLocalGateway;
  const {
    canOfferLocalStart,
    relaunchViaConfirmedSuccessor,
    fetchHealthInfo,
    fetchGatewayReadiness,
  } = createSuccessorHandoff({
    app, http, spawn, processObj, path, dirname, port: PORT, backendUrl: BACKEND_URL, log: glog,
    setTimeoutFn, clearTimeoutFn, quitting, mainWindow, canRelaunchThisApp, probeGatewayPortOwner,
    sendStatus, splashQuery, currentThemeAccent, showLoadingThenConnect,
    getLivenessMonitor: () => livenessMonitor,
    setLivenessMonitor: (monitor) => { livenessMonitor = monitor; },
  });
  const {
    showGatewayErrorDialog,
    revealForUserDecision,
    showUnrecoverableGatewayError,
  } = createFailureDialog({
    app, BrowserWindow, nativeTheme, shell, fs, isMac: IS_MAC, log: glog, gatewayLogPath,
    setTimeoutFn, setIntervalFn, clearIntervalFn, quitting, quitApp, mainWindow, cancelTrayHide,
    getInstallingUpdate: () => installingUpdate,
  });
  const {
    isCurrentBundleGateway,
    resolveUnresponsiveHolder,
    resolveGatewayConflict,
  } = createHolderDecision({
    app, store, dialog, path, processObj, port: PORT, isWindows: IS_WIN, log: glog, setTimeoutFn,
    quitting, mainWindow, sendStatus, checkBackend, waitForPortFree, probeGatewayPortBinding,
    probeGatewayPortOwner, snapshotGatewayPortPids, psCommand, unverifiedIncumbent, waitForIncumbentExit,
    sameHomeGatewayPid, runGatewayStop, probeLockHolderPid, fetchHealthInfo, fetchGatewayReadiness,
    resolveFamilyConflict, getRunLocalGateway, setGatewayOwnership,
  });
  const {
    stopGatewayGracefully,
    stopGatewayForUpdate,
    killGatewayProcessTree,
  } = createShutdown({
    fs, path, http, processObj, port: PORT, backendUrl: BACKEND_URL, isWindows: IS_WIN, log: glog,
    setTimeoutFn, waitForPortFree, probeGatewayPortBinding, snapshotGatewayPortPids, psCommand,
    posixDescendantPids, isTrustedWindowsGatewayCommand, sameHomeGatewayPid, runGatewayStop,
    isCurrentBundleGateway, getRunLocalGateway, getGatewayOwnership,
    getGatewayProcess: () => gatewayProcess,
    setGatewayProcess: (child) => { gatewayProcess = child; },
    setSpawnedExecutablePaths: (paths) => { spawnedExecutablePaths = paths; },
  });
  const {
    reconnectExternalGateway,
    reconnectOrRespawnAdoptedGateway,
  } = createReconnect({
    app, path, dirname, port: PORT, backendUrl: BACKEND_URL, healthUrl: HEALTH_URL, log: glog,
    setTimeoutFn, quitting, sendStatus, splashQuery, checkBackend, waitForPortFree,
    probeGatewayPortBinding, probeGatewayPortOwner, snapshotGatewayPortPids, unverifiedIncumbent,
    waitForIncumbentExit, startGateway, showLoadingThenConnect, showUnrecoverableGatewayError,
    probeLockHolderPid, fetchHealthInfo, fetchGatewayReadiness, getGatewayOwnership, setGatewayOwnership,
    setGatewayStartFailure: (failure) => { gatewayStartFailure = failure; },
  });

  function sendStatus(message) {
    // A WebContents can exist and still be destroyed -- that is the state of
    // the main window while its renderer is in a crash loop -- and `send` on
    // it throws. Several callers reach here from async paths with no catch, so
    // without this guard that throw surfaced as an unhandledRejection (#14524).
    const contents = mainWindow()?.webContents;
    if (!contents || contents.isDestroyed()) return;
    contents.send("status", message);
  }

  function currentThemeAccent() {
    const configured = store.get("themeAccent") || "";
    return THEME_ACCENT_RE.test(configured) ? configured : DEFAULT_THEME_ACCENT;
  }

  /**
   * The loadFile query every loading.html painter uses. `primary` is decided
   * here, from the window being painted, so no painter can mark a connection
   * window as the main one (see SPLASH_PRIMARY_QUERY).
   */
  function splashQuery(window, { reconnect = false, accent = "" } = {}) {
    const query = {};
    if (accent) query.accent = accent;
    if (reconnect) Object.assign(query, SPLASH_RECONNECT_QUERY);
    if (window === mainWindow()) Object.assign(query, SPLASH_PRIMARY_QUERY);
    return query;
  }

  function probeGatewayPortOwner(probePort) {
    if (IS_WIN) {
      return classifyPortOwner(probePort, {
        getListenPids: winListenPids,
        getCommand: (p) => windowsProcessCommand(p, { execFileFn: execFile }),
        isKirocrew: isTrustedWindowsGatewayCommand,
        log: glog,
      });
    }
    return classifyPortOwner(probePort, {
      getListenPids: lsofListenPids,
      getCommand: psCommand,
      getPpid: psPpid,
      log: glog,
    });
  }

  // Whether the port is occupied, with no judgement about who occupies it. The
  // waiting paths (waitForPortFree below, and the owners' rebind and drain
  // waits) need exactly this, and asking it here keeps them off the identity
  // predicate: they would otherwise pay for a command-line judgement whose
  // answer they discard, and inherit its trust in what a process claims to be.
  function probeGatewayPortBinding(probePort) {
    return probePortBinding(probePort, {
      getListenPids: IS_WIN ? winListenPids : lsofListenPids,
      log: glog,
    });
  }

  // Host-capability IPC may verify only this launch's primary gateway. Keep the
  // port out of the public call shape so an untrusted renderer cannot turn the
  // supervisor's process inspection into an arbitrary-port probe.
  function probePrimaryPortOwner() {
    return probeGatewayPortOwner(PORT);
  }

  // The LISTEN socket, not an HTTP answer, is the mutex. A wedged process or
  // dropped SSH tunnel can stop answering while it continues to hold the port.
  async function waitForPortFree(maxWaitMs = 30000) {
    const start = Date.now();
    for (;;) {
      const binding = await probeGatewayPortBinding(PORT);
      if (binding === "free") return true;
      if (binding === "unknown") {
        glog(`port-free: listener probe unavailable on :${PORT} — falling back to an HTTP probe`);
        try { await checkBackend(); } catch { return true; }
      }
      if (Date.now() - start > maxWaitMs) return false;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  function startGateway() {
    reresolveAttempts = 0;
    glog(`launch: port=${PORT} home=${KIROCREW_HOME} packaged=${app.isPackaged} resourcesPath=${processObj.resourcesPath || "(none)"} log=${gatewayLogPath()}`);
    sendStatus("Checking if gateway is running…");
    return new Promise((resolve) => {
      // Both the silent-port and takeover branches funnel through this gate, so
      // client-only mode cannot be honored on one path and ignored on another.
      const spawnUnlessClientOnly = () => {
        // Selection targets a crew's port on purpose, so that a live tunnel there
        // is found and adopted. Reaching here means nothing answered, and binding
        // it now is the shadowing this whole path exists to prevent: the conflict
        // resolver reads the same `remoteHosts` entry and would classify the
        // gateway this app just started as that crew. Only a process that has not
        // fixed its port can choose another one, so this launch refuses and the
        // dialog's own button re-execs with a crew-free port pinned.
        const crewOnTarget = getRemoteHostConfig(store, PORT)?.host || "";
        if (runLocalGateway && crewOnTarget) {
          glog(`not starting a gateway on :${PORT}: the crew ${crewOnTarget} is configured there and a gateway bound here would shadow it`);
          sendStatus("No gateway is answering…");
          gatewayStartFailure = {
            port: PORT,
            remoteHost: crewOnTarget,
            remotePort: getRemoteHostConfig(store, PORT)?.remotePort || "",
            localStartBlocked: "crew-configured",
            // canOfferLocalStart's first argument asks whether the SETTING is
            // off, and here it is on -- so the question is only whether a
            // relaunch is possible. The button's `enable-retry` still does the
            // half that matters: re-exec with a crew-free port pinned.
            canStartHere: canRelaunchThisApp(),
          };
          resolve(false);
          return;
        }
        if (runLocalGateway) {
          spawnGateway(resolve);
          return;
        }
        glog(`no gateway on :${PORT} and local gateway is off — not starting one`);
        sendStatus("No gateway is answering…");
        // The host is part of the state, not decoration: on a remote crew's port
        // a local gateway would shadow that crew's identity, so the dialog needs
        // to name the target and withhold the start-one-here offer. `remotePort`
        // travels with it because the crew binds THAT port on its own machine
        // (see effectivePort in fetchRemoteToken) while PORT is only the local
        // end of the connection -- naming the local one sends the user to check
        // a port nothing was ever expected to serve over there.
        const remoteConfig = getRemoteHostConfig(store, PORT);
        const remoteHost = remoteConfig?.host || "";
        gatewayStartFailure = {
          disabled: true,
          port: PORT,
          remoteHost,
          remotePort: remoteConfig?.remotePort || "",
          // The dialog decides its own button with this same predicate. Carrying
          // the answer on the record is what keeps the message from describing a
          // button the dialog does not render: the record is rebuilt on every
          // wait, so an attempt that has just failed is reflected in both.
          canStartHere: canOfferLocalStart(true, remoteHost),
          // Not a gate on the button -- only on the wording. One failed handoff
          // does not establish that a second cannot finish: a successor killed at
          // the readiness deadline can be a transient loss, and the remedy the
          // old copy offered ("quit and open Kiro Crew again") re-execs the app
          // exactly as the button does, only by hand. So the attempt is
          // acknowledged and the button is offered again.
          localStartFailed: localStartRelaunchFailed,
          // Same reason, and the same source: a refusal that named a port has to
          // survive the rebuild, or a Retry silently drops the one instruction
          // that state carried.
          localStartPortBusy,
          // And the same again for the state whose remedy is not the button: it
          // must survive a Retry, or the message returns to offering a click that
          // this host has already shown repeats identically.
          localStartUnverifiable,
        };
        resolve(false);
      };

      // A responder on the port goes through the identity and readiness
      // decision, whether it answered at once or inside the stuck-holder grace.
      const adoptOrSpawn = async () => {
        const outcome = await resolveGatewayConflict();
        if (outcome === "reuse") { resolve(true); return; }
        if (outcome === "foreign-holder") {
          // No `canStartHere`: the dialog decides the button from
          // canOfferLocalStart, and with no crew on this port that answer is
          // unconditional, so a copy of it here would be state nobody reads.
          gatewayStartFailure = {
            port: PORT,
            localStartBlocked: "foreign-holder",
          };
          resolve(false);
          return;
        }
        // Two producers: the drain path when the lock probe refuses, and the
        // manual takeover (runtime/gateway/family-takeover.js) when it cannot
        // capture the incumbent. Falling through would spawn into a lock that
        // may still be held.
        if (outcome === "probe-failed") {
          gatewayStartFailure = {
            error: `could not verify the previous gateway process on port ${PORT}`,
          };
          resolve(false);
          return;
        }
        if (outcome === "abort") {
          quitApp();
          resolve(false);
          return;
        }
        spawnUnlessClientOnly();
      };

      // Nothing answered. Only a stuck gateway for this data folder changes
      // the answer from spawning; every other holder keeps today's path.
      const recoverOrSpawn = async () => {
        let outcome = "spawn";
        try { outcome = await resolveUnresponsiveHolder(); }
        catch (error) {
          glog(`stuck gateway: holder check failed (${error && error.message}) — spawning as before`);
        }
        if (outcome === "healthy") { await adoptOrSpawn(); return; }
        if (outcome === "abort") {
          quitApp();
          resolve(false);
          return;
        }
        if (outcome && outcome.stopFailed) {
          const pidHint = outcome.stopFailed.length
            ? ` Process ${outcome.stopFailed.join(", ")} still holds it. To end it, run this in Terminal, then try again:\nkill -TERM ${outcome.stopFailed.join(" ")}`
            : " End the process that holds it, then try again.";
          gatewayStartFailure = {
            error: `could not stop the gateway that is not responding on port ${PORT}.${pidHint}`,
          };
          resolve(false);
          return;
        }
        spawnUnlessClientOnly();
      };

      ensureManagedTunnel()
        .then(() => checkBackend())
        .then(adoptOrSpawn, recoverOrSpawn)
        .catch(() => { spawnUnlessClientOnly(); });
    });
  }

  // Every call probes the candidate list afresh (findKirocrewBin does live
  // access() checks), which is what lets a stale-bundle respawn pick up a
  // backend swapped in at the same path. resourcesPath itself is a launch-time
  // snapshot and so is every other Electron path API; there is nothing newer to
  // read.
  function spawnGateway(resolve) {
    // The gateway owns its data root. Create it before deriving the redirected
    // bytecode cache, honoring an explicit KIROCREW_HOME without touching the
    // deprecated legacy directory on a clean install.
    const kirocrewDir = processObj.env.KIROCREW_HOME || canonicalHome();
    try {
      fs.mkdirSync(kirocrewDir, { recursive: true, mode: 0o700 });
    } catch (error) {
      userWarn(`WARN failed to create kirocrew dir ${kirocrewDir}: ${error.message}`);
    }

    const bin = resolveGatewayBin();
    const bundled = bin.includes("backend-dist");
    let execState = "executable";
    try { fs.accessSync(bin, fs.constants.X_OK); }
    catch (error) { execState = `NOT-EXECUTABLE(${error.code})`; }
    glog(`no gateway on :${PORT} — spawning bundled backend: bin=${bin} bundled=${bundled} ${execState} staleRetries=${reresolveAttempts}`);

    // A packaged Windows app with no backend at any probed path does not hand
    // the launch to whatever `kirocrew.exe` comes first on PATH. That is the
    // state an install manager leaves when it prunes the running version's
    // directory: the shell's own executable and app.asar are mapped, so they
    // survive, and the backend tree under them is gone. What PATH names then
    // is not this app's backend -- typically the manager's own command shim --
    // and a child that starts, serves nothing and exits 0 reads as a clean
    // stop, so spawning it only buys a silent respawn loop behind a loading
    // screen that never clears. Refusing here
    // routes it to the "installation still finishing" dialog instead, whose
    // probe re-runs this same resolution and starts the backend the moment one
    // is back at a probed path. Unpackaged (source checkout) launches keep the
    // PATH fallback, which is how a developer's own install is found.
    if (IS_WIN && app.isPackaged && isPathFallback(bin)) {
      if (reresolveAttempts >= 1) backendPruned = true;
      userError(`spawn REFUSED: no bundled backend under ${processObj.resourcesPath || "(no resourcesPath)"} — a packaged app does not fall back to ${bin} on PATH`);
      gatewayStartFailure = {
        error: backendPruned
          ? PRUNED_BACKEND_MESSAGE
          : describeIncompleteBundle([], { autoRetry: true }),
        incompleteBundle: true,
        bundled: true,
        backendPruned,
      };
      sendStatus(backendPruned ? PRUNED_STATUS : INSTALLING_STATUS);
      resolve(false);
      return;
    }
    backendPruned = false;

    // The Windows installer writes backend-dist incrementally. Refusing an
    // incomplete interpreter is preventive but cannot see package siblings that
    // have not landed yet; the current-attempt traceback classifier below is the
    // sound after-the-fact backstop. Neither replaces the other. The same
    // predicate drives the installing dialog's auto-retry probe, so what it
    // refuses here is exactly what that dialog waits for.
    const missingParts = launchBlockingBundleParts(fs, path, bin);
    if (missingParts && missingParts.length) {
      const backendRoot = path.resolve(path.dirname(bin), "..");
      userError(`spawn REFUSED: incomplete bundle at ${backendRoot} — missing: ${missingParts.join(", ")}`);
      gatewayStartFailure = {
        error: describeIncompleteBundle(missingParts, { autoRetry: true }),
        incompleteBundle: true,
        bundled: true,
      };
      sendStatus(INSTALLING_STATUS);
      resolve(false);
      return;
    }

    sendStatus("Starting gateway…");

    warnSandboxProfileNeed(bin);

    // The explicit --port is the single source of truth. Inheriting
    // KIROCREW_PORT would let the child re-derive a port which differs from the
    // shell URL and from the gateway's own frame-ancestor claim.
    const { KIROCREW_PORT: _ignored, ...cleanEnv } = processObj.env;

    // A GUI-launched macOS app appends only the launchd domain's additions.
    const gatewayPath = recoverLaunchdPath(cleanEnv.PATH || "");

    // Write child stdout/stderr directly to a file descriptor. A JS pipe could
    // backpressure a long-running gateway; the descriptor also preserves Python
    // tracebacks which otherwise disappear on clean recipient machines.
    let childOut = "ignore";
    try { childOut = fs.openSync(gatewayLogPath(), "a"); }
    catch (error) { userWarn(`WARN could not open child log fd: ${error.message}`); }
    glog(SPAWN_MARKER);
    gatewayStartFailure = null;

    // Node refuses .cmd/.bat without shell:true, so gatewayCliInvocation runs the
    // relocatable bundled Python directly instead of opening the
    // command-injection-prone shell path. Its `-P` mirrors the launcher shim it
    // replaces: it keeps the spawn cwd off sys.path, so a stdlib-named
    // directory there cannot shadow the interpreter's own standard library.
    // The refusal above already required python.exe beside a .cmd shim.
    const { bin: spawnBin, args: spawnArgs } = gatewayCliInvocation(
      bin,
      ["gateway", "--no-open", "--port", String(PORT)],
      path,
    );

    const child = spawn(spawnBin, spawnArgs, {
      stdio: ["ignore", childOut, childOut],
      detached: false,
      windowsHide: true,
      env: buildGatewayEnvironment({
        ...cleanEnv,
        ...(gatewayPath ? { PATH: gatewayPath.path } : {}),
        KIROCREW_PROJECT_DIR: IS_WIN
          ? resolveProjectDir()
          : path.resolve(dirname, ".."),
        // The kiro-cli staged into the app's resources at build time; spread
        // only when it shipped AND runs on this machine, so an unbundled build,
        // or a bundled copy this host cannot execute, keeps the user's own
        // install (see gateway-env.js).
        ...bundledKiroCliEnvironment(fs, path, processObj.resourcesPath, {
          platform: processObj.platform,
          spawnSync: defaultSpawnSync,
          env: cleanEnv,
          log: glog,
        }),
        // The KAS engine bundle staged beside it (BUNDLE_KAS): spread only when
        // it shipped. Nothing is run at boot; the route is off by default (see
        // gateway-env.js).
        ...bundledKasEnvironment(fs, path, processObj.resourcesPath, {
          log: glog,
        }),
        ...gatewayBytecodeEnvironment(
          processObj.platform,
          path.join(kirocrewDir, "cache", "pycache"),
          app.isPackaged,
        ),
      }, { bundled, platform: processObj.platform }),
    });
    gatewayProcess = child;
    gatewayOwnership = "spawned";
    if (IS_WIN) {
      spawnedExecutablePaths = windowsGatewayExecutablePaths(spawnBin, {
        realpathSync: windowsRealpath,
      });
    }
    if (typeof childOut === "number") {
      try { fs.closeSync(childOut); } catch { /* ignore */ }
    }

    // Bind handlers to this child, not the mutable slot. Recovery can replace a
    // child before its late error/exit event arrives; stale events must never
    // orphan the replacement or fabricate a start failure for it.
    //
    // Both handlers share one stale-bundle recovery. It returns true when it
    // took the child's fate over (respawned, or a fresh copy of the app is
    // being started), so the caller skips its ordinary failure bookkeeping;
    // `giveUp` is that bookkeeping, for the one case where taking over fails
    // later (the successor never started). `resolve` may already be settled by
    // then; a second call is a no-op, which is what a child that dies hours
    // after boot needs.
    const recoverStaleBackend = ({ exitCode = null, spawnErrorCode = "", giveUp }) => {
      // Probe the relaunch target first: when the bundle was swapped in place
      // the new executable sits at the same path; when it was pruned the path
      // is gone and exiting would leave the user with no app at all.
      const relaunchTargetExists = canRelaunchThisApp();
      const verdict = shouldReresolveBackend({
        isMac: IS_MAC,
        isWindows: IS_WIN,
        bundled,
        exitCode,
        spawnErrorCode,
        attempts: reresolveAttempts,
        quitting: quitting(),
        installingUpdate,
        relaunchTargetExists,
      });
      const cause = exitCode === null ? `spawn ${spawnErrorCode}` : `exit ${exitCode}`;
      if (verdict === "none") {
        // reresolveAttempts only ever rises on macOS and Windows, so a spent budget plus a
        // stale signal plus a missing executable is exactly the pruned case.
        if (
          reresolveAttempts >= 1 && !relaunchTargetExists
          && isStaleBundleSignal({ exitCode, spawnErrorCode })
        ) {
          glog(`stale bundle persists after re-resolve (${cause} on bin=${bin}) but this app's executable is gone (${processObj.execPath || "?"}) — cannot relaunch; surfacing the failure instead`);
        }
        return false;
      }
      if (verdict === "reresolve") {
        reresolveAttempts += 1;
        glog(`stale bundle (${cause} on bin=${bin}) — re-resolving the backend and respawning (attempt ${reresolveAttempts})`);
        gatewayProcess = null;
        spawnedExecutablePaths = [];
        gatewayStartFailure = null;
        spawnGateway(resolve);
        return true;
      }
      glog(`stale bundle persists after re-resolve (${cause} on bin=${bin}) — starting a fresh copy of the app from ${processObj.execPath}`);
      void relaunchViaConfirmedSuccessor(giveUp);
      return true;    };

    child.on("error", (error) => {
      userError(`spawn ERROR code=${error.code || "?"} msg=${error.message}`);
      if (gatewayProcess !== child) return;
      spawnedExecutablePaths = [];
      const giveUp = () => {
        gatewayStartFailure = { error: error.message, bundled };
        sendStatus(`Gateway failed: ${error.message}`);
        resolve(false);
      };
      if (recoverStaleBackend({ spawnErrorCode: error.code || "", giveUp })) return;
      giveUp();
    });
    child.on("exit", (code, signal) => {
      const exitMessage = `gateway child exited code=${code} signal=${signal}`;
      const currentChild = gatewayProcess === child;
      const expectedExit = !currentChild || quitting() || installingUpdate;
      if (expectedExit) glog(exitMessage);
      else userError(exitMessage);
      // Node's Windows kill maps both signal names to TerminateProcess. The
      // Gatekeeper hint is meaningful only on macOS, never on normal teardown.
      if (signal === "SIGKILL" && IS_MAC && !expectedExit) {
        userWarn("HINT: SIGKILL on a freshly-spawned bundled binary almost always means macOS Gatekeeper blocked an unsigned/quarantined nested executable. On the recipient's Mac run: xattr -cr <path to KiroCrew.app>");
      }
      if (!currentChild) return;
      spawnedExecutablePaths = [];
      const giveUp = () => {
        if (!gatewayStartFailure) gatewayStartFailure = { code, signal, bundled };
        gatewayProcess = null;
      };
      if (recoverStaleBackend({ exitCode: code, giveUp })) return;
      giveUp();
    });
    resolve(true);
  }

  function stopGatewayOnQuit() {
    tunnelKeeper.stop();
    stopGatewayGracefully()
      .catch((error) => console.error("Gateway stop failed:", error?.message));
  }

  // How long the BOOT poll waits for one answer. The poll runs every
  // POLL_INTERVAL_MS, so a slow answer here only means "poll again" — unlike
  // the post-handoff liveness probe, whose three misses force-kill the gateway
  // and which therefore carries its own, wider LIVENESS_PROBE_TIMEOUT_MS.
  const BOOT_PROBE_TIMEOUT_MS = 2000;

  function checkBackend(healthUrl = HEALTH_URL) {
    // Same request shape as the liveness probe, built by the same factory so
    // the two never drift; only the budget differs.
    return createBackendProbe({ httpMod: http, url: healthUrl, timeoutMs: BOOT_PROBE_TIMEOUT_MS })();
  }

  function waitForBackend(targetWindow, healthUrl = HEALTH_URL, { watchSpawn = false } = {}) {
    return waitForGateway({
      checkBackend: () => checkBackend(healthUrl),
      // Only primary own-port boot watches the child. A connection window points
      // at a process this supervisor never spawned and must not see shared state.
      getFailure: watchSpawn ? (() => gatewayStartFailure) : (() => null),
      isWindowAlive: () => !targetWindow?.isDestroyed(),
      onStatus: (message) => {
        try { targetWindow?.webContents?.send("status", message); }
        catch { /* window gone */ }
      },
      maxWaitMs: gatewayWaitTimeoutMs({
        platform: processObj.platform,
        watchSpawn: watchSpawn && gatewayOwnership === "spawned",
      }),
      pollIntervalMs: POLL_INTERVAL_MS,
    });
  }

  function dashboardEntryUrl(targetBackendUrl, initialPath = "", token = "") {
    const target = initialPath
      ? new URL(initialPath, targetBackendUrl)
      : new URL(targetBackendUrl);
    if (token) target.searchParams.set("token", token);
    return target.toString();
  }

  /**
   * Tell the reveal splash the gateway is ready, then wait for its fade before
   * navigating. The timeout covers reduced motion, renderer errors, and loading
   * pages which never send the completion IPC.
   */
  function fadeLoadingScreen(webContents, timeoutMs = 8000) {
    return new Promise((resolve) => {
      if (!webContents || webContents.isDestroyed()) { resolve(); return; }
      let settled = false;
      let timer = null;
      const onComplete = (event) => {
        if (event.sender === webContents) finish();
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        ipcMain.removeListener("boot-complete", onComplete);
        resolve();
      };
      ipcMain.on("boot-complete", onComplete);
      timer = setTimeout(finish, timeoutMs);
      try { webContents.send("boot-ready"); }
      catch { finish(); }
    });
  }

  /** Start or replace the primary own-port post-handoff liveness monitor. */
  function startLivenessMonitor(window) {
    // A gateway that reached handoff is healthy; a stale-bundle incident that
    // hits it later starts with a fresh re-resolve budget.
    reresolveAttempts = 0;
    if (livenessMonitor) {
      livenessMonitor.stop();
      livenessMonitor = null;
    }
    livenessMonitor = createLivenessMonitor({
      // Not checkBackend(): that is the boot poll's 2s probe. The post-handoff
      // probe has its own, wider budget — see LIVENESS_PROBE_TIMEOUT_MS.
      probe: createBackendProbe({ httpMod: http, url: HEALTH_URL }),
      isWindowAlive: () => !!window && !window.isDestroyed(),
      onUnresponsive: () => {
        if (livenessMonitor) {
          livenessMonitor.stop();
          livenessMonitor = null;
        }
        if (quitting() || installingUpdate) return;
        recoverWedgedGateway(window)
          .catch((error) => glog(`liveness recovery failed: ${error && error.message}`));
      },
      onRecovered: () => {
        // A re-resolved backend answering again closes its incident; the next
        // stale signal is a new one and gets the cheaper in-place re-resolve.
        reresolveAttempts = 0;
        glog("liveness: backend responsive again (transient blip)");
      },
      log: (message) => glog(`liveness: ${message}`),
    });
    livenessMonitor.start();
  }

  /**
   * Recover a gateway that is alive but unresponsive. Ownership is the sole
   * authority: external/tunnel holders are never killed, adopted local holders
   * get a bounded wait, and only a spawned child takes the kill/respawn path.
   */
  async function recoverWedgedGateway(window, { userInitiated = false } = {}) {
    const strategy = chooseRecoveryStrategy({ gatewayOwnership });
    if (strategy === "reconnect") {
      glog("liveness: backend unresponsive on a gateway we did not spawn (remote tunnel / external gateway) — waiting for it to recover instead of killing the port");
      if (!window || window.isDestroyed() || quitting()) return;
      return reconnectExternalGateway(window);
    }
    if (strategy === "reconnect-bounded") {
      glog("liveness: backend unresponsive on an adopted local Kiro Crew gateway — bounded wait, then respawn");
      if (!window || window.isDestroyed() || quitting()) return;
      return reconnectOrRespawnAdoptedGateway(window);
    }

    glog("liveness: backend unresponsive — force-killing wedged gateway and restarting");
    // Capture frozen Python stacks from outside the starved event loop before
    // killing it. This is best-effort and bounded by capturePySpyDump itself.
    if (gatewayProcess && gatewayProcess.pid) {
      await capturePySpyDump({
        pid: gatewayProcess.pid,
        dumpDir: path.dirname(gatewayLogPath()),
        log: (message) => glog(`liveness: ${message}`),
      }).catch((error) => glog(`liveness: py-spy capture threw: ${error && error.message}`));
    }
    // The frozen loop cannot service /api/shutdown. On Windows, await the tree
    // sweep before probing the port or descendants escape and retain locks.
    await killGatewayProcessTree(gatewayProcess, "SIGKILL");
    gatewayProcess = null;
    spawnedExecutablePaths = [];
    let freed = true;
    let foreignHolder = false;
    let probeFailed = false;
    try {
      ({ freed, foreignHolder, probeFailed = false } = await forceStopGatewayPort(PORT));
    } catch (error) {
      // POSIX lsof may be unavailable. Let bind arbitrate, but record that the
      // pre-spawn ownership proof could not be completed.
      glog(`liveness: port probe failed (${error && error.message}); attempting respawn and letting bind confirm`);
    }
    if (!window || window.isDestroyed() || quitting()) return;
    if (!freed) {
      const reason = probeFailed
        ? "probe failed"
        : (foreignHolder ? "foreign holder" : "unkillable wedge");
      glog(`liveness: port not confirmed free after force-stop (${reason}); surfacing restart-required`);
      return showUnrecoverableGatewayError(window, PORT, { probeFailed });
    }
    gatewayStartFailure = null;
    await startGateway();
    if (window.isDestroyed() || quitting()) return;
    // An update-install failure is user initiated and may raise. Autonomous
    // liveness recovery remains silent and never steals focus.
    return showLoadingThenConnect(window, BACKEND_URL, {
      reconnect: !userInitiated,
    });
  }

  async function showLoadingThenConnect(
    window,
    targetBackendUrl = BACKEND_URL,
    { reconnect = false, initialPath = "" } = {},
  ) {
    const healthUrl = `${targetBackendUrl}/api/status`;
    const webContents = window.webContents;
    webContents.loadFile(path.join(dirname, "loading.html"), {
      query: splashQuery(window, { reconnect, accent: currentThemeAccent() }),
    });
    // Cold boot and user-clicked retries raise. Autonomous liveness recovery
    // loads into the existing hidden/minimized window without touching focus.
    revealWindowForConnect(window, { reconnect });

    try {
      await waitForBackend(window, healthUrl, {
        watchSpawn: targetBackendUrl === BACKEND_URL,
      });
      if (window.isDestroyed()) return;

      // A newly started gateway regenerates .local_secret. /api/status can answer
      // just before local mint accepts that secret, so retry only an own-gateway
      // 403; foreign/SSH gateways can never be minted from this machine.
      for (let attempt = 0; ; attempt += 1) {
        let token = await mintLocalToken(targetBackendUrl);
        if (!token) {
          ({ token } = await fetchRemoteToken(defaultedPort(targetBackendUrl)));
        }
        if (window.isDestroyed()) return;

        if (token) {
          await fadeLoadingScreen(webContents);
          if (window.isDestroyed()) return;
          webContents.loadURL(dashboardEntryUrl(targetBackendUrl, initialPath, token));
          if (targetBackendUrl === BACKEND_URL && window === mainWindow()) {
            startLivenessMonitor(window);
          }
          return;
        }

        // No token may be legitimate when auth is disabled. Probe the dashboard
        // origin itself before classifying a 403 and asking where to mint.
        const status = await new Promise((resolve) => {
          http.get(targetBackendUrl, (response) => {
            response.resume();
            resolve(response.statusCode);
          }).on("error", () => resolve(0));
        });
        if (window.isDestroyed()) return;

        if (status !== 403) {
          webContents.loadURL(dashboardEntryUrl(targetBackendUrl, initialPath));
          if (targetBackendUrl === BACKEND_URL && window === mainWindow()) {
            startLivenessMonitor(window);
          }
          return;
        }

        // URL.port is empty on a default-port URL. Normalize it before looking
        // up per-port remote settings or probing the local LISTEN owner.
        const promptPort = defaultedPort(targetBackendUrl);
        const remoteHost = getRemoteHostConfig(store, promptPort)?.host || "";
        const localOwner = remoteHost
          ? "foreign"
          : await probeGatewayPortOwner(promptPort);
        const kind = classifyAuthBlock({ localOwner, remoteHost });

        if (shouldRetryLocalTokenMint({ kind, attempt })) {
          glog(`token mint: transient 403 on own gateway (kind=${kind}, attempt=${attempt + 1}/${TOKEN_MINT_MAX_RETRIES + 1}) — retrying after backoff`);
          await new Promise((resolve) => {
            setTimeout(resolve, tokenMintRetryDelayMs(attempt));
          });
          if (window.isDestroyed()) return;
          continue;
        }

        glog(`token prompt: kind=${kind} owner=${localOwner} port=${promptPort} host=${remoteHost || "(none)"}`);
        if (window.isDestroyed()) return;
        // Token input is a needs-user state. Reveal before leaving fullscreen so
        // a pending fullscreen-exit tray hide cannot re-hide the prompt.
        revealForUserDecision(window);
        leaveImmersiveModes(window);
        // A packaged AppImage installs no `kirocrew` on PATH, so the page's
        // "run kirocrew token" instruction is unrunnable for an AppImage-only
        // user (#16258). The launcher ships inside the image, and
        // resolveGatewayBin() resolves its absolute path; pass it so the page
        // can name a command the user can actually run. Only for an AppImage
        // ($APPIMAGE set) with an absolute, bundled launcher path — a normal
        // install keeps the on-PATH wording, and a bare "kirocrew" fallback is
        // not forwarded. The bundled CLI mints only against OUR gateway's own
        // secret, so this changes the LOCAL recovery wording only and does not
        // touch the refusal of a silent mint against an adopted gateway.
        const bundledCliPath = appImageBundledCliPath(processObj.env, resolveGatewayBin);
        webContents.loadFile(path.join(dirname, "token-prompt.html"), {
          query: {
            port: promptPort,
            kind,
            host: remoteHost,
            ...(bundledCliPath ? { cli: bundledCliPath } : {}),
          },
        });
        return;
      }
    } catch (error) {
      if (window.isDestroyed()) return;
      const failedToStart = error && error.kind === "failed";
      const launchLogPath = gatewayLogPath();
      let logTail = "";
      try { logTail = tailLines(fs.readFileSync(launchLogPath, "utf8"), 60); }
      catch { /* no log yet */ }

      // A pre-spawn integrity refusal is already authoritative. Otherwise only
      // a bundled current-attempt stdlib crash may be relabelled as installation;
      // current-attempt EADDRINUSE wins because its remedy is force-stop.
      // Only a pre-spawn refusal may auto-retry. Its probe sees exactly what
      // refused, so "complete" there means the launcher will spawn. A crash the
      // matcher reclassifies as installing died on a module the probe never
      // inspects (a sibling file, a name outside REQUIRED_STDLIB_PARTS); were
      // that dialog to auto-retry, a permanently truncated bundle -- disk full,
      // an interrupted installer, a quarantined file -- would respawn and crash
      // on every tick with no exit. That dialog keeps the manual Retry.
      const refusedBeforeSpawn = !!(error.failure && error.failure.incompleteBundle);
      const failureRecord = shouldReclassifyAsInstalling({
        failedToStart,
        failure: error.failure,
        logTail,
        portInUseInLog: isPortInUse(currentAttemptLog(logTail)),
        bundled: !!(error.failure && error.failure.bundled),
      })
        ? { ...error.failure, incompleteBundle: true }
        : error.failure;
      const failureKind = classifyStartFailure({
        failedToStart,
        failure: failureRecord,
        isOwnPort: targetBackendUrl === BACKEND_URL,
        portInUseInLog: isPortInUse(logTail),
      });
      const localGatewayOff = failureKind === "client-only";
      const remoteTarget = localGatewayOff ? (failureRecord?.remoteHost || "") : "";
      // Which local-start outcome this dialog is reporting, if any. Read in the
      // same precedence the message uses, so the title and the body cannot name
      // different states: the unreadable probe is newest, then a lost restart,
      // then a port that was already served, then a port reserved for a crew.
      const localStartOutcome = !localGatewayOff
        ? ""
        : (failureRecord?.localStartUnverifiable
          ? "could not identify what is on the port"
          : (failureRecord?.localStartFailed
            ? "the local gateway did not finish starting"
            : (failureRecord?.localStartPortBusy
              ? `port ${failureRecord.localStartPortBusy} is already in use`
              : (failureRecord?.localStartBlocked === "crew-configured"
                ? `port ${PORT} is reserved for ${failureRecord?.remoteHost || "a crew"}`
                : (failureRecord?.localStartBlocked === "foreign-holder"
                  ? `port ${PORT} is in use by another program`
                  : "")))));
      // The crew's own port when it has one; PORT is only this end of the link.
      const remoteTargetPort = (localGatewayOff && failureRecord?.remotePort) || PORT;
      const portConflict = failureKind === "port-conflict";

      let title;
      let message;
      if (failureKind === "installing") {
        title = failureRecord?.backendPruned ? PRUNED_DIALOG_TITLE : INSTALLING_DIALOG_TITLE;
        message = refusedBeforeSpawn
          ? error.message
          : describeIncompleteBundle([]);
      } else if (localGatewayOff) {
        title = remoteTarget
          ? `Kiro Crew — nothing answering at ${remoteTarget}:${remoteTargetPort}`
          : `Kiro Crew — no gateway on port ${PORT}`;
        // After a click, the news is what happened to the local start, and the
        // title is what a reader takes in first. Leaving it on the crew made the
        // busy-port and lost-restart dialogs indistinguishable at a glance --
        // same title, same buttons, one word different in the body.
        if (localStartOutcome) title = `Kiro Crew — ${localStartOutcome}`;
        message = error.message;
      } else if (portConflict) {
        title = `Kiro Crew — port ${PORT} already in use`;
        message = `Another Kiro Crew gateway is already using port ${PORT} (it may be wedged). `
          + `Force-stop it and retry, or quit. From a terminal you can also run: `
          + `kirocrew stop --port ${PORT}`;
      } else if (failedToStart) {
        title = "Kiro Crew — gateway failed to start";
        message = error.message;
      } else {
        title = "Kiro Crew — can't reach the gateway";
        message = "Could not connect to the Kiro Crew backend. Make sure "
          + "'kirocrew gateway' is running, or check kirocrew doctor.";
      }

      revealForUserDecision(window);
      // Reveal Log reopens the dialog after showing the file. Every other action
      // either retries the complete boot state machine or terminates this window.
      for (;;) {
        const action = await showGatewayErrorDialog(window, {
          title,
          message,
          logTail,
          logPath: launchLogPath,
          portConflict,
          port: PORT,
          localGatewayOff,
          // Withheld for the foreign-holder state: its message names freeing the
          // port or recording the crew, never this button, and pressing it only
          // flips a setting that is already on and re-enters the same refusal --
          // the identical dialog, one click later. A control that returns the user
          // to where they started is worse than an absent one.
          offerLocalStart: canOfferLocalStart(localGatewayOff, remoteTarget)
            && gatewayStartFailure?.localStartBlocked !== "foreign-holder",
          // The one state whose paragraph asks for Quit: the ownership probe could
          // not run, so both action buttons reach the same check and quitting and
          // reopening is the route that recovers.
          ...(gatewayStartFailure?.localStartUnverifiable ? { accentQuit: true } : {}),
          // The pruned-backend paragraph asks for Quit first, so the accent
          // follows it.
          ...(failureRecord?.backendPruned ? { accentQuit: true, backendPruned: true } : {}),
          // Retry reaches the crew in every one of these states, so it says which
          // crew in every one of them. Labelling it only where the accent moves
          // made the same control read as two: "Retry" in one opening of a dialog
          // and "Retry <host>" in the next, with nothing in the window to explain
          // the difference.
          //
          // The foreign-holder state has no crew to name, and a bare "Retry" gave
          // a reader nothing either -- retry what, when the message says the port
          // belongs to another program? It names the port instead, which is the
          // thing that has to change for the click to work.
          ...(remoteTarget
            ? { retryLabel: `Retry ${remoteTarget}` }
            : (gatewayStartFailure?.localStartBlocked === "foreign-holder"
              ? { retryLabel: `Retry port ${PORT}` }
              : {})),
          // Both local-start states name Start Local Gateway as the action to
          // take, and Retry reaches only the crew that is already unreachable, so
          // in both the accent follows the sentence. The condition is the two
          // states together rather than either one alone: an accent resting on
          // Retry while the paragraph asks for the other button spends the
          // reader's first click on the control that cannot help.
          //
          // The dialog demotes Retry to a secondary rather than dropping it,
          // which is also what keeps this from offering Start Local Gateway
          // twice.
          ...(localGatewayOff
            && (gatewayStartFailure?.localStartPortBusy
              || gatewayStartFailure?.localStartFailed)
            && canOfferLocalStart(localGatewayOff, remoteTarget)
            ? {
              primaryAction: "enable-retry",
              primaryLabel: "Start Local Gateway",
            }
            : {}),
          crewAction: remoteCrewAction({
            localGatewayOff,
            remoteHost: remoteTarget,
          }),
          // The one failure state known to clear on its own, and only where the
          // probe can see what is missing. Every other dialog stays manual.
          autoRetryProbe: failureKind === "installing" && refusedBeforeSpawn
            ? probeLaunchBlockingParts
            : undefined,
        });
        if (window.isDestroyed()) return;
        if (action === "reveal") {
          try { shell.showItemInFolder(launchLogPath); }
          catch { /* best effort */ }
          continue;
        }
        if (action === "configure-remote") {
          let draft = remoteCrewDraft(getRemoteHostConfig(store, PORT) || {});
          let configured = false;
          for (;;) {
            const fields = await promptRemoteCrew(window, PORT, draft);
            if (window.isDestroyed()) return;
            // Dismissed: nothing about the launch changed, so the failure
            // dialog is where this returns to.
            if (!fields) break;
            const { saved, error: saveError } = saveRemoteCrewConfig(store, PORT, fields);
            if (saved) {
              configured = true;
              glog(`remote crew for :${PORT} configured from the error dialog`);
              break;
            }
            // Reopen on what the user typed. A refused save writes nothing, so
            // the store holds no copy, and one bad field must not cost the
            // other three.
            draft = fields;
            await dialog.showMessageBox(
              window.isDestroyed() ? null : window,
              { type: "error", title: "Invalid Input", message: saveError },
            );
            if (window.isDestroyed()) return;
          }
          if (!configured) continue;
        }
        if (action === "enable-retry") {
          setLocalGatewayEnabled(store, true);
          glog("local gateway turned back on from the error dialog");
          if (remoteTarget) {
            // PORT is fixed for this process and is the local end of the link to
            // the crew, so a gateway started here would bind the crew's port.
            // Port selection reads the setting once per process, so only a fresh
            // process can pick a local port. The successor therefore lands on a
            // port this one never served, and this process chooses that port and
            // pins it into the successor's environment, so the port it watches is
            // the port the successor binds. This process stays client-only: the
            // setting is persisted either way, so a manual launch also recovers.
            const successorPort = predictLocalPort();
            glog(`re-execing so port selection runs again; expecting the successor on :${successorPort}`);
            void relaunchViaConfirmedSuccessor(({ reason, port: busyPort } = {}) => {
              if (reason === "successor-unverifiable") {
                // The restart ran and its gateway answered; what failed was this
                // app's check of who holds the port, and that outcome is stable
                // on this host -- so the same click repeats it exactly. Saying
                // "the restarted app never served one" would deny what the user
                // watched, and offering the button again would send them round the
                // same loop. The state carries the port and its own flag, and the
                // message names the route that does recover: quit and reopen,
                // which works because the setting is already persisted.
                localStartUnverifiable = busyPort || successorPort;
                localStartRelaunchFailed = false;
                localStartPortBusy = 0;
                if (gatewayStartFailure) {
                  gatewayStartFailure.localStartUnverifiable = localStartUnverifiable;
                  gatewayStartFailure.localStartFailed = false;
                  gatewayStartFailure.localStartPortBusy = 0;
                }
                glog(`could not establish who holds :${localStartUnverifiable}; the setting stays on so the next launch starts a gateway`);
                showLoadingThenConnect(window, targetBackendUrl, { initialPath })
                  .catch((error) => glog(`resurfacing the gateway failure failed: ${error && error.message}`));
                return;
              }
              if (reason === "port-busy") {
                // Nothing restarted, so claiming one did would deny what the
                // user saw. The port is held by something outside this app and
                // can be freed, so the button stays available rather than being
                // refused over a condition that is not this app's to fix. The
                // setting is already persisted, which is why a later launch
                // still picks a local port on its own.
                localStartPortBusy = busyPort;
                if (gatewayStartFailure) gatewayStartFailure.localStartPortBusy = busyPort;
                // And retire an earlier failed attempt, the mirror of what that
                // path does to an earlier busy port. The message reads the failed
                // flag first, so a leftover would report "the restarted app never
                // served one" for a click that spawned nothing and would never
                // name the port to free -- and the likeliest occupant of that port
                // is an orphan from the very attempt that set the flag.
                localStartRelaunchFailed = false;
                if (gatewayStartFailure) gatewayStartFailure.localStartFailed = false;
                // And the unreadable-probe flag, for the same reason and more
                // sharply: BOTH readers test it first, so a leftover would keep
                // reporting a successor this click never spawned, would never name
                // the port to free, and would keep the accent on Quit while the
                // paragraph asks for a port to be freed. Every one of these three
                // branches retires the other two.
                localStartUnverifiable = 0;
                if (gatewayStartFailure) gatewayStartFailure.localStartUnverifiable = 0;
                glog(`:${busyPort} is already served by something else; nothing was restarted and the setting stays on`);
                showLoadingThenConnect(window, targetBackendUrl, { initialPath })
                  .catch((error) => glog(`resurfacing the gateway failure failed: ${error && error.message}`));
                return;
              }
              localStartRelaunchFailed = true;
              // An earlier busy port is no longer the story: this attempt gets to
              // the restart and loses it, which is the newer outcome, and the
              // message reads that field first. Cleared on the state, not only on
              // the record, so a later rebuild does not bring it back.
              localStartPortBusy = 0;
              // Same for the unreadable-probe flag: this attempt reached the
              // restart and lost it, which is the newer outcome, and that field is
              // read ahead of this one.
              localStartUnverifiable = 0;
              // The reopened wait reuses this same record rather than building a
              // new one, so the flag is set here too or the message cannot tell a
              // failed attempt from an app that never offered the restart.
              //
              // canStartHere is deliberately NOT forced false: the button is
              // offered again after a failure, and the record has to agree with
              // the gate or the message would deny a control the window renders.
              if (gatewayStartFailure) {
                gatewayStartFailure.localStartFailed = true;
                gatewayStartFailure.localStartPortBusy = 0;
                gatewayStartFailure.localStartUnverifiable = 0;
              }
              glog("successor never served; this process stays client-only and the setting is on for the next launch");
              showLoadingThenConnect(window, targetBackendUrl, { initialPath })
                .catch((error) => glog(`resurfacing the gateway failure failed: ${error && error.message}`));
            }, { expectPort: successorPort, pinPort: true, restartingStatus: RESTARTING_FOR_LOCAL_GATEWAY_STATUS });
            return;
          }
          runLocalGateway = true;
        }
        if (action === "force-retry") {
          let freed = true;
          let probeFailed = false;
          try {
            ({ freed, probeFailed = false } = await forceStopGatewayPort(PORT));
          } catch (probeError) {
            glog(`force-stop: port probe failed (${probeError && probeError.message}); letting retry's bind confirm`);
          }
          if (window.isDestroyed()) return;
          if (!freed) {
            return showUnrecoverableGatewayError(window, PORT, { probeFailed });
          }
        }
        if (
          action === "retry"
          || action === "force-retry"
          || action === "enable-retry"
          || action === "configure-remote"
        ) {
          gatewayStartFailure = null;
          // A primary own-port retry respawns only when no child remains. Timeouts
          // may leave a live child; connection tabs never own one at all.
          if (targetBackendUrl === BACKEND_URL && !gatewayProcess) {
            await startGateway();
          }
          if (window.isDestroyed()) return;
          return showLoadingThenConnect(window, targetBackendUrl, { initialPath });
        }

        if (window === mainWindow()) quitApp();
        else window.destroy();
        return;
      }
    }
  }

  function onInstallDispatched() {
    // The flag closes the interval between dispatch and stop; stopping the
    // monitor ensures nothing probes while bundle replacement is in flight.
    installingUpdate = true;
    if (livenessMonitor) {
      livenessMonitor.stop();
      livenessMonitor = null;
    }
    glog("update install dispatched — liveness recovery disarmed");
  }

  function onInstallFailed(window = mainWindow()) {
    // The deferred-quit path is already quitting and never sets this live flag.
    if (!installingUpdate) return;
    installingUpdate = false;
    glog("update install failed — restoring gateway and liveness recovery");
    // stopGatewayGracefully deliberately preserved spawned ownership. Recovery
    // therefore takes the respawn path instead of waiting forever as external.
    recoverWedgedGateway(window, { userInitiated: true })
      .catch((error) => glog(`post-install-failure recovery failed: ${error && error.message}`));
  }

  return Object.freeze({
    start: startGateway,
    connect: showLoadingThenConnect,
    mintLocalToken,
    fetchRemoteToken,
    entryUrl: dashboardEntryUrl,
    probePrimaryPortOwner,
    stopForUpdate: stopGatewayForUpdate,
    stopOnQuit: stopGatewayOnQuit,
    // After sleep the old forward can look alive while carrying nothing, so a
    // wake rebuilds it at once instead of waiting out ssh's keepalive.
    reopenTunnel: () => tunnelKeeper.restart(),
    // A remote-crew edit re-applies the launch port's tunnel choice at once:
    // start, replace (new host or port) or stop the forward to match it.
    syncTunnel: () => tunnelKeeper.start(),
    onInstallDispatched,
    onInstallFailed,
  });
}

module.exports = { createGatewaySupervisor };
