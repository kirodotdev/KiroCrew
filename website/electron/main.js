const electron = require("electron");
const {
  app,
  BrowserWindow,
  nativeTheme,
  dialog,
  shell,
  ipcMain,
  session,
  crashReporter,
} = electron;
const Store = require("electron-store");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { createLaunchLog } = require("./launch-log");

// First, so the stdio 'error' listeners are attached before anything writes to
// the console: a write to a dead tty must not become an uncaughtException that
// the safety net mirrors back to the same tty (#15950).
const launchLog = createLaunchLog({
  append: (entry) => fs.appendFileSync(gatewayLogPath(), entry),
});

const { findConfiguredDashboardPort } = require("./data-home");
const { defaultedPort } = require("./gateway-auth-hint");
const {
  classifyBundleLocation,
  containingDirForBundle,
  shouldOfferRelocation,
  describeLocation,
} = require("./bundle-location");
const { DEFAULT_REMOTE_BIN } = require("./remote-token");
const {
  fallbackLocalPort,
  isSelectablePort,
  legacyMigrationPort,
  migrateRemoteHostConfig,
  selectLaunchPort,
} = require("./host-config");
const { isLocalGatewayEnabled } = require("./local-gateway");
const { seedRenamedStore } = require("./store-rename");
const { resolveHome } = require("./home-dir");
const { internalSecretFor } = require("./local-token");
const { identityFamily } = require("./instance-guard");
const { describeSingletonLock } = require("./singleton-lock");
const { initNativeLogging } = require("./native-logging");
const { armCrashCollector, collectCrashReports } = require("./crash-collector");
const { initGpuPolicy } = require("./disable-gpu");
const { applyAuthServerAllowlist } = require("./auth-allowlist");
const { initGpuCrashFallback } = require("./gpu-crash-fallback");
const { cancelPendingTrayHide } = require("./hide-to-tray");
const { exitImmersiveModes } = require("./blocking-prompt");
const { createMetricsRecorder } = require("./perf-metrics");
const { initMochi, shutdownMochi } = require("./mochi/index");
const { borrowSessionToken } = require("./mochi-session-token");
const { clearCacheOnUpgrade } = require("./upgrade-cache");
const {
  initCrewCompanion,
  shutdownCrewCompanion,
  suspendCrewCompanion,
  resumeCrewCompanion,
} = require("./crew-companion/index");

// An update install stops the gateway and quits the app. Close the Crew
// Companion overlay at dispatch so it does not float orphaned over the vanished
// dashboard during the quit handoff, and reopen it if the install fails and the
// gateway is restored. suspend/resume keep the companion's loop and IPC handlers
// intact, so the failure path needs no re-init. Both are best-effort — a
// companion teardown must never block an update or its recovery.
function closeCrewCompanionForUpdate() {
  try { suspendCrewCompanion(); } catch { /* best effort */ }
}
function reopenCrewCompanionAfterUpdate() {
  try { resumeCrewCompanion(); } catch { /* best effort */ }
}
const { createGatewaySupervisor } = require("./gateway-supervisor");
const { createWindowLifecycle } = require("./window-lifecycle");
const { createIpcRegistrar } = require("./ipc-registrar");
const { createDownloadExpectations } = require("./download-expectations");
const { installEarlyBootGuard } = require("./early-boot-guard");

// Everything from here to `app.whenReady()` runs synchronously at module load,
// before Chromium is ready and before any window, tray, or crash reporter
// exists. The guard turns a throw anywhere in that span into a log entry, a
// native error box, and exit(1) instead of a silent process death. `glog` and
// `gatewayLogPath` are function declarations further down; they hoist, so the
// guard can call them when it fires. The ready handler releases it once the
// post-ready safety net below can take over.
const releaseEarlyBootGuard = installEarlyBootGuard({
  app,
  dialog,
  glog,
  logPath: gatewayLogPath,
});

