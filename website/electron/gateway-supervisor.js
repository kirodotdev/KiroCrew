"use strict";

const defaultFs = require("fs");
const defaultOs = require("os");
const defaultPath = require("path");
const defaultHttp = require("http");
const {
  spawn: defaultSpawn,
  execFile: defaultExecFile,
  execFileSync: defaultExecFileSync,
} = require("child_process");

const { findKirocrewBin } = require("./find-bin");
const { buildGatewayEnvironment, gatewayBytecodeEnvironment } = require("./gateway-env");
const { resolveGatewayPath } = require("./mac-env");
const {
  findMissingBundleParts,
  describeIncompleteBundle,
  shouldReclassifyAsInstalling,
  currentAttemptLog,
  SPAWN_MARKER,
} = require("./bundle-integrity");
const { classifyAuthBlock, defaultedPort } = require("./gateway-auth-hint");
const {
  shouldRetryLocalTokenMint,
  tokenMintRetryDelayMs,
  TOKEN_MINT_MAX_RETRIES,
} = require("./token-acquire");
const {
  stopGatewayGracefully: stopGatewayProcessGracefully,
  forceStopPort,
  classifyPortOwner,
  isKirocrewCommand,
} = require("./gateway-stop");
const {
  windowsGatewayExecutablePaths,
  windowsListenPids,
  windowsProcessCommand,
  windowsTaskkill,
} = require("./windows-port");
const {
  gatewayWaitTimeoutMs,
  waitForGateway,
  tailLines,
  isPortInUse,
} = require("./gateway-wait");
const { describeSandboxProfileNeed } = require("./sandbox-profile");
const { createLivenessMonitor } = require("./gateway-liveness");
const {
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  revealWindowForConnect,
  waitForServiceRebind,
  waitForProcessExit,
  snapshotPortPids,
  incumbentSnapshotBlocksRespawn,
  unrecoverableGatewayDialog,
} = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const {
  decideGatewayAction,
  classifyGatewayReadiness,
  FAMILY_META,
  HEALTH_IDENTITY_PATH,
  READY_PATH,
} = require("./instance-guard");
const { getRemoteHostConfig } = require("./host-config");
const { validateRemoteSettings } = require("./validation");
const {
  buildRemoteTokenCommand,
  parseTokenFromStdout,
} = require("./remote-token");
const { fetchLocalToken: fetchTokenFromHome } = require("./local-token");
const { resolveHome, canonicalHome, secretCandidates } = require("./home-dir");
const {
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
} = require("./local-gateway");

