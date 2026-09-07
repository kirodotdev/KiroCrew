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

const { findConfiguredDashboardPort } = require("./data-home");
const {
  classifyBundleLocation,
  containingDirForBundle,
  shouldOfferRelocation,
  describeLocation,
} = require("./bundle-location");
const { DEFAULT_REMOTE_BIN } = require("./remote-token");
const {
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  isSelectablePort,
} = require("./host-config");
const { isLocalGatewayEnabled } = require("./local-gateway");
const { seedRenamedStore } = require("./store-rename");
const { resolveHome, secretCandidates } = require("./home-dir");
const { identityFamily } = require("./instance-guard");
const { initNativeLogging } = require("./native-logging");
const { armCrashCollector, collectCrashReports } = require("./crash-collector");
const { initGpuPolicy } = require("./disable-gpu");
const { cancelPendingTrayHide } = require("./hide-to-tray");
const { exitImmersiveModes } = require("./blocking-prompt");
const { createMetricsRecorder } = require("./perf-metrics");
const { initMochi, shutdownMochi } = require("./mochi/index");
const { borrowSessionToken } = require("./mochi-session-token");
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

// Carry settings across the npm name rename before electron-store opens the
// destination. Construction writes defaults, after which the seed could no
// longer distinguish a first launch from an existing store.
seedRenamedStore(app.getPath("userData"), {
  log: (message) => console.log("store migration: " + message),
});

const store = new Store({
  defaults: {
    remoteHost: "",
    kirocrewBinPath: DEFAULT_REMOTE_BIN,
    remoteHosts: {},
    sshTimeoutMs: 20000,
    windowState: null,
    globalHotkey: null,
    lastNudgedVersion: "",
    themeAccent: "",
    updateChannel: "",
    autoDownloadUpdates: true,
    runLocalGateway: true,
    linuxFrameless: null,
  },
});

const KIROCREW_HOME = resolveHome();

function resolvePort() {
  const raw = process.env.KIROCREW_PORT;
  if (raw) {
    const parsed = parseInt(raw, 10);
    if (isNaN(parsed) || parsed < 1 || parsed > 65535) {
      console.warn('Invalid KIROCREW_PORT="' + raw + '", falling back to 5476');
      return 5476;
    }
    return parsed;
  }

  // dashboard.url in the resolved data home is the backend source of truth.
  const configuredPort = findConfiguredDashboardPort(fs, path, [KIROCREW_HOME]);

  // With "Run a local gateway" off, a dashboard.url naming a port that has no
  // remote host of its own records a backend which will not run here: nothing
  // binds it and there is no host to mint a token from. A machine switched from
  // local to remote-only keeps exactly that record, so honouring it would
  // rebuild the dead end the opt-out is meant to avoid. A dashboard.url that
  // DOES name a configured crew still wins -- that is the user choosing between
  // crews rather than a leftover.
  if (!isLocalGatewayEnabled(store)) {
    if (
      configuredPort
      && isSelectablePort(configuredPort)
      && getRemoteHostConfig(store, configuredPort)?.host
    ) {
      return configuredPort;
    }
    const remotePort = remoteHostPort(store);
    if (remotePort) {
      console.log(
        "Local gateway is off; targeting the configured remote crew on port " + remotePort,
      );
      return remotePort;
    }
    // No crew is configured, so there is no better target than the local
    // record: naming the port the user configured beats naming the default.
  }

  if (configuredPort) return configuredPort;
  console.debug("No usable dashboard.url port in the data home, falling back to 5476");
  return 5476;
}

const PORT = resolvePort();
const BACKEND_URL = "http://localhost:" + PORT;

if (migrateRemoteHostConfig(store, PORT)) {
  console.log("Migrated legacy remoteHost to remoteHosts[" + PORT + "]");
}

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

function glog(line) {
  const entry = "[" + new Date().toISOString() + "] " + line + "\n";
  try {
    fs.appendFileSync(gatewayLogPath(), entry);
  } catch {
    // Never let logging break launch or recovery.
  }
  console.log("[gateway-launch] " + line);
}

function readInternalSecret() {
  // Re-read on every call. The gateway rotates this secret across restarts, so
  // caching turns a successful recovery into a stream of spurious 403s.
  for (const candidate of secretCandidates()) {
    try {
      const value = fs.readFileSync(candidate, "utf8").trim();
      if (value) return value;
    } catch {
      // Try the next platform-compatible home candidate.
    }
  }
  return "";
}

let isQuitting = false;
let desktopMetricsRecorder = null;
let windows = null;

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

  initNativeLogging({
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
  logPath: gatewayLogPath,
});

windows = createWindowLifecycle({
  electron,
  store,
  backendUrl: BACKEND_URL,
  port: PORT,
  glog,
  readInternalSecret,
  fetchLocalToken: (...args) => gateway.fetchLocalToken(...args),
  fetchRemoteToken: (...args) => gateway.fetchRemoteToken(...args),
  isQuitting: () => isQuitting,
  requestQuit,
  connectWindow: (...args) => gateway.connect(...args),
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
  const localValue = await gateway.fetchLocalToken(backendUrl);
  if (localValue) return { value: localValue, viaCookie: false };
  const { token: remoteValue } = await gateway.fetchRemoteToken(new URL(backendUrl).port);
  if (remoteValue) return { value: remoteValue, viaCookie: false };
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
    glog("uncaughtException: " + (error && error.stack ? error.stack : error));
  } catch {
    // Logging must never throw from the safety net.
  }
});
process.on("unhandledRejection", (reason) => {
  try {
    glog("unhandledRejection: " + (reason && reason.stack ? reason.stack : reason));
  } catch {
    // Same last-resort rule as uncaughtException.
  }
});

app.whenReady().then(async () => {
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
      fetchLocalToken: (...args) => gateway.fetchLocalToken(...args),
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