// Carry settings across the npm name rename before electron-store opens the
// destination. Construction writes defaults, after which the seed could no
// longer distinguish a first launch from an existing store.
//
// Capture whether this install already existed BEFORE either the rename-seed or
// construction can create the file: a FRESH install gets the summon hotkey
// opted OUT (globalHotkey "") just below, while an upgrading user keeps their
// existing null (the auto-bound platform default) untouched. The seed carrying
// legacy settings also counts as "not fresh".
const storeConfigFile = path.join(app.getPath("userData"), "config.json");
const storeFileExistedBeforeBoot = fs.existsSync(storeConfigFile);
const renameSeedCarriedSettings = seedRenamedStore(app.getPath("userData"), {
  log: (message) => glog("store migration: " + message),
});
const isFreshInstall = !storeFileExistedBeforeBoot && !renameSeedCarriedSettings;

const store = new Store({
  defaults: {
    remoteHost: "",
    kirocrewBinPath: DEFAULT_REMOTE_BIN,
    remoteHosts: {},
    sshTimeoutMs: 20000,
    windowState: null,
    globalHotkey: null, // system-wide summon accelerator: null = platform default, "" = disabled, string = custom (see global-hotkey.js). A fresh install is seeded "" below so the default hotkey is opt-in; an upgrading user keeps null.
    lastNudgedVersion: "",
    themeAccent: "",
    updateChannel: "",
    autoDownloadUpdates: true,
    runLocalGateway: true,
    // "background" or "quit". See close-action.js.
    closeAction: "background",
    linuxFrameless: null,
    // Written by gpu-crash-fallback.js when the GPU process dies at startup;
    // read before Chromium initializes on the next launch.
    gpuSoftwareFallback: null,
  },
});

// Opt the summon hotkey OUT for FRESH installs only. Before this, a null/unset
// globalHotkey auto-bound the platform default (Cmd+Shift+K on macOS, which is
// Slack's "compose a new message" chord) — a global accelerator is exclusive, so
// the app swallowed it for every other program out of the box. New installs now
// start with it disabled ("") and opt in deliberately. An UPGRADING user is left
// at null and keeps the hotkey they already had, so nothing is yanked from under
// them. Written once, at store creation; a later launch finds the file and skips.
// The fresh-vs-upgrade decision is the pure planFreshInstallHotkeySeed() helper,
// unit-tested in global-hotkey.test.js.
const { planFreshInstallHotkeySeed } = require("./global-hotkey");
const hotkeySeed = planFreshInstallHotkeySeed(store.get("globalHotkey"), isFreshInstall);
if (hotkeySeed !== null) {
  store.set("globalHotkey", hotkeySeed);
}

const KIROCREW_HOME = resolveHome();

// dashboard.url in the resolved data home is the backend source of truth.
const CONFIGURED_PORT = findConfiguredDashboardPort(fs, path, [KIROCREW_HOME]);

// Resolved above the migration because both readers need it: an explicit
// override is the port this launch binds, so it is also the port a legacy crew
// has to be keyed under. 0 means absent or unusable.
const ENV_PORT = (() => {
  const raw = process.env.KIROCREW_PORT;
  if (!raw) return 0;
  const parsed = parseInt(raw, 10);
  // isSelectablePort, not just a range check: an override short-circuits
  // selection, so a port selection would REFUSE reaches the launch through here
  // untouched. Port 80 is refused because the shell's own URL loses it --
  // `new URL("http://localhost:80").port` is "" -- so every per-port lookup
  // misses, `isGatewayLocalForWindow` reads a tunnelled crew as local, and the
  // idle heartbeat sends `X-Internal-Secret` to it. Guarding only the selection
  // path left this route open: `legacyMigrationPort` then keyed a `remoteHosts`
  // entry under 80 as well, which is the miss that makes the crew read as local.
  if (isSelectablePort(parsed)) return parsed;
  // An unusable value counts as absent rather than as a request for the product
  // default, so every launch with no usable port of its own takes the same
  // decision through selection. The warning stays, because the value was given
  // and ignored.
  console.warn('Invalid KIROCREW_PORT="' + raw + '", choosing a port as if it were unset');
  return 0;
})();