const DEFAULT_THEME_ACCENT = "#8E48FF";
const THEME_ACCENT_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const INSTALLING_STATUS = "Finishing installation…";
const POLL_INTERVAL_MS = 500;
const ADOPTED_RECOVERY_WAIT_MS = 30_000;
const LSOF_CANDIDATES = ["/usr/sbin/lsof", "/usr/bin/lsof"];

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
  logPath,
  fsMod = defaultFs,
  osMod = defaultOs,
  pathMod = defaultPath,
  httpMod = defaultHttp,
  spawnFn = defaultSpawn,
  execFileFn = defaultExecFile,
  execFileSyncFn = defaultExecFileSync,
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

  function sendStatus(message) {
    mainWindow()?.webContents?.send("status", message);
  }

  function currentThemeAccent() {
    const configured = store.get("themeAccent") || "";
    return THEME_ACCENT_RE.test(configured) ? configured : DEFAULT_THEME_ACCENT;
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

  // Ask the other channel app to quit through its normal lifecycle. Both app
  // flavors share a bundle identifier, so AppleScript must target app NAME.
  function quitOtherApp(appName) {
    return new Promise((resolve) => {
      if (processObj.platform !== "darwin") { resolve(false); return; }
      execFile(
        "osascript",
        ["-e", `quit app "${appName}"`],
        { timeout: 10000 },
        (err) => resolve(!err),
      );
    });
  }

  function isTrustedWindowsGatewayCommand(command) {
    const gatewayBin = findKirocrewBin(
      fs,
      os,
      path,
      processObj.resourcesPath,
      dirname,
    );
    return isKirocrewCommand(command, {
      trustedExecutablePaths: windowsGatewayExecutablePaths(gatewayBin),
    });
  }

  function probeGatewayPortOwner(probePort) {
    if (IS_WIN) {
      return classifyPortOwner(probePort, {
        getListenPids: windowsListenPids,
        getCommand: windowsProcessCommand,
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

  // Host-capability IPC may verify only this launch's primary gateway. Keep the
  // port out of the public call shape so an untrusted renderer cannot turn the
  // supervisor's process inspection into an arbitrary-port probe.
  function probePrimaryPortOwner() {
    return probeGatewayPortOwner(PORT);
  }

  // Signal 0 probes without delivering on POSIX. EPERM still means the process
  // is alive and may be holding gateway.lock.
  function pidAlive(pid) {
    try { processObj.kill(pid, 0); return true; }
    catch (error) { return !!(error && error.code === "EPERM"); }
  }

  // Capture the listener while the socket is still bound. Once it clears,
  // neither lsof nor netstat can name the process still holding gateway.lock.
  function snapshotGatewayPortPids(probePort) {
    return snapshotPortPids({
      port: probePort,
      isWindows: IS_WIN,
      getWindowsPids: windowsListenPids,
      getPosixPids: lsofListenPids,
    });
  }

  function unverifiedIncumbent(pids) {
    return incumbentSnapshotBlocksRespawn({ pids, isWindows: IS_WIN });
  }

  // Port free is not lock free. Wait for captured incumbent PIDs to die so the
  // kernel has released gateway.lock before attempting the replacement spawn.
  async function waitForIncumbentExit(pids, label) {
    const verdict = await waitForProcessExit({
      pids,
      isAlive: pidAlive,
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    });
    if (verdict === "timeout") {
      glog(`${label}: incumbent gateway process still alive after the exit grace (port already free) — spawning anyway; a lock refusal will surface via the start-failure watcher`);
    }
    return verdict;
  }

  // The LISTEN socket, not an HTTP answer, is the mutex. A wedged process or
  // dropped SSH tunnel can stop answering while it continues to hold the port.
  async function waitForPortFree(maxWaitMs = 30000) {
    const start = Date.now();
    for (;;) {
      const owner = await probeGatewayPortOwner(PORT);
      if (owner === "none") return true;
      if (owner === "unknown") {
        glog(`port-free: listener probe unavailable on :${PORT} — falling back to an HTTP probe`);
        try { await checkBackend(); } catch { return true; }
      }
      if (Date.now() - start > maxWaitMs) return false;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
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
    const decision = decideGatewayAction(app.getVersion(), health, { localOwner });
    if (decision.action === "reuse") {
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
              isPortBound: async () => (await probeGatewayPortOwner(PORT)) !== "none",
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
          if (localOwner !== "service" || (await probeGatewayPortOwner(PORT)) === "none") {
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

    const other = FAMILY_META[decision.otherFamily];
    glog(`gateway on :${PORT} is owned by ${other.appName} (${decision.otherVersion}) — prompting for takeover`);
    const canTakeover = processObj.platform === "darwin";
    const { response } = await dialog.showMessageBox({
      type: "warning",
      title: `${other.displayName} is running`,
      message: `${other.displayName} (${decision.otherVersion}) is already running with your Kiro Crew data.`,
      detail: canTakeover
        ? `Only one Kiro Crew app can use ~/.kiro/crew at a time. Quit ${other.displayName} and continue here?`
        : `Only one Kiro Crew app can use ~/.kiro/crew at a time. Quit ${other.displayName}, then reopen this app.`,
      buttons: canTakeover ? [`Quit ${other.displayName} & Continue`, "Cancel"] : ["OK"],
      defaultId: 0,
      cancelId: canTakeover ? 1 : 0,
    });
    if (!canTakeover || response !== 0) return "abort";
    sendStatus(`Waiting for ${other.displayName} to quit…`);
    await quitOtherApp(other.appName);
    if (!(await waitForPortFree())) {
      glog(`takeover failed: ${other.appName} did not release :${PORT}`);
      await dialog.showMessageBox({
        type: "error",
        message: `${other.displayName} did not quit.`,
        detail: "Quit it manually, then relaunch this app.",
        buttons: ["OK"],
      });
      return "abort";
    }
    glog(`takeover: ${other.appName} released :${PORT} — proceeding to spawn`);
    return "spawn";
  }

  function startGateway() {
    glog(`launch: port=${PORT} home=${KIROCREW_HOME} packaged=${app.isPackaged} resourcesPath=${processObj.resourcesPath || "(none)"} log=${gatewayLogPath()}`);
    sendStatus("Checking if gateway is running…");
    return new Promise((resolve) => {
      // Both the silent-port and takeover branches funnel through this gate, so
      // client-only mode cannot be honored on one path and ignored on another.
      const spawnUnlessClientOnly = () => {
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
        gatewayStartFailure = {
          disabled: true,
          port: PORT,
          remoteHost: remoteConfig?.host || "",
          remotePort: remoteConfig?.remotePort || "",
        };
        resolve(false);
      };

      checkBackend()
        .then(async () => {
          const outcome = await resolveGatewayConflict();
          if (outcome === "reuse") { resolve(true); return; }
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
        })
        .catch(() => { spawnUnlessClientOnly(); });
    });
  }

  // Resolve the tree that ships agents/ and skills/. Packaged resources keep it
  // beside electron/; source checkouts keep it at the repo root two levels up.
  function resolveProjectDir() {
    const candidates = [
      path.resolve(dirname, ".."),
      path.resolve(dirname, "..", ".."),
    ];
    for (const candidate of candidates) {
      try {
        if (
          fs.existsSync(path.join(candidate, "agents"))
          && fs.existsSync(path.join(candidate, "skills"))
        ) {
          return candidate;
        }
      } catch { /* try the next candidate */ }
    }
    return path.resolve(dirname, "..");
  }

  function spawnGateway(resolve) {
    // The gateway owns its data root. Create it before deriving the redirected
    // bytecode cache, honoring an explicit KIROCREW_HOME without touching the
    // deprecated legacy directory on a clean install.
    const kirocrewDir = processObj.env.KIROCREW_HOME || canonicalHome();
    try {
      fs.mkdirSync(kirocrewDir, { recursive: true, mode: 0o700 });
    } catch (error) {
      glog(`WARN failed to create kirocrew dir ${kirocrewDir}: ${error.message}`);
    }

    const bin = findKirocrewBin(
      fs,
      os,
      path,
      processObj.resourcesPath,
      dirname,
      processObj.arch,
      IS_WIN,
    );
    const bundled = bin.includes("backend-dist");
    let execState = "executable";
    try { fs.accessSync(bin, fs.constants.X_OK); }
    catch (error) { execState = `NOT-EXECUTABLE(${error.code})`; }
    glog(`no gateway on :${PORT} — spawning bundled backend: bin=${bin} bundled=${bundled} ${execState}`);

    // The Windows installer writes backend-dist incrementally. Refusing an
    // incomplete interpreter is preventive but cannot see package siblings that
    // have not landed yet; the current-attempt traceback classifier below is the
    // sound after-the-fact backstop. Neither replaces the other.
    if (bundled) {
      const backendRoot = path.resolve(path.dirname(bin), "..");
      const missingParts = findMissingBundleParts(fs, path, backendRoot);
      if (missingParts.length) {
        const errorMessage = describeIncompleteBundle(missingParts);
        glog(`spawn REFUSED: incomplete bundle at ${backendRoot} — missing: ${missingParts.join(", ")}`);
        gatewayStartFailure = {
          error: errorMessage,
          incompleteBundle: true,
          bundled: true,
        };
        sendStatus(INSTALLING_STATUS);
        resolve(false);
        return;
      }
    }

    sendStatus("Starting gateway…");

    // AppImage processes receive no AppArmor profile automatically. Log the
    // exact remedy before launch; a failure here is diagnostic-only and must not
    // block the gateway.
    try {
      const need = describeSandboxProfileNeed({
        platform: processObj.platform,
        env: processObj.env,
        readSysctl: (file) => fs.readFileSync(file, "utf8"),
        cliBin: bin,
      });
      if (need) {
        glog(`WARN agent sandbox will fail closed: ${need.reason}`);
        glog(`HINT run this in a terminal (needs sudo), then restart the app: ${need.command}`);
      }
    } catch (error) {
      glog(`WARN sandbox profile check failed: ${error.message}`);
    }

    // The explicit --port is the single source of truth. Inheriting
    // KIROCREW_PORT would let the child re-derive a port which differs from the
    // shell URL and from the gateway's own frame-ancestor claim.
    const { KIROCREW_PORT: _ignored, ...cleanEnv } = processObj.env;

    // GUI-launched macOS apps receive launchd's minimal PATH. Append only the
    // user's launchd-domain additions so an existing resolution can never be
    // shadowed; other platforms and empty additions leave PATH untouched.
    const gatewayPath = resolveGatewayPath({
      execFileSync,
      platform: processObj.platform,
      basePath: cleanEnv.PATH || "",
    });
    if (gatewayPath) {
      glog(`PATH recovered from launchd domain: +${gatewayPath.added.length} dir(s) appended`);
    }

    // Write child stdout/stderr directly to a file descriptor. A JS pipe could
    // backpressure a long-running gateway; the descriptor also preserves Python
    // tracebacks which otherwise disappear on clean recipient machines.
    let childOut = "ignore";
    try { childOut = fs.openSync(gatewayLogPath(), "a"); }
    catch (error) { glog(`WARN could not open child log fd: ${error.message}`); }
    glog(SPAWN_MARKER);
    gatewayStartFailure = null;

    let spawnBin = bin;
    let spawnArgs = ["gateway", "--no-open", "--port", String(PORT)];
    // Node refuses .cmd/.bat without shell:true. Use the relocatable bundled
    // Python directly instead of opening the command-injection-prone shell path.
    if (bin.endsWith("kirocrew.cmd")) {
      const pythonExe = path.resolve(path.dirname(bin), "..", "python.exe");
      if (fs.existsSync(pythonExe)) {
        spawnBin = pythonExe;
        spawnArgs = ["-s", "-m", "kiro_crew", ...spawnArgs];
      } else {
        const errorMessage = describeIncompleteBundle([]);
        glog(`spawn REFUSED: bundled interpreter absent at ${pythonExe} — install likely still extracting`);
        gatewayStartFailure = {
          error: errorMessage,
          incompleteBundle: true,
          bundled: true,
        };
        sendStatus(INSTALLING_STATUS);
        resolve(false);
        return;
      }
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
        ...gatewayBytecodeEnvironment(
          processObj.platform,
          path.join(kirocrewDir, "cache", "pycache"),
          app.isPackaged,
        ),
      }),
    });
    gatewayProcess = child;
    gatewayOwnership = "spawned";
    if (typeof childOut === "number") {
      try { fs.closeSync(childOut); } catch { /* ignore */ }
    }

    // Bind handlers to this child, not the mutable slot. Recovery can replace a
    // child before its late error/exit event arrives; stale events must never
    // orphan the replacement or fabricate a start failure for it.
    child.on("error", (error) => {
      glog(`spawn ERROR code=${error.code || "?"} msg=${error.message}`);
      if (gatewayProcess !== child) return;
      gatewayStartFailure = { error: error.message, bundled };
      sendStatus(`Gateway failed: ${error.message}`);
      resolve(false);
    });
    child.on("exit", (code, signal) => {
      glog(`gateway child exited code=${code} signal=${signal}`);
      // Node's Windows kill maps both signal names to TerminateProcess. The
      // Gatekeeper hint is meaningful only on macOS, never on normal teardown.
      if (signal === "SIGKILL" && IS_MAC) {
        glog("HINT: SIGKILL on a freshly-spawned bundled binary almost always means macOS Gatekeeper blocked an unsigned/quarantined nested executable. On the recipient's Mac run: xattr -cr <path to KiroCrew.app>");
      }
      if (gatewayProcess !== child) return;
      if (!gatewayStartFailure) gatewayStartFailure = { code, signal, bundled };
      gatewayProcess = null;
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
    if (!gateway || gateway.exitCode !== null) { gatewayProcess = null; return; }
    console.log("Stopping gateway gracefully...");
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
    });
    gatewayProcess = null;
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
    if (!IS_WIN || !gateway.pid) { killPid(); return; }
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
    stopGatewayGracefully()
      .catch((error) => console.error("Gateway stop failed:", error?.message));
  }

  function fetchRemoteToken(tokenPort) {
    const config = getRemoteHostConfig(store, tokenPort || PORT);
    if (!config || !config.host) return Promise.resolve({ token: "", error: null });
    const { host: remoteHost, binPath, remotePort, remotePath } = config;
    const validationError = validateRemoteSettings(
      remoteHost,
      binPath,
      remotePort,
      remotePath,
    );
    if (validationError) {
      console.error(`Refusing SSH token fetch: ${validationError}`);
      return Promise.resolve({ token: "", error: validationError });
    }

    const effectivePort = remotePort || tokenPort || PORT;
    const remoteCommand = buildRemoteTokenCommand(binPath, {
      port: effectivePort,
      remotePath: remotePath || undefined,
    });
    const sshArgs = ["-o", "ConnectTimeout=10", remoteHost, remoteCommand];

    return new Promise((resolve) => {
      sendStatus("Fetching token from remote dev desktop…");
      console.log(`SSH token fetch: ssh ${remoteHost} for port ${effectivePort}`);
      execFile(
        "/usr/bin/ssh",
        sshArgs,
        { timeout: Math.max(store.get("sshTimeoutMs") || 20000, 5000) },
        (error, stdout, stderr) => {
          if (error) {
            console.error("SSH token fetch failed:", error.message);
            if (stderr) console.error("SSH stderr:", stderr.trim().slice(0, 500));
            resolve({ token: "", error: stderr?.trim() || error.message });
            return;
          }
          resolve({ token: parseTokenFromStdout(stdout), error: null });
        },
      );
    });
  }

  async function fetchLocalToken(targetBackendUrl = BACKEND_URL) {
    // Re-resolve the home at call time so a KIROCREW_HOME change after Electron
    // starts is honored. Mint only against the literal loopback endpoint.
    return fetchTokenFromHome({
      backendUrl: targetBackendUrl,
      resolveHome,
      path,
      fs,
      http,
    });
  }

  function checkBackend(healthUrl = HEALTH_URL) {
    return new Promise((resolve, reject) => {
      const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
        res.resume();
        if (res.statusCode < 500) resolve();
        else reject();
      });
      req.on("error", reject);
      req.on("timeout", () => { req.destroy(); reject(); });
    });
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
      title,
      message,
      logTail,
      logPath: displayedLogPath,
      portConflict,
      noRetry = false,
      localGatewayOff = false,
      offerLocalStart = false,
      primaryAction: configuredPrimaryAction,
      primaryLabel: configuredPrimaryLabel,
      showQuitButton: configuredShowQuitButton,
    } = options;
    const showQuitButton = configuredShowQuitButton ?? !noRetry;
    // Client-only mode launched nothing, so there is no launch to diagnose:
    // the log on disk belongs to earlier runs, and rendering that tail is what
    // made a state the user asked for read as a crash report.
    const showLog = !localGatewayOff;

    return new Promise((resolve) => {
      const dark = nativeTheme.shouldUseDarkColors;
      const hasParent = parentWindow && !parentWindow.isDestroyed();
      const errorWindow = new BrowserWindow({
        width: 620,
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
      const primaryAction = configuredPrimaryAction
        || (noRetry ? "quit" : (portConflict ? "force-retry" : "retry"));
      const primaryLabel = configuredPrimaryLabel
        || (noRetry ? "Quit" : (portConflict ? "Force-stop & Retry" : "Retry"));
      // Client-only mode cannot reach dashboard Settings to reverse the choice.
      // Keep the explicit local-gateway escape hatch in this pre-dashboard UI --
      // but only where starting one here is coherent. The spawn binds THIS
      // port, so on a remote crew's port the button would stand up a local
      // gateway shadowing that crew's identity on a port the user never chose.
      const enableButton = offerLocalStart && !noRetry
        ? "<button class=\"cancel\" onclick=\"act('enable-retry')\">Start Local Gateway</button>"
        : "";
      const foreground = dark ? "#e2e8f0" : "#1e293b";
      const muted = dark ? "#94a3b8" : "#64748b";
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
      </style></head><body>
        <div class="title">${escapeHtml(title)}</div>
        <div class="msg">${escapeHtml(message)}</div>
        ${logPane}
        <div class="row">
          <button class="ok" onclick="act('${primaryAction}')">${escapeHtml(primaryLabel)}</button>
          ${enableButton}
          ${revealButton}
          ${showQuitButton ? "<button class=\"cancel\" onclick=\"act('quit')\">Quit</button>" : ""}
        </div>
        <script>
          function act(a){ document.title = 'mc-action:' + a; window.close(); }
          document.addEventListener('keydown', e => {
            if (e.key === 'Enter') act('${primaryAction}');
            if (e.key === 'Escape') act('quit');
          });
        </script>
      </body></html>`;

      let action = null;
      errorWindow.on("page-title-updated", (_event, updatedTitle) => {
        if (updatedTitle && updatedTitle.startsWith("mc-action:")) {
          action = updatedTitle.slice("mc-action:".length);
        }
      });
      errorWindow.on("closed", () => resolve(action || "quit"));
      errorWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
    });
  }

  // Packaged GUI apps inherit a minimal PATH. macOS and Linux install lsof in
  // different absolute locations; probe both before falling back to PATH.
  function resolveLsof() {
    for (const candidate of LSOF_CANDIDATES) {
      try { if (fs.existsSync(candidate)) return candidate; }
      catch { /* unreadable candidate */ }
    }
    return "lsof";
  }

  function lsofListenPids(probePort) {
    return new Promise((resolve, reject) => {
      execFile(
        resolveLsof(),
        ["-nP", `-iTCP:${probePort}`, "-sTCP:LISTEN", "-t"],
        { timeout: 5000 },
        (error, stdout) => {
          // lsof exits non-zero with empty output for no match. Only an EXECUTE
          // failure is unknown; treating it as a free port permits blind kills.
          if (error && (error.code === "ENOENT" || error.code === "EACCES")) {
            reject(error);
            return;
          }
          resolve(String(stdout || "").split(/\s+/)
            .map((value) => parseInt(value, 10))
            .filter((value) => Number.isInteger(value) && value > 1));
        },
      );
    });
  }

  function psCommand(pid) {
    return new Promise((resolve) => {
      execFile(
        "/bin/ps",
        ["-p", String(pid), "-o", "command="],
        { timeout: 5000 },
        (_error, stdout) => resolve(String(stdout || "")),
      );
    });
  }

  // PPID 1 distinguishes service-managed gateways (and conservative orphans)
  // which must never be evicted into a launchd/systemd respawn race.
  function psPpid(pid) {
    return new Promise((resolve) => {
      execFile(
        "/bin/ps",
        ["-p", String(pid), "-o", "ppid="],
        { timeout: 5000 },
        (_error, stdout) => resolve(String(stdout || "")),
      );
    });
  }

  function forceStopGatewayPort(probePort) {
    if (IS_WIN) {
      return forceStopPort(probePort, {
        getListenPids: windowsListenPids,
        getCommand: windowsProcessCommand,
        kill: (pid) => windowsTaskkill(pid, {
          isTrustedCommand: isTrustedWindowsGatewayCommand,
        }),
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
        isKirocrew: isTrustedWindowsGatewayCommand,
        failClosedOnProbeError: true,
        log: glog,
      });
    }
    return forceStopPort(probePort, {
      getListenPids: lsofListenPids,
      getCommand: psCommand,
      getPpid: psPpid,
      kill: (pid, signal) => processObj.kill(pid, signal),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      log: glog,
    });
  }

  /** Start or replace the primary own-port post-handoff liveness monitor. */
  function startLivenessMonitor(window) {
    if (livenessMonitor) {
      livenessMonitor.stop();
      livenessMonitor = null;
    }
    livenessMonitor = createLivenessMonitor({
      probe: () => checkBackend(HEALTH_URL),
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
      onRecovered: () => glog("liveness: backend responsive again (transient blip)"),
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
    try { webContents.loadFile(path.join(dirname, "loading.html")); }
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
    try { webContents.loadFile(path.join(dirname, "loading.html")); }
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
        isPortBound: async () => (await probeGatewayPortOwner(PORT)) !== "none",
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
    // Cancel before leaving fullscreen: the fullscreen-exit event can fire the
    // deferred hide listener and immediately undo this reveal.
    cancelTrayHide(window);
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
      query: { accent: currentThemeAccent() },
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
        let token = await fetchLocalToken(targetBackendUrl);
        if (!token) {
          ({ token } = await fetchRemoteToken(new URL(targetBackendUrl).port));
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
        webContents.loadFile(path.join(dirname, "token-prompt.html"), {
          query: { port: promptPort, kind, host: remoteHost },
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
      // The crew's own port when it has one; PORT is only this end of the link.
      const remoteTargetPort = (localGatewayOff && failureRecord?.remotePort) || PORT;
      const portConflict = failureKind === "port-conflict";

      let title;
      let message;
      if (failureKind === "installing") {
        title = "Kiro Crew — installation still finishing";
        message = error.failure?.incompleteBundle
          ? error.message
          : describeIncompleteBundle([]);
      } else if (localGatewayOff) {
        title = remoteTarget
          ? `Kiro Crew — nothing answering at ${remoteTarget}:${remoteTargetPort}`
          : `Kiro Crew — no gateway on port ${PORT}`;
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
          offerLocalStart: localGatewayOff && !remoteTarget,
        });
        if (window.isDestroyed()) return;
        if (action === "reveal") {
          try { shell.showItemInFolder(launchLogPath); }
          catch { /* best effort */ }
          continue;
        }
        if (action === "enable-retry") {
          setLocalGatewayEnabled(store, true);
          runLocalGateway = true;
          glog("local gateway turned back on from the error dialog");
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
    fetchLocalToken,
    fetchRemoteToken,
    entryUrl: dashboardEntryUrl,
    probePrimaryPortOwner,
    stopGracefully: stopGatewayGracefully,
    stopOnQuit: stopGatewayOnQuit,
    onInstallDispatched,
    onInstallFailed,
  });
}

module.exports = { createGatewaySupervisor };
