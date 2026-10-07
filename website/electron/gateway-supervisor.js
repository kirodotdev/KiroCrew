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
  gatewayBytecodeEnvironment,
} = require("./gateway-env");
const {
  launchBlockingBundleParts,
  describeIncompleteBundle,
  nextInstallingDialogState,
  INSTALLING_DIALOG_TITLE,
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
  stopGatewayGracefully: stopGatewayProcessGracefully,
  classifyPortOwner,
  probePortBinding,
} = require("./gateway-stop");
const {
  windowsGatewayExecutablePaths,
  windowsProcessCommand,
  windowsTaskkill,
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
  classifyAdoptedGateway,
  revealWindowForConnect,
  waitForServiceRebind,
  unrecoverableGatewayDialog,
  shouldReresolveBackend,
  isStaleBundleSignal,
} = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const {
  decideGatewayAction,
  classifyGatewayReadiness,
  HEALTH_IDENTITY_PATH,
  READY_PATH,
} = require("./instance-guard");
const { getRemoteHostConfig } = require("./host-config");
const { createTunnelKeeper } = require("./tunnel-keeper");
const {
  remoteCrewAction,
  remoteCrewDraft,
  saveRemoteCrewConfig,
} = require("./remote-crew-setup");
const { resolveHome, canonicalHome, secretCandidates } = require("./home-dir");
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