// Ahead of port selection, because selection is the reader that has to see the
// crew. A legacy `remoteHost` carries no port of its own, so until it is folded
// into `remoteHosts` there is no configured crew for selection to weigh, and the
// launch decides as though the machine had none -- which is the shadowing this
// whole path exists to prevent.
if (migrateRemoteHostConfig(store, legacyMigrationPort({
  envPort: ENV_PORT,
  configuredPort: CONFIGURED_PORT,
}))) {
  glog("Migrated legacy remoteHost into remoteHosts before selecting a port");
}

function resolvePort() {
  if (ENV_PORT) return ENV_PORT;
  return selectLaunchPort({
    store,
    configuredPort: CONFIGURED_PORT,
    localGatewayEnabled: isLocalGatewayEnabled(store),
    log: glog,
  });
}

const PORT = resolvePort();
const BACKEND_URL = "http://localhost:" + PORT;

app.name = identityFamily(app.getVersion()) === "nightly"
  ? "Kiro Crew Nightly"
  : "Kiro Crew";

// Windows groups and pins the live window by this ID. Nightly must remain
// side-by-side with stable, matching the packaged app IDs.
if (process.platform === "win32") {
  const appUserModelId = identityFamily(app.getVersion()) === "nightly"
    ? "com.amazon.kiro.crew.nightly"
    : "com.amazon.kiro.crew";
  app.setAppUserModelId(appUserModelId);
}

function gatewayLogPath() {
  let directory;
  try {
    directory = app.getPath("logs");
  } catch {
    directory = os.tmpdir();
  }
  try {
    fs.mkdirSync(directory, { recursive: true });
  } catch {
    // Logging is diagnostic and must never block launch.
  }
  return path.join(directory, "gateway-launch.log");
}

// The console mirror stops once stdio is dead; see launch-log.js.
function glog(line) {
  launchLog.glog(line);
}

function gwarn(line) {
  launchLog.gwarn(line);
}

function gerror(line) {
  launchLog.gerror(line);
}

/**
 * The X-Internal-Secret to send to `gatewayUrl`, or "" to send none.
 *
 * Only the entry the gateway published for the dialed listener counts
 * (`internalSecretFor`), so the secret can authenticate only against the
 * listener it is sent to. The home-wide `.local_secret` is never read here: it
 * is a last-writer-wins slot, and a loopback port can be a tunnel's local end
 * or another gateway, so sending it there could hand over a credential minted
 * elsewhere.
 *
 * Re-read on every call. The gateway rotates this secret across restarts, so
 * caching turns a successful recovery into a stream of spurious 403s.
 *
 * @param {string} gatewayUrl the URL the request is about to dial
 * @returns {string}
 */
function readInternalSecret(gatewayUrl) {
  return internalSecretFor({ gatewayUrl, home: resolveHome(), path, fs });
}

let isQuitting = false;
let desktopMetricsRecorder = null;
let windows = null;
// Whether a dashboard document has finished loading in this run, owned by the
// GPU-crash fallback (the single-instance winner arms it below). The dead-
// renderer dialog reads it to tell a renderer that never started from one
// that crashed later.
let dashboardLoaded = () => false;

// The native-logging result, kept because its `tightenLiveLog` can only run once
// Chromium has opened `--log-file` — which is after `app.whenReady`, in a
// different scope from the pre-ready arming call. Null when this process is not
// the lock winner and so never armed logging at all.
let nativeLogging = null;

let crashScan = null;
// Separate from `crashScan` so a scan that failed is not retried on every call:
// the failure is a broken path or a missing directory, not a transient.
let crashScanDone = false;

/**
 * Scan for crash artifacts once per app session, on first demand.
 *
 * LAZY on purpose, unlike `initNativeLogging` above. Native logging has to be
 * armed before Chromium initializes, but this only READS what a previous run
 * left behind — and it reads files, on the launch immediately after a crash,
 * which is the launch a user is already watching impatiently. Nothing needs the
 * answer until the dashboard's crash notice asks for it, so it costs nothing
 * until then and nothing at all on a run where the dashboard never opens.
 */
