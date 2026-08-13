const { app, BaseWindow, BrowserWindow, WebContentsView, shell, dialog, Tray, Menu, nativeImage, nativeTheme, Notification, ipcMain, webContents, session, desktopCapturer, systemPreferences, screen } = require("electron");
const Store = require("electron-store");
const fs = require("fs");
const os = require("os");
const { spawn, execFile } = require("child_process");
const path = require("path");
const http = require("http");

const { findKirocrewBin } = require("./find-bin");
const {
  findMissingBundleParts,
  describeIncompleteBundle,
  shouldReclassifyAsInstalling,
  currentAttemptLog,
  SPAWN_MARKER,
} = require("./bundle-integrity");
const { findConfiguredDashboardPort } = require("./data-home");
const { createTokenRetryHandler } = require("./token-retry");
const { classifyAuthBlock, defaultedPort } = require("./gateway-auth-hint");
const { exitImmersiveModes } = require("./blocking-prompt");
const { shouldRetryLocalTokenMint, tokenMintRetryDelayMs, TOKEN_MINT_MAX_RETRIES } = require("./token-acquire");
const { createDisplayMediaHandler } = require("./display-media");
const {
  createPermissionRequestHandler,
  createPermissionCheckHandler,
} = require("./permission-handler");
const { createWindowOpenHandler, openExternalSafely } = require("./external-scheme");
const { resolveThemeSource } = require("./native-theme");
const { initAutoUpdate } = require("./auto-update");
const { makeUpdaterLogger } = require("./update-logger");
const {
  classifyBundleLocation,
  containingDirForBundle,
  shouldOfferRelocation,
  describeLocation,
} = require("./bundle-location");
const {
  stopGatewayGracefully: _stopGatewayGracefully,
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
const { waitForGateway, describeGatewayFailure, tailLines, isPortInUse } = require("./gateway-wait");
const { describeSandboxProfileNeed } = require("./sandbox-profile");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const {
  DEFAULT_GLOBAL_HOTKEY,
  createSummonHandler,
  bindGlobalHotkey,
  unregisterGlobalHotkey,
  currentGlobalHotkey,
  setGlobalHotkeyLogger,
} = require("./global-hotkey");
const { createLivenessMonitor } = require("./gateway-liveness");
const {
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  waitForServiceRebind,
  waitForProcessExit,
  snapshotPortPids,
  incumbentSnapshotBlocksRespawn,
  unrecoverableGatewayDialog,
} = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const { createMetricsRecorder } = require("./perf-metrics");
const { identityFamily, decideGatewayAction, classifyGatewayReadiness, FAMILY_META, HEALTH_IDENTITY_PATH, READY_PATH } = require("./instance-guard");
const { initMochi, shutdownMochi } = require("./mochi/index");
const { borrowSessionToken } = require("./mochi-session-token");
const { initCrewCompanion, shutdownCrewCompanion } = require("./crew-companion/index");
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
const { secretCandidates } = require("./home-dir");

/**
 * Read the gateway's shared internal secret for machine-to-machine calls.
 *
 * Re-read on every call, never cached: the secret is per-gateway-boot, so a
 * cached value goes stale across a gateway restart — the exact failure mode that
 * produced spurious "Forbidden" responses elsewhere in this app.
 */
function readInternalSecret() {
  for (const candidate of secretCandidates()) {
    try {
      const value = fs.readFileSync(candidate, "utf8").trim();
      if (value) return value;
    } catch { /* try the next candidate */ }
  }
  return "";
}
const { buildMenuTemplate } = require("./app-menu");
const { serializeMenuItems, executeMenuItem } = require("./windows-menu-model");
const {
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
  SYMBOL_DARK: WINDOWS_TITLEBAR_SYMBOL_DARK,
  SYMBOL_LIGHT: WINDOWS_TITLEBAR_SYMBOL_LIGHT,
  OVERLAY_BACKGROUND: WINDOWS_TITLEBAR_BACKGROUND,
} = require("./windows-titlebar");

// ── Persistent settings for remote tunnel mode ──

const {
  DEFAULT_REMOTE_BIN,
  DEFAULT_REMOTE_PATH,
  buildRemoteTokenCommand,
  parseTokenFromStdout,
} = require("./remote-token");

const { migrateRemoteHostConfig, getRemoteHostConfig, setRemoteHostConfig } = require("./host-config");

const { isLocalGatewayEnabled, setLocalGatewayEnabled, classifyStartFailure } = require("./local-gateway");

const store = new Store({
  defaults: {
    remoteHost: "",                        // LEGACY — migrated to remoteHosts
    kirocrewBinPath: DEFAULT_REMOTE_BIN,   // LEGACY — migrated to remoteHosts
    remoteHosts: {},                       // { [port]: { host, binPath, remotePort?, remotePath? } }
    sshTimeoutMs: 20000,
    windowState: null,                     // persisted main-window geometry (see window-state.js)
    globalHotkey: null,                    // system-wide summon accelerator: null = platform default, "" = disabled, string = custom (see global-hotkey.js)
    lastNudgedVersion: "",                 // last update version announced via native notification (nudge once per version)
    themeAccent: "",                       // user's resolved theme accent hex; injected into the boot splash
    updateChannel: "",                     // "" = follow build stamp; "insider"|"stable" = user opt-in (Settings > About)
    runLocalGateway: true,                 // false = act as a pure client; never start a gateway on this machine
    linuxFrameless: null,                  // Linux window chrome: true = frameless, false = native frame, null = follow the desktop environment (see linux-frame.js)
  },
});

// Read ONCE at launch, because the setting takes effect on the next launch.
// startGateway() is also the recovery path for a gateway that died mid-session,
// so re-reading the store there would let a flip made minutes ago refuse to
// replace a gateway this session is still using — stranding the user with no
// backend and no way back short of a relaunch. The error dialog's
// "turn it on and retry" action is the one thing that may change this, since
// that IS the user asking for a gateway right now.
let runLocalGateway = isLocalGatewayEnabled(store);

// The data home whose config.json governs this launch (see home-dir.js): a
// valid KIROCREW_HOME override, else the default ~/.kiro/crew -- mirroring the
// backend resolver in config/paths.py. Boot-time WRITES (mkdir, pycache prefix)
// use canonicalHome() so an override is honored without a stray write.
const { resolveHome, canonicalHome } = require("./home-dir");
const { fetchLocalToken: fetchTokenFromHome } = require("./local-token");
const KIROCREW_HOME = resolveHome();

function resolvePort() {
  const raw = process.env.KIROCREW_PORT;
  if (raw) {
    const n = parseInt(raw, 10);
    if (isNaN(n) || n < 1 || n > 65535) {
      console.warn(`Invalid KIROCREW_PORT="${raw}", falling back to 5476`);
      return 5476;
    }
    return n;
  }
  // No env override — derive the gateway port from config.json. The fork's
  // DashboardConfig has no `dashboard.port` key; the port lives in
  // `dashboard.url` (see backend cli_server.resolve_client_port /
  // dashboard/origin.parse_dashboard_url). Read it from the resolved data home.
  const configuredPort = findConfiguredDashboardPort(fs, path, [KIROCREW_HOME]);
  if (configuredPort) return configuredPort;
  console.debug("No usable dashboard.url port in the data home, falling back to 5476");
  return 5476;
}

const PORT = resolvePort();
const BACKEND_URL = `http://localhost:${PORT}`;

// Migrate legacy single-host config to per-port map
if (migrateRemoteHostConfig(store, PORT)) {
  console.log(`Migrated legacy remoteHost to remoteHosts[${PORT}]`);
}
const HEALTH_URL = `${BACKEND_URL}/api/status`;
const POLL_INTERVAL_MS = 500;
const MAX_WAIT_MS = 30_000; // 30s max wait for backend
const IS_MAC = process.platform === "darwin";
const IS_WINDOWS = process.platform === "win32";
const IS_WIN = IS_WINDOWS;
const WINDOWS_TITLEBAR_MENU_IDS = new Set([
  "file-menu",
  "edit-menu",
  "view-menu",
  "connection-menu",
  "window-menu",
  "help-menu",
]);
const IS_LINUX = process.platform === "linux";
// Whether Linux windows drop the native frame so the dashboard's 42px header
// can double as the title bar (as on macOS/Windows) instead of stacking under
// the WM's own decoration. Decided ONCE at launch from the desktop
// environment plus the operator override, because every window in the process
// must agree (see linux-frame.js for the full contract).
const { decideLinuxFrame, applyWindowControl } = require("./linux-frame");
const LINUX_FRAME_DECISION = IS_LINUX
  ? decideLinuxFrame({ env: process.env, override: store.get("linuxFrameless") })
  : null;
const LINUX_FRAMELESS = !!(LINUX_FRAME_DECISION && LINUX_FRAME_DECISION.frameless);
const DEFAULT_THEME_ACCENT = "#8E48FF";
// Loading-screen status for a refused spawn on a bundle that is still being
// written. Deliberately not "Gateway failed": nothing failed, so the line must
// not contradict the dialog that follows.
const INSTALLING_STATUS = "Finishing installation…";
const THEME_ACCENT_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

function currentThemeAccent() {
  const configured = store.get("themeAccent") || "";
  return THEME_ACCENT_RE.test(configured) ? configured : DEFAULT_THEME_ACCENT;
}


// The dashboard view fills the whole content area on all platforms. On macOS
// and Windows the dashboard's own 42px header doubles as the title bar. macOS
// insets native traffic lights; Windows overlays its native caption controls
// and renders application-menu triggers inside the header. On Linux the window
// is frameless (frame:false) on desktops that prefer client-side decorations —
// same injected drag region, with an injected caption-control cluster instead
// of native controls (see linux-frame.js).

const { validateRemoteSettings } = require("./validation");
const { attachContextMenu } = require("./context-menu");

// Set app name for macOS menu bar and dock. Nightly ships as a separate
// side-by-side app, so its menu bar must say so.
app.name = identityFamily(app.getVersion()) === "nightly" ? "Kiro Crew Nightly" : "Kiro Crew";

// Windows taskbar identity. Without an explicit AppUserModelID, Windows groups
// the app under the generic Electron host (wrong icon in the taskbar/jumplist,
// pinning targets Electron rather than KiroCrew). Match the packaged appId
// (build.appId = "com.amazon.kiro.crew"); nightly gets a distinct id so it
// pins/groups side-by-side with stable, mirroring the app.name split above.
if (IS_WIN) {
  const appUserModelId = identityFamily(app.getVersion()) === "nightly"
    ? "com.amazon.kiro.crew.nightly" : "com.amazon.kiro.crew";
  app.setAppUserModelId(appUserModelId);
}

// Single-instance lock. On macOS LaunchServices reuses the already-running .app
// when the user relaunches from the Dock / Spotlight, so a second instance is
// harmless (a no-op there). The fork's supported non-mac target is the Linux
// AppImage, which has no such reuse — double-clicking the AppImage again spawns
// a fresh process. Two instances against the same ~/.kiro/crew racing
// .local_secret and stopping each other's gateway on before-quit is bad news
// (kills the shared gateway out from under the other instance). Grab the lock;
// if we can't, exit immediately and let the existing instance surface itself.
// Uses app.exit(0) not app.quit(): quit() is async so this module's remaining
// top-level code (store mutations via migrateRemoteHostConfig, resolvePort side
// effects) would still run and race the primary instance's state before quit
// fires. app.exit(0) is synchronous with no lifecycle side effects.
if (!app.requestSingleInstanceLock()) {
  app.exit(0);
} else {
  app.on("second-instance", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

let mainWindow = null;
let tray = null;
let gatewayProcess = null;
// How the gateway on this flavor's port was obtained — ONE mutually-exclusive
// state (previously three module-level booleans that encoded it redundantly and
// could drift out of sync). Vocabulary + the recovery-strategy mapping live in
// gateway-recovery.js (chooseRecoveryStrategy / classifyAdoptedGateway); assign
// at exactly one place per outcome.
//   "none"           — no gateway yet, or the reuse path could NOT positively
//                      identify the port-holder as a local Kiro Crew process
//                      (remote SSH tunnel, manual/external gateway, probe
//                      failure). Recovery must NEVER kill or respawn it: the
//                      port-holder is someone else's process, and the correct
//                      fix on a dropped tunnel is to re-probe and reconnect
//                      once it heals, not to force-stop the port or spawn a
//                      (nonexistent, in remote mode) local backend.
//   "spawned"        — WE spawned the bundled backend on this flavor's port;
//                      recovery may kill + respawn it.
//   "reused-local"   — reuse path, holder POSITIVELY identified as a local
//                      same-family Kiro Crew process (same-family /api/health
//                      + a "kirocrew" LISTEN owner). An adopted local gateway
//                      that dies will never come back on its own, so recovery
//                      must wait BOUNDED and then respawn — never the
//                      indefinite tunnel-heal wait, which leaves the shell
//                      dead until the user manually relaunches.
//   "reused-service" — like "reused-local", but the holder was SERVICE-
//                      classified (PPID = init: a real launchd/systemd unit —
//                      or an orphan, which reparents to init and is
//                      indistinguishable at classify time). If that gateway
//                      later releases the port, a real service manager may be
//                      about to respawn it, so recovery must offer a bounded
//                      rebind grace before spawning locally — spawning
//                      immediately races the manager for the bind and one side
//                      dies with EADDRINUSE.
let gatewayOwnership = "none";
// Post-handoff backend liveness monitor (primary window only). Detects a wedged
// gateway — alive TCP socket, frozen event loop — that the spawn 'exit' watcher
// can't, since the process never exits. See gateway-liveness.js.
let livenessMonitor = null;
// Terminal exit of the gateway we SPAWNED, recorded so the readiness wait can
// fail fast instead of polling a dead port. {code,signal} on exit, {error} on a
// spawn error, null while the child is alive (or was never spawned — reuse
// path). Consulted only during the primary boot wait (see showLoadingThenConnect).
let gatewayStartFailure = null;
let isQuitting = false;
// Debug-only metrics recorder handle; null unless KIROCREW_DEBUG enabled it.
let desktopMetricsRecorder = null;
// True from the moment an update install is dispatched. The updater stops the
// gateway ON PURPOSE before the bundle swap; without this flag the liveness
// watchdog reads that intentional stop as a wedge and resurrects the gateway
// mid-swap. Observed live (gateway-launch.log 2026-07-29T22:18): probe failed
// 3/3 twenty seconds after the stop, the gateway respawned while ShipIt was
// moving the bundle, the reconnect reloaded the page, and the install button
// re-armed. Checked ONLY by the liveness watchdog -- unlike isQuitting it must
// NOT change window-close semantics, because a real quit mid-handoff before
// Squirrel finishes staging would lose the update entirely.
let installingUpdate = false;

// ── Backend lifecycle ──


function sendStatus(msg) {
  mainWindow?.webContents?.send("status", msg);
}

// ── Gateway launch diagnostics ─────────────────────────────────────────────
// A persistent, retrievable log of the gateway-launch path. This matters on a
// CLEAN machine (a recipient not already running a gateway): there, the
// checkBackend() probe fails, so the app must SPAWN the bundled backend farm —
// the path the developer's own machine never exercises, because a gateway is
// already listening on this port and the app just reuses it. The spawn
// previously used stdio:"ignore", so any failure (Gatekeeper SIGKILL on an
// unsigned/quarantined nested binary, a dylib/Python error, a missing or
// non-executable bin) was completely silent. We now tee the child's
// stdout+stderr to a file and record the resolved bin, the reuse-vs-spawn
// decision, and the exit code AND signal.
function gatewayLogPath() {
  let dir;
  try { dir = app.getPath("logs"); } catch { dir = os.tmpdir(); }
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* best effort */ }
  return path.join(dir, "gateway-launch.log");
}

function glog(line) {
  const entry = `[${new Date().toISOString()}] ${line}\n`;
  try { fs.appendFileSync(gatewayLogPath(), entry); } catch { /* never let logging break launch */ }
  console.log(`[gateway-launch] ${line}`);
}

// ── Cross-app gateway ownership (shared ~/.kiro/crew, shared port) ─────────
// The nightly app and the production app are different bundles sharing one
// data home and one port, so the port is the mutex. When a gateway is already
// listening, we must decide REUSE (same family / dev / legacy) vs TAKEOVER
// (the OTHER channel app owns it — prompt, quit it gracefully, then spawn our
// own). Decision logic is pure in instance-guard.js; the effects live here.

// NOTE: defaults to /api/health (HEALTH_IDENTITY_PATH), NOT HEALTH_URL --
// HEALTH_URL is /api/status, whose payload carries no `app` identity field.
function fetchHealthInfo(healthUrl = `${BACKEND_URL}${HEALTH_IDENTITY_PATH}`) {
  return new Promise((resolve) => {
    const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

// Is the answering gateway actually SERVING, or draining after /api/shutdown?
// /api/status and /api/health both stay 200 through a graceful drain, so they
// cannot make this call — /api/ready flips to 503 with `shutting_down: true`
// the moment shutdown_event is set (see handlers/core.py api_ready). Resolves
// to a classifyGatewayReadiness verdict; never rejects (probe failures map to
// "unknown", which adopts — fail-open like every other ambiguity in the guard).
function fetchGatewayReadiness(readyUrl = `${BACKEND_URL}${READY_PATH}`) {
  return new Promise((resolve) => {
    const req = http.get(readyUrl, { timeout: 2000 }, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        let payload = null;
        try { payload = JSON.parse(body); } catch { /* non-JSON body — classify on status alone */ }
        resolve(classifyGatewayReadiness(res.statusCode, payload));
      });
    });
    req.on("error", () => resolve("unknown"));
    req.on("timeout", () => { req.destroy(); resolve("unknown"); });
  });
}

// Ask the OTHER channel app to quit through its normal lifecycle (its
// before-quit stops its own gateway). Never kill the gateway out from under
// its shell — the shell's exit watcher would treat that as a crash.
// Targets by app NAME: both installs share one bundle identifier
// (com.amazon.kiro.crew), so `quit app id` would be ambiguous.
function quitOtherApp(appName) {
  return new Promise((resolve) => {
    if (process.platform !== "darwin") { resolve(false); return; }
    execFile("osascript", ["-e", `quit app "${appName}"`], { timeout: 10000 }, (err) => resolve(!err));
  });
}

// Who locally owns :PORT's LISTEN socket? Thin wiring over classifyPortOwner
// (gateway-stop.js) using the same lsof/ps helpers forceStopGatewayPort uses.
// Function declarations are hoisted, so the helpers defined further down this
// file are available here.
//
function isTrustedWindowsGatewayCommand(command) {
  const gatewayBin = findKirocrewBin(
    fs,
    os,
    path,
    process.resourcesPath,
    __dirname
  );
  return isKirocrewCommand(command, {
    trustedExecutablePaths: windowsGatewayExecutablePaths(gatewayBin),
  });
}

function probeGatewayPortOwner(port) {
  if (IS_WIN) {
    return classifyPortOwner(port, {
      getListenPids: windowsListenPids,
      getCommand: windowsProcessCommand,
      isKirocrew: isTrustedWindowsGatewayCommand,
      log: glog,
    });
  }
  return classifyPortOwner(port, {
    getListenPids: _lsofListenPids,
    getCommand: _psCommand,
    getPpid: _psPpid,
    log: glog,
  });
}

// Is `pid` still running? Signal 0 probes without delivering: ESRCH means dead,
// EPERM means alive but not ours (still holding its locks) — treat as alive.
function _pidAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (e) { return !!(e && e.code === "EPERM"); }
}

// Best-effort capture of the PIDs LISTENing on our port, for the incumbent-exit
// wait below. It must happen while the port is still bound: after it clears,
// neither lsof nor netstat can name the draining process.
function snapshotGatewayPortPids(port) {
  return snapshotPortPids({
    port,
    isWindows: IS_WIN,
    getWindowsPids: windowsListenPids,
    getPosixPids: _lsofListenPids,
  });
}

// A snapshot the probe could not take blocks an automatic respawn only where a
// working probe is guaranteed (see incumbentSnapshotBlocksRespawn).
function unverifiedIncumbent(pids) {
  return incumbentSnapshotBlocksRespawn({ pids, isWindows: IS_WIN });
}

// A draining gateway releases its LISTEN socket EARLY but holds the exclusive
// gateway.lock flock until the process exits (turn drain + session flush run
// after the socket closes). Spawning on "port is free" alone races that lock:
// the replacement is refused and exits, then the incumbent exits — no gateway.
// Wait (bounded) for the captured incumbent pids to die before spawning; the
// kernel releases the flock atomically on exit. On timeout, spawn anyway and
// let the child's own honest lock-refusal error surface through the existing
// gatewayStartFailure watcher — better than silently hanging here forever.
async function waitForIncumbentExit(pids, label) {
  const verdict = await waitForProcessExit({
    pids,
    isAlive: _pidAlive,
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
  });
  if (verdict === "timeout") {
    glog(`${label}: incumbent gateway process still alive after the exit grace (port already free) — spawning anyway; a lock refusal will surface via the start-failure watcher`);
  }
  return verdict;
}

// Budget: the other app's graceful gateway stop runs up to 15s
// (POST /api/shutdown -> SIGTERM -> SIGKILL) after the quit event lands,
// so the wait must comfortably exceed it.
//
// "Free" means the LISTEN socket is gone, NOT merely that /api/status stopped
// answering. Those differ in exactly the cases that matter: a gateway wedged in
// an uninterruptible kernel wait still holds the port while failing probes, and
// a dropped SSH forward stops answering while `ssh` keeps the socket. Either
// way the old HTTP heuristic reported "released" and we respawned straight into
// EADDRINUSE. forceStopPort already learned this lesson; this is the same check.
async function waitForPortFree(maxWaitMs = 30000) {
  const start = Date.now();
  for (;;) {
    const owner = await probeGatewayPortOwner(PORT);
    if (owner === "none") return true;
    if (owner === "unknown") {
      // We cannot see the listener at all (no lsof). Fall back to the historical
      // HTTP heuristic rather than blocking the takeover forever — but say so,
      // because this is the weaker signal.
      glog(`port-free: listener probe unavailable on :${PORT} — falling back to an HTTP probe`);
      try { await checkBackend(); } catch { return true; }
    }
    if (Date.now() - start > maxWaitMs) return false;
    await new Promise((r) => setTimeout(r, 500));
  }
}

async function resolveGatewayConflict(rebindDepth = 0) {
  const health = await fetchHealthInfo();
  // A remote host configured for THIS port means the user deliberately pointed
  // this app at a gateway on another machine, so the local holder is a tunnel
  // by construction and there is nothing here to evict.
  const remoteHost = getRemoteHostConfig(store, PORT)?.host || "";
  if (remoteHost) {
    glog(`:${PORT} is a configured remote host (${remoteHost}) — holder treated as non-local`);
  }
  const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(PORT);
  const decision = decideGatewayAction(app.getVersion(), health, { localOwner });
  if (decision.action === "reuse") {
    // Adopt-or-wait: the /api/status probe that got us here stays 200 while the
    // backend DRAINS after POST /api/shutdown, so "answering" is not "serving".
    // Adopting a draining gateway strands the shell: seconds later the process
    // exits and nothing ever answers this port again (a relaunch arriving
    // seconds into a graceful stop reads "reusing existing gateway" and then
    // goes dark). Only a positive shutting-down verdict refuses; every ambiguity
    // (legacy gateway, probe failure) keeps the historical adopt behavior.
    // A configured remote host is exempt: its port-holder is a tunnel by
    // construction, so "wait for the port to clear, then spawn fresh" can never
    // apply — adopting and letting the reconnect path wait out the remote
    // restart is the only correct move there.
    let adoptedDraining = false;
    const readiness = remoteHost ? "unknown" : await fetchGatewayReadiness();
    if (readiness === "shutting-down") {
      glog(`gateway on :${PORT} answers but /api/ready reports shutting-down — refusing to adopt a draining gateway`);
      sendStatus("Waiting for the previous gateway to exit…");
      // Capture the draining process NOW, while it still owns the LISTEN
      // socket — needed below to wait out its gateway.lock after the port clears.
      const drainingPids = await snapshotGatewayPortPids(PORT);
      if (unverifiedIncumbent(drainingPids)) {
        glog(`drain: could not capture the incumbent PID on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
        return "probe-failed";
      }
      if (await waitForPortFree()) {
        if (localOwner === "service") {
          // A SERVICE-classified holder that released its port may be mid-restart
          // (kirocrew restart bounces the launchd/systemd unit): the manager is
          // about to respawn it, and spawning now races that rebind — one side
          // exits with EADDRINUSE. But orphans (reparented to init) classify as
          // service too and have no manager to respawn them, so don't exempt —
          // grace-wait: adopt a rebind, spawn only if the port stays free.
          sendStatus("Waiting for the gateway to restart…");
          const verdict = await waitForServiceRebind({
            isPortBound: async () => (await probeGatewayPortOwner(PORT)) !== "none",
            sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
          });
          if (verdict === "rebound") {
            // Whatever re-bound the port has NOT been validated: it could be a
            // foreign process, a different-family gateway, or another draining
            // gateway. Do not assert "reuse" — re-run the full decision table
            // (identity + readiness) against the new holder. Depth-capped: one
            // re-entry per boot; a second rebind-into-drain falls through to
            // the adopt-anyway path rather than looping.
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
          // Port free ≠ lock free: wait for the draining process itself to
          // exit so the replacement is not refused by the singleton lock.
          await waitForIncumbentExit(drainingPids, "drain");
          glog(`drain complete: :${PORT} released — spawning a fresh gateway`);
          return "spawn";
        }
      }
      // The drain is stuck holding the socket past the graceful-stop budget.
      // Spawning now would only hit EADDRINUSE, and evicting is not ours to do
      // (we did not spawn this gateway). Adopt as before — loudly — and let the
      // liveness recovery below handle its eventual death with a bounded wait.
      glog(`drain wait timed out — :${PORT} still held; adopting anyway (recovery will respawn if it dies)`);
      adoptedDraining = true;
    }
    glog(`reusing existing gateway on :${PORT} (${decision.reason}) — bundled backend NOT spawned`);
    // Reuse path — recovery must not kill/respawn a gateway we don't own. A
    // same-family gateway held by a local Kiro Crew process is OURS in spirit
    // even though we didn't spawn it: if it dies, no tunnel will resurrect it,
    // so recovery may respawn after a bounded wait. Anything less positively
    // identified (tunnel, no visible owner, probe failure) keeps the
    // never-respawn external classification ("none").
    gatewayOwnership = classifyAdoptedGateway({ reason: decision.reason, localOwner });
    // A gateway we adopted mid-drain is not a success to celebrate: recovery
    // may immediately retract it. Keep the status neutral for that case.
    sendStatus(adoptedDraining ? "Connecting to the existing gateway…" : "Gateway already running ✓");
    return "reuse";
  }
  const other = FAMILY_META[decision.otherFamily];
  glog(`gateway on :${PORT} is owned by ${other.appName} (${decision.otherVersion}) — prompting for takeover`);
  const canTakeover = process.platform === "darwin";
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

// Running from a Gatekeeper App Translocation copy, or a read-only disk image,
// is invisible at launch (all writes already redirect to ~/) but makes the
// macOS in-place bundle swap useless — electron-updater delegates the install to
// Squirrel.Mac, whose ShipIt replaces the running .app — so the app would
// download every release and apply none. Surface it once and offer the
// one-click move.
// A /Volumes path alone is NOT enough to condemn: an external disk or network
// share lives there too and is replaceable, so writability decides.
// Returns the classified location so the caller can log it. Never throws — a
// boot-time dialog failure must not reject the whole app.whenReady() chain.
async function offerRelocationIfUnupdatable() {
  const location = classifyBundleLocation(process.resourcesPath);
  const dir = containingDirForBundle(process.resourcesPath);
  let bundleWritable = true;
  if (dir) {
    try { fs.accessSync(dir, fs.constants.W_OK); } catch { bundleWritable = false; }
  }
  glog(`bundle location: ${location} writable=${bundleWritable} (resourcesPath=${process.resourcesPath || "(none)"})`);
  if (!app.isPackaged || !shouldOfferRelocation(location, { bundleWritable })) return location;

  let response = 1;
  try {
    ({ response } = await dialog.showMessageBox({
      type: "warning",
      title: "Move Kiro Crew to Applications?",
      message: describeLocation(location, { bundleWritable }),
      detail: "Move it to your Applications folder to receive updates. "
        + "You can keep using it from here for now, but it will not update itself.",
      buttons: ["Move to Applications", "Continue Anyway"],
      defaultId: 0,
      cancelId: 1,
    }));
  } catch (err) {
    // A dialog that cannot be shown must not strand boot with no window, no
    // gateway and no tray — degrade to "continue anyway" and log why.
    glog(`bundle location: relocation prompt failed: ${err && err.message}`);
    return location;
  }
  if (response !== 0) {
    glog(`bundle location: user declined relocation from ${location}`);
    return location;
  }

  // moveToApplicationsFolder RETURNS FALSE (it does not throw) when the user
  // cancels the macOS authorization prompt — the likeliest outcome of asking to
  // write into /Applications. Treat false and throw identically, or a cancel
  // looks like success and the user keeps booting from the old location.
  let moved = false;
  try {
    // Relaunches from /Applications on success and does not return.
    moved = app.moveToApplicationsFolder() !== false;
  } catch (err) {
    glog(`bundle location: move to /Applications threw: ${err && err.message}`);
    moved = false;
  }
  if (moved) return location;
  glog("bundle location: move to /Applications did not complete");
  try {
    await dialog.showMessageBox({
      type: "error",
      message: "Could not move Kiro Crew automatically.",
      detail: "Drag Kiro Crew into your Applications folder, then reopen it from there.",
      buttons: ["OK"],
    });
  } catch { /* boot must continue even if we cannot report the failure */ }
  return location;
}

function startGateway() {
  glog(`launch: port=${PORT} home=${KIROCREW_HOME} packaged=${app.isPackaged} resourcesPath=${process.resourcesPath || "(none)"} log=${gatewayLogPath()}`);
  sendStatus("Checking if gateway is running…");
  return new Promise((resolve) => {
    // Both branches below funnel through here, so the client-only choice cannot
    // be honoured on one path and ignored on the other. A takeover reaches it
    // too: quitting the other channel's app frees the port on this machine, and
    // that is not a request to run a gateway here.
    //
    // With nothing to spawn there is no exit code and no log to wait for, so the
    // reason is reported as a fail-fast failure rather than left to time out.
    // The error dialog's Retry re-enters this function, which is what makes
    // "bring the connection up, then retry" work without a relaunch.
    const spawnUnlessClientOnly = () => {
      if (runLocalGateway) {
        spawnGateway(resolve);
        return;
      }
      glog(`no gateway on :${PORT} and local gateway is off — not starting one`);
      sendStatus("No gateway is answering…");
      gatewayStartFailure = { disabled: true, port: PORT };
      resolve(false);
    };
    checkBackend()
      .then(async () => {
        // A gateway is already listening on this port. Same-family, dev, and
        // legacy gateways are reused as before. A gateway owned by the other
        // channel app triggers the takeover prompt.
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
          isQuitting = true;
          app.quit();
          resolve(false);
          return;
        }
        spawnUnlessClientOnly();
      })
      .catch(() => {
        spawnUnlessClientOnly();
      });
  });
}

// Resolve the KiroCrew project root (the tree that ships `agents/` + `skills/`)
// for the gateway's KIROCREW_PROJECT_DIR. The bundled app keeps these alongside
// the Electron files (Resources/), i.e. one level up from `electron/`; a source
// checkout has them at the repo root, two levels up (<repo>/website/electron).
// Probe both and pick the first that actually contains the markers so a source
// run doesn't mis-point at `website/`. Falls back to the legacy one-level-up
// path when neither has markers (preserves prior behavior).
function resolveProjectDir() {
  const candidates = [
    path.resolve(__dirname, ".."),
    path.resolve(__dirname, "..", ".."),
  ];
  for (const c of candidates) {
    try {
      if (fs.existsSync(path.join(c, "agents")) && fs.existsSync(path.join(c, "skills"))) {
        return c;
      }
    } catch { /* ignore and try next */ }
  }
  return path.resolve(__dirname, "..");
}

function spawnGateway(resolve) {
        // Pre-create the backend's data root so the pycache prefix below has a
        // live target. Honor a KIROCREW_HOME override, else the default home
        // (canonicalHome()). The gateway creates/owns its home and
        // .local_secret regardless.
        const kirocrewDir = process.env.KIROCREW_HOME || canonicalHome();
        try {
          fs.mkdirSync(kirocrewDir, { recursive: true, mode: 0o700 });
        } catch (err) {
          glog(`WARN failed to create kirocrew dir ${kirocrewDir}: ${err.message}`);
        }

        const bin = findKirocrewBin(fs, os, path, process.resourcesPath, __dirname);
        const bundled = bin.includes("backend-dist");
        let execState = "executable";
        try { fs.accessSync(bin, fs.constants.X_OK); } catch (e) { execState = `NOT-EXECUTABLE(${e.code})`; }
        glog(`no gateway on :${PORT} — spawning bundled backend: bin=${bin} bundled=${bundled} ${execState}`);

        // Refuse to exec a bundled interpreter whose stdlib is only partly on
        // disk. The installer extracts backend-dist/ incrementally and starts the
        // app as it finishes (runAfterFinish), so a launch inside that window
        // finds python.exe present but late-alphabet stdlib packages missing --
        // the interpreter then dies on `from urllib.parse import ...` from inside
        // pathlib, which reads as a corrupt install rather than an unfinished one.
        //
        // This is PREVENTIVE and unsound; the launch-log backstop in the failure
        // handler is sound but after-the-fact. They cover different halves, so
        // neither replaces the other: refusing before spawn() keeps a doomed
        // interpreter from running module-scope work against the user's live data
        // home (it creates the home and .local_secret, and writes bytecode caches)
        // and from failing in messier ways than ModuleNotFoundError while
        // extraction is still writing underneath it -- the backstop only ever
        // explains a crash that already happened.
        if (bundled) {
          const backendRoot = path.resolve(path.dirname(bin), "..");
          const missingParts = findMissingBundleParts(fs, path, backendRoot);
          if (missingParts.length) {
            const errMsg = describeIncompleteBundle(missingParts);
            glog(`spawn REFUSED: incomplete bundle at ${backendRoot} — missing: ${missingParts.join(", ")}`);
            gatewayStartFailure = { error: errMsg, incompleteBundle: true, bundled: true };
            // Neutral status: nothing failed, the install has not finished.
            sendStatus(INSTALLING_STATUS);
            resolve(false);
            return;
          }
        }

        sendStatus("Starting gateway…");

        // Linux AppImage only: this process is about to exec the backend with no
        // AppArmor profile applied to either of them, because nothing attaches
        // one to a directly launched binary (see sandbox-profile.js for why the
        // app cannot fix that itself). Record the exact remedy command here —
        // this log is what a bug report pastes, and without it the failure looks
        // like a generic "no sandbox backend" verdict on a host that has one.
        try {
          const need = describeSandboxProfileNeed({
            platform: process.platform,
            env: process.env,
            readSysctl: (p) => fs.readFileSync(p, "utf8"),
            // The bundled CLI's absolute path: this persona installed no CLI, so
            // `kirocrew` is not on their PATH and a bare command would fail.
            cliBin: bin,
          });
          if (need) {
            glog(`WARN agent sandbox will fail closed: ${need.reason}`);
            glog(`HINT run this in a terminal (needs sudo), then restart the app: ${need.command}`);
          }
        } catch (e) {
          glog(`WARN sandbox profile check failed: ${e.message}`);
        }

        // Strip KIROCREW_PORT and pass the port EXPLICITLY instead (below).
        // Inheriting it would leave the child free to re-derive its own port
        // from env/config; the explicit flag makes the shell's resolvePort()
        // the single source of truth. Before this, the shell honoured
        // KIROCREW_PORT while the stripped child fell back to config.json (or
        // 5476), so the two could disagree — the window loaded one port while
        // the backend bound another, and the backend's own DASHBOARD_PORT (used
        // as the remote-embed frame-ancestor claim) named a port nothing was
        // served on.
        const { KIROCREW_PORT: _ignored, ...cleanEnv } = process.env;

        // Tee the child's stdout+stderr straight to the launch log via a file
        // descriptor — no JS pipe to drain, no backpressure on a long-running
        // child. This is what surfaces a Python traceback / dylib load error /
        // "killed: 9" on a recipient's machine.
        let childOut = "ignore";
        try { childOut = fs.openSync(gatewayLogPath(), "a"); } catch (e) { glog(`WARN could not open child log fd: ${e.message}`); }
        glog(SPAWN_MARKER);
        gatewayStartFailure = null; // re-arm for this spawn attempt

        // Bind handlers to THIS child via a captured reference, not the
        // module-global. recoverWedgedGateway SIGKILLs the wedged child and then
        // respawns; the dead child's 'exit'/'error' fire asynchronously and could
        // land AFTER the fresh child is assigned. Without an identity guard they
        // would null out the healthy replacement and set a bogus
        // gatewayStartFailure, breaking the very recovery they race with.
        // Windows bundled layout: spawn the interpreter directly instead of
        // the .cmd shim. Node refuses spawn() of .cmd/.bat without
        // shell:true (CVE-2024-27980 hardening), and shell-quoting a
        // spaced install path is fragile -- the shim exists for humans and
        // find-bin identity; the process tree runs python.exe.
        let spawnBin = bin;
        // --port is explicit so the child binds exactly what resolvePort()
        // chose. Never omit it: an unset port makes the backend re-derive one,
        // which is how the shell and the backend came to disagree.
        let spawnArgs = ["gateway", "--no-open", "--port", String(PORT)];
        if (bin.endsWith("kirocrew.cmd")) {
          const pyExe = path.resolve(path.dirname(bin), "..", "python.exe");
          if (fs.existsSync(pyExe)) {
            spawnBin = pyExe;
            spawnArgs = ["-s", "-m", "kiro_crew", ...spawnArgs];
          } else {
            // The .cmd shim is here but python.exe is not. That is the same
            // extraction race as the incomplete-stdlib case above, caught one
            // wave earlier — bin/ lands before the interpreter — so it gets the
            // same "still installing, retry" framing.
            //
            // A mid-extraction tree and a permanently truncated one are
            // indistinguishable at this instant, so this deliberately reads the
            // ambiguity as transient. Mid-extraction is the common state (every
            // install and update passes through it) and the costs are asymmetric:
            // guessing "installing" wrongly costs a retry, after which the copy's
            // "if this persists, reinstall" gives the right instruction anyway,
            // while guessing "corrupted" wrongly sends the user to reinstall a
            // bundle that needed a few more seconds — the harm this whole path
            // exists to prevent, and not undoable once done.
            const errMsg = describeIncompleteBundle([]);
            glog(`spawn REFUSED: bundled interpreter absent at ${pyExe} — install likely still extracting`);
            gatewayStartFailure = { error: errMsg, incompleteBundle: true, bundled: true };
            sendStatus(INSTALLING_STATUS);
            resolve(false);
            return;
          }
        }
        const child = spawn(spawnBin, spawnArgs, {
          stdio: ["ignore", childOut, childOut],
          detached: false,
          // win32: the bundled interpreter is a console-subsystem binary;
          // without this every app launch opens a persistent console window
          // beside the Electron app. Ignored on POSIX.
          windowsHide: true,
          env: {
            ...cleanEnv,
            // Windows source layout puts agents/ + skills/ at the repo root
            // (two levels up from electron/), so resolve by markers there.
            // macOS/Linux keep the original one-level-up path unchanged.
            KIROCREW_PROJECT_DIR: IS_WIN ? resolveProjectDir() : path.resolve(__dirname, ".."),
            // Keep CPython bytecode caches OUT of the signed app bundle.
            // Without this, the embedded interpreter writes __pycache__/*.pyc
            // next to the bundled sources on first import, breaking the
            // codesign seal ("a sealed resource is missing or invalid") --
            // Gatekeeper then fails the installed app, and Squirrel's
            // installer can trip over the corrupted target during updates.
            // CPython creates the directory tree on demand (PEP 3147 /
            // sys.pycache_prefix). Inherited by every Python child the
            // gateway spawns (app servers run on the same interpreter), so
            // the whole process tree stays out of the bundle.
            PYTHONPYCACHEPREFIX: path.join(kirocrewDir, "cache", "pycache"),
          },
        });
        gatewayProcess = child;
        // We own this child — recovery may kill+respawn it. Ownership
        // transitioned: any stale adopted/service classification must not
        // outlive the spawn.
        gatewayOwnership = "spawned";
        // The child inherits its own dup of the fd; close our copy so it doesn't leak.
        if (typeof childOut === "number") { try { fs.closeSync(childOut); } catch { /* ignore */ } }

        child.on("error", (err) => {
          // ENOENT = bin not found on disk; EACCES = present but not executable.
          glog(`spawn ERROR code=${err.code || "?"} msg=${err.message}`);
          if (gatewayProcess !== child) return; // stale child we already replaced
          gatewayStartFailure = { error: err.message, bundled };
          sendStatus(`Gateway failed: ${err.message}`);
          resolve(false);
        });
        child.on("exit", (code, signal) => {
          glog(`gateway child exited code=${code} signal=${signal}`);
          if (signal === "SIGKILL") {
            glog("HINT: SIGKILL on a freshly-spawned bundled binary almost always means macOS Gatekeeper blocked an unsigned/quarantined nested executable. On the recipient's Mac run: xattr -cr <path to KiroCrew.app>");
          }
          // Only the CURRENT child may mutate the shared state. A stale child's
          // late exit (e.g. the one recoverWedgedGateway just SIGKILLed) must be
          // a no-op so it can't orphan the replacement or fake a spawn failure.
          if (gatewayProcess !== child) return;
          // Record the terminal exit so waitForBackend fails fast instead of
          // polling a dead port. Harmless on a graceful shutdown (no wait is
          // running) and on a healthy start (the wait already resolved); a
          // user-initiated Retry clears it so a re-probe can genuinely succeed.
          // Guard: preserve the root cause from the 'error' handler if it fired
          // first (Node fires both 'error' then 'exit' on spawn failure).
          if (!gatewayStartFailure) gatewayStartFailure = { code, signal, bundled };
          gatewayProcess = null;
        });
        resolve(true);
}

/**
 * Gracefully stop the embedded gateway and await its exit (POST /api/shutdown
 * -> SIGTERM -> SIGKILL). Core logic lives in gateway-stop.js for testability;
 * this thin wrapper binds the module-level child process + config.
 *
 * Uses call-time home resolution (secretCandidates) rather than the boot-time
 * KIROCREW_HOME pin, so a KIROCREW_HOME change between boot and shutdown is
 * honored when locating the secret.
 */
async function stopGatewayGracefully({ timeoutMs = 15000 } = {}) {
  const proc = gatewayProcess;
  if (!proc || proc.exitCode !== null) { gatewayProcess = null; return; }
  console.log("Stopping gateway gracefully...");
  // Resolve the secret location at call time. Collect every readable candidate
  // value and let gateway-stop POST each one — the gateway answers 200 only to
  // the secret it actually loaded, so a stale copy can't force a hard SIGTERM.
  const candidates = secretCandidates();
  const kirocrewHome = path.dirname(candidates[0]); // canonical dir (for logs/SIGTERM path)
  const secrets = [];
  for (const candidate of candidates) {
    try {
      const value = fs.readFileSync(candidate, "utf8").trim();
      if (value) secrets.push(value);
    } catch { /* candidate absent/unreadable */ }
  }
  await _stopGatewayGracefully(proc, {
    backendUrl: BACKEND_URL,
    kirocrewHome,
    secrets,
    timeoutMs,
  });
  gatewayProcess = null;
}

/** Best-effort synchronous-ish stop for the before-quit path (can't await). */
function stopGateway() {
  stopGatewayGracefully().catch((err) => console.error("Gateway stop failed:", err?.message));
}

// ── Remote tunnel token fetch ──

function fetchRemoteToken(port) {
  const config = getRemoteHostConfig(store, port || PORT);
  if (!config || !config.host) return Promise.resolve({ token: "", error: null });
  const { host, binPath, remotePort, remotePath } = config;
  const validationErr = validateRemoteSettings(host, binPath, remotePort, remotePath);
  if (validationErr) {
    console.error(`Refusing SSH token fetch: ${validationErr}`);
    return Promise.resolve({ token: "", error: validationErr });
  }

  const effectivePort = remotePort || port || PORT;
  const remoteCmd = buildRemoteTokenCommand(binPath, { port: effectivePort, remotePath: remotePath || undefined });
  const sshArgs = ["-o", "ConnectTimeout=10", host, remoteCmd];

  return new Promise((resolve) => {
    sendStatus("Fetching token from remote dev desktop…");
    console.log(`SSH token fetch: ssh ${host} for port ${effectivePort}`);
    execFile("/usr/bin/ssh", sshArgs, { timeout: Math.max(store.get("sshTimeoutMs") || 20000, 5000) }, (err, stdout, stderr) => {
      if (err) {
        console.error("SSH token fetch failed:", err.message);
        if (stderr) console.error("SSH stderr:", stderr.trim().slice(0, 500));
        return resolve({ token: "", error: stderr?.trim() || err.message });
      }
      resolve({ token: parseTokenFromStdout(stdout), error: null });
    });
  });
}

async function fetchLocalToken(backendUrl = BACKEND_URL) {
  // Re-resolve the authoritative home at call time so a KIROCREW_HOME change
  // after Electron starts is honored. Send exactly that one secret to the
  // gateway's literal IPv4 bind address; never probe alternate homes/addresses.
  return fetchTokenFromHome({
    backendUrl,
    resolveHome,
    path,
    fs,
    http,
  });
}

/**
 * Resolve a gateway credential for Mochi's poller, trying the same paths (in
 * the same order) the main window itself would: the local secret first
 * (same machine, unchanged), then an explicitly configured SSH remote host
 * for this port (unchanged), then — new — the session the main window has
 * ALREADY established. That third path only runs when the first two come
 * back empty, so an ordinary same-machine or SSH-remote install never
 * reaches it at all. See mochi-session-token.js for why it exists and why it
 * cannot weaken auth: it only ever hands back a credential a genuine prior
 * authentication already produced.
 */
async function fetchMochiGatewayAuth(backendUrl = BACKEND_URL) {
  const localValue = await fetchLocalToken(backendUrl);
  if (localValue) return { value: localValue, viaCookie: false };
  const { token: remoteValue } = await fetchRemoteToken(new URL(backendUrl).port);
  if (remoteValue) return { value: remoteValue, viaCookie: false };
  const borrowed = await borrowSessionToken({ electronSession: session.defaultSession, backendUrl });
  return borrowed ? { value: borrowed, viaCookie: true } : { value: "" };
}

function checkBackend(healthUrl = HEALTH_URL) {
  return new Promise((resolve, reject) => {
    const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
      res.resume();
      res.statusCode < 500 ? resolve() : reject();
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(); });
  });
}

function waitForBackend(targetWin, healthUrl = HEALTH_URL, { watchSpawn = false } = {}) {
  return waitForGateway({
    checkBackend: () => checkBackend(healthUrl),
    // Only the primary boot — our own spawned gateway on this port — should
    // fail fast on a child exit. Connection tabs point at OTHER ports we never
    // spawned, so they must not read this flag (it would be cross-talk).
    getFailure: watchSpawn ? (() => gatewayStartFailure) : (() => null),
    isWindowAlive: () => !targetWin?.isDestroyed(),
    onStatus: (msg) => { try { targetWin?.webContents?.send("status", msg); } catch { /* window gone */ } },
    maxWaitMs: MAX_WAIT_MS,
    pollIntervalMs: POLL_INTERVAL_MS,
  });
}

// ── Theme-aware modal styles ──

/** Read CSS custom properties from the active KiroCrew dashboard. */
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
  } catch {}
  return null;
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

/** Get modal CSS — reads live theme vars from dashboard, falls back to dark/light mode. */
async function getModalCSS() {
  const vars = await getDashboardThemeVars();
  if (vars && vars.bg) return modalCSSFromVars(vars);
  const dark = nativeTheme.shouldUseDarkColors;
  return modalCSSForMode(dark);
}

// ── Window ──

// Mirror the dashboard's dark/light MODE PREFERENCE onto Chromium's native
// theme, so native chrome (tab bar, context menus, DevTools, macOS window
// frame) matches the dashboard.
//
// Reads `data-mode-pref` (the preference), NOT `data-mode` (the resolved
// mode). `themeSource = 'dark' | 'light'` does not only restyle native chrome:
// it also OVERRIDES `prefers-color-scheme` in every renderer, which is the
// media query the dashboard's Auto mode resolves through. Feeding the resolved
// mode back in therefore pinned that query to whatever Auto happened to
// resolve at first load, and OS appearance changes stopped propagating — Auto
// froze. Electron's own docs prescribe exactly this mapping:
//   Follow OS -> 'system', Dark -> 'dark', Light -> 'light'.
//
// `data-mode-pref` is absent on a dashboard build older than the field, so an
// unrecognised value falls back to the resolved mode: the pre-fix behaviour for
// explicit dark/light (correct), and no worse than before for Auto.
function syncNativeTheme(view, win) {
  if (win.isDestroyed()) return;
  view.webContents.executeJavaScript(
    `JSON.stringify({` +
      `pref: document.documentElement.dataset.modePref || "",` +
      `mode: document.documentElement.dataset.mode || ""` +
    `})`
  ).then(raw => {
    let pref = "";
    let mode = "";
    try {
      const parsed = JSON.parse(raw);
      pref = parsed.pref || "";
      mode = parsed.mode || "";
    } catch { return; }
    nativeTheme.themeSource = resolveThemeSource(pref, mode);
    if (mode === "dark" || mode === "light") {
      updateWindowsTitleBarOverlay(win, mode);
    }
  }).catch(() => {});
}

/**
 * Session partition for the embedded browser views.
 *
 * `persist:` so the browser keeps its own logins across restarts; separate from
 * the default partition so it never receives the dashboard's `mc_token_<port>`
 * cookie (cookies are host-scoped, not port-scoped).
 */
const BROWSER_PARTITION = "persist:kirocrew-browser";

/**
 * Lock down the embedded-browser partition.
 *
 * Permission handlers are PER-SESSION. Moving these views off the default
 * partition therefore takes them out from under the dashboard's handlers, and an
 * un-handled session falls back to Chromium/Electron defaults — which are far
 * more permissive than what this app grants. So the browser partition gets its
 * own handlers that refuse everything: nothing loaded in the embedded browser
 * needs the mic, camera, geolocation, notifications or MIDI.
 */
function hardenBrowserPartition(sessionApi) {
  const browserSession = sessionApi.fromPartition(BROWSER_PARTITION);
  browserSession.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  browserSession.setPermissionCheckHandler(() => false);
  return browserSession;
}

/**
 * Dispatch one browser-control op onto a panel's native op layer.
 *
 * `entry` is a browser panel (has `.control` + `.manager`). The FULL wire-op
 * vocabulary (navigate/snapshot/click/type/press_key/hover/select_option/
 * screenshot/evaluate/wait_for/back/console) is served natively over the panel's
 * CDP debugger by browser-ops.js — never a raw CDP method from the caller, so
 * the surface cannot be widened by whoever calls it. Shared by the renderer IPC
 * handler and the agent command channel so both are limited to exactly the same
 * capabilities.
 *
 * The op layer is built once per panel and cached, so its console buffer (fed by
 * the panel's CDP event stream) persists across calls. `sendCommand` is the
 * control plane's raw passthrough, valid only while LIGHT holds control — which
 * the caller guarantees by taking the owner (and running the gate) first.
 */
function browserOpsFor(entry) {
  if (entry._ops) return entry._ops;
  entry._ops = createBrowserOps({
    sendCommand: (method, params) => entry.control.send(method, params),
    // Buffer the panel's console/log stream. The debugger object is stable per
    // webContents, so a listener registered once survives attach/detach.
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

function setupWindowContents(win, backendUrl) {
  const port = new URL(backendUrl).port;
  let customName = null;

  // Create a WebContentsView filling the window's content area
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // The SPA reserves header space for the injected Linux caption controls
      // only when the window is actually frameless -- a runtime decision
      // (desktop environment + override), not a platform constant, so it is
      // carried to the preload explicitly (read back via process.argv there).
      additionalArguments: LINUX_FRAMELESS ? ["--kc-linux-frameless"] : [],
    },
  });
  view.setBackgroundColor("#00000000");
  win.contentView.addChildView(view);

  // Clean up views when window is closed
  win.on("closed", () => {
    if (win._mcAgentChannel) void win._mcAgentChannel.stop();
    if (win._mcBrowserPanels) {
      for (const id of [...win._mcBrowserPanels.keys()]) win._mcDestroyBrowserPanel(id);
    }
    view.webContents.close();
  });

  // The dashboard view fills the entire content area; the SPA's own header is
  // the title bar (drag region injected below on macOS).
  function updateViewBounds() {
    if (win.isDestroyed()) return;
    const { width, height } = win.getContentBounds();
    view.setBounds({ x: 0, y: 0, width, height });
    // Embedded browser views are PARTIAL rects inside the same content area,
    // so every event that resizes the window invalidates their clamp too.
    if (win._mcBrowserPanels) {
      for (const entry of win._mcBrowserPanels.values()) entry.manager.refreshBounds();
    }
  }
  updateViewBounds();
  win.on("resize", updateViewBounds);
  // Fullscreen also notifies the renderer: macOS hides the traffic lights in
  // fullscreen, so the SPA drops its 84px header inset (mac-fullscreen class).
  const sendFullScreen = () => {
    if (win.isDestroyed() || view.webContents.isDestroyed()) return;
    view.webContents.send("fullscreen-changed", win.isFullScreen());
  };
  win.on("enter-full-screen", () => { updateViewBounds(); sendFullScreen(); });
  win.on("leave-full-screen", () => { updateViewBounds(); sendFullScreen(); });
  // The initial updateViewBounds() above runs before win.show() and before the
  // dashboard finishes loading, so getContentBounds() can return a pre-layout
  // size — leaving the WebContentsView mis-sized (content overflows / gets cut
  // off a few seconds in once the window settles to its real size). Recompute
  // on every event that can change the final content size.
  win.on("show", updateViewBounds);
  win.on("restore", updateViewBounds);
  win.on("move", updateViewBounds); // display / scale-factor changes
  view.webContents.on("did-finish-load", () => {
    updateViewBounds();
    // Initial state for the renderer (covers booting straight into fullscreen
    // via the fullscreen-restore flag) — and after in-app reloads.
    sendFullScreen();
    // The dashboard loads built-in apps and other content asynchronously after
    // did-finish-load, which can drive a late layout pass; recompute once more
    // shortly after so a content-triggered resize can't leave the view cut off.
    setTimeout(updateViewBounds, 1500);
  });

  // Expose webContents on the window for compatibility
  win.webContents = view.webContents;

  function applyTitle() {
    const remoteName = getRemoteHostConfig(store, port)?.defaultName;
    if (!IS_WIN) {
      // macOS/Linux behavior unchanged: always show the [:port] suffix.
      const suffix = customName || remoteName || `[:${port}]`;
      win.setTitle(`Kiro Crew ${suffix}`);
      return;
    }
    // Windows: the bare "[:5476]" read as part of the product name and confused
    // users, so the primary local window is just "Kiro Crew"; keep the suffix
    // only for a non-default (remote/secondary) window.
    let suffix = customName || remoteName || "";
    if (!suffix && port && String(port) !== "5476") suffix = `[:${port}]`;
    win.setTitle(suffix ? `Kiro Crew ${suffix}` : "Kiro Crew");
  }

  win._mcSetCustomName = (name) => { customName = name; applyTitle(); };
  win._mcGetCustomName = () => customName;
  win._mcBackendUrl = backendUrl;
  win._mcView = view;

  // ── Native browser panels (one per dashboard Browser panel) ──
  // Lazily hosts a real Chromium view over a Browser side-panel's rect. The
  // renderer owns layout (it reports the rect and overlay state); the main
  // process owns the view. See browser-view.js for the security posture — in
  // particular, these views get NO preload, because they render untrusted web
  // content and must never see the dashboard's IPC bridges.
  //
  // Keyed by PANEL, not by window. The dashboard renders one Browser panel per
  // chat session, so a single per-window slot let two sessions clobber each
  // other's agent-act authorization and fight over one shared view.
  const browserPanels = new Map();

  function browserPanel(panelId, { create = true } = {}) {
    const id = typeof panelId === "string" ? panelId.trim() : "";
    if (!id) return null;
    const existing = browserPanels.get(id);
    if (existing || !create) return existing || null;

    // `agentAct` is the HUMAN panel's own "Let the agent act" control (it owns the
    // LIGHT/CDP handoff surfaced in the Browser panel), NOT the agent's
    // authorization to drive the page. That authorization is Browser Mode — the
    // Settings toggle the agent command-channel dispatch now honors directly (see
    // `entry.gate`). So a fresh entry starts with the human control OFF; a mounted
    // panel sets it authoritatively via `browser:set-agent-act`. It is deliberately
    // NO LONGER seeded from a per-session consent set: that set — and the gate that
    // read it — are gone, because gating the built-in view behind a second grant
    // gated a strictly weaker capability than Browser Mode already authorizes.
    const entry = { id, agentAct: false };
    entry.manager = createBrowserViewManager({
      createView: () =>
        new WebContentsView({
          webPreferences: {
            // A SEPARATE persistent partition, not the default session.
            //
            // Cookies are host-scoped, NOT port-scoped (RFC 6265 — see the note
            // on cookie isolation in dashboard/server.py). The dashboard's own
            // auth cookie is `mc_token_<port>` on `localhost`, so an embedded
            // view sharing the default jar would send that credential to ANY
            // `http://localhost:<other-port>` it visited — handing dashboard
            // auth to any local service the user browses to.
            //
            // `persist:` keeps the browser's own logins across restarts (the
            // point of the shared-session decision) while isolating it from the
            // dashboard's credentials, which were never meant to be shared.
            partition: BROWSER_PARTITION,
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            webviewTag: false,
          },
        }),
      getContentBounds: () => win.getContentBounds(),
      addView: (v) => win.contentView.addChildView(v),
      removeView: (v) => win.contentView.removeChildView(v),
      // Chrome the embedded page needs but the module must not import Electron
      // for: the shared right-click menu (spelling suggestions, cut/copy/paste,
      // Look Up). Safe for untrusted content — every item is a plain edit role,
      // none reaches app state.
      onCreate: (v) => attachContextMenu(v.webContents),
      onEvent: (name, payload) => {
        // Page-driven window.open goes to the real browser, never a second
        // native window inside the dashboard. Routed through the audited helper,
        // which swallows BOTH failure shapes (a throwing call and a rejected
        // promise from a scheme the OS cannot handle).
        if (name === "open-external") {
          if (payload && payload.url) {
            openExternalSafely(shell.openExternal, payload.url,
              (msg) => console.warn(`[browser-panel] ${msg}`));
          }
          return;
        }
        // Tag the panel so the renderer routes the event to the right one.
        if (!view.webContents.isDestroyed()) {
          view.webContents.send(`browser:${name}`, { ...(payload || {}), panelId: id });
        }
      },
    });

    // Display (above) and control (here) are independent: the human always
    // drives the page with real OS input, while at most ONE agent owner may
    // hold it over CDP. `agentAct` mirrors the dashboard's general "let the
    // agent act" authorization — recorded here so a control request is checked
    // against state the main process owns, not whatever the caller asserts.
    entry.control = createControlPlane({
      getWebContents: () => entry.manager.getWebContents(),
      onAudit: (event, detail) => {
        console.warn(`[browser-control] ${id} ${event} ${JSON.stringify(detail)}`);
      },
    });
    // The gate the control plane consults on every owner transition — for both
    // the agent command channel and the human panel wiring. It reduces to the
    // VIEW PRECONDITION, because authorization to drive the built-in browser is
    // Browser Mode (the Settings toggle), the agent's keystone-level grant.
    //
    // Precedent (src/kiro_crew/security.py ~line 4236): Browser Mode is documented
    // as keystone-level authorization — "Presence alone is the authorization" —
    // and in attach mode it authorizes driving the operator's OWN running,
    // logged-in browser. The agent's browser_* tools only exist while Browser Mode
    // is on. Gating this isolated embedded Electron view behind a SECOND
    // per-session grant therefore gated a strictly WEAKER capability than the
    // toggle already authorizes; that inconsistency is removed by passing
    // agentActEnabled:true rather than the per-session flag. (canAgentControl's
    // loopback exemption is now redundant — nothing is gated on a per-session
    // grant — so no URL is passed here; the function + its tests stay in
    // browser-control.js as documented there.)
    entry.gate = () =>
      canAgentControl({
        agentActEnabled: true,
        viewOpen: entry.manager.getState().open,
      });

    browserPanels.set(id, entry);
    return entry;
  }

  /** Release one panel's control + view and forget it. */
  function destroyBrowserPanel(id) {
    const entry = browserPanels.get(id);
    if (!entry) return;
    browserPanels.delete(id);
    try { void entry.control.release(); } catch { /* mid-teardown */ }
    try { entry.manager.close(); } catch { /* mid-teardown */ }
  }

  win._mcBrowserPanel = browserPanel;
  win._mcBrowserPanels = browserPanels;
  win._mcDestroyBrowserPanel = destroyBrowserPanel;
  // Chat sessions this window hosts that MAY host a browser panel, whether or
  // not one is mounted right now.
  //
  // This is what makes the built-in browser the default. The agent command
  // channel only long-polls the gateway for the session keys it reports (see
  // listPanelIds), and the gateway's command bus treats "a key was polled for"
  // as "a live panel exists" — so a chat whose Browser tab was never opened had
  // NO key, the bus answered NoPanelError, and the very first "open this page"
  // fell back to the Playwright mirror. Reporting the session keys here keeps the
  // on-demand bootstrap in `dispatch` reachable.
  //
  // Reachability is simply "the active slots the renderer declares" (see the
  // `browser:track-session` IPC + ChatPage's tracking effect). It grants NOTHING:
  // authorization to drive the built-in browser is Browser Mode itself — the
  // Settings toggle, whose presence is the agent's keystone-level grant and
  // without which the browser_* tools do not exist. There is therefore no longer
  // a separate per-session consent set to keep alongside this one; the two sets
  // that used to coexist (durable-consent + transient-reachability) collapse to
  // this single reachability set. Kept SEPARATE from `browserPanels` on purpose:
  // closing the panel destroys its entry (see `browser:close`), and a session
  // must stay reachable across that so the next request re-opens natively.
  const reachableSessions = new Set();
  win._mcReachableSessions = reachableSessions;

  // NOTE: there is deliberately no did-navigate "revoke consent on reload"
  // handler here anymore. It existed ONLY to reset the per-session grant when the
  // dashboard document reloaded (a granted background slot must not be re-seeded
  // as agent-controlled). With the per-session grant removed — Browser Mode is
  // the agent's authorization, and it does not reset on a dashboard repaint —
  // there is nothing consent-shaped left to revoke, and releasing the agent's
  // LIGHT control just because the dashboard reloaded would wrongly interrupt a
  // still-authorized agent. Reachability is re-declared by the renderer's tracking
  // effect on mount (and pruned by its cleanup on slot change), so it needs no
  // reload sweep either.

  // ── Agent command channel ──
  // The agent's `browser_*` MCP calls originate in the Python gateway, which has
  // no way to call INTO Electron. So Electron pulls: it long-polls the gateway
  // for queued commands and posts results back. Outbound-only, which keeps the
  // direction of trust identical to the existing frame pump and means no new
  // listening socket in this process.
  //
  // Commands are routed BY SESSION KEY onto the matching panel's control plane —
  // which only works because panels are session-scoped. Ops go through the same
  // closed verb set the renderer IPC uses (`dispatchBrowserOp`), so the agent can
  // never reach a capability the panel itself does not expose.
  win._mcAgentChannel = createAgentCommandChannel({
    fetchFn: (url, init) => fetch(url, init),
    getGatewayUrl: () => win._mcBackendUrl,
    getSecret: () => readInternalSecret(),
    // The idle host-presence heartbeat must fire ONLY when the gateway is truly
    // on this machine. A loopback URL is necessary but NOT sufficient: a REMOTE
    // gateway reached over a tunnel also presents as localhost, so additionally
    // require that no remote host is configured for THIS window's port (each
    // window has its own `port` from its backendUrl; the module-global PORT is
    // only the primary window's, so a secondary remote window would otherwise
    // read the wrong config and leak the local secret over its tunnel).
    isGatewayLocal: () =>
      isLoopbackUrl(win._mcBackendUrl) && !getRemoteHostConfig(store, port)?.host,
    // Panels that exist PLUS sessions that may host one on demand. Reporting a
    // key is what registers it with the gateway's command bus, so a declared-but-
    // unmounted session must appear here or its first navigate can never arrive.
    // An empty list parks the poller instead of spinning.
    listPanelIds: () => {
      // Never report a key to a gateway that is not on THIS machine.
      //
      // This channel authenticates with the local internal secret, so the only
      // gateway that can accept it is a local one. When the window is connected
      // to a REMOTE gateway, `_mcBackendUrl` IS that remote host, and polling it
      // would push the local secret through the tunnel to be rejected 403 —
      // forever, since the poller retries, making the remote append one SEL
      // denial per attempt without bound.
      //
      // This was unreachable while only CONSENTED sessions were reported (a
      // remote-connected window usually had none, and an empty list parks the
      // poller). Reporting every active slot removed that accident, so the
      // condition is now explicit. Parking is also the correct behaviour on a
      // remote host: it has no Electron view to drive, and the mirror is the
      // right transport there. Fails closed — an unset or unparseable URL is
      // not loopback, so it reports nothing.
      if (!isLoopbackUrl(win._mcBackendUrl)) return [];
      return [...new Set([...browserPanels.keys(), ...reachableSessions])];
    },
    dispatch: async (sessionKey, op, args) => {
      // Proves the op crossed the bus into THIS Electron process (drain worked
      // and a native panel is being driven). Refusals below throw and are logged
      // by the channel's onError; a dispatch with no following error is a success.
      console.warn(`[browser-cmdbus] dispatch op=${op} session=${sessionKey}`);
      // A `navigate` may CREATE the panel it needs, so the agent's first
      // "open this page" can reach the native view instead of falling back to the
      // Playwright mirror. Any other op has no page to act on until one exists, so
      // it still requires a live panel.
      const bootstrapping = op === "navigate";
      const entry = browserPanel(sessionKey, { create: bootstrapping });
      if (!entry) throw new Error(`no native browser panel for session ${sessionKey}`);
      // A `navigate` is what BOOTSTRAPS the view, and the ORDER here matters.
      // `canAgentControl` refuses with `no-browser-view` when nothing is open, so
      // acquiring LIGHT first would refuse before any bootstrap could run — the
      // agent's very first "open this page" would fall back to Playwright and
      // never reach the native browser.
      //
      // Authorization is Browser Mode itself (see `entry.gate`) — the agent's
      // browser_* tools only exist while it is on — so NO per-session grant is
      // required here. Only the "a view exists" precondition is satisfied by
      // creating one; the view precondition is still enforced, never bypassed.
      if (bootstrapping && !entry.manager.getWebContents()) {
        const pre = entry.gate();
        // `mayBootstrapView` tolerates ONLY the absent-view refusal this branch is
        // about to satisfy by opening a view. It is a named, tested predicate
        // because the inline version of it shipped broken — it read a property the
        // gate never returns, making the verdict a constant that refused every
        // native bootstrap. Every OTHER refusal still stops the bootstrap.
        if (!mayBootstrapView(pre)) {
          throw new Error(`browser control refused: ${pre.reason}`);
        }
        // `manager.navigate` creates the view when none is open and applies the
        // same http/https-only guard as the CDP path and the human's own control.
        const opened = entry.manager.navigate(String((args && args.url) || ""));
        if (opened && opened.refused) {
          return { ok: false, code: "bad_url", error: `refused non-web URL: ${args && args.url}` };
        }
        // The view now exists in THIS process but the dashboard owns layout, so
        // without a rect it would be composited nowhere and the user would see an
        // empty panel. Tell the SPA to surface the Browser panel for this session
        // so it mounts, measures and reports bounds (see useNativeBrowser).
        try {
          view.webContents.send("browser:agent-opened", {
            panelId: sessionKey,
            url: (opened && opened.url) || String((args && args.url) || ""),
          });
        } catch {
          // A torn-down dashboard view must not fail the navigation itself.
        }
        // Take LIGHT now that a view exists, so ownership is recorded for the ops
        // that follow and the same gate runs against the real post-open state.
        const takenAfterOpen = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
        if (takenAfterOpen.refused) {
          throw new Error(`browser control refused: ${takenAfterOpen.refused}`);
        }
        return { ok: true, url: (opened && opened.url) || String((args && args.url) || "") };
      }
      // The agent is acting unattended, so it must hold LIGHT — and taking it
      // runs the same gate (the view precondition; authorization is Browser Mode).
      const taken = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
      if (taken.refused) throw new Error(`browser control refused: ${taken.refused}`);
      return dispatchBrowserOp(entry, op, args);
    },
    onError: (err, context) =>
      console.warn(`[browser-agent-channel] ${context}: ${err && err.message}`),
  });
  win._mcAgentChannel.start();

  attachContextMenu(view.webContents);

  // Keep native window controls centered in the zoom-scaled header row.
  // "zoom-changed" covers pinch / ctrl+wheel gestures; the View-menu zoom
  // items call positionTrafficLights explicitly (see zoomItem in the menu).
  if (IS_MAC) {
    positionTrafficLights(win);
    view.webContents.on("zoom-changed", () => setTimeout(() => positionTrafficLights(win), 0));
  }
  if (IS_WINDOWS) {
    updateWindowsTitleBarOverlay(win);
    view.webContents.on("zoom-changed", () => setTimeout(() => updateWindowsTitleBarOverlay(win), 0));
  }

  // The frameless macOS window emits system-context-menu for the drag region;
  // replace it with our window actions.
  win.on("system-context-menu", (e, point) => {
    e.preventDefault();
    Menu.buildFromTemplate([
      { label: "Rename Window…", click: () => renameCurrentWindow() },
      { label: "Set Remote Host…", click: () => promptRemoteHost() },
      { label: "Refresh Token", click: () => refreshToken() },
      { type: "separator" },
      { label: "New Connection Window…", click: () => openNewConnectionWindow() },
    ]).popup({ window: win, x: point.x, y: point.y });
  });

  view.webContents.on("did-finish-load", applyTitle);
  view.webContents.on("page-title-updated", (e) => { e.preventDefault(); applyTitle(); });

  view.webContents.on("did-finish-load", () => {
    // Frameless platforms need an injected drag region so the dashboard
    // header can move the window. On macOS titleBarStyle:"hidden" makes the
    // whole window frameless; on Windows titleBarOverlay provides caption
    // controls but no drag area; on Linux frame:false (when the desktop
    // environment prefers client-side decorations) removes the WM-provided
    // drag surface entirely, so without this bar the window is undraggable.
    // The drag bar is pointer-events:none so clicks pass through to the SPA;
    // interactive controls are marked no-drag so they remain clickable.
    if (IS_MAC || IS_WIN || LINUX_FRAMELESS) {
      view.webContents.insertCSS(`
        #electron-drag-bar {
          position: fixed;
          top: 0; left: 0; right: ${IS_WIN ? '138px' : LINUX_FRAMELESS ? '108px' : '0'};
          height: 42px;
          -webkit-app-region: drag;
          z-index: 99999;
          pointer-events: none;
        }
        a, button, input, select, textarea,
        [role="button"], [tabindex], iframe {
          -webkit-app-region: no-drag;
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
    // Frameless Linux has no OS-painted caption controls (macOS keeps traffic
    // lights, Windows keeps titleBarOverlay), so inject a minimal
    // minimize / maximize / close cluster into the header's top-right corner.
    // Same injection mechanism as the drag bar; actions round-trip through the
    // preload's windowControl bridge to applyWindowControl in this process.
    // The 108px drag-bar inset above keeps the drag region from covering it.
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
        #electron-linux-controls button:hover { opacity: 1; background: rgba(128,128,128,0.18); }
        #electron-linux-controls button.close:hover { background: #e81123; color: #fff; }
        /* The control marks are CSS-drawn (borders/pseudo-elements), not font
           glyphs: U+2013/U+25A1/U+2715 render at inconsistent sizes or as
           tofu boxes depending on the distro's font set. */
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
        /* Maximized: the "restore" mark — two offset squares. */
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
            const b = document.createElement('button');
            b.className = cls;
            b.setAttribute('aria-label', label);
            // Caption controls are window chrome, not page content: native
            // caption buttons are never in the tab order, so keep these out
            // of it too (WM shortcuts cover keyboard users).
            b.tabIndex = -1;
            b.addEventListener('click', () => window.kirocrew?.windowControl?.(action));
            return b;
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
    // Sync window background to theme color (visible in tab bar padding area)
    view.webContents.executeJavaScript(
      `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`
    ).then(bg => { if (bg && !win.isDestroyed()) win.setBackgroundColor(bg); }).catch(() => {});
    // Sync native chrome on first load
    syncNativeTheme(view, win);
  });

  // Sync native tab bar to dashboard dark/light mode on focus (process-global setting)
  win.on("focus", () => syncNativeTheme(view, win));

  // Same-origin opens in-app; cross-origin http(s) and allowlisted non-web
  // schemes are handed to the OS. The non-web branch is what makes the
  // Settings -> Computer Use "Open System Settings" shortcuts work: the
  // dashboard renders inside an instance <iframe>, where a `location.href`
  // assignment to a custom scheme is refused by CSP `frame-src`, so the panel
  // routes through `window.open` — which arrives here. See external-scheme.js.
  view.webContents.setWindowOpenHandler(
    createWindowOpenHandler({
      openExternal: (url) => shell.openExternal(url),
      getAppOrigin: () => backendUrl,
      log: glog,
    }),
  );

  view.webContents.session.webRequest.onBeforeSendHeaders((details, callback) => {
    delete details.requestHeaders["Referer"];
    callback({ requestHeaders: details.requestHeaders });
  });
}

// ── Traffic lights ──
//
// The SPA renders a 42px (CSS px) header that acts as the title bar. The
// native traffic lights are AppKit controls with a fixed ~14px visual height —
// they do not scale with webContents zoom. To keep them visually centered in
// the header at any zoom level, recompute their inset from the current zoom
// factor: the header's on-screen height is 42 * zoomFactor, so both the x
// inset and the vertical centering scale with it.
const HEADER_CSS_PX = 42;

// Repaint one window's Windows caption-control overlay for the resolved theme.
// The painting itself (guards, zoom scaling, and the swallow of the framed
// windows Electron throws on) lives in ./windows-titlebar so it is unit
// testable without an Electron runtime.
function updateWindowsTitleBarOverlay(win, mode) {
  if (!IS_WINDOWS) return;
  const resolvedMode = mode || (nativeTheme.shouldUseDarkColors ? "dark" : "light");
  paintTitleBarOverlay(win, resolvedMode, HEADER_CSS_PX);
}
// Visible AppKit traffic-light control height (fixed; does not scale with zoom).
const TRAFFIC_LIGHT_NATIVE_H = 12;
// AppKit anchors the button GROUP a few px below the naive top inset, so the
// naive (H - buttonH)/2 lands the group low. Measured from a user screenshot at
// a 42px header (lights centered ~3px below the search bar / selector midline),
// this constant nudges the group up to sit on the header centerline. It is a
// fixed device-px correction, applied after the zoom-scaled centering term.
const TRAFFIC_LIGHT_Y_NUDGE = -4;

function trafficLightPositionForZoom(zoomFactor) {
  const stripPx = Math.round(HEADER_CSS_PX * zoomFactor);
  return {
    x: Math.round(16 * zoomFactor),
    y: Math.max(4, Math.round((stripPx - TRAFFIC_LIGHT_NATIVE_H) / 2) + TRAFFIC_LIGHT_Y_NUDGE),
  };
}

function positionTrafficLights(win) {
  if (!IS_MAC || !win || win.isDestroyed()) return;
  try {
    const zoom = win._mcView ? win._mcView.webContents.getZoomFactor() : 1;
    win.setWindowButtonPosition(trafficLightPositionForZoom(zoom));
  } catch { /* window mid-teardown */ }
}

// Map a WebContents (an IPC event.sender) back to the BaseWindow that hosts it.
// The shell renders each page in a WebContentsView (win._mcView), so we match on
// that — BrowserWindow.fromWebContents() is null for a BaseWindow. Needed because
// connection windows load the same SPA and each can emit window-scoped IPC.
function windowForWebContents(wc) {
  for (const win of BaseWindow.getAllWindows()) {
    try {
      if (win._mcView && win._mcView.webContents === wc) return win;
    } catch { /* window mid-teardown */ }
  }
  return null;
}

// Keep the injected Linux caption cluster's maximize button in sync with the
// window's real state: the native control it replaces flips between a
// "maximize" and a "restore" mark, and screen-reader users get the matching
// verb. Renderer-side the state is just a class on the cluster (the restore
// mark is CSS-drawn off `.is-maximized` — see the insertCSS block in
// setupWindowContents). Fire-and-forget: a mid-teardown window rejects the
// executeJavaScript promise, which is fine to drop.
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
  // Called from did-finish-load, which re-fires on every reload: register the
  // window listeners once and only re-push the current state afterwards.
  if (!win._mcLinuxMaximizeSyncArmed) {
    win._mcLinuxMaximizeSyncArmed = true;
    win.on("maximize", push);
    win.on("unmaximize", push);
  }
  push();
}

// Persist the main window's state (geometry + fullscreen + Keep on Top) to the
// store. Module-level so both createWindow()'s geometry listeners and the View
// menu's Keep on Top toggle can trigger a save. No-op while mainWindow is
// absent/destroyed (captureWindowState returns null).
function persistMainWindowState() {
  const s = captureWindowState(mainWindow);
  if (s) store.set("windowState", s);
}

function createWindow() {
  // Restore the saved geometry so quitting from native fullscreen (or any size)
  // comes back correctly. Without this the window is always rebuilt at the
  // default size and macOS drops that fixed-size window into the fullscreen
  // Space it restored — which is the long-standing "blacked out" (view doesn't
  // fill the Space) / "super tiny" (window doesn't fill the Space) bug. We own
  // the geometry and re-enter fullscreen ourselves instead. screen.* is only
  // valid after app.whenReady(); createWindow runs from the whenReady handler.
  const state = sanitizeWindowState(store.get("windowState"), {
    displays: screen.getAllDisplays().map((d) => ({ workArea: d.workArea })),
    defaults: { width: 1280, height: 860 },
    minSize: { width: 550, height: 600 },
  });

  const opts = {
    width: state.width,
    height: state.height,
    minWidth: 550,
    minHeight: 600,
    backgroundColor: "#0f1117",
  };
  // Frameless chrome: the dashboard's 42px header doubles as the title bar.
  // macOS: titleBarStyle:"hidden" + native traffic lights inset into it.
  // Windows: titleBarStyle:"hidden" + titleBarOverlay puts native caption
  //   controls (minimize/maximize/close) in an overlay strip synced to theme.
  // Linux: frame:false on desktops that expect client-side decorations,
  //   native frame elsewhere (see linux-frame.js). titleBarStyle is ignored
  //   by Electron on Linux, so the explicit frame flag is the mechanism.
  if (IS_MAC) opts.titleBarStyle = "hidden";
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
    // A visible menu bar under a frameless window re-creates the stacked-bars
    // problem (#3606); removing it entirely would take the app menu — and with
    // it the discoverable path to windowing actions — away. Auto-hide keeps it
    // out of the resting chrome while Alt still reveals it.
    opts.autoHideMenuBar = true;
  }
  // Window + taskbar icon (Windows only): running unpackaged (`electron .`)
  // otherwise shows the default Electron icon. macOS takes its icon from the
  // .app bundle and Linux from the .desktop/AppImage, so leave those untouched.
  if (IS_WIN) {
    const iconFile = identityFamily(app.getVersion()) === "nightly"
      && fs.existsSync(path.join(__dirname, "icon-nightly.png"))
      ? "icon-nightly.png" : "icon.png";
    opts.icon = path.join(__dirname, iconFile);
  }
  // Inset the native traffic lights into the dashboard's 42px header row.
  // Kept in sync with zoom by positionTrafficLights().
  if (IS_MAC) opts.trafficLightPosition = trafficLightPositionForZoom(1);
  // Only include `fullscreen` when we actually want fullscreen: the flag
  // preserves the fullscreen-restore intent — the window comes up already
  // fullscreen when we quit in fullscreen. The width/height above become the
  // normal frame to return to on exit.
  if (state.fullScreen) opts.fullscreen = true;
  // Restore the Keep on Top preference (View menu checkbox). Constructor
  // option, not a post-create setAlwaysOnTop(), so the window never flashes
  // at normal z-order first.
  if (state.alwaysOnTop) opts.alwaysOnTop = true;
  if (typeof state.x === "number" && typeof state.y === "number") {
    opts.x = state.x;
    opts.y = state.y;
  }
  mainWindow = new BaseWindow(opts);
  if (IS_WINDOWS && typeof mainWindow.setMenuBarVisibility === "function") {
    mainWindow.setMenuBarVisibility(false);
  }

  setupWindowContents(mainWindow, BACKEND_URL);

  // Persist geometry on every change (debounced) so a quit/crash at any point
  // keeps the last good size + fullscreen flag. captureWindowState() uses
  // getNormalBounds(), so we store the restore size, never the fullscreen frame.
  let saveTimer = null;
  const persist = persistMainWindowState;
  const persistDebounced = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(persist, 400);
  };
  mainWindow.on("resize", persistDebounced);
  mainWindow.on("move", persistDebounced);
  mainWindow.on("enter-full-screen", persist);
  mainWindow.on("leave-full-screen", persist);

  // Auto-refresh token on 403 (gateway secret regenerated after restart)
  const onNavigate = createTokenRetryHandler(async () => {
    let token = await fetchLocalToken(BACKEND_URL);
    if (!token) ({ token } = await fetchRemoteToken(PORT));
    if (token && !mainWindow.isDestroyed()) {
      mainWindow.webContents.loadURL(`${BACKEND_URL}?token=${token}`);
    }
  });
  mainWindow.webContents.on("did-navigate", (_e, _url, httpCode) => {
    onNavigate(httpCode).catch((err) => console.error("Token retry failed:", err));
  });

  mainWindow.on("close", (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
      return;
    }
    // Real quit — capture the final geometry synchronously before teardown so
    // the pending debounced save can't be lost.
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    persist();
  });

  return mainWindow;
}

function createTray() {
  // Nightly ships its own icon (night-sky variant) so the menu-bar presence
  // matches the Dock identity; app.name was set channel-aware at boot.
  const nightly = identityFamily(app.getVersion()) === "nightly";
  const iconFile = nightly && fs.existsSync(path.join(__dirname, "icon-nightly.png"))
    ? "icon-nightly.png" : "icon.png";
  let icon;
  const templatePath = path.join(__dirname, "trayTemplate.png");
  if (IS_MAC && fs.existsSync(templatePath)) {
    // macOS menu-bar icons are template images: a monochrome (black +
    // alpha) glyph the system recolors for light/dark/tinted menu bars.
    // Passing the full-colour icon here renders it as-is, which clashes
    // with neighbouring status items and loses contrast on tinted bars.
    // The asset ships at 18px with an @2x retina variant that
    // createFromPath picks up via the DPI-suffix convention, so no
    // resize. setTemplateImage is explicit even though the *Template
    // filename convention already implies it. The stable and nightly
    // glyphs share one silhouette (the channels differ only in field
    // colour), so a single template asset serves both; channel identity
    // stays on the Dock icon and app.name.
    icon = nativeImage.createFromPath(templatePath);
    icon.setTemplateImage(true);
  } else {
    // Other platforms render tray icons literally, so keep the
    // channel-aware full-colour icon (nightly identity stays visible).
    // Reaching here on macOS means the template asset did not ship;
    // the menu bar silently regresses to the colour icon, so leave a
    // signal for whoever debugs the packaging.
    if (IS_MAC) console.warn("tray: trayTemplate.png missing, falling back to colour icon");
    icon = nativeImage.createFromPath(path.join(__dirname, iconFile))
      .resize({ width: 18, height: 18 });
  }
  tray = new Tray(icon);
  tray.setToolTip(app.name);
  // Each connection opens as its own window on every platform (native window
  // tabs were removed with the single-surface shell redesign).
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: `Show ${app.name}`, click: () => mainWindow?.show() },
      { type: "separator" },
      { label: "New Connection Window…", click: () => openNewConnectionWindow() },
      { type: "separator" },
      { label: "Open Config File", click: () => shell.openPath(store.path) },
      { type: "separator" },
      { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
    ])
  );
  tray.on("click", () => mainWindow?.show());
}

// ── Remote host settings ──

async function promptRemoteHost() {
  const focused = BaseWindow.getFocusedWindow() || mainWindow;
  if (!focused || focused.isDestroyed() || !focused._mcBackendUrl) return;
  const port = new URL(focused._mcBackendUrl).port;
  const config = getRemoteHostConfig(store, port);
  const currentHost = config?.host || "";
  const currentBin = config?.binPath || DEFAULT_REMOTE_BIN;
  const currentRemotePort = config?.remotePort || "";
  const currentRemotePath = config?.remotePath || "";

  const css = await getModalCSS();
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const promptWin = new BrowserWindow({
    width: 480, height: 400, resizable: false, useContentSize: true,
    parent: focused, modal: true, backgroundColor: "#00000000",
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const html = `<!DOCTYPE html><html><head><style>
    ${css}
  </style></head><body>
    <label>Remote host for :${port}</label>
    <input id="h" value="${esc(currentHost)}" placeholder="myhost.corp.example.com" autofocus>
    <div class="hint">Leave empty to use local token (no SSH).</div>
    <label>kirocrew binary path</label>
    <input id="b" value="${esc(currentBin)}" placeholder="${DEFAULT_REMOTE_BIN}">
    <label>Remote port <span style="font-weight:normal;opacity:0.6">(default: same as tab = ${port})</span></label>
    <input id="rp" value="${esc(currentRemotePort)}" placeholder="${port}">
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
      document.addEventListener('keydown', e => { if(e.key==='Enter') save(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  promptWin.setMenu(null);

  let savedTitle = null;
  promptWin.on("page-title-updated", (_e, title) => { savedTitle = title; });
  promptWin.on("closed", () => {
    try {
      if (savedTitle && savedTitle.startsWith("{")) {
        const { host, bin, remotePort, remotePath } = JSON.parse(savedTitle);
        if (host) {
          const err = validateRemoteSettings(host, bin, remotePort, remotePath);
          const parent = focused && !focused.isDestroyed() ? focused : null;
          if (err) {
            dialog.showMessageBox(parent, { type: "error", title: "Invalid Input", message: err });
            return;
          }
        }
        setRemoteHostConfig(store, port, { host, binPath: bin, remotePort, remotePath });
        const parent = focused && !focused.isDestroyed() ? focused : null;
        const msg = host ? `Remote host for :${port} set to ${host}` : `Remote host for :${port} cleared (using local token)`;
        console.log(msg);
        dialog.showMessageBox(parent, { message: msg, type: "info" });
      }
    } catch (e) { console.error("Failed to parse remote host settings:", e.message); }
  });
}

async function refreshToken() {
  const win = BaseWindow.getFocusedWindow() || mainWindow;
  if (!win || win.isDestroyed() || !win._mcBackendUrl) return;
  const backendUrl = win._mcBackendUrl;
  const port = new URL(backendUrl).port;

  let token = await fetchLocalToken(backendUrl);
  let sshErr = null;
  if (!token) ({ token, error: sshErr } = await fetchRemoteToken(port));
  if (win.isDestroyed()) return;
  if (token) {
    win.webContents.loadURL(`${backendUrl}?token=${token}`);
  } else {
    const config = getRemoteHostConfig(store, port);
    dialog.showMessageBox(win, {
      type: "warning",
      title: "Token Refresh",
      message: "Could not fetch a fresh token.",
      detail: config?.host
        ? `SSH to ${config.host} failed.\n\n${sshErr || "Check your connection."}`
        : "No remote host configured for this tab. Use 'Set Remote Host…' from the Tab menu.",
    });
  }
}

// ── Loading screen ──

/**
 * Tell the boot-reveal loading screen the gateway is ready, then wait for it to
 * finish its animation + fade-out before we navigate to the dashboard. The
 * loading screen replies via the "boot-complete" IPC once its fade ends; a
 * timeout is a safety net (reduced-motion, JS error, or a non-reveal screen).
 */
function fadeLoadingScreen(wc, timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (!wc || wc.isDestroyed()) return resolve();
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      ipcMain.removeListener("boot-complete", onComplete);
      resolve();
    };
    const onComplete = (e) => { if (e.sender === wc) finish(); };
    ipcMain.on("boot-complete", onComplete);
    const timer = setTimeout(finish, timeoutMs);
    try { wc.send("boot-ready"); } catch { finish(); }
  });
}

/**
 * Themed, fixed-size error window with a SCROLLABLE log pane. Replaces the
 * native dialog.showMessageBox for gateway-launch failures: the native dialog's
 * `detail` grows the dialog vertically with no scroll, so a long launch-log
 * tail made it "super tall". Here the log lives in a <pre> with a capped
 * max-height + overflow:auto, so the window stays a sane size no matter how
 * long the log is. Returns the chosen action.
 *
 * @param {Electron.BaseWindow} parentWin
 * @param {{title:string, message:string, logTail:string, logPath:string,
 *          portConflict:boolean, port:number, noRetry?:boolean}} opts
 * @returns {Promise<'retry'|'force-retry'|'reveal'|'quit'>}
 */
function showGatewayErrorDialog(parentWin, opts) {
  const {
    title,
    message,
    logTail,
    logPath,
    portConflict,
    noRetry = false,
    localGatewayOff = false,
    primaryAction: configuredPrimaryAction,
    primaryLabel: configuredPrimaryLabel,
    showQuitButton: configuredShowQuitButton,
  } = opts;
  const showQuitButton = configuredShowQuitButton ?? !noRetry;
  return new Promise((resolve) => {
    const dark = nativeTheme.shouldUseDarkColors;
    const hasParent = parentWin && !parentWin.isDestroyed();
    const win = new BrowserWindow({
      width: 620, height: 460, minWidth: 460, minHeight: 320,
      resizable: true, useContentSize: true,
      parent: hasParent ? parentWin : undefined,
      modal: !!hasParent,
      backgroundColor: dark ? "#1e293b" : "#f8fafc",
      webPreferences: { nodeIntegration: false, contextIsolation: true },
    });
    win.setMenu(null);

    const esc = (s) => String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    // The primary action depends on whether the port is held: a plain retry
    // can't clear a port conflict, so offer force-stop instead. A TERMINAL
    // caller (one that quits on every action) passes noRetry — showing a
    // "Retry" button it would ignore contradicts the dialog's own message.
    const primaryAction = configuredPrimaryAction
      || (noRetry ? "quit" : (portConflict ? "force-retry" : "retry"));
    const primaryLabel = configuredPrimaryLabel
      || (noRetry ? "Quit" : (portConflict ? "Force-stop & Retry" : "Retry"));
    // A client-only install whose remote is unreachable needs an in-app way to
    // change its mind: Settings lives inside the dashboard, which a gateway has
    // to serve, so the page holding the switch is exactly what it cannot reach.
    // Retry stays primary because restoring the remote is the likelier fix.
    const enableButton = (localGatewayOff && !noRetry)
      ? `<button class="cancel" onclick="act('enable-retry')">Start Local Gateway</button>`
      : "";
    const fg = dark ? "#e2e8f0" : "#1e293b";
    const muted = dark ? "#94a3b8" : "#64748b";
    const html = `<!DOCTYPE html><html><head><style>
      * { margin:0; padding:0; box-sizing:border-box; }
      body { font-family:-apple-system,sans-serif; padding:20px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${fg};
        display:flex; flex-direction:column; height:100vh; }
      .title { font-size:15px; font-weight:700; margin-bottom:6px; }
      .msg { font-size:13px; line-height:1.45; margin-bottom:10px; }
      .pathline { font-size:11px; color:${muted}; margin-bottom:6px; word-break:break-all; }
      /* Scrollable, fixed-height log pane — the whole point of this window. */
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
      <div class="title">${esc(title)}</div>
      <div class="msg">${esc(message)}</div>
      <div class="pathline">${esc(logPath)}</div>
      <pre class="log">${esc(logTail || "(launch log is empty)")}</pre>
      <div class="row">
        <button class="ok" onclick="act('${primaryAction}')">${esc(primaryLabel)}</button>
        ${enableButton}
        <button class="cancel" onclick="act('reveal')">Reveal Log</button>
        ${showQuitButton ? '<button class="cancel" onclick="act(\'quit\')">Quit</button>' : ""}
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
    win.on("page-title-updated", (_e, t) => {
      if (t && t.startsWith("mc-action:")) action = t.slice("mc-action:".length);
    });
    win.on("closed", () => resolve(action || "quit"));
    win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  });
}

// Absolute tool paths because a packaged GUI app has a minimal PATH. lsof lives
// at DIFFERENT paths per platform: /usr/sbin/lsof on macOS, /usr/bin/lsof on
// Linux. Hard-coding one means the other platform silently fails to exec, and a
// swallowed ENOENT looked like "no LISTEN owner" → forceStopPort reported the
// port freed and respawned into a still-wedged backend. Probe both, then PATH.
const LSOF_CANDIDATES = ["/usr/sbin/lsof", "/usr/bin/lsof"];
function _resolveLsof() {
  for (const c of LSOF_CANDIDATES) {
    try { if (fs.existsSync(c)) return c; } catch { /* ignore unreadable candidate */ }
  }
  return "lsof"; // fall back to PATH
}
function _lsofListenPids(port) {
  return new Promise((resolve, reject) => {
    execFile(_resolveLsof(), ["-nP", `-iTCP:${port}`, "-sTCP:LISTEN", "-t"], { timeout: 5000 }, (err, stdout) => {
      // lsof exits non-zero with empty output when there is simply no match —
      // that is a genuinely free port, NOT an error. Only a failure to EXECUTE
      // the binary (ENOENT/EACCES) must be surfaced, so the caller never
      // mistakes "couldn't probe" for "port is free".
      if (err && (err.code === "ENOENT" || err.code === "EACCES")) {
        return reject(err);
      }
      resolve(String(stdout || "").split(/\s+/)
        .map((s) => parseInt(s, 10)).filter((n) => Number.isInteger(n) && n > 1));
    });
  });
}

function _psCommand(pid) {
  return new Promise((resolve) => {
    execFile("/bin/ps", ["-p", String(pid), "-o", "command="], { timeout: 5000 }, (_e, cmdOut) => {
      resolve(String(cmdOut || ""));
    });
  });
}

// Parent PID of `pid`. Used to tell an OS-service-managed gateway (reparented to
// init) from one this app spawned, so a service-managed holder is reused rather
// than evicted — killing it just loses a race with launchd/systemd's respawn.
// Resolves "" on any failure; classifyPortOwner treats that as "do not touch".
function _psPpid(pid) {
  return new Promise((resolve) => {
    execFile("/bin/ps", ["-p", String(pid), "-o", "ppid="], { timeout: 5000 }, (_e, out) => {
      resolve(String(out || ""));
    });
  });
}

/**
 * Best-effort force-stop of whatever holds `port`, scoped to KiroCrew processes
 * only, then VERIFY the port actually freed (see forceStopPort in gateway-stop.js).
 * Returns {killed, freed, survivors}: `freed === false` means the holder could
 * not be killed (uninterruptible-sleep wedge) and a respawn would just fail to
 * bind — callers must surface "restart required" instead of retrying.
 *
 * @param {number} port
 * @returns {Promise<{killed:number, freed:boolean, survivors:number[]}>}
 */
function forceStopGatewayPort(port) {
  if (IS_WIN) {
    return forceStopPort(port, {
      getListenPids: windowsListenPids,
      getCommand: windowsProcessCommand,
      kill: (pid) => windowsTaskkill(pid, {
        isTrustedCommand: isTrustedWindowsGatewayCommand,
      }),
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      isKirocrew: isTrustedWindowsGatewayCommand,
      failClosedOnProbeError: true,
      log: glog,
    });
  }
  return forceStopPort(port, {
    getListenPids: _lsofListenPids,
    getCommand: _psCommand,
    getPpid: _psPpid,
    kill: (pid, sig) => process.kill(pid, sig),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    log: glog,
  });
}

/**
 * Start (or restart) the post-handoff liveness monitor for the primary window.
 * Polls /api/status; on sustained unresponsiveness it force-restarts the wedged
 * gateway. Only the primary window on our own port is monitored — connection
 * tabs point at backends we didn't spawn and must not trigger a respawn.
 */
function startLivenessMonitor(win) {
  if (livenessMonitor) { livenessMonitor.stop(); livenessMonitor = null; }
  livenessMonitor = createLivenessMonitor({
    probe: () => checkBackend(HEALTH_URL),
    isWindowAlive: () => !!win && !win.isDestroyed(),
    onUnresponsive: () => {
      if (livenessMonitor) { livenessMonitor.stop(); livenessMonitor = null; }
      if (isQuitting || installingUpdate) return; // intentional shutdown or install — not a wedge
      recoverWedgedGateway(win).catch((e) => glog(`liveness recovery failed: ${e && e.message}`));
    },
    onRecovered: () => glog("liveness: backend responsive again (transient blip)"),
    log: (m) => glog(`liveness: ${m}`),
  });
  livenessMonitor.start();
}

/**
 * Recover a gateway that is alive-but-unresponsive (wedged event loop). A
 * graceful /api/shutdown can't help — that endpoint runs on the very loop that
 * is frozen — so SIGKILL the child, clear the port, respawn, and re-run the
 * boot flow. showLoadingThenConnect shows the loading screen + status (a visible
 * "restarting" state instead of an eternal spinner) and starts a fresh monitor
 * on success; its own catch handles a restart that fails.
 */
async function recoverWedgedGateway(win) {
  // We only OWN (and may kill/respawn) a gateway we spawned. On the reuse path
  // the port-holder is someone else's process — in the remote-tunnel setup it is
  // our own SSH forward, whose backend lives on a remote host. An unresponsive
  // probe there almost always means the SSH tunnel dropped (lid close,
  // Wi-Fi→Ethernet handoff, VPN blip), not a wedged backend. Killing the port
  // would tear down the tunnel; force-stop correctly refuses, then the old code
  // fell through to showUnrecoverableGatewayError, which QUIT the app on any
  // button (that was the "crash on Retry"). Instead: leave the tunnel alone and
  // re-probe until it heals, then reconnect.
  const strategy = chooseRecoveryStrategy({ gatewayOwnership });
  if (strategy === "reconnect") {
    glog("liveness: backend unresponsive on a gateway we did not spawn (remote tunnel / external gateway) — waiting for it to recover instead of killing the port");
    if (!win || win.isDestroyed() || isQuitting) return;
    return reconnectExternalGateway(win);
  }
  // An ADOPTED local same-family gateway (reuse path, but the holder was a
  // local Kiro Crew process) gets neither the kill (we don't own it) nor the
  // indefinite tunnel wait (nothing external will bring it back): bounded
  // wait, then respawn once the port clears on its own.
  if (strategy === "reconnect-bounded") {
    glog("liveness: backend unresponsive on an adopted local Kiro Crew gateway — bounded wait, then respawn");
    if (!win || win.isDestroyed() || isQuitting) return;
    return reconnectOrRespawnAdoptedGateway(win);
  }
  glog("liveness: backend unresponsive — force-killing wedged gateway and restarting");
  // Capture the frozen stack from OUTSIDE the wedged process BEFORE the kill.
  // The in-process faulthandler watchdog races (and loses to) this very SIGKILL,
  // and a starved loop often can't dump itself — py-spy reads stacks via ptrace
  // so the post-restart crash report gets the real frozen frame. Best-effort and
  // time-bounded; it never blocks the kill beyond its own timeout.
  if (gatewayProcess && gatewayProcess.pid) {
    await capturePySpyDump({
      pid: gatewayProcess.pid,
      dumpDir: path.dirname(gatewayLogPath()),
      log: (m) => glog(`liveness: ${m}`),
    }).catch((e) => glog(`liveness: py-spy capture threw: ${e && e.message}`));
  }
  try { if (gatewayProcess) gatewayProcess.kill("SIGKILL"); } catch (e) { glog(`SIGKILL failed: ${e && e.message}`); }
  gatewayProcess = null;
  let freed = true;
  let foreignHolder = false;
  let probeFailed = false;
  try { ({ freed, foreignHolder, probeFailed = false } = await forceStopGatewayPort(PORT)); }
  catch (e) {
    // We couldn't even probe the port (lsof failed to exec). Don't silently
    // assume freed — let the respawn's bind be the arbiter, but say so loudly.
    glog(`liveness: port probe failed (${e && e.message}); attempting respawn and letting bind confirm`);
  }
  if (!win || win.isDestroyed() || isQuitting) return;
  // If the wedged process is unkillable, or a foreign process still owns the
  // port, respawning would just hit "address already in use". Don't pretend we
  // recovered — show the honest error path so the user learns a restart (or
  // freeing the other app) is required.
  if (!freed) {
    const reason = probeFailed ? "probe failed"
      : (foreignHolder ? "foreign holder" : "unkillable wedge");
    glog(`liveness: port not confirmed free after force-stop (${reason}); surfacing restart-required`);
    return showUnrecoverableGatewayError(win, PORT, { probeFailed });
  }
  gatewayStartFailure = null; // re-arm so waitForGateway doesn't fail-fast on the kill we just did
  await startGateway(); // spawn a fresh child before re-waiting
  if (win.isDestroyed() || isQuitting) return;
  return showLoadingThenConnect(win, BACKEND_URL);
}

/**
 * Recover a gateway we do NOT own (reuse path — remote tunnel or external
 * gateway). We must not kill the port-holder or spawn a local backend. Instead
 * show the loading screen and gently re-probe /api/status until the backend is
 * reachable again (the SSH tunnel typically re-establishes within ~15s), then
 * re-run the normal connect flow — which re-fetches a fresh token over SSH,
 * since the dropped link likely invalidated the old one. Bailing out whenever
 * the window is torn down or the app is quitting keeps this loop from outliving
 * its window.
 */
async function reconnectExternalGateway(win) {
  const wc = win.webContents;
  try { wc.loadFile(path.join(__dirname, "loading.html")); } catch { /* window may be mid-teardown */ }
  if (!win || win.isDestroyed() || isQuitting) return; // loadFile may have thrown on a torn-down window; show() would too
  win.show();
  sendStatus("Connection lost — waiting for the gateway to come back…");
  for (;;) {
    if (!win || win.isDestroyed() || isQuitting) return;
    let healthy = false;
    try { await checkBackend(HEALTH_URL); healthy = true; } catch { /* still down */ }
    if (healthy) break;
    await new Promise((r) => setTimeout(r, 5000));
  }
  if (!win || win.isDestroyed() || isQuitting) return;
  glog("liveness: external gateway reachable again — refetching token and reconnecting");
  gatewayStartFailure = null;
  return showLoadingThenConnect(win, BACKEND_URL);
}

// How long recovery waits for an ADOPTED local gateway to answer again before
// concluding it is gone for good. A local gateway's plausible comebacks (a
// long GC pause, a graceful drain we adopted mid-flight finishing + something
// relaunching it) resolve well inside this; a tunnel-style multi-minute outage
// cannot happen to a process on this machine.
const ADOPTED_RECOVERY_WAIT_MS = 30_000;

/**
 * Recover an ADOPTED local same-family gateway (reuse path, port held by a
 * local Kiro Crew process we did not spawn). We must not kill the holder — but
 * unlike a tunnel, a dead local gateway will never come back on its own, so the
 * indefinite reconnectExternalGateway loop is exactly the observed failure:
 * adopt a draining gateway, classify its death as "remote tunnel", and wait
 * forever for a comeback a local process cannot make. Instead: re-probe for
 * a bounded interval; if the backend answers, reconnect; otherwise wait for
 * the port to clear (the dying process releasing its socket) and spawn our
 * own backend.
 * If the port never clears, the holder is alive-but-unresponsive and not ours
 * to kill — surface the honest restart-required dialog instead of spinning.
 */
async function reconnectOrRespawnAdoptedGateway(win) {
  const wc = win.webContents;
  try { wc.loadFile(path.join(__dirname, "loading.html")); } catch { /* window may be mid-teardown */ }
  if (!win || win.isDestroyed() || isQuitting) return;
  win.show();
  sendStatus("Gateway stopped responding — waiting for it to recover…");
  const deadline = Date.now() + ADOPTED_RECOVERY_WAIT_MS;
  while (Date.now() < deadline) {
    if (!win || win.isDestroyed() || isQuitting) return;
    let healthy = false;
    try { await checkBackend(HEALTH_URL); healthy = true; } catch { /* still down */ }
    if (healthy) {
      glog("liveness: adopted local gateway answering again — reconnecting");
      gatewayStartFailure = null;
      return showLoadingThenConnect(win, BACKEND_URL);
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  if (!win || win.isDestroyed() || isQuitting) return;
  glog(`liveness: adopted local gateway did not recover within ${ADOPTED_RECOVERY_WAIT_MS}ms — waiting for :${PORT} to clear, then spawning our own backend`);
  sendStatus("Waiting for the previous gateway to exit…");
  // Capture the incumbent NOW, while it may still own the LISTEN socket —
  // needed below to wait out its gateway.lock after the port clears.
  const incumbentPids = await snapshotGatewayPortPids(PORT);
  if (unverifiedIncumbent(incumbentPids)) {
    glog(`liveness: could not capture the incumbent PID on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
    return showUnrecoverableGatewayError(win, PORT, { probeFailed: true });
  }
  if (!(await waitForPortFree())) {
    glog(`liveness: :${PORT} still held by a process we did not spawn — surfacing port-held instead of waiting forever`);
    // No stop was attempted on this path (the holder is not ours to evict), so
    // the "wedged, restart your computer" copy would be false — use the honest
    // port-held variant: quit the holder and relaunch.
    return showUnrecoverableGatewayError(win, PORT, "held");
  }
  if (!win || win.isDestroyed() || isQuitting) return;
  if (gatewayOwnership === "reused-service") {
    // The dead gateway was SERVICE-classified: a real launchd/systemd unit is
    // (or may be) about to respawn it, and spawning immediately races that
    // rebind for the port. Orphans classify as service too but nothing rebinds
    // them — so grace-wait: reconnect to a rebind, spawn only on a quiet port.
    glog(`liveness: adopted gateway was service-managed — waiting a bounded grace for its manager to respawn it before spawning our own`);
    sendStatus("Waiting for the gateway to restart…");
    const verdict = await waitForServiceRebind({
      isPortBound: async () => (await probeGatewayPortOwner(PORT)) !== "none",
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    });
    if (win.isDestroyed() || isQuitting) return;
    if (verdict === "rebound") {
      // Validate the new holder through the SAME identity table the boot path
      // uses — owner probe + /api/health family check + readiness — not a
      // weaker ad-hoc test: a cross-family gateway that rebinds here must hit
      // the same takeover guard as at boot, not get silently reconnected to.
      const owner = await probeGatewayPortOwner(PORT);
      const health = await fetchHealthInfo();
      const decision = decideGatewayAction(app.getVersion(), health, { localOwner: owner });
      const readiness = await fetchGatewayReadiness();
      if (decision.action === "reuse" && readiness !== "shutting-down") {
        glog(`liveness: service manager re-bound :${PORT} (owner=${owner}, reason=${decision.reason}, readiness=${readiness}) — reconnecting to the restarted gateway`);
        gatewayOwnership = classifyAdoptedGateway({ reason: decision.reason, localOwner: owner });
        gatewayStartFailure = null;
        return showLoadingThenConnect(win, BACKEND_URL);
      }
      glog(`liveness: :${PORT} was re-bound by an unusable holder (owner=${owner}, action=${decision.action}, readiness=${readiness}) — cannot reconnect or spawn over it`);
      return showUnrecoverableGatewayError(win, PORT, "held");
    }
    glog(`liveness: :${PORT} stayed free past the rebind grace — no manager respawned it; spawning our own backend`);
  }
  // Port free ≠ lock free: the incumbent may still be flushing sessions while
  // holding gateway.lock. Wait for the process itself before spawning.
  await waitForIncumbentExit(incumbentPids, "liveness");
  sendStatus("Starting a fresh gateway…");
  gatewayStartFailure = null;
  await startGateway(); // spawn a fresh child (port is confirmed free)
  if (win.isDestroyed() || isQuitting) return;
  return showLoadingThenConnect(win, BACKEND_URL);
}

/**
 * Terminal state, three variants:
 *  - "wedged" (default): a force-stop was actually attempted and the holder
 *    survived it (uninterruptible kernel sleep). Only a machine restart clears
 *    it, and the copy says so.
 *  - "held": port ${port} is occupied by a process this app will not evict
 *    (an unresponsive previous gateway, a foreign process, or a cross-family
 *    gateway that grabbed the port). No stop was attempted — telling the user
 *    to reboot would be false and needlessly heavy; quitting the holder and
 *    relaunching suffices.
 *  - probeFailed: ownership could not be verified, so no process was terminated.
 *    Ask the user to reopen first and restart only if the port remains blocked.
 */
async function showUnrecoverableGatewayError(win, port, options = {}) {
  const { variant = "wedged", probeFailed = false } = typeof options === "string"
    ? { variant: options }
    : options;
  if (!win || win.isDestroyed()) return;
  let logTail = "";
  try { logTail = tailLines(fs.readFileSync(gatewayLogPath(), "utf8"), 60); } catch { /* no log yet */ }
  const action = await showGatewayErrorDialog(win, {
    ...unrecoverableGatewayDialog({
      port,
      variant,
      probeFailed,
      isPrimaryWindow: win === mainWindow,
    }),
    logTail,
    logPath: gatewayLogPath(),
    port,
    // This caller is terminal and quits on every action.
    noRetry: true,
  });
  if (win.isDestroyed()) return;
  if (action === "reveal") {
    try { shell.showItemInFolder(gatewayLogPath()); } catch { /* best effort */ }
  }
  if (win === mainWindow) { isQuitting = true; app.quit(); } else { win.destroy(); }
}

async function showLoadingThenConnect(win, backendUrl = BACKEND_URL) {
  const healthUrl = `${backendUrl}/api/status`;
  const wc = win.webContents;
  // Paint the splash in the user's chosen accent (persisted from a prior session
  // via the "theme-accent-changed" IPC). Defaults to the Kiro brand purple.
  wc.loadFile(path.join(__dirname, "loading.html"), {
    query: { accent: currentThemeAccent() },
  });
  win.show();

  try {
    await waitForBackend(win, healthUrl, { watchSpawn: backendUrl === BACKEND_URL });
    if (win.isDestroyed()) return;

    // Acquire a dashboard token, retrying a transient warmup 403 on our OWN
    // gateway. A gateway we just (re)started regenerates its .local_secret at
    // boot, so right after /api/status answers the local mint can 403 briefly
    // while the secret settles. For a foreign gateway (SSH forward / external)
    // the secret is on the remote host and the local mint can never succeed, so
    // that case falls straight through to the prompt (see shouldRetryLocalTokenMint).
    // The healthy path mints on attempt 0 and returns immediately — no added latency.
    for (let attempt = 0; ; attempt++) {
      let token = await fetchLocalToken(backendUrl);
      if (!token) ({ token } = await fetchRemoteToken(new URL(backendUrl).port));
      if (win.isDestroyed()) return;

      if (token) {
        // Hold the boot reveal until it has both finished its animation and the
        // gateway is ready, then fade out and hand off to the dashboard.
        await fadeLoadingScreen(wc);
        if (win.isDestroyed()) return;
        wc.loadURL(`${backendUrl}?token=${token}`);
        if (backendUrl === BACKEND_URL) startLivenessMonitor(win);
        return;
      }

      // No token — check if the gateway allows unauthenticated access.
      const status = await new Promise((resolve) => {
        http.get(backendUrl, (res) => {
          res.resume();
          resolve(res.statusCode);
        }).on("error", () => resolve(0));
      });
      if (win.isDestroyed()) return;

      if (status !== 403) {
        // Not an auth block — the gateway serves without a token.
        wc.loadURL(backendUrl);
        if (backendUrl === BACKEND_URL) startLivenessMonitor(win);
        return;
      }

      // 403: classify WHICH machine to mint on. A gateway we did not spawn (an
      // `ssh -L` forward, or an externally-started one) has its own
      // .local_secret, so our CLI can only mint against it FROM that machine;
      // pointing the user at this one would send them where the gateway is not.
      // Reuse the boot-time port-owner probe rather than guessing.
      //
      // NOTE `URL.port` is "" for a default-port URL (http://host/ on :80).
      // Left empty it would look up the wrong remote-host entry, probe no port
      // at all, and let the page fall back to :5476 — i.e. describe and submit
      // to a gateway that isn't the one we just got a 403 from.
      const promptPort = defaultedPort(backendUrl);
      const remoteHost = getRemoteHostConfig(store, promptPort)?.host || "";
      const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(promptPort);
      const kind = classifyAuthBlock({ localOwner, remoteHost });

      // Our own gateway may still be warming up its regenerated secret — retry
      // the mint with backoff before giving up. A foreign gateway can't be
      // minted against locally, so never spin on it: fall through to the prompt.
      if (shouldRetryLocalTokenMint({ kind, attempt })) {
        glog(`token mint: transient 403 on own gateway (kind=${kind}, attempt=${attempt + 1}/${TOKEN_MINT_MAX_RETRIES + 1}) — retrying after backoff`);
        await new Promise((r) => setTimeout(r, tokenMintRetryDelayMs(attempt)));
        if (win.isDestroyed()) return;
        continue;
      }

      glog(`token prompt: kind=${kind} owner=${localOwner} port=${promptPort} host=${remoteHost || "(none)"}`);
      if (win.isDestroyed()) return;
      // token-prompt.html replaces the dashboard inside THIS window's
      // WebContentsView (win.webContents is the view's, see setupWindowContents).
      // In fullscreen/kiosk the traffic lights + app menu are hidden, so the
      // user was trapped with no way out but force-kill. Drop immersive modes
      // first, which restores Close / Cmd-Q as the exit.
      //
      // Deliberately NO in-page exit: the window is a BaseWindow, so a
      // `window.close()` in the page would destroy the VIEW and leave a blank
      // shell behind. A keyboard exit has to go through the main process
      // (windowForWebContents) to close the host window.
      exitImmersiveModes(win);
      wc.loadFile(path.join(__dirname, "token-prompt.html"), {
        query: { port: promptPort, kind, host: remoteHost },
      });
      return;
    }
  } catch (err) {
    if (win.isDestroyed()) return;
    // A spawned gateway that exited gives a tagged 'failed' error (see
    // gateway-wait.js). Surface the cause + a SCROLLABLE tail of the launch log
    // so the user sees the real reason (ModuleNotFoundError / Gatekeeper kill /
    // port-in-use) — and so a long log can't make the dialog grow unbounded
    // (it scrolls inside a fixed-size window; see showGatewayErrorDialog).
    const failedToStart = err && err.kind === "failed";
    const logPath = gatewayLogPath();
    let logTail = "";
    try { logTail = tailLines(fs.readFileSync(logPath, "utf8"), 60); } catch { /* no log yet */ }

    // A wedged/other gateway already holding this flavor's port is a distinct,
    // recoverable case: the spawn dies with "address already in use" and a plain
    // retry can't help (the holder is still there). Detect it and offer to
    // force-stop the stuck KiroCrew process. Only meaningful for OUR own port.
    // Nothing was spawned in the client-only case, so it is classified before
    // the port-conflict probe — see classifyStartFailure for why the log tail
    // cannot be trusted to mean "a holder exists right now".
    // The pre-spawn check cannot be complete: extraction order within a package
    // is not ours to control, so a spawn can still die on a stdlib import that
    // was a moment away from existing. The interpreter's own traceback settles
    // what no filesystem probe could, so a missing-stdlib crash is reclassified
    // here as an unfinished install rather than reported as a defect.
    //
    // Requires the tail to be free of a bound-port report. The launch log is
    // append-only across launches, so a stdlib traceback left by an EARLIER run
    // would otherwise relabel today's "address already in use" exit as
    // "installing" and hide the force-stop path — the port holder is still
    // there, so Retry alone would loop. When both signals appear, the port
    // conflict is the actionable one. This guard applies only to the log-sniffed
    // path; a pre-spawn refusal sets `incompleteBundle` explicitly and keeps
    // outranking a stale port line, since nothing was spawned in that case.
    const failureRecord = shouldReclassifyAsInstalling({
      failedToStart,
      failure: err.failure,
      logTail,
      // Scoped to THIS attempt, like the crash match itself: a bound-port line
      // left by an earlier launch must not suppress a genuine current stdlib
      // crash. The port-conflict branch below keeps using the whole tail, since
      // its own guard is about not offering force-stop for a port nothing holds.
      portInUseInLog: isPortInUse(currentAttemptLog(logTail)),
      bundled: !!(err.failure && err.failure.bundled),
    })
      ? { ...err.failure, incompleteBundle: true }
      : err.failure;
    const failureKind = classifyStartFailure({
      failedToStart,
      failure: failureRecord,
      isOwnPort: backendUrl === BACKEND_URL,
      portInUseInLog: isPortInUse(logTail),
    });
    const localGatewayOff = failureKind === "client-only";
    const portConflict = failureKind === "port-conflict";

    let title, message;
    if (failureKind === "installing") {
      // The bundled backend is still being written to disk, so the honest
      // framing is "not ready yet" and Retry is the whole remedy. Use the
      // unfinished-install copy rather than err.message: on the reclassified
      // path err.message is the interpreter's own exit report, which is what
      // this branch exists to stop showing as the headline.
      title = "Kiro Crew — installation still finishing";
      message = err.failure?.incompleteBundle ? err.message : describeIncompleteBundle([]);
    } else if (localGatewayOff) {
      // Nothing failed here — the app was told not to start a gateway and the
      // port is silent. "Failed to start" would send the user hunting a crash.
      title = `Kiro Crew — no gateway on port ${PORT}`;
      message = err.message;
    } else if (portConflict) {
      title = `Kiro Crew — port ${PORT} already in use`;
      message = `Another Kiro Crew gateway is already using port ${PORT} (it may be wedged). `
        + `Force-stop it and retry, or quit. From a terminal you can also run: `
        + `kirocrew stop --port ${PORT}`;
    } else if (failedToStart) {
      title = "Kiro Crew — gateway failed to start";
      message = err.message;
    } else {
      title = "Kiro Crew — can't reach the gateway";
      message = "Could not connect to the Kiro Crew backend. Make sure "
        + "'kirocrew gateway' is running, or check kirocrew doctor.";
    }

    // Loop so "Reveal Log" can re-show the dialog after opening Finder.
    for (;;) {
      const action = await showGatewayErrorDialog(win, {
        title, message, logTail, logPath, portConflict, port: PORT, localGatewayOff,
      });
      if (win.isDestroyed()) return;
      if (action === "reveal") {
        try { shell.showItemInFolder(logPath); } catch { /* best effort */ }
        continue; // re-show the dialog
      }
      if (action === "enable-retry") {
        // The user is asking for a gateway now, from the one surface they can
        // still reach. Persist it so the next launch agrees, and lift this
        // session's snapshot so the retry below actually spawns.
        setLocalGatewayEnabled(store, true);
        runLocalGateway = true;
        glog("local gateway turned back on from the error dialog");
      }
      if (action === "force-retry") {
        let freed = true;
        let probeFailed = false;
        try { ({ freed, probeFailed = false } = await forceStopGatewayPort(PORT)); }
        catch (e) { glog(`force-stop: port probe failed (${e && e.message}); letting retry's bind confirm`); }
        if (win.isDestroyed()) return;
        if (!freed) {
          // The port is still held — by an unkillable wedge or a foreign app.
          // Either way a retry would just re-hit "address already in use", so
          // tell the user a restart is required rather than looping the failure.
          return showUnrecoverableGatewayError(win, PORT, { probeFailed });
        }
      }
      if (action === "retry" || action === "force-retry" || action === "enable-retry") {
        gatewayStartFailure = null; // let the retry genuinely re-probe
        // If our own spawned gateway is confirmed gone (or we just force-stopped
        // the port holder), respawn before re-waiting. For a timeout (child may
        // still be alive) or a tab on another port, just re-poll.
        if (backendUrl === BACKEND_URL && !gatewayProcess) {
          await startGateway();
        }
        // The dialog, force-stop, and respawn above are all async — the user may
        // have closed the window meanwhile. Re-check before showLoadingThenConnect,
        // which calls win.show()/loadFile and would throw on a destroyed window.
        if (win.isDestroyed()) return;
        return showLoadingThenConnect(win, backendUrl);
      }
      // Quit
      if (win === mainWindow) {
        isQuitting = true;
        app.quit();
      } else {
        win.destroy();
      }
      return;
    }
  }
}

// ── New Connection Window ──

async function openNewConnectionWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.show();

  const css = await getModalCSS();
  const promptWin = new BrowserWindow({
    width: 400, height: 180, resizable: false, useContentSize: true,
    parent: mainWindow, modal: true, backgroundColor: "#00000000",
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
      function go() { document.title = document.getElementById('p').value.trim(); window.close(); }
      document.addEventListener('keydown', e => { if(e.key==='Enter') go(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  promptWin.setMenu(null);

  let savedTitle = null;
  promptWin.on("page-title-updated", (_e, title) => { savedTitle = title; });
  promptWin.on("closed", async () => {
    if (!savedTitle) return;
    const port = parseInt(savedTitle, 10);
    if (isNaN(port) || port < 1 || port > 65535) return;
    if (!mainWindow || mainWindow.isDestroyed()) return;

    const backendUrl = `http://localhost:${port}`;
    const connOpts = {
      width: 1280,
      height: 860,
      minWidth: 550,
      minHeight: 600,
      backgroundColor: "#0f1117",
    };
    // Same platform-conditional chrome as the main window (see createWindow):
    // frameless + inset traffic lights on macOS, titleBarOverlay on Windows,
    // frame:false on CSD-preferring Linux desktops, native frame elsewhere.
    if (IS_MAC) connOpts.titleBarStyle = "hidden";
    if (IS_MAC) connOpts.trafficLightPosition = trafficLightPositionForZoom(1);
    if (IS_WINDOWS) {
      connOpts.titleBarStyle = "hidden";
      connOpts.autoHideMenuBar = true;
      connOpts.titleBarOverlay = {
        color: WINDOWS_TITLEBAR_BACKGROUND,
        symbolColor: nativeTheme.shouldUseDarkColors
          ? WINDOWS_TITLEBAR_SYMBOL_DARK
          : WINDOWS_TITLEBAR_SYMBOL_LIGHT,
        height: HEADER_CSS_PX,
      };
    }
    if (LINUX_FRAMELESS) {
      connOpts.frame = false;
      connOpts.autoHideMenuBar = true; // same rationale as createWindow
    }
    const connWin = new BaseWindow(connOpts);
    if (IS_WINDOWS && typeof connWin.setMenuBarVisibility === "function") {
      connWin.setMenuBarVisibility(false);
    }

    setupWindowContents(connWin, backendUrl);

    const onNavigate = createTokenRetryHandler(async () => {
      let token = await fetchLocalToken(backendUrl);
      if (!token) ({ token } = await fetchRemoteToken(port));
      if (token && !connWin.isDestroyed()) {
        connWin.webContents.loadURL(`${backendUrl}?token=${token}`);
      }
    });
    connWin.webContents.on("did-navigate", (_e, _url, httpCode) => {
      onNavigate(httpCode).catch((err) => console.error("Token retry failed:", err));
    });

    // Every connection is a standalone window (tracked for menu actions).
    await showLoadingThenConnect(connWin, backendUrl);
  });
}

// ── Rename Window ──

function renameCurrentWindow() {
  const focused = BaseWindow.getFocusedWindow();
  if (!focused || !focused._mcSetCustomName) return;

  const currentTitle = focused.getTitle();
  const port = focused._mcBackendUrl ? new URL(focused._mcBackendUrl).port : "";
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  getDashboardThemeVars().then((vars) => {
  const css = vars && vars.bg ? modalCSSFromVars(vars) : modalCSSForMode(nativeTheme.shouldUseDarkColors);
  const promptWin = new BrowserWindow({
    width: 400, height: 200, resizable: false, useContentSize: true,
    parent: focused, modal: true, backgroundColor: "#00000000",
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const html = `<!DOCTYPE html><html><head><style>
    ${css}
    .check-row { display:flex; align-items:center; gap:6px; margin-top:8px; }
    .check-row input { width:auto; margin:0; }
    .check-row label { margin:0; font-size:12px; }
  </style></head><body>
    <label>Window name</label>
    <input id="n" value="${esc(currentTitle.replace(/^Kiro ?Crew /g, ''))}" autofocus>
    <div class="row"><button class="ok" onclick="go()">Rename</button>
    <button class="cancel" onclick="window.close()">Cancel</button></div>
    <div class="check-row"><input type="checkbox" id="d"><label for="d">Set as default name for :${port} windows</label></div>
    <script>
      function go() { document.title = JSON.stringify({name: document.getElementById('n').value.trim(), setDefault: document.getElementById('d').checked}); window.close(); }
      document.addEventListener('keydown', e => { if(e.key==='Enter') go(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  promptWin.setMenu(null);

  let savedTitle = null;
  promptWin.on("page-title-updated", (_e, title) => { savedTitle = title; });
  promptWin.on("closed", () => {
    if (!savedTitle || !focused || focused.isDestroyed()) return;
    try {
      const { name, setDefault } = JSON.parse(savedTitle);
      if (name) {
        focused._mcSetCustomName(name);
        if (setDefault && port) {
          const hosts = store.get("remoteHosts") || {};
          const key = String(port);
          hosts[key] = { ...(hosts[key] || {}), defaultName: name };
          store.set("remoteHosts", hosts);
        }
      }
    } catch {
      // Legacy plain-text fallback (shouldn't happen)
      if (savedTitle) focused._mcSetCustomName(savedTitle);
    }
  });
  }); // end getDashboardThemeVars().then()
}

// ── App lifecycle ──

// Guide the user to grant macOS Screen Recording permission when it has been
// explicitly denied — the snip tool cannot capture any frame without it. Opens
// the exact Privacy pane. Note: the granted entity must be the packaged
// KiroCrew.app, not the terminal that launched a dev build.
function showScreenPermissionDialog() {
  const pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
  dialog
    .showMessageBox({
      type: "info",
      title: "Screen Recording permission needed",
      message: "Allow Kiro Crew to capture the screen",
      detail:
        "The screen-snip tool needs macOS Screen Recording permission. Open System Settings › Privacy & Security › Screen Recording, enable Kiro Crew, then try the snip again.",
      buttons: ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: 1,
    })
    .then(({ response }) => {
      if (response === 0) shell.openExternal(pane);
    })
    .catch(() => {});
}

// The microphone twin of showScreenPermissionDialog, shown when macOS reports
// the mic as denied/restricted. This dialog IS the recovery route: TCC's own
// prompt is one-shot, so once denied the OS never asks again and the mic button
// would otherwise fail forever with no way for the user to fix it.
//
// Latched, unlike the screen-capture dialog: that one has a single entry point,
// whereas the mic is reachable from several independent capture call sites
// (dictation, streaming STT, the settings mic test, meeting transcription), so
// an unlatched dialog can stack copies of itself.
let micDialogOpen = false;
function showMicPermissionDialog(status = "denied") {
  if (micDialogOpen) return;
  micDialogOpen = true;
  const pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone";
  // 'restricted' is policy-managed (MDM/parental controls): the toggle the
  // 'denied' copy tells the user to flip either is not there or will not stick,
  // so do not send them on an errand they cannot complete.
  const restricted = status === "restricted";
  dialog
    .showMessageBox({
      type: "info",
      title: "Microphone permission needed",
      message: restricted
        ? "Microphone access is blocked by a policy"
        : "Allow Kiro Crew to use the microphone",
      detail: restricted
        ? "Voice input needs macOS Microphone permission, but access is restricted by a device-management policy on this Mac. Contact whoever manages it to allow microphone access for Kiro Crew."
        : "Voice input needs macOS Microphone permission, and macOS will not ask again once it has been denied. Open System Settings › Privacy & Security › Microphone, enable Kiro Crew, then try the mic again.",
      buttons: restricted ? ["OK"] : ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: restricted ? 0 : 1,
    })
    .then(({ response }) => {
      if (!restricted && response === 0) shell.openExternal(pane);
    })
    .catch(() => {})
    .then(() => {
      micDialogOpen = false;
    });
}

// Last-resort safety net. An unhandled exception/rejection anywhere on the main
// process would otherwise tear the app down with no trace — the exact "it just
// crashed" the remote-tunnel drop used to produce. Log it (best-effort; logging
// must never itself throw here) and stay alive so the recovery paths above can
// run. glog appends to the retrievable gateway-launch.log the user can inspect.
process.on("uncaughtException", (err) => {
  try { glog(`uncaughtException: ${err && err.stack ? err.stack : err}`); } catch { /* logging must never throw here */ }
});
process.on("unhandledRejection", (reason) => {
  try { glog(`unhandledRejection: ${reason && reason.stack ? reason.stack : reason}`); } catch { /* ignore */ }
});

app.whenReady().then(async () => {
  // The frame decision's `reason` exists for support bundles: "why does my
  // window (not) have a native frame" is answerable from the gateway log
  // without asking the user for their desktop environment.
  if (LINUX_FRAME_DECISION) {
    glog(`linux frame decision: frameless=${LINUX_FRAME_DECISION.frameless} reason=${LINUX_FRAME_DECISION.reason}`);
  }
  // Debug-only per-process metrics recorder. No-ops unless KIROCREW_DEBUG is set,
  // so a normal install pays nothing; when on, it writes a bounded rolling
  // artifact next to the gateway log for `kirocrew desktop metrics` to read.
  try {
    desktopMetricsRecorder = createMetricsRecorder({
      dir: path.dirname(gatewayLogPath()),
      getAppMetrics: () => app.getAppMetrics(),
      log: (m) => glog(`perf: ${m}`),
      meta: { electron: process.versions && process.versions.electron },
    });
    desktopMetricsRecorder.start();
  } catch (e) {
    // A diagnostic aid must never take the app down at boot.
    try { glog(`perf: metrics recorder failed to start: ${e && e.message}`); } catch { /* ignore */ }
  }
  // Running from a mounted DMG or a Gatekeeper App Translocation copy looks
  // fine at launch but can NEVER install an update (the macOS install path
  // replaces the running .app in place). Say so once, up front, and offer the
  // one-click move — otherwise the user silently never receives another release.
  await offerRelocationIfUnupdatable();
  // Zoom items are explicit (not `role:`-based) so each zoom change can also
  // recenter the macOS traffic lights in the zoom-scaled header row.
  // Resolve the dashboard WebContents of the focused window. The de-tabbed
  // shell hosts pages in WebContentsViews inside BaseWindows: BaseWindow has
  // no `webContents`, so menu `role:` items (reload/forceReload) and
  // BrowserWindow.getFocusedWindow() lookups silently no-op on main windows.
  // Window-first resolution (focused window -> its content view) is also
  // deterministic when DevTools has focus, where getFocusedWebContents()
  // would return the DevTools page itself.
  const focusedDashboardWC = () => {
    const win = BaseWindow.getFocusedWindow();
    if (win) {
      const views = win.contentView && win.contentView.children;
      if (views && views.length > 0) {
        // First view with a real page loaded is the dashboard (works for
        // localhost AND remote-host connection windows).
        const mainView = views.find((v) => {
          try { return !!(v.webContents && v.webContents.getURL()); }
          catch { return false; }
        });
        if (mainView) return mainView.webContents;
      }
      if (win.webContents) return win.webContents; // plain BrowserWindow (prompts)
    }
    return webContents.getFocusedWebContents();
  };
  const zoomItem = (apply) => () => {
    const wc = webContents.getFocusedWebContents();
    if (!wc) return;
    apply(wc);
    // Chromium applies per-origin zoom to every same-origin window at once,
    // so recenter traffic lights on all shell windows, not just the focused one.
    for (const win of BaseWindow.getAllWindows()) {
      if (win._mcView) positionTrafficLights(win);
    }
  };
  // Menu → dashboard SPA navigation (Settings…, About). Targets the focused
  // dashboard window, falling back to the main window so the items still work
  // from the dock/tray-only state; surfaces the window before navigating.
  // `_mcView` marks every window that hosts a dashboard (setupWindowContents),
  // which skips modal prompt BrowserWindows that have no SPA to navigate.
  // Resolve the dashboard WINDOW (not WebContents): the focused one, falling
  // back to the main window so menu items still work from the dock/tray-only
  // state. `_mcView` marks every window that hosts a dashboard
  // (setupWindowContents), which skips modal prompt BrowserWindows.
  const focusedDashboardWindow = () =>
    [BaseWindow.getFocusedWindow(), mainWindow].find(
      (w) => w && !w.isDestroyed() && w._mcView
    );
  const openSettingsPage = (tab) => {
    const win = focusedDashboardWindow();
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    const wc = win._mcView.webContents;
    if (wc && !wc.isDestroyed()) {
      wc.send("navigate", tab ? `/settings?tab=${tab}` : "/settings");
    }
  };
  // View > Keep on Top: flip always-on-top on the focused dashboard window,
  // then reconcile the checkbox with the window's ACTUAL state (read back) so
  // the checkmark cannot drift if the platform refuses or another code path
  // changes it. Persisted so the pinned window survives a relaunch.
  const toggleAlwaysOnTop = () => {
    const win = focusedDashboardWindow();
    if (!win) return;
    try {
      win.setAlwaysOnTop(!win.isAlwaysOnTop());
      const menu = Menu.getApplicationMenu();
      const item = menu && menu.getMenuItemById("keep-on-top");
      if (item) item.checked = win.isAlwaysOnTop();
    } catch { /* window mid-teardown */ }
    persistMainWindowState();
  };
  const appMenu = Menu.buildFromTemplate(
    buildMenuTemplate({
      isMac: process.platform === "darwin",
      appName: app.name,
      openSettings: () => openSettingsPage(),
      openAbout: () => openSettingsPage("about"),
      reload: () => { const wc = focusedDashboardWC(); if (wc) wc.reload(); },
      forceReload: () => { const wc = focusedDashboardWC(); if (wc) wc.reloadIgnoringCache(); },
      toggleDevTools: () => { const wc = focusedDashboardWC(); if (wc) wc.toggleDevTools(); },
      zoomActualSize: zoomItem((wc) => wc.setZoomFactor(1)),
      zoomIn: zoomItem((wc) => wc.setZoomFactor(stepZoomFactor(wc.getZoomFactor(), +1))),
      zoomOut: zoomItem((wc) => wc.setZoomFactor(stepZoomFactor(wc.getZoomFactor(), -1))),
      // Menu is built before createWindow(), so seed the checkbox from the
      // same persisted state createWindow() restores from.
      alwaysOnTop: !!(store.get("windowState") || {}).alwaysOnTop,
      toggleAlwaysOnTop,
      openNewConnectionWindow: () => openNewConnectionWindow(),
      renameCurrentWindow: () => renameCurrentWindow(),
      promptRemoteHost: () => promptRemoteHost(),
      refreshToken: () => refreshToken(),
      openConfigFile: () => shell.openPath(store.path),
    })
  );
  Menu.setApplicationMenu(appMenu);

  // Windows renders the menu surface in the custom titlebar so pointer hover
  // can switch between top-level menus. Native Menu.popup() captures input on
  // Windows and prevents that Zed-style interaction. Commands still execute
  // through Electron's MenuItems, preserving roles and accelerator behavior.
  ipcMain.handle("app-menu:items", (event, id) => {
    if (!IS_WINDOWS || !WINDOWS_TITLEBAR_MENU_IDS.has(id)) return [];
    const item = appMenu.getMenuItemById(id);
    const win = windowForWebContents(event.sender);
    if (!item || !item.submenu || !win || win.isDestroyed()) return [];
    return serializeMenuItems(item.submenu);
  });

  ipcMain.on("app-menu:execute", (event, id, index) => {
    if (!IS_WINDOWS || !WINDOWS_TITLEBAR_MENU_IDS.has(id) || !Number.isInteger(index)) return;
    const topLevelItem = appMenu.getMenuItemById(id);
    const win = windowForWebContents(event.sender);
    if (!win || win.isDestroyed()) return;
    // `event.sender`, not `event`: role items dispatch off the third click
    // argument as a WebContents (`webContentsMethod(focusedWebContents)`), and
    // the titlebar menu can only be clicked while its own renderer has focus,
    // so the sender IS the focused WebContents the native menu would resolve.
    executeMenuItem(topLevelItem, index, win, event.sender);
  });

  // DevTools gate: renderer sends dev-mode state, we toggle menu visibility.
  ipcMain.on("dev-mode-changed", (_event, enabled) => {
    const menu = Menu.getApplicationMenu();
    const item = menu && menu.getMenuItemById("devtools-toggle");
    if (item) item.visible = !!enabled;
  });

  // System-wide summon hotkey: shows + focuses the dashboard from anywhere,
  // launching a window when none exists. The handler and IPC surface are set
  // up here; the actual registration happens after the boot path's
  // createWindow() below, so a keypress cannot race window creation and
  // produce two windows. Torn down on will-quit; the binding persists in the
  // store (`globalHotkey`) so the user can rebind or disable it via the
  // config file (Connection > Open Config File). A stored value that cannot be
  // bound falls back to the default; a default that another app already owns
  // degrades to no hotkey — logged, never fatal (see global-hotkey.js).
  setGlobalHotkeyLogger(glog);
  const summonDashboard = createSummonHandler({
    // The focused dashboard window when there is one, else the main window,
    // else ANY surviving dashboard window (`_mcView` marks the windows that
    // host a dashboard — see focusedDashboardWindow above). The main window is
    // hidden, not destroyed, on close, so createWindow() is a last resort.
    getWindow: () =>
      [BaseWindow.getFocusedWindow(), mainWindow, ...BaseWindow.getAllWindows()].find(
        (w) => w && !w.isDestroyed() && w._mcView
      ) || null,
    createWindow: () => createWindow(),
    // A global shortcut fires while ANOTHER app is frontmost; on macOS the
    // window rises without keyboard focus unless the app steals activation.
    focusApp: () => {
      if (IS_MAC) app.focus({ steal: true });
    },
  });
  // The shortcuts UI reads what is ACTUALLY bound (registration can degrade
  // to the default or to nothing), so it never advertises a dead chord.
  ipcMain.handle("global-hotkey:get", () => ({
    accelerator: currentGlobalHotkey(),
    default: DEFAULT_GLOBAL_HOTKEY,
  }));

  // The renderer reports the user's resolved theme accent whenever it changes
  // (see useTheme.tsx). Persist a validated hex so the NEXT launch's boot splash
  // can paint in the user's colour. Anything not a plain hex is ignored.
  ipcMain.on("theme-accent-changed", (_event, hex) => {
    if (typeof hex === "string" && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex)) {
      store.set("themeAccent", hex);
    }
  });

  // Caption controls for the frameless Linux window (see the injected
  // #electron-linux-controls cluster in setupWindowContents). Resolved from
  // the SENDER's own window -- never a broadcast -- so a connection window's
  // close button cannot touch the main window. Actions outside the
  // applyWindowControl allowlist are no-ops. Gated on LINUX_FRAMELESS: on
  // framed windows the native controls own these verbs, and no button that
  // sends this message is ever injected there.
  ipcMain.on("window-control", (event, action) => {
    if (!LINUX_FRAMELESS) return;
    const win = windowForWebContents(event.sender);
    if (win) applyWindowControl(win, action);
  });

  // The renderer reports its dark/light mode PREFERENCE whenever it changes (see
  // useTheme.tsx). Pushed rather than only pulled on window focus so that
  // switching back to Auto un-pins `prefers-color-scheme` right away — while it
  // stays pinned, Auto cannot see the OS appearance at all. Unrecognised values
  // are ignored; the focus/load pull remains the fallback.
  ipcMain.on("theme-mode-changed", (_event, pref) => {
    if (pref === "system" || pref === "dark" || pref === "light") {
      nativeTheme.themeSource = resolveThemeSource(pref, "");
    }
  });

  // Windows titleBarOverlay symbol sync: when the resolved dark/light mode
  // changes, repaint every window's caption glyphs to match. The renderer sends
  // the resolved mode ("dark" | "light") after any theme change. Shares
  // ./windows-titlebar with the load/focus path (syncNativeTheme) so every
  // window converges on the same transparent, zoom-aware overlay instead of two
  // handlers racing with different colors — and the loop CONTINUES past the
  // framed modal windows Electron throws on, so a theme switch with a dialog
  // open cannot leave the windows behind it painted for the old theme.
  ipcMain.on("titlebar-overlay-theme", (_event, mode) => {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode === "dark" || mode === "light"
      ? mode
      : (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    paintAllTitleBarOverlays(BaseWindow.getAllWindows(), resolvedMode, HEADER_CSS_PX);
  });

  // Dock/taskbar badge (RFC notification bus Phase 4): renderer pushes its
  // unread notification count. Clamped to a sane non-negative integer;
  // Electron no-ops setBadgeCount on unsupported platforms (Windows).
  const { clampBadgeCount } = require("./badge");
  ipcMain.on("badge:set", (_event, count) => {
    app.setBadgeCount(clampBadgeCount(count));
  });

  // Local-gateway switch for Settings > Developer. The choice lives in the
  // app's own electron-store config, which page JS cannot reach, so the
  // renderer round-trips through these handlers. Both resolve with the stored
  // value so the toggle renders what was actually written. Reading it at launch
  // (startGateway) rather than here is what gives the setting its next-launch
  // semantics: flipping it never touches the gateway currently running.
  ipcMain.handle("local-gateway:get", () => isLocalGatewayEnabled(store));
  ipcMain.handle("local-gateway:set", (_event, enabled) => setLocalGatewayEnabled(store, enabled));

  // Native zoom bridge for the Settings > Display "Zoom Level" stepper.
  // A renderer cannot touch Chromium's per-origin zoom itself, so it
  // round-trips through these handlers. The same stepZoomFactor ladder backs
  // the View menu (Cmd/Ctrl +/-/0), so the stepper and the shortcuts always
  // agree on the value ladder. Chromium persists the factor per-origin in the
  // persistent session — no store writes needed. Handlers target event.sender
  // (the dashboard that asked), and return the applied factor so the stepper
  // can render it without a second round-trip.
  const applyZoom = (wc, factor) => {
    wc.setZoomFactor(factor);
    for (const win of BaseWindow.getAllWindows()) {
      if (win._mcView) positionTrafficLights(win);
    }
    return factor;
  };
  ipcMain.handle("zoom:get", (event) => event.sender.getZoomFactor());
  ipcMain.handle("zoom:set", (event, factor) => applyZoom(event.sender, clampZoomFactor(factor)));
  ipcMain.handle("zoom:step", (event, dir) =>
    applyZoom(event.sender, stepZoomFactor(event.sender.getZoomFactor(), dir > 0 ? +1 : -1)));

  // ── Native browser panel IPC ──
  // The dashboard renderer drives the embedded browser view: it opens/navigates,
  // reports the panel rectangle it measured, and reports when one of its own
  // overlays (modal, dropdown, drag preview) covers that rectangle — the native
  // view composites ABOVE the SPA, so it has to be hidden for the overlay's
  // duration.
  //
  // Every call carries a `panelId` (the renderer's session key), because the
  // dashboard renders one Browser panel per chat session and each gets its own
  // view, control plane and agent-act authorization.
  const panelFor = (event, panelId, opts) => {
    const owner = windowForWebContents(event.sender);
    if (!owner || !owner._mcBrowserPanel) return null;
    return owner._mcBrowserPanel(panelId, opts);
  };
  ipcMain.handle("browser:open", (event, panelId, url) => {
    const p = panelFor(event, panelId);
    return p ? p.manager.open(url) : null;
  });
  ipcMain.handle("browser:navigate", (event, panelId, url) => {
    const p = panelFor(event, panelId);
    return p ? p.manager.navigate(url) : null;
  });
  ipcMain.handle("browser:set-bounds", (event, panelId, rect, viewport) => {
    // Do not CREATE a panel just because layout reported a rect — only an
    // explicit open() should bring a view into existence.
    const p = panelFor(event, panelId, { create: false });
    return p ? p.manager.setPanelBounds(rect, viewport) : null;
  });
  ipcMain.handle("browser:set-overlay", (event, panelId, active) => {
    const p = panelFor(event, panelId, { create: false });
    return p ? p.manager.setOverlayActive(active) : null;
  });
  // Going INACTIVE (this panel's side-panel tab is not the visible one) hides
  // the view but keeps the page alive. Deliberately NOT `close`: switching tabs
  // away and back must not lose unsaved form input, scroll position or history.
  ipcMain.handle("browser:set-inactive", (event, panelId, value) => {
    const p = panelFor(event, panelId, { create: false });
    return p ? p.manager.setInactive(value) : null;
  });
  ipcMain.handle("browser:close", (event, panelId) => {
    const owner = windowForWebContents(event.sender);
    const p = panelFor(event, panelId, { create: false });
    if (!p) return null;
    const state = p.manager.getState();
    // Releasing control before the view goes away keeps the invariant honest:
    // an owner must never outlive the page it was driving.
    if (owner && owner._mcDestroyBrowserPanel) owner._mcDestroyBrowserPanel(p.id);
    return { ...state, open: false, visible: false };
  });
  ipcMain.handle("browser:get-state", (event, panelId) => {
    const p = panelFor(event, panelId, { create: false });
    return p ? p.manager.getState() : null;
  });

  // ── Agent control IPC ──
  // The dashboard renderer is the authority on whether the agent is authorized
  // to act (it owns that toggle), so it pushes the flag; the main process keeps
  // it per panel and evaluates the gate itself on every transition.
  ipcMain.handle("browser:track-session", (event, panelId, tracked) => {
    // Reachability, and now the ONLY session-declaration channel. The renderer
    // declares its active chat slot here so the agent command channel polls for
    // it (see listPanelIds) and the agent's first navigate can arrive and be
    // JUDGED by the gate — which is now just the view precondition, since Browser
    // Mode is the authorization. Grants nothing on its own.
    const owner = windowForWebContents(event.sender);
    const set = owner && owner._mcReachableSessions;
    const id = typeof panelId === "string" ? panelId.trim() : "";
    if (!set || !id) return { ok: false };
    if (tracked) set.add(id);
    else set.delete(id);
    // The tracked-slot set just changed. Nudge the agent command channel to
    // re-read it NOW so a freshly declared key is polled (and thus registered on
    // the gateway bus) within submit's brief wait window, instead of only after
    // the current long-poll ends (up to ~25s) -- the cold-start race that
    // dropped a fresh session's first navigate to the Playwright mirror.
    if (owner._mcAgentChannel) owner._mcAgentChannel.poke();
    return { ok: true };
  });
  ipcMain.handle("browser:set-agent-act", async (event, panelId, enabled) => {
    const p = panelFor(event, panelId);
    if (!p) return { ok: false };
    p.agentAct = !!enabled;
    // Revocation must STOP an agent that is already driving, not merely refuse
    // its next transition. Flipping the flag alone left the plane still holding
    // LIGHT with the debugger attached, so ops kept succeeding against a view
    // that carries the user's logged-in session. Releasing here makes the
    // withdrawal take effect immediately.
    if (!p.agentAct) await p.control.release();
    return { ok: true };
  });
  ipcMain.handle("browser:set-control-owner", async (event, panelId, requested) => {
    const p = panelFor(event, panelId, { create: false });
    if (!p) return null;
    return p.control.setOwner(requested, p.gate());
  });
  ipcMain.handle("browser:get-control", (event, panelId) => {
    const p = panelFor(event, panelId, { create: false });
    if (!p) return null;
    return { owner: p.control.getOwner(), attached: p.control.isAttached(), gate: p.gate() };
  });
  // Op dispatch. Deliberately a CLOSED verb set — never a raw CDP method from
  // the caller — so the control surface cannot be widened by whoever calls it.
  ipcMain.handle("browser:control", async (event, panelId, op, args) => {
    const p = panelFor(event, panelId, { create: false });
    if (!p) return null;
    return dispatchBrowserOp(p, op, args);
  });

  // Enable the chat input's screen-snip tool inside the Electron shell.
  // Without a display-media request handler, Electron (>= 20) rejects the
  // renderer's navigator.mediaDevices.getDisplayMedia(), so the snip button
  // silently no-ops in the packaged app (it works in a plain browser because
  // Chromium shows the OS picker natively). useSystemPicker uses macOS's native
  // screen picker when available; the desktopCapturer-backed handler is the
  // fallback for older macOS / other platforms.
  session.defaultSession.setDisplayMediaRequestHandler(
    createDisplayMediaHandler({
      getSources: () => desktopCapturer.getSources({ types: ["screen", "window"] }),
      getScreenAccessStatus: () =>
        process.platform === "darwin"
          ? systemPreferences.getMediaAccessStatus("screen")
          : "granted",
      onPermissionNeeded: (reason) => {
        if (reason === "denied") showScreenPermissionDialog();
      },
    }),
    { useSystemPicker: true },
  );

  // Grant microphone access for the chat input's voice / speech-to-text
  // feature. Without an explicit permission handler, Electron's default
  // permission *check* can report `media` as denied for the renderer's
  // navigator.mediaDevices.getUserMedia(), so the mic button silently no-ops
  // in the packaged app even though it works in a plain browser (Chromium
  // prompts there). The handlers grant `media` for the app origin and deny
  // every other permission type (geolocation, clipboard, notifications,
  // MIDI, …) per least privilege. Screen capture uses its own
  // setDisplayMediaRequestHandler and is unaffected. See permission-handler.js
  // for why the audio grant must NOT require a populated details.mediaTypes.
  //
  // The macOS (TCC) leg is wired INTO the request handler rather than fired at
  // launch. Asking at launch spent the OS's one-shot prompt before the user had
  // done anything mic-related — easy to dismiss, and once dismissed macOS never
  // asks again, leaving voice input permanently dead. Asking from the handler
  // puts the prompt exactly where the user clicked the mic, and an
  // already-denied state now opens the Privacy pane instead of failing mutely.
  // NOTE: none of this works without com.apple.security.device.audio-input in
  // the signing entitlements — the hardened runtime refuses the mic before TCC
  // is ever consulted, which is what produced a denial with no prompt at all.
  const isMac = process.platform === "darwin";
  // The embedded browser views share this session, so both handlers are told how
  // to recognise them: an untrusted view is refused every permission BY IDENTITY,
  // before the localhost-origin heuristic can grant it the mic. Without this, a
  // page served from any http://localhost:<port> would inherit the dashboard's
  // own microphone grant.
  session.defaultSession.setPermissionRequestHandler(
    createPermissionRequestHandler({
      isUntrusted: isUntrustedContents,
      ...(isMac
        ? {
            getMicAccessStatus: () => systemPreferences.getMediaAccessStatus("microphone"),
            askForMicAccess: () => systemPreferences.askForMediaAccess("microphone"),
            onMicBlocked: () => showMicPermissionDialog(),
          }
        : {}),
    }),
  );
  session.defaultSession.setPermissionCheckHandler(
    createPermissionCheckHandler({ isUntrusted: isUntrustedContents }));

  // The embedded browser views live on their OWN partition (see
  // BROWSER_PARTITION), so the handlers just installed on the default session do
  // NOT cover them. Harden that partition explicitly — otherwise those views
  // would fall through to Electron's defaults. `isUntrustedContents` stays wired
  // above as defence in depth, for any view that ever lands on the default
  // session.
  hardenBrowserPartition(session);

  // Renderer-side recovery route. The handler above only sees requests that
  // reach Electron; a mic can still fail further down (a stale TCC row pinned to
  // an old code-signing identity, a revoked grant mid-session), and all the
  // renderer gets is getUserMedia's opaque NotAllowedError. It reports that here
  // so the OS status can be re-checked and the Privacy pane offered — otherwise
  // the "permission denied" toast is a dead end, since macOS never re-prompts.
  // Only speaks up when macOS is genuinely the one refusing.
  ipcMain.on("mic:denied", () => {
    if (!isMac) return;
    try {
      const status = systemPreferences.getMediaAccessStatus("microphone");
      if (status === "denied" || status === "restricted") showMicPermissionDialog(status);
    } catch {
      /* status probe unavailable — stay silent rather than guess */
    }
  });

  createTray();
  const win = createWindow();
  // Bind the summon hotkey only now that the main window exists: registering
  // earlier would let a keypress during boot race createWindow() and open a
  // second window. Still within app ready — the OS-level chord works from the
  // first frame the user can see.
  bindGlobalHotkey(store.get("globalHotkey"), summonDashboard);

  // Wired BEFORE the awaited gateway boot ON PURPOSE. preload.js exposes
  // window.updateAPI unconditionally, so Settings > About renders a live Check
  // button the moment the renderer loads. If these ipcMain.handle registrations
  // sit after `await startGateway()` / `await showLoadingThenConnect()`, then any
  // slow or failed gateway boot leaves the buttons present with no handler, and
  // the renderer's invoke rejects with a raw
  // "No handler registered for 'update:check'". That is not hypothetical: it is
  // what made the nightly OTA lane (ota-test.yml) fail before it could ever
  // assert a bundle swap, so the one gate that would have caught the install
  // handoff bug never ran. Nothing in this block needs the gateway -- stopGateway
  // is passed as a lazy callback and the window already exists.
  // Desktop auto-update (electron-updater; Squirrel.Mac underneath on macOS,
  // AppImage on Linux). No-op in dev / on platforms without a publish lane.
  // The gateway is stopped gracefully before any bundle swap. Update state is
  // mirrored to the renderer so the in-app UpdateModal + Settings > About can
  // drive the prompt; the native dialog stays as the fallback only when no UI
  // is wired.
  function broadcastUpdateState(payload) {
    try {
      for (const wc of webContents.getAllWebContents()) {
        if (!wc.isDestroyed()) {
          try { wc.send("update-state", payload); } catch { /* view gone */ }
        }
      }
    } catch { /* webContents unavailable */ }
  }
  // FAIL-OPEN: the updater is auxiliary and must never gate the gateway. This
  // block deliberately runs BEFORE the awaited gateway boot (see the note
  // above), which means a throw here would otherwise abort the ready-handler
  // and leave the app fully unusable -- before the reorder the same throw only
  // broke the updater. Catch, stub, continue: the About panel renders its
  // generic "updates unavailable" copy for the unknown disabled reason, and
  // every update:* handler still registers against the stub.
  let updater;
  try {
    updater = initAutoUpdate({
    app,
    // electron-updater's AppUpdater, NOT electron's built-in autoUpdater: it
    // generates/validates the feed metadata, verifies sha512 fail-closed, and
    // covers Linux. On macOS it still drives Squirrel.Mac underneath.
    autoUpdater: require("electron-updater").autoUpdater,
    dialog,
    Notification,
    getFlavor: () => "stable",
    getChannelPreference: () => store.get("updateChannel", ""),
    // Once-per-version nudge: tell the user an update exists; downloading and
    // installing stay in Settings > About (the in-app dot guides them there).
    notifyUpdateFound: (version) => {
      if (!version || store.get("lastNudgedVersion", "") === version) return;
      store.set("lastNudgedVersion", version);
      try {
        const n = new Notification({
          title: `${app.name} update available`,
          body: `Version ${version} is ready. Open Settings > About to download and install.`,
        });
        n.on("click", () => {
          if (mainWindow && !mainWindow.isDestroyed()) {
            if (mainWindow.isMinimized()) mainWindow.restore();
            mainWindow.show();
            mainWindow.focus();
          }
        });
        n.show();
      } catch { /* notifications optional */ }
    },
    stopGateway: () => stopGatewayGracefully(),
    // Called BEFORE the updater stops the gateway. Both actions matter: the
    // flag closes the race where the monitor fires between dispatch and stop,
    // and stopping the monitor means nothing is even probing during the swap.
    onInstallDispatched: () => {
      installingUpdate = true;
      if (livenessMonitor) { livenessMonitor.stop(); livenessMonitor = null; }
      glog("update install dispatched — liveness recovery disarmed");
    },
    // The install FAILED after the gateway was stopped on purpose. Undo both
    // halves of the dispatch: clear the flag so recovery is legal again, and
    // actively bring the gateway back -- nothing else will (the monitor was
    // stopped, and a failed install does not quit the app). recoverWedgedGateway
    // handles the whole path: port sweep, respawn, window reconnect, and it
    // restarts the liveness monitor once the backend answers.
    onInstallFailed: () => {
      if (!installingUpdate) return; // deferred-quit path: app is quitting anyway
      installingUpdate = false;
      glog("update install failed — restoring gateway and liveness recovery");
      recoverWedgedGateway(mainWindow).catch((e) => glog(`post-install-failure recovery failed: ${e && e.message}`));
    },
    onUpdateState: broadcastUpdateState,
    log: makeUpdaterLogger(glog),
    });
  } catch (e) {
    glog(`auto-update init failed — continuing WITHOUT auto-update: ${(e && e.stack) || e}`);
    updater = {
      check: () => {},
      download: async () => {},
      install: async () => {},
      getInfo: () => ({ version: app.getVersion(), packaged: !!app.isPackaged }),
      disabled: "init-failed",
    };
  }
  // Renderer-callable bridges for Settings > About + the UpdateModal.
  // `disabled` lives on the updater handle, NOT inside getInfo() — every
  // disabled path returns it as a sibling key. Merge it in here, once, so
  // Settings > About can render "why" for all four reasons (dev, platform,
  // translocated, volume). Without this the panel's updatesDisabled branch is
  // unreachable and a build with no update lane shows a live Check button that
  // silently does nothing. undefined on the armed path, which reads as falsy.
  const updaterInfo = () => ({ ...updater.getInfo(), disabled: updater.disabled });
  ipcMain.handle("update:get-info", () => updaterInfo());
  ipcMain.handle("update:check", () => { updater.check(); return { ok: true }; });
  ipcMain.handle("update:download", () => { updater.download(); return { ok: true }; });
  ipcMain.handle("update:install", async () => { await updater.install(); return { ok: true }; });
  // Channel switcher (stable ⇄ insider opt-in). Set persists the preference
  // and immediately re-checks so the other channel's build surfaces as the
  // normal consent card -- switching never downloads or installs by itself.
  // Validation is strict: nightly is NOT offered (the nightly app is a
  // separate pinned install), and unknown strings are rejected.
  ipcMain.handle("update:set-channel", (_e, channel) => {
    const c = typeof channel === "string" ? channel : "";
    if (c !== "" && c !== "insider" && c !== "stable") {
      return { ok: false, error: `invalid channel: ${c}` };
    }
    store.set("updateChannel", c);
    updater.check();
    return { ok: true, info: updaterInfo() };
  });

  await startGateway();
  await showLoadingThenConnect(win);

  // Mochi's pet overlay. Opened only when the builtin is enabled -- the app is
  // defaultEnabled:false, so a user who never turned it on gets no overlay and
  // pays nothing. Deliberately AFTER showLoadingThenConnect: the gateway must
  // be answering before we ask it whether Mochi is on, and the pet page is
  // loaded from the gateway origin. Best-effort -- a failure here must never
  // block the dashboard, so everything is inside a catch that only logs.
  initMochi({
    backendUrl: BACKEND_URL,
    fetchGatewayAuth: fetchMochiGatewayAuth,
    glog,
    getMainWindow: () => mainWindow,
  });
  // Same shape and the same best-effort contract: the companion's windows follow
  // the app's enabled state, and a failure here must never block the dashboard.
  try {
    initCrewCompanion({ backendUrl: BACKEND_URL, fetchLocalToken, glog });
  } catch (err) {
    glog(`crew-companion: init failed — ${err && err.message}`);
  }

  app.on("activate", () => {
    if (!mainWindow?.isVisible()) mainWindow?.show();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
  // Flush the final metrics window before the gateway teardown begins.
  try { if (desktopMetricsRecorder) desktopMetricsRecorder.stop(); } catch { /* best effort */ }
  shutdownMochi();
  try { shutdownCrewCompanion(); } catch { /* best effort */ }
  stopGateway();
});

// Release ONLY our own summon accelerator (never unregisterAll — Mochi's
// shortcuts are torn down by its own quit path above).
app.on("will-quit", () => {
  unregisterGlobalHotkey();
});

app.on("window-all-closed", () => {
  // macOS: keep running in tray
  if (process.platform !== "darwin") app.quit();
});
