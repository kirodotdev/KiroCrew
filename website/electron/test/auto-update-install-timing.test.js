// Install-handoff TIMING contract.
//
// This file exists because of a field bug that no data-shape test could catch.
// The migration to electron-updater audited every DATA contract (feed shape,
// base64 sha512, absolute urls, downgrade semantics) and pinned each one -- but
// not the TEMPORAL contract, and that is what broke.
//
// Old client: the app drove Squirrel.Mac directly, so "update-downloaded" meant
// Squirrel had ALREADY staged the bundle and quitAndInstall() was a
// millisecond-scale handoff. A 5s force-exit failsafe was harmless.
//
// New client: because autoInstallOnAppQuit=false (required -- the Python gateway
// must be stopped before any swap), electron-updater WITHHOLDS the downloaded
// zip from Squirrel until install time. quitAndInstall() returns immediately
// while Squirrel is still pulling ~350MB over the loopback proxy, unpacking and
// verifying it. The same 5s exit then landed mid-handoff: staged app on disk,
// ShipIt never armed, user relaunched into the OLD version with no error.
// Observed on 0.1.2-nightly.20260729t073648.
const { test, mock } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

function makeDeps({ appVersion = "1.0.0", withNative = true } = {}) {
  const calls = { quitAndInstall: [], exit: 0 };
  const handlers = {};
  const nativeHandlers = {};
  const autoUpdater = {
    setFeedURL: () => {},
    checkForUpdates: async () => {},
    downloadUpdate: async () => {},
    quitAndInstall: (...a) => calls.quitAndInstall.push(a),
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: () => {},
      removeListener: () => {},
      exit: () => { calls.exit += 1; },
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: () => {},
    log: { info: () => {}, warn: () => {}, error: () => {} },
    ...(withNative
      ? {
        nativeAutoUpdater: {
          once: (ev, fn) => { nativeHandlers[ev] = fn; },
        },
      }
      : { nativeAutoUpdater: null }),
  };
  return {
    deps,
    calls,
    emit: (ev, p) => handlers[ev] && handlers[ev](p),
    fireNative: (ev) => nativeHandlers[ev] && nativeHandlers[ev](),
    hasNativeListener: (ev) => typeof nativeHandlers[ev] === "function",
  };
}

test("TEMPORAL: the app is NOT force-exited while the installer is still working", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { deps, calls, emit, hasNativeListener } = makeDeps();
    const u = initAutoUpdate(deps);
    emit("update-downloaded", { version: "1.1.0" });
    await u.install();

    assert.strictEqual(calls.quitAndInstall.length, 1, "install must dispatch quitAndInstall");
    assert.ok(
      hasNativeListener("before-quit-for-update"),
      "the failsafe must wait for before-quit-for-update, the only proof the installer took over",
    );

    // Squirrel's loopback fetch + unpack of a real build takes far longer than
    // the old 5s budget. Exiting here destroys the update.
    mock.timers.tick(60_000);
    assert.strictEqual(
      calls.exit,
      0,
      "force-exited before the installer took over -- this is the field bug: staged app on disk, ShipIt never armed",
    );
  } finally {
    mock.timers.reset();
  }
});

test("TEMPORAL: once the installer HAS taken over, the failsafe still guarantees exit", async () => {
  // The failsafe's original purpose is real: ShipIt aborts the swap with
  // "App Still Running Error" (Code=-9) if anything keeps the process alive, and
  // the user then silently relaunches the OLD version. Deferring it must not
  // discard it.
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { deps, calls, emit, fireNative } = makeDeps();
    const u = initAutoUpdate(deps);
    emit("update-downloaded", { version: "1.1.0" });
    await u.install();

    fireNative("before-quit-for-update");
    assert.strictEqual(calls.exit, 0, "must not exit instantly -- give the quit a chance to complete");
    mock.timers.tick(5_000);
    assert.strictEqual(calls.exit, 1, "a process still alive after the handoff must be forced out");
  } finally {
    mock.timers.reset();
  }
});