function scanCrashArtifacts() {
  if (crashScanDone) return crashScan;
  crashScanDone = true;
  try {
    crashScan = collectCrashReports({
      logsDir: path.dirname(gatewayLogPath()),
      crashDumpsDir: app.getPath("crashDumps"),
      // macOS only. `.ips` reports are the ONLY channel that captures a
      // main-process abort the Crashpad handler did not survive to write, so
      // they are worth a second directory here. Linux and Windows have no
      // equivalent user-readable per-app report directory, and passing "" makes
      // the collector skip the scan rather than guess at a path.
      diagnosticReportsDir: process.platform === "darwin"
        ? path.join(app.getPath("home"), "Library", "Logs", "DiagnosticReports")
        : "",
      appName: app.getName(),
      // BOTH names, because they are different strings and neither derives from
      // the other: `electron/package.json` sets `executableName` to
      // `kirocrew-desktop` (and the nightly channel overrides it again), while
      // `getName()` is `Kiro Crew`. Off darwin a minidump is our only crash
      // channel, so recognising the executable name is what makes Linux work.
      execName: path.basename(process.execPath),
      fs,
      log: glog,
    });
  } catch (e) {
    // A diagnostic that breaks the launch it exists to explain is worse than no
    // diagnostic. `getPath`/`getName` are the only calls here that can throw.
    glog("crash scan unavailable: " + (e && e.message));
    crashScan = null;
  }
  return crashScan;
}

const requestQuit = () => {
  // Window close handlers consult this synchronously. Set it before app.quit()
  // so a real quit can never be misread as a hide-to-tray request.
  isQuitting = true;
  app.quit();
};

// Only the lock winner may arm native logging. A rejected second instance must
// not rotate chromium.log out from under the primary process.
if (!app.requestSingleInstanceLock()) {
  const userDataDir = app.getPath("userData");
  glog(
    "single-instance lock held by another process; exiting. If no Kiro Crew window is open, "
      + "a crash may have left a stale SingletonLock in " + userDataDir + ". "
      + describeSingletonLock({ userDataDir }),
  );
  app.exit(0);
} else {
  // Record the moment this build became able to collect crashes, BEFORE the
  // crash reporter can produce one. The scan below is lazy — it runs when the
  // dashboard first asks — and the first scan has to distinguish artifacts that
  // predate this feature (which are history, and are marked seen without being
  // read) from ones this build produced. Deciding that at scan time answers the
  // wrong question: an app that crashes before the dashboard ever opens would
  // have its dump written off as pre-existing on the next launch, which is
  // exactly the crash worth reporting. This writes only the cutoff, does not
  // read any artifact, and is idempotent — a second launch keeps the first
  // stamp — so it is cheap enough to sit on the boot path.
  //
  // THE ORDER OF THESE TWO CALLS IS LOAD-BEARING. This must precede
  // `initNativeLogging`, because that is what calls `crashReporter.start()` and
  // so what makes Crashpad able to write a dump at all. Stamping afterwards
  // leaves a window — short, but covering precisely the startup crashes this
  // feature is most needed for — in which a dump exists with no cutoff on
  // record. The next launch then stamps a cutoff LATER than that dump's mtime,
  // the first scan reads it as history, and it is marked seen without ever being
  // surfaced: the crash is silently lost, which is the one outcome this whole
  // feature exists to prevent. Do not reorder for tidiness. Arming first is also
  // free: `armCrashCollector` uses nothing `initNativeLogging` sets up, neither
  // call creates `logsDir`, and the state write fails soft (logs and returns
  // null) rather than throwing.
  armCrashCollector({
    logsDir: path.dirname(gatewayLogPath()),
    fs,
    log: glog,
  });

  nativeLogging = initNativeLogging({
    logsDir: path.dirname(gatewayLogPath()),
    appendSwitch: (name, value) => app.commandLine.appendSwitch(name, value),
    startCrashReporter: (options) => crashReporter.start(options),
    fs,
    log: glog,
  });

  // Chromium reads GPU switches during initialization, so the opt-in policy
  // must run inside the lock-winner branch and before app ready. Reading the
  // winning process's env/argv also avoids pretending a second-instance argv
  // handoff can repair the renderer that already launched.
  initGpuPolicy({
    appendSwitch: (name) => app.commandLine.appendSwitch(name),
    env: process.env,
    argv: process.argv,
    log: glog,
  });

  // Chromium reads the integrated-auth allowlist at network-service start, so
  // an edition's baked allowlist must reach the command line before app ready.
  // The public build bakes none and this appends nothing.
  applyAuthServerAllowlist({
    appendSwitch: (name, value) => app.commandLine.appendSwitch(name, value),
    log: glog,
  });

  // Same timing constraint as the opt-in above: a persisted software-rendering
  // decision has to reach Chromium before it initializes. Also arms the
  // `child-process-gone` listener that makes that decision, so a GPU process
  // that dies before the dashboard loads relaunches the app once in software
  // mode instead of letting Chromium abort it with no window and no log.
  const gpuFallback = initGpuCrashFallback({
    app,
    store,
    backendUrl: BACKEND_URL,
    isQuitting: () => isQuitting,
    requestQuit,
    log: glog,
  });
  dashboardLoaded = gpuFallback.dashboardLoaded;

  app.on("second-instance", () => {
    // Relaunch is explicit intent to see the existing window. The window owner
    // cancels a pending fullscreen/tray hide before restore/show/focus.
    windows?.showMainWindow({ focus: true });
  });
}