const DEFAULT_THEME_ACCENT = "#8E48FF";
const THEME_ACCENT_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const INSTALLING_STATUS = "Finishing installation…";
const RESTARTING_STATUS = "Restarting Kiro Crew to finish the update…";
// The same handoff serves a caller that is not updating anything, and the splash
// is the only surface the user is looking at while it runs, so the reason is the
// caller's to name.
const RESTARTING_FOR_LOCAL_GATEWAY_STATUS = "Restarting Kiro Crew to start a local gateway…";
const POLL_INTERVAL_MS = 500;
// How often the "installation still finishing" dialog re-probes the bundle.
// Extraction runs for minutes (about eight in the install that motivated the
// dialog), so a tick this size costs nothing while still landing the retry
// within seconds of the last part arriving.
const INSTALLING_PROBE_MS = 5_000;
// How long the dialog shows "Installation finished" before it closes itself.
// Painting and closing in the same tick would give the line at most one frame.
const INSTALLING_COMPLETE_LINGER_MS = 700;
const ADOPTED_RECOVERY_WAIT_MS = 30_000;
// How long a gateway that holds the port for this app's own data folder may
// go on failing its health check at launch before the user is offered to stop
// and restart it. Long enough that a gateway still importing on a slow disk
// answers first; short enough that a wedged one is not a blank splash.
const STUCK_GATEWAY_GRACE_MS = 15_000;
const STUCK_GATEWAY_POLL_MS = 1_000;
// `kirocrew stop --port` sends SIGTERM and waits for the port itself.
const STUCK_GATEWAY_STOP_TIMEOUT_MS = 30_000;
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
// How long this instance stays alive waiting for a successor copy of the app
// to prove itself by serving on the gateway port (relaunchViaConfirmedSuccessor):
// Electron boot, then the successor's own gateway budget.
//
// That budget is not one number. `gatewayWaitTimeoutMs` gives a primary local
// gateway on Windows 120s, because importing a freshly installed bundled Python
// tree exceeds the ordinary deadline there -- and both callers here spawn exactly
// that: a primary local gateway, on a machine that may have just installed one.
// So the deadline is derived from the same function the successor will apply to
// itself, rather than stated as a single constant. Stating it was wrong on
// Windows: 60s expired while the successor was still inside its own 120s budget,
// and `fail()` killed a gateway that was starting normally.
const SUCCESSOR_BOOT_MARGIN_MS = 30_000;
const SUCCESSOR_POLL_MS = 500;

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
  // Executables the CURRENT child was actually spawned from. findKirocrewBin
  // re-probes on every call, so after a Toolbox `current` junction is repointed
  // at a newer version it names a backend this shell never started; the child
  // still running is the one spawned before the repoint, and it must keep
  // classifying as ours (stop, liveness, port-owner) until it exits.
  let spawnedExecutablePaths = [];

  // The cohesive owners this facade composes. Each receives only what it
  // reads; the lifecycle state above stays in this closure and reaches them
  // through getters, so no owner can write it.
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
  });
  const { promptRemoteCrew } = createRemoteCrewPrompt({ BrowserWindow, nativeTheme, isWindows: IS_WIN });

  /**
   * May the failure dialog offer "Start Local Gateway"?
   *
   * Reversing the opt-out is reachable only here: the settings page that writes
   * it is served by the gateway that is not running. On this launch's own port
   * the action starts a gateway in place. On a crew's port it cannot -- the
   * spawn binds PORT and would shadow the crew -- so it re-runs port selection
   * in a fresh process, which needs an executable to re-exec and one attempt
   * that has not already failed.
   *
   * @param {boolean} localGatewayOff  this launch started nothing.
   * @param {string} remoteTarget      the crew this launch targeted, if any.
   * @returns {boolean}
   */
  function canOfferLocalStart(localGatewayOff, remoteTarget) {
    if (!localGatewayOff) return false;
    if (!remoteTarget) return true;
    return canRelaunchThisApp();
  }

  /**
   * Restart the app by starting a fresh copy of it and exiting only once that
   * copy is demonstrably alive.
   *
   * app.relaunch() is not usable here: it returns nothing and only schedules a
   * re-exec for exit time, so when the bundle is pruned between the
   * canRelaunchThisApp probe and that re-exec the app exits into nothing and
   * no code is left to notice. Spawning the successor ourselves lets this
   * process observe it, but
   * Node's "spawn" event only proves the exec succeeded: a bundle intact
   * enough to exec and broken enough to crash during initialization would
   * still take the app down. The handshake is therefore the successor's own
   * gateway answering on the port, and it carries that meaning only while the
   * port has nobody on it to begin with. That is checked here rather than
   * assumed: a gateway a separate install or a terminal launched answers exactly
   * like a successor would, and confirming on it would exit this instance on a
   * stranger's liveness. The test is whether ANY responder is there, not what it
   * says, because a gateway too old to serve the readiness endpoint is still a
   * gateway holding the port. So an occupied port abandons the handoff with this
   * instance untouched, and only a port that nothing answered on can confirm
   * one. An answer on such a port means the successor booted, ran its
   * supervisor, and started a serving backend. A 503 "starting" counts too: the
   * successor's gateway is bound and only still restoring sessions.
   *
   * What remains is a bind between the check and the successor's own bind. That
   * window is the spawn itself, and losing it costs a killed successor and a
   * surfaced failure rather than an app that exits into nothing, because the
   * check is what stands between a foreign gateway and `app.exit`.
   *
   * Which port carries that handshake belongs to the caller. A bundle upgrade
   * re-execs the same configuration, so the successor binds this process's own
   * port and inherits the environment that chose it. Turning the local gateway
   * back on does not: the successor would re-run port selection with the setting
   * now on, and an inherited `KIROCREW_PORT` outranks that selection, so the
   * successor could bind a port this process is not watching and be killed while
   * healthy. That caller therefore pins its chosen port into the successor's
   * environment, which makes the watched port and the bound port one value
   * instead of two answers that have to agree.
   *
   * Everything short of that is a failure with this instance still alive:
   * a spawn error (ENOENT on a pruned bundle), the successor exiting before
   * its gateway answered, or the bounded wait running out. The failure path
   * kills a successor that is still running so exactly one instance remains,
   * takes the single-instance lock back so a later manual launch still routes
   * here, and runs the caller's ordinary failure bookkeeping.
   *
   * The liveness monitor is stopped for the duration: were it to fire during
   * the handoff it would force-stop the port the successor's gateway is
   * binding. Mid-session the boot splash is put back first, since only it
   * renders status and the user would otherwise watch the window vanish
   * unannounced; a failed mid-session handoff then surfaces the failure
   * dialog itself, the way the monitor's recovery would have.
   *
   * @param {(outcome: {reason: string, port: number}) => void} onFailed  the
   *        caller's failure bookkeeping. `reason` is "port-busy" when the port
   *        was already held and nothing was spawned, or "successor-failed" when
   *        a successor ran and never served. The two need different wording: one
   *        reports a restart that never happened.
   * @param {object} [options]
   * @param {number} [options.expectPort]  port the successor will serve on.
   * @param {boolean} [options.pinPort]  put expectPort in the successor's
   *        environment, for a caller choosing a port rather than predicting the
   *        one the successor would select for itself.
   * @param {string} [options.restartingStatus]  what the splash says while the
   *        handoff runs, since only the caller knows why it is restarting.
   */
  async function relaunchViaConfirmedSuccessor(
    onFailed,
    { expectPort = PORT, pinPort = false, restartingStatus = RESTARTING_STATUS } = {},
  ) {
    const readyUrl = `http://localhost:${expectPort}${READY_PATH}`;
    const target = processObj.execPath;
    const args = Array.isArray(processObj.argv) ? processObj.argv.slice(1) : [];
    // Before anything is torn down, since abandoning the handoff has to leave
    // this instance exactly as it stands.
    if (await portHasAnyResponder(readyUrl)) {
      glog(`:${expectPort} already has a responder — cannot tell a successor from it, so surfacing the failure instead of handing off`);
      // Nothing was spawned and nothing was torn down, which is a different
      // state from a successor that ran and never served: the caller must not
      // report a restart that did not happen, and the condition is external so
      // it can clear on its own.
      onFailed({ reason: "port-busy", port: expectPort });
      return;
    }
    const midSession = livenessMonitor !== null;
    if (livenessMonitor) {
      livenessMonitor.stop();
      livenessMonitor = null;
    }
    if (midSession) {
      // Only the boot splash renders status messages; the dashboard has no
      // listener. Put the splash back so the announcement lands where the
      // user is looking, without stealing focus from a hidden window.
      const window = mainWindow();
      if (window && !window.isDestroyed()) {
        try {
          window.webContents.loadFile(path.join(dirname, "loading.html"), {
            query: splashQuery(window, { accent: currentThemeAccent() }),
          });
        } catch { /* window may be tearing down */ }
      }
    }
    sendStatus(restartingStatus);
    app.releaseSingleInstanceLock();
    let settled = false;
    let gone = false;
    let pollTimer = null;
    let deadlineTimer = null;
    const clearTimers = () => {
      if (pollTimer) { clearTimeoutFn(pollTimer); pollTimer = null; }
      if (deadlineTimer) { clearTimeoutFn(deadlineTimer); deadlineTimer = null; }
    };
    const spawnOptions = { detached: true, stdio: "ignore" };
    if (pinPort) {
      spawnOptions.env = { ...processObj.env, KIROCREW_PORT: String(expectPort) };
    }
    const successor = spawn(target, args, spawnOptions);

    // `failureReason` is what the CALLER is told, and it is separate from the log
    // line because one of these outcomes is not a successor that failed. When the
    // ownership probe cannot run, the successor answered readiness and was then
    // stopped by this function -- so reporting "successor-failed" would make the
    // dialog deny something the user just watched happen, and would invite the
    // same click, which repeats identically because a probe that cannot run on
    // this host cannot run on the retry either.
    const fail = (reason, { failureReason = "successor-failed" } = {}) => {
      if (settled) return;
      settled = true;
      clearTimers();
      glog(`successor app ${reason} — cannot relaunch; surfacing the failure instead`);
      if (!gone) {
        try { successor.kill(); }
        catch (killError) { glog(`could not stop the unconfirmed successor: ${killError && killError.message}`); }
      }
      try {
        if (!app.requestSingleInstanceLock()) glog("could not re-take the single-instance lock: another instance holds it");
      } catch (lockError) {
        glog(`could not re-take the single-instance lock: ${lockError && lockError.message}`);
      }
      onFailed({ reason: failureReason, port: expectPort });
      if (!midSession) return;
      const window = mainWindow();
      if (!window || window.isDestroyed() || quitting()) return;
      showLoadingThenConnect(window, BACKEND_URL, { reconnect: true })
        .catch((error) => glog(`surfacing the failed relaunch failed: ${error && error.message}`));
    };
    const confirm = () => {
      if (settled) return;
      settled = true;
      clearTimers();
      successor.unref();
      glog(`successor app (pid ${successor.pid}) is serving on :${expectPort} — exiting this instance`);
      app.exit(0);
    };
    // Readiness says something answered on that port; it cannot say the answer
    // came from the successor's gateway. A manual SSH tunnel that binds AFTER the
    // pre-spawn probe passes this poll by construction, and confirmation is an
    // unconditional exit with no recovery step -- the next launch then classifies
    // the tunnel as a local gateway and the idle heartbeat sends it
    // X-Internal-Secret. So an HTTP answer is necessary and not sufficient: the
    // LISTEN owner has to be positively identified, which is the same gate
    // ipc-registrar applies to host-capability IPC for the same tunnel-on-the-
    // local-port reason.
    //
    // The four outcomes are deliberately not collapsed, because they call for
    // three different actions:
    //   kirocrew / service -- positively ours. Confirm. A successor spawns its
    //     own gateway child, so the holder is a grandchild of this process and
    //     never the pid we spawned; this classifies the process, not the
    //     instance, which is why it still passes an ordinary handoff.
    //   foreign -- someone else holds it. Nothing this poll waits for can change
    //     that, so it fails immediately with the reason rather than spending the
    //     remaining window and then reporting a timeout.
    //   none -- answered with no visible local listener: a race, or a socket we
    //     cannot see. Keep polling and let the deadline decide.
    //   unknown -- the probe itself could not run (no lsof, EACCES). This
    //     refuses too, because classifyPortOwner's own rule is never to mistake
    //     "couldn't look" for "safe to kill", and confirming is the more
    //     destructive of the two answers available here: it is an unconditional
    //     app.exit(0) that also makes the next launch treat the holder as local,
    //     which is what sends it X-Internal-Secret. Refusing costs a surfaced
    //     failure on a host whose port-probe tooling is missing -- a state both
    //     platforms have their own lister for, so it is a broken host rather
    //     than a platform -- and the failure path re-offers the button. Waiting
    //     instead would only reach the deadline and then report a successor that
    //     never served, which is the wrong cause.
    const successorOwnsPort = async () => {
      const owner = await probeGatewayPortOwner(expectPort);
      if (owner === "kirocrew" || owner === "service") return "confirm";
      if (owner === "foreign") return "foreign";
      if (owner === "unknown") {
        glog(
          `successor check: listener probe unavailable on :${expectPort} `
          + "-- refusing to hand over to a holder that cannot be identified",
        );
        return "unverifiable";
      }
      return "wait";
    };
    const poll = async () => {
      pollTimer = null;
      if (settled) return;
      // The splash loads asynchronously and may have missed the first send.
      sendStatus(restartingStatus);
      const readiness = await fetchGatewayReadiness(readyUrl);
      if (settled) return;
      if (readiness === "ready" || readiness === "starting") {
        const verdict = await successorOwnsPort();
        if (settled) return;
        if (verdict === "confirm") { confirm(); return; }
        if (verdict === "foreign") {
          fail(`:${expectPort} is held by another process, not the restarted app`);
          return;
        }
        if (verdict === "unverifiable") {
          // Its gateway DID answer; what could not be established is which
          // process holds the port. So this is not a successor that failed, and
          // the caller is told so by name -- the kill still happens, because two
          // live instances with the lock held by the one we cannot vouch for is a
          // worse state than one surfaced failure.
          fail(
            `could not check which process holds :${expectPort}, so this app kept running instead of handing over`,
            { failureReason: "successor-unverifiable" },
          );
          return;
        }
      }
      pollTimer = setTimeoutFn(poll, SUCCESSOR_POLL_MS);
    };

    successor.once("spawn", () => {
      if (settled) return;
      glog(`successor app started (pid ${successor.pid}) — waiting for its gateway to answer on :${expectPort}`);
      // The successor is a primary local gateway it spawns itself, so the budget
      // it will apply is the watchSpawn one -- on Windows 120s, elsewhere 30s.
      const readyTimeoutMs = gatewayWaitTimeoutMs({
        platform: processObj.platform,
        watchSpawn: true,
      }) + SUCCESSOR_BOOT_MARGIN_MS;
      deadlineTimer = setTimeoutFn(
        () => fail(`did not answer on :${expectPort} within ${readyTimeoutMs / 1000}s`),
        readyTimeoutMs,
      );
      void poll();
    });
    successor.once("error", (error) => {
      gone = true;
      fail(`failed to start (${error.code || error.message}) from ${target}`);
    });
    successor.once("exit", (code, signal) => {
      gone = true;
      fail(`exited (code=${code} signal=${signal}) before its gateway answered`);
    });
  }

  function sendStatus(message) {
    mainWindow()?.webContents?.send("status", message);
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

  // NOTE: /api/health carries app identity; /api/status does not.
  function fetchHealthInfo(healthUrl = `${BACKEND_URL}${HEALTH_IDENTITY_PATH}`) {
    return new Promise((resolve) => {
      const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
        let body = "";
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          try { resolve(JSON.parse(body)); } catch { resolve(null); }
        });
      });
      req.on("error", () => resolve(null));
      req.on("timeout", () => { req.destroy(); resolve(null); });
    });
  }

  // Whether ANYTHING is serving on a port, as opposed to what it says. The
  // readiness classifier cannot answer this: it folds a legacy gateway's 404
  // and a refused connection into the same "unknown", and a handoff that treats
  // that as an empty port hands a successor a port someone else holds. Only a
  // transport failure is an empty port. A connection that opens and then says
  // nothing counts as occupied, because refusing a handoff costs a surfaced
  // failure while confirming a stranger's costs the app.
  function portHasAnyResponder(url) {
    return new Promise((resolve) => {
      const req = http.get(url, { timeout: 2000 }, (response) => {
        response.resume();
        resolve(true);
      });
      req.on("error", () => resolve(false));
      req.on("timeout", () => { req.destroy(); resolve(true); });
    });
  }

  // /api/status and /api/health remain 200 while a gateway drains. /api/ready
  // is the only probe that prevents adopting a process which is about to exit.
  function fetchGatewayReadiness(readyUrl = `${BACKEND_URL}${READY_PATH}`) {
    return new Promise((resolve) => {
      const req = http.get(readyUrl, { timeout: 2000 }, (res) => {
        let body = "";
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          let payload = null;
          try { payload = JSON.parse(body); } catch { /* classify on status alone */ }
          resolve(classifyGatewayReadiness(res.statusCode, payload));
        });
      });
      req.on("error", () => resolve("unknown"));
      req.on("timeout", () => { req.destroy(); resolve("unknown"); });
    });
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
  // waiting paths below need exactly this, and asking it here keeps them off the
  // identity predicate: they would otherwise pay for a command-line judgement
  // whose answer they discard, and inherit its trust in what a process claims
  // to be.
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

  async function isCurrentBundleGateway() {
    if (!app.isPackaged || IS_WIN || !path.isAbsolute(processObj.resourcesPath || "")) return false;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return false;
    const command = (await psCommand(pids[0])).trim();
    const bundleRoot = path.join(processObj.resourcesPath, "backend-dist") + path.sep;
    return command.startsWith(bundleRoot);
  }

  /**
   * The one dialog about a local gateway this app found already holding its
   * port. `stale` is a same-family gateway older than this app; `stuck` is a
   * gateway for this app's own data folder that keeps failing its health check.
   * Both name the same lock-checked command, so the copy is built in one place.
   *
   * @returns {Promise<"continue"|"stop-restart"|"quit">}
   */
  async function askAboutExistingGateway(kind, { localOwner, oldVersion = "" }) {
    const stopCommand = `kirocrew stop --port ${PORT}`;
    const serviceNote = localOwner === "service"
      ? "\nIf the gateway starts again automatically, stop or update the service that restarts it."
      : "";
    if (kind === "stuck") {
      const { response } = await dialog.showMessageBox({
        type: "warning",
        message: "The gateway is not responding.",
        detail: `A Kiro Crew gateway for this data folder is running on port ${PORT} but has not answered for at least ${Math.round(STUCK_GATEWAY_GRACE_MS / 1000)} seconds.\n\nStop and restart stops that gateway and starts a fresh one. Anything the stuck gateway was still doing is stopped with it.${serviceNote ? "\nIf a background service restarts it automatically, Kiro Crew connects to it once it answers." : ""}`,
        buttons: ["Stop and restart", "Quit"],
        defaultId: 0,
        cancelId: 1,
      });
      return response === 0 ? "stop-restart" : "quit";
    }
    if (kind === "stuck-manual") {
      // No pidfd here to pin the stop to the checked process, so the app
      // does not stop it. It names the lock-checked command instead.
      await dialog.showMessageBox({
        type: "warning",
        message: "The gateway is not responding.",
        detail: `A Kiro Crew gateway for this data folder is running on port ${PORT} but has not answered for at least ${Math.round(STUCK_GATEWAY_GRACE_MS / 1000)} seconds.\n\nTo fix it, quit Kiro Crew.\nRun this command in Terminal:\n${stopCommand}${serviceNote}\nThen reopen Kiro Crew.`,
        buttons: ["Quit"],
        defaultId: 0,
        cancelId: 0,
      });
      return "quit";
    }
    const { response } = await dialog.showMessageBox({
      type: "warning",
      message: "The gateway is still running an older version.",
      detail: `This app is version ${app.getVersion()}. The gateway is still running version ${oldVersion}.\n\nContinue will try to connect to the existing gateway; updated features may be unavailable.\n\nTo finish the update, quit Kiro Crew.\nRun this command in Terminal:\n${stopCommand}${serviceNote}\nThen reopen Kiro Crew.`,
      buttons: ["Continue with existing gateway", "Quit"],
      defaultId: 0,
      cancelId: 0,
    });
    return response === 1 ? "quit" : "continue";
  }

  // The single LISTEN pid on PORT when it holds this app's own data folder,
  // else 0. Two facts, both required: gateway.lock names that pid, and that
  // pid has gateway.lock open right now. The recorded pid alone survives its
  // writer, so a reused pid serving another home would match it; the open
  // descriptor is what ties the listener to THIS home. lsof reports the
  // kernel-resolved path, so a symlinked home is compared by its real path.
  // POSIX only: Windows has no lsof, and an unproven holder keeps today's
  // spawn path.
  async function sameHomeGatewayPid() {
    if (IS_WIN) return 0;
    const pids = await snapshotGatewayPortPids(PORT);
    if (pids?.length !== 1) return 0;
    const lockFile = path.join(KIROCREW_HOME, "gateway.lock");
    let recorded = NaN;
    try {
      recorded = parseInt(String(fs.readFileSync(lockFile, "utf8")).trim(), 10);
    } catch {
      return 0;
    }
    if (!Number.isInteger(recorded) || recorded !== pids[0]) return 0;
    let realLock = lockFile;
    try { realLock = fs.realpathSync(lockFile); } catch { /* compare the literal path */ }
    const held = await lsofHoldsFile(pids[0], lockFile)
      || (realLock !== lockFile && await lsofHoldsFile(pids[0], realLock));
    return held ? pids[0] : 0;
  }

  // Run the CLI's own stop for PORT, pinned to the pid this app proved. With
  // --expect-pid the CLI re-reads the listener and this home's gateway.lock
  // right before the signal and refuses, signalling nothing, unless that pid
  // still holds both -- so a gateway that replaced it is never stopped. POSIX
  // only: its one caller is behind sameHomeGatewayPid.
  function runGatewayStop(pid) {
    const { KIROCREW_PORT: _ignored, ...cleanEnv } = processObj.env;
    return new Promise((resolve) => {
      execFile(resolveGatewayBin(), ["stop", "--port", String(PORT), "--expect-pid", String(pid)], {
        timeout: STUCK_GATEWAY_STOP_TIMEOUT_MS,
        windowsHide: true,
        env: { ...cleanEnv, KIROCREW_HOME },
      }, (error, stdout, stderr) => {
        const output = `${stdout || ""}${stderr || ""}`.trim();
        if (error) glog(`stuck gateway: \`kirocrew stop --port ${PORT}\` failed: ${error.message}${output ? ` — ${output.slice(0, 200)}` : ""}`);
        else glog(`stuck gateway: \`kirocrew stop --port ${PORT}\` finished${output ? ` — ${output.slice(0, 200)}` : ""}`);
        resolve(!error);
      });
    });
  }

  /**
   * Launch found the port held but no health answer. Today's answer is to
   * spawn anyway. That stays the answer for every holder except one: a local
   * Kiro Crew gateway for this app's own data folder that keeps failing its
   * health check past the grace window. That one is offered a stop-and-restart.
   *
   * @returns {Promise<"spawn"|"healthy"|"abort"|{stopFailed:number[]}>}
   */
  async function resolveUnresponsiveHolder() {
    if (!runLocalGateway || getRemoteHostConfig(store, PORT)?.host) return "spawn";
    if ((await probeGatewayPortBinding(PORT)) !== "bound") return "spawn";
    const localOwner = await probeGatewayPortOwner(PORT);
    if (localOwner !== "kirocrew" && localOwner !== "service") return "spawn";
    const provenPid = await sameHomeGatewayPid();
    if (!provenPid) return "spawn";
    sendStatus("Waiting for the existing gateway to respond…");
    // Counted in polls rather than read off the clock, so the injected timer
    // fully decides how long this waits.
    for (let waited = 0; waited < STUCK_GATEWAY_GRACE_MS; waited += STUCK_GATEWAY_POLL_MS) {
      await new Promise((resolve) => setTimeoutFn(resolve, STUCK_GATEWAY_POLL_MS));
      try { await checkBackend(); return "healthy"; }
      catch { /* still not answering */ }
    }
    glog(`stuck gateway: :${PORT} is held by this data folder's gateway (owner=${localOwner}) and failed its health check for ${STUCK_GATEWAY_GRACE_MS}ms — offering stop and restart`);
    // `kirocrew stop --expect-pid` pins its signal with a pidfd, which only
    // Linux has. Elsewhere the app names the command and stops nothing.
    if (processObj.platform !== "linux") {
      await askAboutExistingGateway("stuck-manual", { localOwner });
      return "abort";
    }
    const choice = await askAboutExistingGateway("stuck", { localOwner });
    if (choice !== "stop-restart") return "abort";
    // The dialog can stay open for minutes. Prove the holder again right
    // before the stop, and require the same pid the grace window judged: a
    // replacement (even one for this home, restarted by a service) has never
    // been shown to be stuck, so it goes through the ordinary decision.
    // A port that emptied meanwhile (the stuck gateway exited on its own) is
    // free to spawn into; any other holder goes through the ordinary decision.
    const portNowFree = async () => (await probeGatewayPortBinding(PORT)) === "free";
    if ((await sameHomeGatewayPid()) !== provenPid) {
      if (await portNowFree()) {
        glog(`stuck gateway: :${PORT} emptied while the dialog was open — starting the bundled backend`);
        return "spawn";
      }
      glog(`stuck gateway: :${PORT} changed holder while the dialog was open — re-validating instead of stopping`);
      return "healthy";
    }
    sendStatus("Stopping the previous gateway…");
    const incumbentPids = await snapshotGatewayPortPids(PORT);
    if (!(await runGatewayStop(provenPid))) {
      // The CLI refuses when the proven pid no longer holds the port. That is
      // a failed stop only if the proven pid is still there.
      if (await portNowFree()) {
        glog(`stuck gateway: :${PORT} emptied before the stop — starting the bundled backend`);
        return "spawn";
      }
      const holders = await snapshotGatewayPortPids(PORT);
      if (holders?.length && !holders.includes(provenPid)) {
        glog(`stuck gateway: :${PORT} was taken by pid ${holders.join(", ")} before the stop — re-validating the new holder`);
        return "healthy";
      }
      return { stopFailed: incumbentPids || [] };
    }
    // A fast service manager can rebind before the port is ever seen free.
    // A different listener pid is that new gateway, not a failed stop.
    const takenByNewPid = async () => {
      if (localOwner !== "service") return false;
      const holders = await snapshotGatewayPortPids(PORT);
      if (!holders?.length || holders.some((pid) => (incumbentPids || []).includes(pid))) return false;
      glog(`stuck gateway: :${PORT} was taken by a new pid ${holders.join(", ")} after the stop — re-validating the new holder`);
      return true;
    };
    if (await takenByNewPid()) return "healthy";
    if (!(await waitForPortFree())) {
      if (await takenByNewPid()) return "healthy";
      return { stopFailed: incumbentPids || [] };
    }
    if (localOwner === "service") {
      // A service manager may bring the gateway straight back. Give it the
      // same bounded rebind grace the drain path does, then re-validate the
      // new holder through the ordinary decision instead of spawning over it.
      sendStatus("Waiting for the gateway to restart…");
      const verdict = await waitForServiceRebind({
        isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
        sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
      });
      if (verdict === "rebound") {
        glog(`stuck gateway: :${PORT} re-bound after the stop — re-validating the new holder`);
        return "healthy";
      }
    }
    await waitForIncumbentExit(incumbentPids, "stuck gateway");
    glog(`stuck gateway: :${PORT} released — starting the bundled backend`);
    return "spawn";
  }

  async function resolveGatewayConflict(rebindDepth = 0) {
    const health = await fetchHealthInfo();
    // A remote host configured for this port makes the holder a tunnel by
    // construction. Nothing local may evict it.
    const remoteHost = getRemoteHostConfig(store, PORT)?.host || "";
    if (remoteHost) {
      glog(`:${PORT} is a configured remote host (${remoteHost}) — holder treated as non-local`);
    }
    const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(PORT);
    const bundledGateway = runLocalGateway
      && health?.version !== app.getVersion()
      && (localOwner === "kirocrew" || localOwner === "service")
      && await isCurrentBundleGateway();
    const decision = decideGatewayAction(app.getVersion(), health, {
      localOwner, bundledGateway,
    });
    // The rule this enforces, as one sentence: adopt a responder on this port
    // only when the port can be attributed either to us (a Kiro Crew LISTEN
    // owner, or a service-managed one) or to a crew the user configured here.
    //
    // It is deliberately NOT gated on which reuse reason decideGatewayAction
    // returned. That function has five paths to `reuse` -- an unclassified shell,
    // an unidentified payload, a same-family gateway, a dev-family one, and a
    // cross-family one whose LISTEN owner is not ours -- and a gateway reached
    // through `ssh -L` answers with a perfectly ordinary same-family payload, so
    // a gate on one reason value misses four. `mintLocalToken` then posts this
    // machine's `.local_secret` to it, because the mint only requires a literal
    // loopback origin and a tunnel's local end is one. The header goes out before
    // any 403 is seen, so there is no recovery after the fact.
    //
    // A `foreign` owner with no `remoteHosts` entry is exactly "cannot attribute
    // it". `kirocrew` and `service` still adopt, and so do `none` and `unknown`:
    // the fail-open this narrows is preserved wherever the probe did not
    // positively find someone else holding the port.
    //
    // `warn-stale` cannot reach here with a `foreign` owner: the bundled-path
    // probe above only runs for a `kirocrew` or `service` owner, so that action
    // is unreachable without positive local attribution.
    if (decision.action === "reuse" && localOwner === "foreign" && !remoteHost) {
      glog(`:${PORT} is served by a process this app did not start and no remote crew is configured there — refusing to adopt it`);
      return "foreign-holder";
    }
    if (decision.action === "warn-stale") {
      glog(`bundled gateway ${decision.oldVersion} predates app ${app.getVersion()} — warning before reuse`);
      const choice = await askAboutExistingGateway("stale", {
        localOwner, oldVersion: decision.oldVersion,
      });
      if (choice === "quit") return "abort";
    }
    if (decision.action === "reuse" || decision.action === "warn-stale") {
      // Adopt-or-wait. Only a positive shutting-down verdict refuses adoption;
      // every ambiguity preserves historical fail-open reuse. Remote tunnels are
      // exempt because their local socket is not expected to clear on restart.
      let adoptedDraining = false;
      const readiness = remoteHost ? "unknown" : await fetchGatewayReadiness();
      if (readiness === "shutting-down") {
        glog(`gateway on :${PORT} answers but /api/ready reports shutting-down — refusing to adopt a draining gateway`);
        sendStatus("Waiting for the previous gateway to exit…");
        const drainingPids = await snapshotGatewayPortPids(PORT);
        if (unverifiedIncumbent(drainingPids)) {
          glog(`drain: could not capture the incumbent PID on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
          return "probe-failed";
        }
        if (await waitForPortFree()) {
          if (localOwner === "service") {
            // A service may be between release and manager rebind. Orphans also
            // classify as service, so wait a bounded grace and then spawn.
            sendStatus("Waiting for the gateway to restart…");
            const verdict = await waitForServiceRebind({
              isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
              sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
            });
            if (verdict === "rebound") {
              // Revalidate the replacement through the full identity/readiness
              // decision. One recursive pass prevents an unbounded drain loop.
              if (rebindDepth < 1) {
                glog(`service rebind: :${PORT} re-bound within the grace window — re-validating the new holder`);
                return resolveGatewayConflict(rebindDepth + 1);
              }
              glog(`service rebind: :${PORT} re-bound again at depth ${rebindDepth} — treating as adopt-anyway to avoid a validation loop`);
            } else {
              glog(`service rebind: :${PORT} stayed free past the grace window (no manager respawned it) — spawning fresh`);
            }
          }
          if (localOwner !== "service" || (await probeGatewayPortBinding(PORT)) === "free") {
            await waitForIncumbentExit(drainingPids, "drain");
            glog(`drain complete: :${PORT} released — spawning a fresh gateway`);
            return "spawn";
          }
        }
        // The holder is not ours to kill. Adopt loudly and let bounded recovery
        // respawn after its eventual death instead of spawning into EADDRINUSE.
        glog(`drain wait timed out — :${PORT} still held; adopting anyway (recovery will respawn if it dies)`);
        adoptedDraining = true;
      }
      glog(`reusing existing gateway on :${PORT} (${decision.reason}) — bundled backend NOT spawned`);
      gatewayOwnership = classifyAdoptedGateway({ reason: decision.reason, localOwner });
      sendStatus(adoptedDraining
        ? "Connecting to the existing gateway…"
        : "Gateway already running ✓");
      return "reuse";
    }

    return resolveFamilyConflict(decision);
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
      userError(`spawn REFUSED: no bundled backend under ${processObj.resourcesPath || "(no resourcesPath)"} — a packaged app does not fall back to ${bin} on PATH`);
      gatewayStartFailure = {
        error: describeIncompleteBundle([], { autoRetry: true }),
        incompleteBundle: true,
        bundled: true,
      };
      sendStatus(INSTALLING_STATUS);
      resolve(false);
      return;
    }

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

    let spawnBin = bin;
    let spawnArgs = ["gateway", "--no-open", "--port", String(PORT)];
    // Node refuses .cmd/.bat without shell:true. Use the relocatable bundled
    // Python directly instead of opening the command-injection-prone shell path.
    // `-P` mirrors the shim this replaces (bin/kirocrew.cmd): it keeps the spawn
    // cwd off sys.path, so a stdlib-named directory there cannot shadow the
    // interpreter's own standard library.
    // The refusal above already required python.exe beside a .cmd shim.
    if (bin.endsWith("kirocrew.cmd")) {
      spawnBin = path.resolve(path.dirname(bin), "..", "python.exe");
      spawnArgs = ["-s", "-P", "-m", "kiro_crew", ...spawnArgs];
    }

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

  /**
   * POST /api/shutdown, then POSIX SIGTERM -> SIGKILL or the bounded Windows
   * tree kill. The updater awaits this method before swapping bundle bytes.
   * Ownership intentionally remains "spawned" after stop: if an update install
   * fails, the recovery hook must know it is allowed to respawn the child that
   * the updater deliberately stopped.
   */
  async function stopGatewayGracefully({ timeoutMs = 15000 } = {}) {
    const gateway = gatewayProcess;
    if (!gateway || gateway.exitCode !== null) {
      gatewayProcess = null;
      spawnedExecutablePaths = [];
      return;
    }
    glog("Stopping gateway gracefully...");
    // Resolve secrets at call time. The gateway accepts only the secret for its
    // current boot; trying every readable candidate prevents a stale copy from
    // forcing the hard-signal path and skipping session/memory/cron flushes.
    const candidates = secretCandidates();
    const currentHome = path.dirname(candidates[0]);
    const secrets = [];
    for (const candidate of candidates) {
      try {
        const value = fs.readFileSync(candidate, "utf8").trim();
        if (value) secrets.push(value);
      } catch { /* absent or unreadable */ }
    }
    await stopGatewayProcessGracefully(gateway, {
      backendUrl: BACKEND_URL,
      kirocrewHome: currentHome,
      secrets,
      timeoutMs,
      // 3s PowerShell + 2s WMIC + 5s taskkill fits inside the 18s hard
      // shutdown deadline. The tree sweep is awaited because taskkill can emit
      // the parent's exit while descendants are still being reaped.
      killTreeFn: killGatewayTreeOnWindowsBounded,
      // POSIX: the child may be a launcher shim that forked the gateway. These
      // let the stop find and stop that gateway once the shim is gone.
      listDescendantsFn: posixDescendantPids,
      getCommandFn: psCommand,
      signalPidFn: (pid, signal) => processObj.kill(pid, signal),
    });
    gatewayProcess = null;
    spawnedExecutablePaths = [];
  }

  function killGatewayTreeOnWindowsBounded(pid) {
    return windowsTaskkill(pid, {
      isTrustedCommand: isTrustedWindowsGatewayCommand,
      getCommandFn: (probePid) => windowsProcessCommand(probePid, {
        powershellTimeoutMs: 3000,
        wmicTimeoutMs: 2000,
      }),
      timeoutMs: 5000,
    });
  }

  // Wedge recovery has no graceful endpoint: the loop serving it is frozen.
  // POSIX lets the parent reap its own children; Windows requires a tree kill or
  // detached kiro-cli/MCP descendants survive with the data-home locks.
  async function killGatewayProcessTree(gateway, signal) {
    if (!gateway || gateway.exitCode !== null) return;
    const killPid = () => {
      try { gateway.kill(signal); }
      catch (error) { glog(`${signal} failed: ${error && error.message}`); }
    };
    if (!gateway.pid) { killPid(); return; }
    if (!IS_WIN) {
      // The child may be a launcher that forked the real gateway (a package
      // manager's shim) instead of exec'ing it. Killing only the child leaves
      // that gateway alive, re-parented to init, holding the port and lock --
      // and a PPID-1 holder is then treated as service-managed and never
      // evicted. List the tree BEFORE the kill re-parents it.
      const descendants = await posixDescendantPids(gateway.pid);
      for (const pid of descendants) {
        try { processObj.kill(pid, signal); }
        catch (error) { glog(`${signal} descendant pid=${pid} failed: ${error && error.message}`); }
      }
      if (descendants.length) glog(`${signal} sent to ${descendants.length} descendant(s) of pid=${gateway.pid}`);
      killPid();
      return;
    }
    try {
      await windowsTaskkill(gateway.pid, {
        isTrustedCommand: isTrustedWindowsGatewayCommand,
      });
    } catch (error) {
      glog(`tree kill refused (${error && error.message}) — falling back to a single-pid kill`);
      killPid();
    }
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

  /**
   * Gateway failures need a bounded window with a scrollable launch-log pane;
   * native message boxes grow vertically with the entire detail string.
   */
  function showGatewayErrorDialog(parentWindow, options) {
    const {
      title: configuredTitle,
      message: configuredMessage,
      logTail,
      logPath: displayedLogPath,
      portConflict,
      noRetry = false,
      localGatewayOff = false,
      offerLocalStart = false,
      crewAction = null,
      primaryAction: configuredPrimaryAction,
      primaryLabel: configuredPrimaryLabel,
      retryLabel: configuredRetryLabel,
      accentQuit: accentQuitAction = false,
      showQuitButton: configuredShowQuitButton,
      // Installing-kind failures only. Re-run every INSTALLING_PROBE_MS while the
      // dialog is up; the message tracks the falling part count and, once the
      // probe reports complete, the dialog fires its own Retry. That retry is the
      // SAME action a click produces, resolved through the same closed handshake,
      // so it re-enters startGateway exactly once and by the caller's path -- no
      // second respawn owner beside recoverWedgedGateway or the liveness monitor.
      // The probe stands down for good, without firing, once the app is quitting
      // or an update install is dispatched: the updater deliberately stops the
      // gateway while it swaps the bundle, and a retry then would relaunch what
      // it is replacing.
      autoRetryProbe,
    } = options;
    const showQuitButton = configuredShowQuitButton ?? !noRetry;
    // The installing dialog opens on what the bundle looks like NOW, not on the
    // refusal-time text: Reveal Log closes and reopens this dialog through the
    // caller's loop, and the hoisted message would paint the original count
    // again after the probe had already shown it falling.
    const opening = typeof autoRetryProbe === "function"
      ? (() => {
        try { return nextInstallingDialogState(autoRetryProbe()); }
        catch { return null; }
      })()
      : null;
    const title = opening && !opening.complete ? opening.title : configuredTitle;
    const message = opening && !opening.complete ? opening.message : configuredMessage;
    // The other escape hatch from the client-only state: name the crew on another
    // machine, or correct the address already stored. Without it this dialog
    // offers no way to reach a crew, so a launch that finds nothing can only
    // retry the same state or quit.
    const remoteCrew = noRetry ? null : crewAction;
    // Client-only mode launched nothing, so there is no launch to diagnose:
    // the log on disk belongs to earlier runs, and rendering that tail is what
    // made a state the user asked for read as a crash report.
    const showLog = !localGatewayOff;

    return new Promise((resolve) => {
      const dark = nativeTheme.shouldUseDarkColors;
      const hasParent = parentWindow && !parentWindow.isDestroyed();
      const errorWindow = new BrowserWindow({
        // Four actions share this row when a crew can be named here, and each
        // label is one line only if the row has room for it.
        width: remoteCrew ? 700 : 620,
        // Without the log pane there is nothing to scroll, so the tall window
        // would open mostly empty under a two-line message.
        height: showLog ? 460 : 260,
        minWidth: 460,
        minHeight: showLog ? 320 : 200,
        resizable: true,
        useContentSize: true,
        parent: hasParent ? parentWindow : undefined,
        modal: !!hasParent,
        backgroundColor: dark ? "#1e293b" : "#f8fafc",
        webPreferences: { nodeIntegration: false, contextIsolation: true },
      });
      errorWindow.setMenu(null);

      const escapeHtml = (value) => String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      // Only the Enter key binding reads this: the row builds each button's own
      // label below, so a separate primary label would be a second place for the
      // rendered text to drift from the action it fires.
      const primaryAction = configuredPrimaryAction
        || (noRetry ? "quit" : (portConflict ? "force-retry" : "retry"));
      // Client-only mode cannot reach dashboard Settings to reverse the choice,
      // so this pre-dashboard UI carries the only in-app way back. The spawn
      // binds THIS port: on a crew's port a gateway started in place would
      // shadow that crew, so there the action re-runs port selection in a fresh
      // process instead. The caller decides whether that route is available.
      //
      // Both routes stay on screen whenever both exist, and ONLY the accent
      // moves -- which means the row cannot be built primary-first. Ordering by
      // emphasis moved Start Local Gateway from the third slot to the first and
      // pushed Retry the other way, so between two openings of a dialog carrying
      // the same title the leftmost button did two different things and a habit
      // press landed on the wrong recovery. Each action keeps its own slot and
      // the accent travels between them: the retry-family action first, then the
      // crew form, then the local-start route.
      const promotedLocalStart = primaryAction === "enable-retry";
      // Demoted, the retry slot is the one control whose target the surrounding
      // sentence does not give -- that paragraph is about a local start -- so the
      // caller may name the crew in its label. It may name it in the UNPROMOTED
      // states too, and does: labelling only the promoted ones made one control
      // read as two, "Retry" in one opening of this dialog and "Retry <host>" in
      // the next. Only where the slot really is a plain retry, though -- a
      // force-stop or a quit is a different action and keeps its own words.
      const retrySlotAction = promotedLocalStart
        ? "retry"
        : (configuredPrimaryAction
          || (noRetry ? "quit" : (portConflict ? "force-retry" : "retry")));
      const retrySlotLabel = promotedLocalStart
        ? (configuredRetryLabel || "Retry")
        : (configuredPrimaryLabel
          || (noRetry
            ? "Quit"
            : (portConflict ? "Force-stop & Retry" : (configuredRetryLabel || "Retry"))));
      // One state's paragraph asks for Quit: the ownership probe could not run, so
      // clicking either of the two action buttons reaches the same check, and
      // quitting and reopening is what recovers. The accent follows the sentence
      // in this dialog, so here it sits on Quit and every other slot is a
      // secondary -- an orange Retry under text that says to quit is the
      // contradiction this removes.
      const accentQuit = !!accentQuitAction && showQuitButton;
      const retrySlotButton = `<button class="${promotedLocalStart || accentQuit ? "cancel" : "ok"}" `
        + `onclick="act('${retrySlotAction}')">${escapeHtml(retrySlotLabel)}</button>`;
      // Present whenever the route exists, accented only when it is the primary,
      // so it is never rendered twice and never silently withdrawn.
      const enableButton = offerLocalStart && !noRetry
        ? `<button class="${promotedLocalStart && !accentQuit ? "ok" : "cancel"}" `
          + "onclick=\"act('enable-retry')\">Start Local Gateway</button>"
        : "";
      // The label names which of the two this is, because correcting a stored
      // address and naming a first one are the same form and different intents.
      const remoteSetupButton = remoteCrew
        ? `<button class="cancel" onclick="act('configure-remote')">`
          + `${remoteCrew === "edit" ? "Edit" : "Add"} Remote Crew…</button>`
        : "";
      const foreground = dark ? "#e2e8f0" : "#1e293b";
      const muted = dark ? "#94a3b8" : "#64748b";
      // One blank line in the message is a paragraph break. These messages carry
      // two stories at once -- what happened to the local start, and what to do
      // about the crew -- and run together they read as a single wall a reader
      // gives up on partway through. The branches join the two with a blank line
      // and the rendering honours it; a message with no blank line is unchanged.
      const renderParagraphs = (text) => String(text)
        .split(/\n{2,}/)
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => `<p>${escapeHtml(part)}</p>`)
        .join("");
      const messageParagraphs = renderParagraphs(message);
      const logPane = showLog
        ? `<div class="pathline">${escapeHtml(displayedLogPath)}</div>`
          + `<pre class="log">${escapeHtml(logTail || "(launch log is empty)")}</pre>`
        : "";
      const revealButton = showLog
        ? "<button class=\"cancel\" onclick=\"act('reveal')\">Reveal Log</button>"
        : "";
      const html = `<!DOCTYPE html><html><head><style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,sans-serif; padding:20px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${foreground};
          display:flex; flex-direction:column; height:100vh; }
        .title { font-size:15px; font-weight:700; margin-bottom:6px; }
        .msg { font-size:13px; line-height:1.45; margin-bottom:10px; }
        .msg p + p { margin-top:8px; }
        .pathline { font-size:11px; color:${muted}; margin-bottom:6px; word-break:break-all; }
        pre.log { flex:1 1 auto; min-height:120px; overflow:auto; white-space:pre;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; line-height:1.45;
          padding:10px; border-radius:6px; border:1px solid #334155; background:#0f172a; color:#e2e8f0;
          margin-bottom:14px; }
        .row { display:flex; gap:8px; flex:0 0 auto; }
        button { flex:1; padding:9px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
        .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
        .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; }
        .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }
        button:disabled { opacity:.45; cursor:default; }
      </style></head><body>
        <div class="title">${escapeHtml(title)}</div>
        <div class="msg">${messageParagraphs}</div>
        ${logPane}
        <div class="row">
          ${retrySlotButton}
          ${remoteSetupButton}
          ${enableButton}
          ${revealButton}
          ${showQuitButton ? `<button class="${accentQuit ? "ok" : "cancel"}" onclick="act('quit')">Quit</button>` : ""}
        </div>
        <script>
          function act(a){ document.title = 'mc-action:' + a; window.close(); }
          // Enter presses the primary BUTTON rather than calling act() directly,
          // so a button the completion paint has disabled stays disabled for the
          // keyboard too; Escape maps to Quit, which is never disabled.
          document.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
              for (const b of document.querySelectorAll('button')) {
                if (b.getAttribute('onclick') !== "act('${primaryAction}')") continue;
                if (!b.disabled) b.click();
                break;
              }
            }
            if (e.key === 'Escape') act('quit');
          });
        </script>
      </body></html>`;

      let action = null;
      let probeTimer = null;
      const stopProbe = () => {
        if (probeTimer === null) return;
        clearIntervalFn(probeTimer);
        probeTimer = null;
      };
      if (typeof autoRetryProbe === "function") {
        let paintedMessage = message;
        probeTimer = setIntervalFn(() => {
          if (errorWindow.isDestroyed() || quitting() || installingUpdate) {
            stopProbe();
            return;
          }
          let state;
          try {
            state = nextInstallingDialogState(autoRetryProbe());
          } catch (probeError) {
            glog(`install probe failed (${probeError && probeError.message}); will probe again`);
            return;
          }
          if (state.complete) {
            // Stop before closing: the closed handler also stops, but a tick
            // must never be able to fire twice for one dialog.
            stopProbe();
            glog("bundle complete — retrying the gateway from the install dialog after the linger");
          }
          if (state.message !== paintedMessage) {
            paintedMessage = state.message;
            // Same paragraph rendering as the first paint; the markup is built
            // here from escaped text, so the page only ever receives HTML this
            // process composed.
            errorWindow.webContents
              .executeJavaScript(`document.querySelector(".msg").innerHTML = ${JSON.stringify(renderParagraphs(state.message))}`)
              .catch(() => { /* window closing under the paint */ });
          }
          if (state.complete) {
            // The whole frame says "finished", not just the paragraph: the title
            // moves with it, Retry is disabled and relabelled because the retry
            // is already under way (Enter routes through the same button, so the
            // keyboard cannot fire what the mouse cannot), and Quit says what it
            // now does. A click during the linger still wins: it sets the action
            // and closes first, and this close finds the window already gone.
            errorWindow.webContents
              .executeJavaScript(
                `document.querySelector(".title").textContent = ${JSON.stringify(state.title)};`
                + " for (const b of document.querySelectorAll('button')) {"
                + " if (b.getAttribute('onclick') === \"act('retry')\") { b.disabled = true; b.textContent = 'Starting…'; }"
                + " if (b.getAttribute('onclick') === \"act('quit')\") b.textContent = 'Quit anyway'; }",
              )
              .catch(() => { /* window closing under the paint */ });
            setTimeoutFn(() => {
              if (errorWindow.isDestroyed()) return;
              // The retry is decided HERE, not at the tick: a quit or an update
              // install that began during the linger must find no retry armed,
              // or startGateway would spawn into the bundle the updater is
              // swapping. The dialog then simply stays, with its buttons live.
              if (quitting() || installingUpdate) {
                glog("bundle complete but the app is quitting or installing an update — leaving the dialog to the user");
                return;
              }
              // A click lands as a title update first and the close a beat
              // later; in that gap the window is not destroyed yet, and the
              // user's choice must not be overwritten by the auto-retry.
              if (action !== null) return;
              action = "retry";
              errorWindow.close();
            }, INSTALLING_COMPLETE_LINGER_MS);
          }
        }, INSTALLING_PROBE_MS);
      }
      errorWindow.on("page-title-updated", (_event, updatedTitle) => {
        if (updatedTitle && updatedTitle.startsWith("mc-action:")) {
          // The user chose; a probe landing after this must not overrule them.
          stopProbe();
          action = updatedTitle.slice("mc-action:".length);
        }
      });
      errorWindow.on("closed", () => {
        stopProbe();
        resolve(action || "quit");
      });
      errorWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
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

  async function reconnectExternalGateway(window) {
    const webContents = window.webContents;
    try { webContents.loadFile(path.join(dirname, "loading.html"), { query: splashQuery(window, { reconnect: true }) }); }
    catch { /* window may be tearing down */ }
    if (!window || window.isDestroyed() || quitting()) return;
    // No reveal here: network/tunnel healing must not re-surface a window the
    // user minimized or hid to tray.
    sendStatus("Connection lost — waiting for the gateway to come back…");
    for (;;) {
      if (!window || window.isDestroyed() || quitting()) return;
      let healthy = false;
      try { await checkBackend(HEALTH_URL); healthy = true; }
      catch { /* still down */ }
      if (healthy) break;
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
    if (!window || window.isDestroyed() || quitting()) return;
    glog("liveness: external gateway reachable again — refetching token and reconnecting");
    gatewayStartFailure = null;
    return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
  }

  async function reconnectOrRespawnAdoptedGateway(window) {
    const webContents = window.webContents;
    try { webContents.loadFile(path.join(dirname, "loading.html"), { query: splashQuery(window, { reconnect: true }) }); }
    catch { /* window may be tearing down */ }
    if (!window || window.isDestroyed() || quitting()) return;
    sendStatus("Gateway stopped responding — waiting for it to recover…");
    const deadline = Date.now() + ADOPTED_RECOVERY_WAIT_MS;
    while (Date.now() < deadline) {
      if (!window || window.isDestroyed() || quitting()) return;
      let healthy = false;
      try { await checkBackend(HEALTH_URL); healthy = true; }
      catch { /* still down */ }
      if (healthy) {
        if (!window || window.isDestroyed() || quitting()) return;
        glog("liveness: adopted local gateway answering again — reconnecting");
        gatewayStartFailure = null;
        return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
      }
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
    if (!window || window.isDestroyed() || quitting()) return;
    glog(`liveness: adopted local gateway did not recover within ${ADOPTED_RECOVERY_WAIT_MS}ms — waiting for :${PORT} to clear, then spawning our own backend`);
    sendStatus("Waiting for the previous gateway to exit…");
    const incumbentPids = await snapshotGatewayPortPids(PORT);
    if (unverifiedIncumbent(incumbentPids)) {
      glog(`liveness: could not capture the incumbent PID on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
      return showUnrecoverableGatewayError(window, PORT, { probeFailed: true });
    }
    if (!(await waitForPortFree())) {
      glog(`liveness: :${PORT} still held by a process we did not spawn — surfacing port-held instead of waiting forever`);
      return showUnrecoverableGatewayError(window, PORT, "held");
    }
    if (!window || window.isDestroyed() || quitting()) return;

    if (gatewayOwnership === "reused-service") {
      // A real manager may rebind after the socket release. Orphans share the
      // same classification, so this is a bounded grace, never an exemption.
      glog("liveness: adopted gateway was service-managed — waiting a bounded grace for its manager to respawn it before spawning our own");
      sendStatus("Waiting for the gateway to restart…");
      const verdict = await waitForServiceRebind({
        isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      });
      if (window.isDestroyed() || quitting()) return;
      if (verdict === "rebound") {
        // Reuse the full boot decision table for the new holder. A weaker ad-hoc
        // health check could silently adopt a cross-family or draining process.
        const owner = await probeGatewayPortOwner(PORT);
        const health = await fetchHealthInfo();
        const decision = decideGatewayAction(app.getVersion(), health, {
          localOwner: owner,
        });
        const readiness = await fetchGatewayReadiness();
        if (window.isDestroyed() || quitting()) return;
        if (decision.action === "reuse" && readiness !== "shutting-down") {
          glog(`liveness: service manager re-bound :${PORT} (owner=${owner}, reason=${decision.reason}, readiness=${readiness}) — reconnecting to the restarted gateway`);
          gatewayOwnership = classifyAdoptedGateway({
            reason: decision.reason,
            localOwner: owner,
          });
          gatewayStartFailure = null;
          return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
        }
        glog(`liveness: :${PORT} was re-bound by an unusable holder (owner=${owner}, action=${decision.action}, readiness=${readiness}) — cannot reconnect or spawn over it`);
        return showUnrecoverableGatewayError(window, PORT, "held");
      }
      glog(`liveness: :${PORT} stayed free past the rebind grace — no manager respawned it; spawning our own backend`);
    }

    await waitForIncumbentExit(incumbentPids, "liveness");
    sendStatus("Starting a fresh gateway…");
    gatewayStartFailure = null;
    await startGateway();
    if (window.isDestroyed() || quitting()) return;
    return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
  }

  /** Reveal only states which need a human decision. */
  function revealForUserDecision(window) {
    if (!window || window.isDestroyed() || quitting()) return;
    // The main window can own an app-level fullscreen hide while this
    // needs-user state belongs to a connection window. Disarm that owner before
    // app.show() or its reassertion hides every window again.
    const primaryWindow = mainWindow();
    if (
      primaryWindow
      && primaryWindow !== window
      && !primaryWindow.isDestroyed()
    ) {
      cancelTrayHide(primaryWindow);
    }
    // Cancel before leaving fullscreen: the fullscreen-exit event can fire the
    // deferred hide listener and immediately undo this reveal.
    cancelTrayHide(window);
    // A fullscreen tray-close hides the whole APP (hide-to-tray.js), and a
    // hidden app ignores a window-level show. Unhide it first or the dialog
    // this reveal precedes parks invisibly. Harmless when the app is visible.
    if (IS_MAC && typeof app.show === "function") app.show();
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
    if (IS_MAC) app.focus({ steal: true });
  }

  async function showUnrecoverableGatewayError(window, failedPort, options = {}) {
    const { variant = "wedged", probeFailed = false } = typeof options === "string"
      ? { variant: options }
      : options;
    if (!window || window.isDestroyed()) return;
    revealForUserDecision(window);
    let logTail = "";
    try { logTail = tailLines(fs.readFileSync(gatewayLogPath(), "utf8"), 60); }
    catch { /* no log yet */ }
    const action = await showGatewayErrorDialog(window, {
      ...unrecoverableGatewayDialog({
        port: failedPort,
        variant,
        probeFailed,
        isPrimaryWindow: window === mainWindow(),
      }),
      logTail,
      logPath: gatewayLogPath(),
      port: failedPort,
      noRetry: true,
    });
    if (window.isDestroyed()) return;
    if (action === "reveal") {
      try { shell.showItemInFolder(gatewayLogPath()); }
      catch { /* best effort */ }
    }
    if (window === mainWindow()) quitApp();
    else window.destroy();
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
        title = INSTALLING_DIALOG_TITLE;
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
    stopGracefully: stopGatewayGracefully,
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
