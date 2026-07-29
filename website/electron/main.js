const { app, BaseWindow, BrowserWindow, WebContentsView, shell, dialog, Tray, Menu, nativeImage, nativeTheme, Notification, ipcMain, webContents, session, desktopCapturer, systemPreferences, screen } = require("electron");
const Store = require("electron-store");
const fs = require("fs");
const os = require("os");
const { spawn, execFile } = require("child_process");
const path = require("path");
const http = require("http");

// Squirrel.Windows fires the app with --squirrel-install / -updated /
// -uninstall / -obsolete during install lifecycle events; the app must
// handle them (shortcut creation/removal) and exit WITHOUT starting the
// gateway or opening windows. No-op on macOS/Linux. Kept dependency-free
// (no electron-squirrel-startup package) to match the repo's built-in
// updater philosophy.
(function handleSquirrelEvents() {
  if (process.platform !== "win32" || process.argv.length < 2) return;
  const cmd = process.argv[1];
  if (!cmd.startsWith("--squirrel-")) return;
  const { app } = require("electron");
  const { spawn } = require("child_process");
  const updateExe = path.resolve(path.dirname(process.execPath), "..", "Update.exe");
  const target = path.basename(process.execPath);
  const run = (args) => {
    try {
      spawn(updateExe, args, { detached: true, stdio: "ignore" }).unref();
    } catch (_) {
      /* Update.exe missing (dev run) -- nothing to do */
    }
  };
  if (cmd === "--squirrel-install" || cmd === "--squirrel-updated") {
    run(["--createShortcut=" + target]);
  } else if (cmd === "--squirrel-uninstall") {
    run(["--removeShortcut=" + target]);
  } else if (cmd !== "--squirrel-obsolete") {
    // --squirrel-firstrun (Squirrel's normal launch after a fresh install)
    // and any unknown --squirrel-* flag: continue normal startup. Quitting
    // here would make the app exit on every first launch.
    return;
  }
  // Lifecycle events (install/updated/uninstall/obsolete) end here.
  app.quit();
  process.exit(0);
})();
const { findKirocrewBin } = require("./find-bin");
const { findConfiguredDashboardPort } = require("./data-home");
const { createTokenRetryHandler } = require("./token-retry");
const { classifyAuthBlock, defaultedPort } = require("./gateway-auth-hint");
const { createDisplayMediaHandler } = require("./display-media");
const { initAutoUpdate } = require("./auto-update");
const { stopGatewayGracefully: _stopGatewayGracefully, forceStopPort, classifyPortOwner } = require("./gateway-stop");
const { waitForGateway, describeGatewayFailure, tailLines, isPortInUse } = require("./gateway-wait");
const { sanitizeWindowState, captureWindowState } = require("./window-state");
const { createLivenessMonitor } = require("./gateway-liveness");
const { chooseRecoveryStrategy } = require("./gateway-recovery");
const { capturePySpyDump } = require("./pyspy-dump");
const { identityFamily, decideGatewayAction, FAMILY_META, HEALTH_IDENTITY_PATH } = require("./instance-guard");
const { clampZoomFactor, stepZoomFactor } = require("./zoom");
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
// Platforms without lsof//bin/ps (Windows) resolve to "unknown", which reuses.
// That is the safe direction: reuse can never produce two gateways on one data
// home, so the cross-app mutex still holds — only the eviction prompt is lost,
// and eviction was already darwin-only (canTakeover below).
function probeGatewayPortOwner(port) {
  return classifyPortOwner(port, {
    getListenPids: _lsofListenPids,
    getCommand: _psCommand,
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
        let spawnArgs = ["gateway", "--no-open"];
        if (bin.endsWith("kirocrew.cmd")) {
          const pyExe = path.resolve(path.dirname(bin), "..", "python.exe");
          if (fs.existsSync(pyExe)) {
            spawnBin = pyExe;
            spawnArgs = ["-s", "-m", "kiro_crew", ...spawnArgs];
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
            KIROCREW_PROJECT_DIR: path.resolve(__dirname, ".."),
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
 */
async function stopGatewayGracefully({ timeoutMs = 15000 } = {}) {
  const proc = gatewayProcess;
  if (!proc || proc.exitCode !== null) { gatewayProcess = null; return; }
  console.log("Stopping gateway gracefully...");
  await _stopGatewayGracefully(proc, {
    backendUrl: BACKEND_URL,
    kirocrewHome: KIROCREW_HOME,
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

function syncNativeTheme(view, win) {
  if (win.isDestroyed()) return;
  view.webContents.executeJavaScript(
    `document.documentElement.dataset.mode || ""`
  ).then(mode => {
    if (mode === "dark" || mode === "light") nativeTheme.themeSource = mode;
  }).catch(() => {});
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
    view.webContents.close();
  });

  // The dashboard view fills the entire content area; the SPA's own header is
  // the title bar (drag region injected below on macOS).
  function updateViewBounds() {
    if (win.isDestroyed()) return;
    const { width, height } = win.getContentBounds();
    view.setBounds({ x: 0, y: 0, width, height });
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
    const suffix = customName || getRemoteHostConfig(store, port)?.defaultName || `[:${port}]`;
    win.setTitle(`Kiro Crew ${suffix}`);
  }

  win._mcSetCustomName = (name) => { customName = name; applyTitle(); };
  win._mcGetCustomName = () => customName;
  win._mcBackendUrl = backendUrl;
  win._mcView = view;
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
    view.webContents.insertCSS(`
      #electron-drag-bar {
        position: fixed;
        top: 0; left: 0; right: 0;
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
    // Sync window background to theme color (visible in tab bar padding area)
    view.webContents.executeJavaScript(
      `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`
    ).then(bg => { if (bg && !win.isDestroyed()) win.setBackgroundColor(bg); }).catch(() => {});
    // Sync native chrome on first load
    syncNativeTheme(view, win);
  });

  // Sync native tab bar to dashboard dark/light mode on focus (process-global setting)
  win.on("focus", () => syncNativeTheme(view, win));

  view.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const u = new URL(url);
      if (u.origin === new URL(backendUrl).origin) {
        return { action: 'allow' };
      }
      if (u.protocol === 'http:' || u.protocol === 'https:') {
        shell.openExternal(url);
      }
    } catch {}
    return { action: 'deny' };
  });

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
  // Frameless chrome is macOS-only: the dashboard's 42px header doubles as
  // the title bar with the native traffic lights inset into it. Windows has
  // no equivalent inset controls -- hiding the title bar there would ship
  // windows with no minimize/maximize/close at all -- so it keeps the native
  // frame, exactly like the shipped Linux AppImage (Electron ignores
  // titleBarStyle on Linux). A Windows title-bar overlay with inset controls
  // is the tracked follow-up.
  if (IS_MAC) opts.titleBarStyle = "hidden";
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
      if (isQuitting) return; // intentional shutdown — not a wedge
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
    } else {
      // Fallback — check if gateway allows unauthenticated access
      const status = await new Promise((resolve) => {
        http.get(backendUrl, (res) => {
          res.resume();
          resolve(res.statusCode);
        }).on("error", () => resolve(0));
      });
      if (win.isDestroyed()) return;
      if (status === 403) {
        // The page has to say WHICH machine to mint on. A gateway we did not
        // spawn (an `ssh -L` forward, or an externally-started one) has its own
        // .local_secret, so our CLI can only mint against it FROM that machine;
        // pointing the user at this one would send them where the gateway is
        // not. Reuse the boot-time port-owner probe rather than guessing.
        //
        // NOTE `URL.port` is "" for a default-port URL (http://host/ on :80).
        // Left empty it would look up the wrong remote-host entry, probe no
        // port at all, and let the page fall back to :5476 — i.e. describe and
        // submit to a gateway that isn't the one we just got a 403 from.
        const promptPort = defaultedPort(backendUrl);
        const remoteHost = getRemoteHostConfig(store, promptPort)?.host || "";
        const localOwner = remoteHost ? "foreign" : await probeGatewayPortOwner(promptPort);
        const kind = classifyAuthBlock({ localOwner, remoteHost });
        glog(`token prompt: kind=${kind} owner=${localOwner} port=${promptPort} host=${remoteHost || "(none)"}`);
        if (win.isDestroyed()) return;
        wc.loadFile(path.join(__dirname, "token-prompt.html"), {
          query: { port: promptPort, kind, host: remoteHost },
        });
      } else {
        wc.loadURL(backendUrl);
        if (backendUrl === BACKEND_URL) startLivenessMonitor(win);
      }
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
    // frameless + inset traffic lights on macOS, native frame elsewhere.
    if (IS_MAC) connOpts.titleBarStyle = "hidden";
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
  // prompts there). Scope the grant to the `media` permission type only
  // (what getUserMedia needs for the mic) and deny every other permission
  // type (geolocation, clipboard, notifications, MIDI, …) per least
  // privilege. Screen capture uses its own setDisplayMediaRequestHandler and
  // is unaffected. On macOS we also proactively trigger the OS microphone
  // (TCC) permission prompt.
  const isAppOrigin = (wc) => {
    try { return new URL(wc?.getURL?.() || "").hostname === "localhost"; } catch { return false; }
  };
  session.defaultSession.setPermissionRequestHandler((wc, permission, callback, details) => {
    const isAudioOnly = details?.mediaTypes?.includes("audio") && !details?.mediaTypes?.includes("video");
    callback(permission === "media" && isAppOrigin(wc) && isAudioOnly);
  });
  session.defaultSession.setPermissionCheckHandler((wc, permission, _origin, details) => {
    if (permission === "media") return isAppOrigin(wc) && details?.mediaType === "audio";
    return false;
  });
  if (process.platform === "darwin") {
    systemPreferences.askForMediaAccess("microphone").catch(() => {
      /* best effort — older macOS or TCC denied */
    });
  }

  createTray();
  const win = createWindow();

  await startGateway();
  await showLoadingThenConnect(win);

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
  const updater = initAutoUpdate({
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
    onUpdateState: broadcastUpdateState,
  });
  // Renderer-callable bridges for Settings > About + the UpdateModal.
  ipcMain.handle("update:get-info", () => updater.getInfo());
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
    return { ok: true, info: updater.getInfo() };
  });

  app.on("activate", () => {
    if (!mainWindow?.isVisible()) mainWindow?.show();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
  stopGateway();
});

app.on("window-all-closed", () => {
  // macOS: keep running in tray
  if (process.platform !== "darwin") app.quit();
});