// Factories are created at module load, before any renderer can change settings.
// In particular, the supervisor snapshots runLocalGateway once for this launch.
const gateway = createGatewaySupervisor({
  app,
  store,
  BrowserWindow,
  nativeTheme,
  dialog,
  shell,
  ipcMain,
  port: PORT,
  backendUrl: BACKEND_URL,
  home: KIROCREW_HOME,
  getMainWindow: () => windows?.getMainWindow() || null,
  isQuitting: () => isQuitting,
  requestQuit,
  cancelPendingTrayHide,
  exitImmersiveModes,
  log: glog,
  warn: gwarn,
  error: gerror,
  logPath: gatewayLogPath,
  // The port a successor re-exec'd from the error dialog will select. That
  // successor starts with the local-gateway setting on, which is why the
  // setting is named here rather than read: the store write that turns it on
  // and this prediction describe the same next process. Running the same pure
  // function that successor will run is what keeps the two answers identical.
  // The port the successor will BIND, which is a different question from the
  // port this launch targets: selection names a crew's port so a live tunnel
  // there is adopted, and a new gateway must not bind that same port. This
  // process pins the answer into the successor's environment, so the port it
  // watches is the port the successor takes.
  predictLocalPort: () => fallbackLocalPort(store, glog),
});

// Shared single-use registry of downloads the renderer explicitly asked for.
// The ipc bridge registers an expectation when the export path announces a
// download; the will-download handler consumes it, so only announced downloads
// auto-save to Downloads (everything else keeps Chromium's default handling).
const downloadExpectations = createDownloadExpectations();

windows = createWindowLifecycle({
  electron,
  store,
  backendUrl: BACKEND_URL,
  port: PORT,
  glog,
  logPath: gatewayLogPath,
  dashboardLoaded: () => dashboardLoaded(),
  readInternalSecret,
  mintLocalToken: (...args) => gateway.mintLocalToken(...args),
  fetchRemoteToken: (...args) => gateway.fetchRemoteToken(...args),
  isQuitting: () => isQuitting,
  requestQuit,
  connectWindow: (...args) => gateway.connect(...args),
  syncTunnel: () => gateway.syncTunnel(),
  downloadExpectations,
});

const ipcRegistrar = createIpcRegistrar({
  electron,
  store,
  backendUrl: BACKEND_URL,
  port: PORT,
  windows,
  gateway,
  glog,
  closeCrewCompanionForUpdate,
  reopenCrewCompanionAfterUpdate,
  crashScan: scanCrashArtifacts,
  downloadExpectations,
  readInternalSecret,
});

