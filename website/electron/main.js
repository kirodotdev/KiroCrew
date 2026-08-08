const { app, BaseWindow, BrowserWindow, WebContentsView, shell, dialog, Tray, Menu, nativeImage, nativeTheme, Notification, ipcMain, webContents, session, desktopCapturer, systemPreferences, screen } = require("electron");
const Store = require("electron-store");
const fs = require("fs");
const os = require("os");
const { spawn, execFile } = require("child_process");
const path = require("path");
const http = require("http");

const { findKirocrewBin } = require("./find-bin");
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
const {
  classifyBundleLocation,
  containingDirForBundle,
  shouldOfferRelocation,
  describeLocation,
} = require("./bundle-location");
const { stopGatewayGracefully: _stopGatewayGracefully, forceStopPort, classifyPortOwner } = require("./gateway-stop");
const { waitForGateway, describeGatewayFailure, tailLines, isPortInUse } = require("./gateway-wait");
const { describeSandboxProfileNeed } = require("./sandbox-profile");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const { createLivenessMonitor } = require("./gateway-liveness");
const { chooseRecoveryStrategy } = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const { createMetricsRecorder } = require("./perf-metrics");
const { identityFamily, decideGatewayAction, FAMILY_META, HEALTH_IDENTITY_PATH } = require("./instance-guard");
const { initMochi, shutdownMochi } = require("./mochi/index");
const { initCrewCompanion, shutdownCrewCompanion } = require("./crew-companion/index");
const { clampZoomFactor, stepZoomFactor } = require("./zoom");
const { createBrowserViewManager, isUntrustedContents } = require("./browser-view");
// chooseControlTransport is exercised by the routing layer (see Stage 6 notes),
// not here — main.js only owns the plane and its gate.
const { canAgentControl, createControlPlane, OWNER } = require("./browser-control");
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

// ── Persistent settings for remote tunnel mode ──

const {
  DEFAULT_REMOTE_BIN,
  DEFAULT_REMOTE_PATH,
  buildRemoteTokenCommand,
  parseTokenFromStdout,
} = require("./remote-token");

const { migrateRemoteHostConfig, getRemoteHostConfig, setRemoteHostConfig } = require("./host-config");

const store = new Store({
  defaults: {
    remoteHost: "",                        // LEGACY — migrated to remoteHosts
    kirocrewBinPath: DEFAULT_REMOTE_BIN,   // LEGACY — migrated to remoteHosts
    remoteHosts: {},                       // { [port]: { host, binPath, remotePort?, remotePath? } }
    sshTimeoutMs: 20000,
    windowState: null,                     // persisted main-window geometry (see window-state.js)
    lastNudgedVersion: "",                 // last update version announced via native notification (nudge once per version)
    themeAccent: "",                       // user's resolved theme accent hex; injected into the boot splash
    updateChannel: "",                     // "" = follow build stamp; "insider"|"stable" = user opt-in (Settings > About)
  },
});

// The PRE-SPAWN read home (see home-dir.js for the full contract): whichever
// directory's config.json governs this launch under the backend's migration
// rules -- legacy ~/.kirocrew when it exists (the backend force-copies it
// over ~/.kiro/crew, marker or not), canonical otherwise. Parity with
// config/paths.py is gated by test/fixtures/home-resolution-cases.json.
// Boot-time WRITES (mkdir, pycache prefix) use canonicalHome() instead --
// writing into the legacy dir re-arms the migration every launch (#483).
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
  // dashboard/origin.parse_dashboard_url). A real legacy home is checked first
  // because the backend migration makes legacy data authoritative on conflict.
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
const IS_WIN = process.platform === "win32";
const DEFAULT_THEME_ACCENT = "#8E48FF";
const THEME_ACCENT_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

function currentThemeAccent() {
  const configured = store.get("themeAccent") || "";
  return THEME_ACCENT_RE.test(configured) ? configured : DEFAULT_THEME_ACCENT;
}