test("TEMPORAL: with no native updater surface, the timer fallback is kept", async () => {
  // Outside an Electron runtime there is nothing to observe, so losing the
  // guarantee entirely would be worse than the timer.
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { deps, calls, emit } = makeDeps({ withNative: false });
    const u = initAutoUpdate(deps);
    emit("update-downloaded", { version: "1.1.0" });
    await u.install();
    mock.timers.tick(5_000);
    assert.strictEqual(calls.exit, 1);
  } finally {
    mock.timers.reset();
  }
});

// --------------------------------------------------------------------------
// Library contract: WHERE the expensive work happens.
//
// Asserted against the installed electron-updater source, so a version bump that
// moves Squirrel's fetch fails CI instead of resurfacing the field bug.
// --------------------------------------------------------------------------

test("TEMPORAL contract: MacUpdater defers Squirrel's fetch to install time when autoInstallOnAppQuit is false", () => {
  const src = require("fs").readFileSync(
    require.resolve("electron-updater/out/MacUpdater.js"),
    "utf8",
  );

  // At DOWNLOAD time the native fetch is gated on autoInstallOnAppQuit...
  const downloadGate = /if \(this\.autoInstallOnAppQuit\)\s*\{\s*[\s\S]{0,200}?nativeUpdater\.checkForUpdates\(\)/;
  assert.match(
    src,
    downloadGate,
    "MacUpdater no longer gates the download-time Squirrel fetch on autoInstallOnAppQuit -- re-derive when the expensive work happens before trusting the exit failsafe",
  );

  // ...and quitAndInstall starts it when that flag is false, i.e. AT INSTALL TIME.
  const installStart = /if \(!this\.autoInstallOnAppQuit\)\s*\{[\s\S]{0,400}?nativeUpdater\.checkForUpdates\(\)/;
  assert.match(
    src,
    installStart,
    "MacUpdater no longer starts Squirrel's fetch inside quitAndInstall -- the install-time work may have moved, so the handoff assumptions need re-checking",
  );
});

test("TEMPORAL contract: MacUpdater quits the app itself once the update is staged", () => {
  // This is why the failsafe can stay disarmed on darwin: the library, not our
  // timer, ends the process when the install is genuinely armed.
  const src = require("fs").readFileSync(
    require.resolve("electron-updater/out/MacUpdater.js"),
    "utf8",
  );
  assert.match(src, /handleUpdateDownloaded\(\)\s*\{[\s\S]{0,300}?nativeUpdater\.quitAndInstall\(\)/);
});

// --------------------------------------------------------------------------
// Library contract: WHY autoInstallOnAppQuit is platform-dependent.
//
// configureUpdater sets it TRUE on darwin and FALSE elsewhere. That looks like
// an inconsistency and will invite a "cleanup" unless the reason is pinned:
// electron-updater gives the one flag two unrelated meanings, decided by which
// base class the platform's updater extends.
// --------------------------------------------------------------------------

function libSrc(name) {
  return require("fs").readFileSync(
    require.resolve(`electron-updater/out/${name}.js`),
    "utf8",
  );
}

test("PLATFORM contract: MacUpdater extends AppUpdater, so darwin has NO install-on-quit handler", () => {
  assert.match(
    libSrc("MacUpdater"),
    /class MacUpdater extends AppUpdater_1\.AppUpdater/,
    "MacUpdater's base class changed -- if it now extends BaseUpdater it inherits addQuitHandler(), a SECOND install-on-quit path alongside deferredInstallOnQuit that does not stop the gateway",
  );
  // The quit handler lives in BaseUpdater, which only the other platforms extend.
  assert.match(libSrc("BaseUpdater"), /addQuitHandler\(\)\s*\{/);
  assert.match(libSrc("AppImageUpdater"), /class AppImageUpdater extends BaseUpdater_1\.BaseUpdater/);
  assert.doesNotMatch(
    libSrc("MacUpdater"),
    /addQuitHandler/,
    "MacUpdater gained a quit handler -- re-derive whether autoInstallOnAppQuit=true is still safe on darwin",
  );
});

test("PLATFORM contract: BaseUpdater's quit install is gated on autoInstallOnAppQuit (so FALSE is load-bearing off darwin)", () => {
  assert.match(
    libSrc("BaseUpdater"),
    /addQuitHandler\(\)\s*\{[\s\S]{0,120}?!this\.autoInstallOnAppQuit/,
    "BaseUpdater no longer gates its quit handler on autoInstallOnAppQuit -- Linux/Windows could install on quit without stopping the gateway",
  );
});

test("CONSENT contract: install-on-quit cannot fire without a consented download", () => {
  // autoDownload=false means nothing is fetched until the user clicks Download.
  // install() then refuses when no file was downloaded, so the quit handler can
  // only ever act on an update the user already consented to.
  assert.match(
    libSrc("BaseUpdater"),
    /if \(installerPath == null \|\| downloadedFileInfo == null\) \{[\s\S]{0,200}?No update filepath provided/,
    "BaseUpdater.install() no longer bails without a downloaded file -- re-check whether install-on-quit can fire on an unconsented update",
  );
});

test("PLATFORM contract: the loopback proxy is macOS-only (no deferred bulk work elsewhere)", () => {
  // Squirrel.Mac only accepts work via a feed URL, so electron-updater has to
  // impersonate a server to hand it a local file. AppImage renames the file and
  // NSIS spawns an installer -- neither re-transfers anything at install time.
  assert.match(libSrc("MacUpdater"), /createServer/);
  for (const name of ["AppImageUpdater", "NsisUpdater", "BaseUpdater"]) {
    assert.doesNotMatch(
      libSrc(name),
      /createServer|127\.0\.0\.1/,
      `${name} gained a loopback server -- it may now defer bulk work to install time like MacUpdater did`,
    );
  }
});

test("STAGING contract: the download-time Squirrel fetch stays gated, so lazy staging is reachable", () => {
  // This is the property the safety invariant rests on. autoInstallOnAppQuit is
  // false everywhere (configureUpdater), which means MacUpdater must take the
  // branch that resolves WITHOUT telling Squirrel about the zip. If upstream
  // ever stages unconditionally, Squirrel arms ShipIt at download time and the
  // bundle can be swapped by any process death -- including exits that skip our
  // gateway teardown -- and there is no API to un-arm it.
  //
  // Why a source assertion and not a behavioural one: the arming happens in
  // Squirrel.Mac (SQRLUpdater prepareUpdateForInstallation writes
  // ShipItState.plist and launches ShipIt), which is native code we cannot
  // observe from node. The reachable proxy is that our flag still gates the call.
  const src = libSrc("MacUpdater");
  const gated = /if \(this\.autoInstallOnAppQuit\)\s*\{[\s\S]{0,200}?nativeUpdater\.checkForUpdates\(\)[\s\S]{0,80}?\}\s*else\s*\{/;
  assert.match(
    src,
    gated,
    "MacUpdater may now hand Squirrel the zip at download time regardless of autoInstallOnAppQuit -- that ARMS ShipIt, so re-derive whether every quit path stops the gateway first",
  );
});

test("STAGING contract: nothing in electron-updater can un-arm a staged Squirrel update", () => {
  // Documents WHY the flag is the control point rather than something we could
  // undo later: closeServerIfExists only tears down the loopback proxy. If a
  // real un-arm API ever appears, eager staging becomes revisitable.
  const src = libSrc("MacUpdater");
  assert.match(src, /closeServerIfExists\(\)\s*\{/);
  assert.doesNotMatch(
    src,
    /cancelUpdate|unstage|abortInstall|clearShipIt/i,
    "MacUpdater gained something that looks like an un-arm hook -- if a staged update can be cancelled, revisit eager staging (and the retraction path)",
  );
});