/**
 * Warn when the running bundle cannot be replaced in place and offer the
 * supported macOS relocation. Never rejects: an updater diagnostic cannot
 * strand boot before the app has a window, tray, or gateway.
 */
async function offerRelocationIfUnupdatable() {
  const location = classifyBundleLocation(process.resourcesPath);
  const directory = containingDirForBundle(process.resourcesPath);
  let bundleWritable = true;
  if (directory) {
    try {
      fs.accessSync(directory, fs.constants.W_OK);
    } catch {
      bundleWritable = false;
    }
  }
  glog(
    "bundle location: " + location + " writable=" + bundleWritable
      + " (resourcesPath=" + (process.resourcesPath || "(none)") + ")",
  );
  if (!app.isPackaged || !shouldOfferRelocation(location, { bundleWritable })) {
    return location;
  }

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
  } catch (error) {
    glog("bundle location: relocation prompt failed: " + (error && error.message));
    return location;
  }

  if (response !== 0) {
    glog("bundle location: user declined relocation from " + location);
    return location;
  }

  // moveToApplicationsFolder returns false (rather than throwing) when the
  // authorization prompt is cancelled. False and throw are both non-success.
  let moved = false;
  try {
    moved = app.moveToApplicationsFolder() !== false;
  } catch (error) {
    glog("bundle location: move to /Applications threw: " + (error && error.message));
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
  } catch {
    // Boot continues even when the failure dialog itself is unavailable.
  }
  return location;
}

async function fetchMochiGatewayAuth(backendUrl = BACKEND_URL) {
  // Keep the dashboard established credential order: local secret, explicit
  // SSH host, then a token borrowed from the already-authenticated session.
  // Every credential here is delivered to `backendUrl` as written, which is what
  // keeps the shell on one origin and its renderer on one storage bucket. What
  // makes that address safe for a locally minted token is the mint's own refusal:
  // `localhost` names both loopback families, so mintLocalToken() produces
  // nothing unless this gateway holds every family that host resolves to.
  const localValue = await gateway.mintLocalToken(backendUrl);
  if (localValue) return { value: localValue, viaCookie: false };
  const { token: remoteValue } = await gateway.fetchRemoteToken(defaultedPort(backendUrl));
  if (remoteValue) {
    return { value: remoteValue, viaCookie: false };
  }
  const borrowed = await borrowSessionToken({
    electronSession: session.defaultSession,
    backendUrl,
  });
  return borrowed ? { value: borrowed, viaCookie: true } : { value: "" };
}

// Last-resort safety net: preserve evidence and keep the process alive so the
// bounded renderer/gateway recovery paths can still run.
process.on("uncaughtException", (error) => {
  try {
    gerror("uncaughtException: " + (error && error.stack ? error.stack : error));
  } catch {
    // Logging must never throw from the safety net.
  }
});
process.on("unhandledRejection", (reason) => {
  try {
    gerror("unhandledRejection: " + (reason && reason.stack ? reason.stack : reason));
  } catch {
    // Same last-resort rule as uncaughtException.
  }
});