// The dashboard view fills the whole content area on all platforms. On macOS
// the window is frameless (titleBarStyle:"hidden") and the dashboard's own
// 42px header doubles as the title bar: an injected drag region makes it
// draggable and the native traffic lights are inset into it (see
// positionTrafficLights).

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
// True only when WE spawned the bundled backend on this flavor's port. False on
// the reuse path — i.e. a gateway was already answering when we booted, which is
// exactly the remote-tunnel setup (localhost:<port> is an SSH forward to a
// remote gateway) and also the "dev ran `kirocrew gateway` in a terminal" case.
// Recovery must NEVER kill or respawn a gateway we did not spawn: the port-holder
// is someone else's process (our SSH tunnel, a manual gateway), and the correct
// fix on a dropped tunnel is to re-probe and reconnect once it heals, not to
// force-stop the port or spawn a (nonexistent, in remote mode) local backend.
let weSpawnedGateway = false;
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
// On Windows there is no lsof/ps, so classifyPortOwner returns "unknown" and
// the caller reuses the port rather than force-killing an owner it cannot
// identify. A Windows-native netstat/taskkill owner probe is a follow-up
// (tracked separately) — it needs runtime verification on real Windows.
function probeGatewayPortOwner(port) {
  return classifyPortOwner(port, {
    getListenPids: _lsofListenPids,
    getCommand: _psCommand,
    getPpid: _psPpid,
    log: glog,
  });
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

async function resolveGatewayConflict() {
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
    glog(`reusing existing gateway on :${PORT} (${decision.reason}) — bundled backend NOT spawned`);
    weSpawnedGateway = false; // reuse path — recovery must not kill/respawn a gateway we don't own
    sendStatus("Gateway already running ✓");
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
    checkBackend()
      .then(async () => {
        // A gateway is already listening on this port. Same-family, dev, and
        // legacy gateways are reused as before. A gateway owned by the other
        // channel app triggers the takeover prompt.
        const outcome = await resolveGatewayConflict();
        if (outcome === "reuse") { resolve(true); return; }
        if (outcome === "abort") {
          isQuitting = true;
          app.quit();
          resolve(false);
          return;
        }
        spawnGateway(resolve);
      })
      .catch(() => {
        spawnGateway(resolve);
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
        // Pre-create the backend's POST-migration data root so the pycache
        // prefix below has a live target. Deliberately NOT resolveHome():
        // that answers "which config content governs this launch" and can be
        // the legacy dir -- pre-creating or writing into ~/.kirocrew re-arms
        // the backend's legacy migration on every launch (issue #483 class).
        // The gateway creates/owns its home and .local_secret regardless.
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
        glog("---- spawning gateway; child stdout+stderr follows ----");
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
            // Bundled layout is present but python.exe is missing/corrupted.
            // Surface this as a clear error instead of letting spawn() hang or
            // emit a cryptic ENOENT for the .cmd shim.
            const errMsg = `Bundled Python interpreter not found at ${pyExe}. `
              + `The installation may be corrupted — reinstall the app.`;
            glog(`spawn ERROR: ${errMsg}`);
            gatewayStartFailure = { error: errMsg };
            sendStatus(`Gateway failed: ${errMsg}`);
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
        weSpawnedGateway = true; // we own this child — recovery may kill+respawn it
        // The child inherits its own dup of the fd; close our copy so it doesn't leak.
        if (typeof childOut === "number") { try { fs.closeSync(childOut); } catch { /* ignore */ } }

        child.on("error", (err) => {
          // ENOENT = bin not found on disk; EACCES = present but not executable.
          glog(`spawn ERROR code=${err.code || "?"} msg=${err.message}`);
          if (gatewayProcess !== child) return; // stale child we already replaced
          gatewayStartFailure = { error: err.message };
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
          if (!gatewayStartFailure) gatewayStartFailure = { code, signal };
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
 * KIROCREW_HOME pin, because on the migration launch the boot-time dir may
 * have been deleted by the backend — the secret lives in whichever candidate
 * still exists at shutdown time.
 */
async function stopGatewayGracefully({ timeoutMs = 15000 } = {}) {
  const proc = gatewayProcess;
  if (!proc || proc.exitCode !== null) { gatewayProcess = null; return; }
  console.log("Stopping gateway gracefully...");
  // Resolve the secret location at call time: try each candidate in order
  // (canonical first, legacy second) so graceful stop works even when the
  // boot-time home was moved/deleted during migration.
  // Resolve the secret location at call time: a migration may have moved or
  // deleted the boot-time home, and a partial migration can leave BOTH a
  // canonical and a legacy `.local_secret`. Collect every readable candidate
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
  // Re-resolve the authoritative home at call time: migration may move or pin
  // the live secret after Electron starts. Send exactly that one secret to the
  // gateway's literal IPv4 bind address; never probe alternate homes/addresses.
  return fetchTokenFromHome({
    backendUrl,
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
    entry.gate = () =>
      canAgentControl({
        agentActEnabled: entry.agentAct,
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
    // Only panels that actually exist can be driven; an empty list parks the
    // poller instead of spinning.
    listPanelIds: () => [...browserPanels.keys()],
    dispatch: async (sessionKey, op, args) => {
      const entry = browserPanel(sessionKey, { create: false });
      if (!entry) throw new Error(`no native browser panel for session ${sessionKey}`);
      // A `navigate` is what BOOTSTRAPS the view, and the ORDER here matters.
      // `canAgentControl` refuses with `no-browser-view` when nothing is open, so
      // acquiring LIGHT first would refuse before any bootstrap could run — the
      // agent's very first "open this page" would fall back to Playwright and
      // never reach the native browser, defeating the point of routing natively.
      //
      // Authorization is still enforced first and separately: agent-act must
      // already be granted. Only the "a view exists" precondition is satisfied by
      // creating one, never the permission itself.
      if (op === "navigate" && !entry.manager.getWebContents()) {
        const pre = entry.gate();
        if (!pre || !pre.agentActEnabled) {
          throw new Error("browser control refused: agent-act-not-authorized");
        }
        // `manager.navigate` creates the view when none is open and applies the
        // same non-web URL guard as the CDP path.
        const opened = entry.manager.navigate(String((args && args.url) || ""));
        if (opened && opened.refused) {
          return { ok: false, code: "bad_url", error: `refused non-web URL: ${args && args.url}` };
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
      // runs the same gate (agent-act authorization) as any other transition.
      const taken = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
      if (taken.refused) throw new Error(`browser control refused: ${taken.refused}`);
      return dispatchBrowserOp(entry, op, args);
    },
    onError: (err, context) =>
      console.warn(`[browser-agent-channel] ${context}: ${err && err.message}`),
  });
  win._mcAgentChannel.start();

  attachContextMenu(view.webContents);

  // Keep the native traffic lights centered in the zoom-scaled header row.
  // "zoom-changed" covers pinch / ctrl+wheel gestures; the View-menu zoom
  // items call positionTrafficLights explicitly (see zoomItem in the menu).
  if (IS_MAC) {
    positionTrafficLights(win);
    view.webContents.on("zoom-changed", () => setTimeout(() => positionTrafficLights(win), 0));
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
    // macOS + Windows + Linux (non-native-frame): the frameless window needs
    // an injected drag region so the dashboard header can move the window.
    // On macOS titleBarStyle:"hidden" makes the whole window frameless; on
    // Windows titleBarOverlay provides caption controls but no drag area.
    // The drag bar is pointer-events:none so clicks pass through to the SPA;
    // interactive controls are marked no-drag so they remain clickable.
    if (IS_MAC || IS_WIN) {
      view.webContents.insertCSS(`
        #electron-drag-bar {
          position: fixed;
          top: 0; left: 0; right: ${IS_WIN ? '138px' : '0'};
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
  // Linux: Electron ignores titleBarStyle, so it keeps the native frame.
  if (IS_MAC) opts.titleBarStyle = "hidden";
  if (IS_WIN) {
    opts.titleBarStyle = "hidden";
    opts.titleBarOverlay = {
      color: nativeTheme.shouldUseDarkColors ? "#0f1117" : "#f8fafc",
      symbolColor: nativeTheme.shouldUseDarkColors ? "#e2e8f0" : "#1e293b",
      height: 42,
    };
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
  if (typeof state.x === "number" && typeof state.y === "number") {
    opts.x = state.x;
    opts.y = state.y;
  }
  mainWindow = new BaseWindow(opts);

  setupWindowContents(mainWindow, BACKEND_URL);

  // Persist geometry on every change (debounced) so a quit/crash at any point
  // keeps the last good size + fullscreen flag. captureWindowState() uses
  // getNormalBounds(), so we store the restore size, never the fullscreen frame.
  let saveTimer = null;
  const persist = () => {
    const s = captureWindowState(mainWindow);
    if (s) store.set("windowState", s);
  };
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
  const iconPath = path.join(__dirname, iconFile);
  const icon = nativeImage.createFromPath(iconPath).resize({ width: 18, height: 18 });
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
 *          portConflict:boolean, port:number}} opts
 * @returns {Promise<'retry'|'force-retry'|'reveal'|'quit'>}
 */
function showGatewayErrorDialog(parentWin, opts) {
  const { title, message, logTail, logPath, portConflict } = opts;
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
    // can't clear a port conflict, so offer force-stop instead.
    const primaryAction = portConflict ? "force-retry" : "retry";
    const primaryLabel = portConflict ? "Force-stop &amp; Retry" : "Retry";
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
        <button class="ok" onclick="act('${primaryAction}')">${primaryLabel}</button>
        <button class="cancel" onclick="act('reveal')">Reveal Log</button>
        <button class="cancel" onclick="act('quit')">Quit</button>
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
  if (chooseRecoveryStrategy({ weSpawnedGateway }) === "reconnect") {
    glog("liveness: backend unresponsive on a gateway we did not spawn (remote tunnel / external gateway) — waiting for it to recover instead of killing the port");
    if (!win || win.isDestroyed() || isQuitting) return;
    return reconnectExternalGateway(win);
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
  try { ({ freed, foreignHolder } = await forceStopGatewayPort(PORT)); }
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
    glog(`liveness: port still held after force-stop (${foreignHolder ? "foreign holder" : "unkillable wedge"}); surfacing restart-required`);
    return showUnrecoverableGatewayError(win, PORT);
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

/**
 * Terminal state: a wedged gateway is holding the port and cannot be killed
 * (uninterruptible kernel sleep — e.g. a blocking close() on a dead socket).
 * No respawn can succeed until the OS reaps it, which only a restart guarantees.
 * Tell the user plainly instead of looping a doomed retry.
 */
async function showUnrecoverableGatewayError(win, port) {
  if (!win || win.isDestroyed()) return;
  let logTail = "";
  try { logTail = tailLines(fs.readFileSync(gatewayLogPath(), "utf8"), 60); } catch { /* no log yet */ }
  const action = await showGatewayErrorDialog(win, {
    title: `Kiro Crew — backend stuck on port ${port}`,
    message: `The Kiro Crew backend is wedged and cannot be stopped — it is in an `
      + `uninterruptible state and is still holding port ${port}, so it can't be `
      + `force-stopped or restarted in place. Restart your computer to clear it. `
      + `(This is a known backend hang; see the launch log below for the cause.)`,
    logTail,
    logPath: gatewayLogPath(),
    portConflict: false, // hide "Force-stop & Retry" — it cannot work here
    port,
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
    const portConflict = failedToStart && backendUrl === BACKEND_URL && isPortInUse(logTail);

    let title, message;
    if (portConflict) {
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
        title, message, logTail, logPath, portConflict, port: PORT,
      });
      if (win.isDestroyed()) return;
      if (action === "reveal") {
        try { shell.showItemInFolder(logPath); } catch { /* best effort */ }
        continue; // re-show the dialog
      }
      if (action === "force-retry") {
        let freed = true;
        try { ({ freed } = await forceStopGatewayPort(PORT)); }
        catch (e) { glog(`force-stop: port probe failed (${e && e.message}); letting retry's bind confirm`); }
        if (win.isDestroyed()) return;
        if (!freed) {
          // The port is still held — by an unkillable wedge or a foreign app.
          // Either way a retry would just re-hit "address already in use", so
          // tell the user a restart is required rather than looping the failure.
          return showUnrecoverableGatewayError(win, PORT);
        }
      }
      if (action === "retry" || action === "force-retry") {
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
    // native frame elsewhere (Linux).
    if (IS_MAC) connOpts.titleBarStyle = "hidden";
    if (IS_WIN) {
      connOpts.titleBarStyle = "hidden";
      connOpts.titleBarOverlay = {
        color: nativeTheme.shouldUseDarkColors ? "#0f1117" : "#f8fafc",
        symbolColor: nativeTheme.shouldUseDarkColors ? "#e2e8f0" : "#1e293b",
        height: 42,
      };
    }
    if (IS_MAC) connOpts.trafficLightPosition = trafficLightPositionForZoom(1);
    const connWin = new BaseWindow(connOpts);

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
  const openSettingsPage = (tab) => {
    const win = [BaseWindow.getFocusedWindow(), mainWindow].find(
      (w) => w && !w.isDestroyed() && w._mcView
    );
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    const wc = win._mcView.webContents;
    if (wc && !wc.isDestroyed()) {
      wc.send("navigate", tab ? `/settings?tab=${tab}` : "/settings");
    }
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
      openNewConnectionWindow: () => openNewConnectionWindow(),
      renameCurrentWindow: () => renameCurrentWindow(),
      promptRemoteHost: () => promptRemoteHost(),
      refreshToken: () => refreshToken(),
      openConfigFile: () => shell.openPath(store.path),
    })
  );
  Menu.setApplicationMenu(appMenu);

  // DevTools gate: renderer sends dev-mode state, we toggle menu visibility.
  ipcMain.on("dev-mode-changed", (_event, enabled) => {
    const menu = Menu.getApplicationMenu();
    const item = menu && menu.getMenuItemById("devtools-toggle");
    if (item) item.visible = !!enabled;
  });

  // The renderer reports the user's resolved theme accent whenever it changes
  // (see useTheme.tsx). Persist a validated hex so the NEXT launch's boot splash
  // can paint in the user's colour. Anything not a plain hex is ignored.
  ipcMain.on("theme-accent-changed", (_event, hex) => {
    if (typeof hex === "string" && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex)) {
      store.set("themeAccent", hex);
    }
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

  // Windows titleBarOverlay color sync: when the resolved dark/light mode
  // changes, update the overlay background and symbol colors to match. The
  // renderer sends the resolved mode ("dark" | "light") after any theme change.
  ipcMain.on("titlebar-overlay-theme", (_event, mode) => {
    if (!IS_WIN) return;
    const dark = mode === "dark";
    const color = dark ? "#0f1117" : "#f8fafc";
    const symbolColor = dark ? "#e2e8f0" : "#1e293b";
    for (const win of BaseWindow.getAllWindows()) {
      try {
        if (typeof win.setTitleBarOverlay === "function") {
          win.setTitleBarOverlay({ color, symbolColor, height: 42 });
        }
      } catch { /* window mid-teardown */ }
    }
  });

  // Dock/taskbar badge (RFC notification bus Phase 4): renderer pushes its
  // unread notification count. Clamped to a sane non-negative integer;
  // Electron no-ops setBadgeCount on unsupported platforms (Windows).
  const { clampBadgeCount } = require("./badge");
  ipcMain.on("badge:set", (_event, count) => {
    app.setBadgeCount(clampBadgeCount(count));
  });

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
  initMochi({ backendUrl: BACKEND_URL, fetchLocalToken, glog });
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

app.on("window-all-closed", () => {
  // macOS: keep running in tray
  if (process.platform !== "darwin") app.quit();
});