app.whenReady().then(async () => {
  // The crash reporter and the keep-alive safety net above are armed; from
  // here on an exception is recovered, not fatal.
  releaseEarlyBootGuard();

  // A managed SSH forward does not survive sleep; reopen it on wake. A no-op
  // unless this launch is keeping one.
  try {
    electron.powerMonitor.on("resume", () => {
      glog("power: resumed from sleep");
      gateway.reopenTunnel();
    });
  } catch (error) {
    glog("power: could not watch for resume: " + (error && error.message));
  }

  // Chromium has opened `--log-file` by now, so this is the moment the live log's
  // mode can be asserted rather than assumed. The pre-create in
  // `initNativeLogging` only holds if Chromium opened the inode it created; if it
  // unlinked and recreated the path instead, the file is sitting at the process
  // umask (0644) with the dashboard session token in it. Idempotent and
  // fail-soft — it logs and returns false rather than throwing — so it is cheap
  // enough to run unconditionally on every boot.
  try {
    nativeLogging?.tightenLiveLog();
  } catch (error) {
    glog("native log post-open chmod failed: " + (error && error.message));
  }

  const frameDecision = windows.platform.linuxFrameDecision;
  if (frameDecision) {
    glog(
      "linux frame decision: frameless=" + frameDecision.frameless
        + " reason=" + frameDecision.reason,
    );
  }

  // Debug-only and bounded. A diagnostic aid must never take the app down.
  try {
    desktopMetricsRecorder = createMetricsRecorder({
      dir: path.dirname(gatewayLogPath()),
      getAppMetrics: () => app.getAppMetrics(),
      log: (message) => glog("perf: " + message),
      meta: { electron: process.versions && process.versions.electron },
    });
    desktopMetricsRecorder.start();
  } catch (error) {
    try {
      glog("perf: metrics recorder failed to start: " + (error && error.message));
    } catch {
      // Ignore a failure in the failure logger.
    }
  }

  await offerRelocationIfUnupdatable();

  // Security and every non-update bridge are installed before the first
  // dashboard or untrusted browser WebContents can be created.
  ipcRegistrar.registerShell();
  windows.createTray();
  const mainWindow = windows.createMainWindow();

  // The global accelerator needs an existing main window. The updater needs
  // that same window for notifications, but MUST be fully registered before
  // either awaited gateway boot step: preload exposes updateAPI immediately.
  ipcRegistrar.bindGlobalHotkey();
  ipcRegistrar.registerUpdater();

  await gateway.start();
  await gateway.connect(mainWindow);
  // The gateway now answers and the dashboard is loading. After an upgrade, drop
  // the old build's cached copies and connect again: a fresh navigation replaces
  // the pending one, where a reload would replay the uncommitted splash.
  const cleared = await clearCacheOnUpgrade({
    session: session.defaultSession,
    store,
    appVersion: app.getVersion(),
    probe: () => fetch(BACKEND_URL + "/api/health", { signal: AbortSignal.timeout(2000) })
      .then((response) => (response.ok ? response.json() : null)).catch(() => null),
    log: glog,
  });
  if (cleared && !mainWindow.isDestroyed()) await gateway.connect(mainWindow);

  // Optional companion surfaces start only after the primary gateway handoff.
  // Both are best-effort and must never block an otherwise usable dashboard.
  initMochi({
    backendUrl: BACKEND_URL,
    fetchGatewayAuth: fetchMochiGatewayAuth,
    glog,
    getMainWindow: () => windows.getMainWindow(),
  });
  try {
    initCrewCompanion({
      backendUrl: BACKEND_URL,
      // The same credential chain Mochi and the dashboard window use. The local
      // mint alone refuses for a remote crew's port and for a gateway this app
      // adopted rather than spawned, which left the companion unable to ever ask
      // whether it was enabled.
      fetchGatewayAuth: () => fetchMochiGatewayAuth(BACKEND_URL),
      glog,
      getDashboardWindow: () => windows.focusedDashboardWindow() || null,
    });
  } catch (error) {
    glog("crew-companion: init failed — " + (error && error.message));
  }

  // Preserve the historical registration point: activation starts being
  // handled only after boot and optional companion initialization finish.
  app.on("activate", () => {
    windows.activateMainWindow();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
  // Flush the final metrics window before gateway teardown begins.
  try {
    desktopMetricsRecorder?.stop();
  } catch {
    // Best effort during quit.
  }
  // contentTracing writes only when recording is stopped. Do not await or
  // prevent quit for diagnostics, but give an armed capture its chance to land.
  void windows.diagnostics.stopForQuit();
  shutdownMochi();
  try {
    shutdownCrewCompanion();
  } catch {
    // Best effort during quit.
  }
  gateway.stopOnQuit();
});

// Release only the shell summon accelerator. Mochi owns and removes its own
// shortcuts on the before-quit path above.
app.on("will-quit", () => {
  ipcRegistrar.unregisterGlobalHotkey();
});

app.on("window-all-closed", () => {
  // macOS keeps the menu-bar/tray process alive without windows.
  if (process.platform !== "darwin") app.quit();
});
