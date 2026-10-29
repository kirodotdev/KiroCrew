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
  const calls = { quitAndInstall: [], notifications: [], states: [], exit: 0 };
  const handlers = {};
  const nativeHandlers = {};
  // appOnceMap tracks app.once registrations by event name so tests can fire them.
  // Using a Map instead of a plain object preserves the most-recent handler per event,
  // which mirrors how EventEmitter.once + removeListener interact.
  const appOnceMap = new Map();
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
      once: (ev, fn) => { appOnceMap.set(ev, fn); },
      removeListener: (ev, fn) => { if (appOnceMap.get(ev) === fn) appOnceMap.delete(ev); },
      exit: () => { calls.exit += 1; },
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function (options) {
      calls.notifications.push(options);
      return { show: () => {} };
    },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (payload) => calls.states.push(payload),
    log: { info: () => {}, warn: () => {}, error: () => {} },
    ...(withNative
      ? {
        nativeAutoUpdater: {
          once: (ev, fn) => { nativeHandlers[ev] = fn; },
        },
      }
      : { nativeAutoUpdater: null }),
  };
  // Fire an app.once handler by event name, then remove it (mirrors once semantics).
  // Passes a mock event with preventDefault() since deferredInstallOnQuit
  // calls event.preventDefault() to hold the quit while the gateway stops.
  const fireAppOnce = (ev) => {
    const fn = appOnceMap.get(ev);
    if (fn) {
      appOnceMap.delete(ev);
      fn({ preventDefault: () => {} });
    }
  };
  // Mock BaseWindow for session-end (Electron 43: session-end is on BaseWindow, not App).
  const winOnceMap = new Map();
  const mockWindow = {
    once: (ev, fn) => { winOnceMap.set(ev, fn); },
    removeListener: (ev, fn) => { if (winOnceMap.get(ev) === fn) winOnceMap.delete(ev); },
    isDestroyed: () => false,
  };
  deps.getMainWindow = () => deps.osPlatform === "win32" ? mockWindow : null;
  const fireWindowOnce = (ev) => {
    const fn = winOnceMap.get(ev);
    if (fn) { winOnceMap.delete(ev); fn({ preventDefault: () => {} }); }
  };
  return {
    deps,
    calls,
    emit: (ev, p) => handlers[ev] && handlers[ev](p),
    fireNative: (ev) => nativeHandlers[ev] && nativeHandlers[ev](),
    hasNativeListener: (ev) => typeof nativeHandlers[ev] === "function",
    hasAppOnce: (ev) => appOnceMap.has(ev),
    fireAppOnce,
    hasWindowOnce: (ev) => winOnceMap.has(ev),
    fireWindowOnce,
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

test("Windows uses visible native progress and leaves a recovery notification", async () => {
  const { deps, calls, emit } = makeDeps();
  deps.osPlatform = "win32";
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();

  assert.deepStrictEqual(
    calls.quitAndInstall,
    [[false, true]],
    "Windows must not pass /S; the update-only NSIS path owns the visible progress and automatic relaunch",
  );
  assert.strictEqual(calls.notifications.length, 1);
  assert.match(calls.notifications[0].body, /Start menu/i);
  assert.doesNotMatch(calls.notifications[0].body, /several minutes|reopen automatically/i);
  assert.strictEqual(
    calls.states.find((state) => state.state === "downloaded").installHandoff,
    "windows-installer",
  );
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

// --------------------------------------------------------------------------
// Host coordination: the install must disarm gateway recovery BEFORE the
// gateway stops.
//
// Field incident (gateway-launch.log 2026-07-29T22:18): install stopped the
// gateway intentionally; the liveness watchdog counted 3 failed probes and
// "recovered" it 20s later -- respawning the gateway into the middle of the
// bundle swap, reloading the page, and re-arming the install button. ORDER is
// the contract: fired after stopGateway would leave the race open.
// --------------------------------------------------------------------------

test("install() fires onInstallDispatched BEFORE stopping the gateway", async () => {
  const order = [];
  const { deps, emit } = makeDeps();
  const updater = initAutoUpdate({
    ...deps,
    onInstallDispatched: () => order.push("dispatched"),
    stopGateway: async () => { order.push("stopGateway"); },
  });
  // Stage an update so install() passes the updateReady guard.
  emit("update-downloaded", { version: "9.9.9" });
  await updater.install();
  assert.deepStrictEqual(order.slice(0, 2), ["dispatched", "stopGateway"]);
});

test("a throwing onInstallDispatched cannot block the install", async () => {
  const order = [];
  const { deps, emit } = makeDeps();
  const updater = initAutoUpdate({
    ...deps,
    onInstallDispatched: () => { throw new Error("host hook broke"); },
    stopGateway: async () => { order.push("stopGateway"); },
  });
  emit("update-downloaded", { version: "9.9.9" });
  await updater.install();
  assert.deepStrictEqual(order, ["stopGateway"], "install must proceed past a broken advisory hook");
});

test("a FAILED install fires onInstallFailed and allows a retry", async () => {
  // GPT round-3 finding on #786: dispatch disarms gateway recovery, so a
  // Squirrel failure at handoff time (observed live in the OTA lane: signature
  // rejection) left a running app with a dead dashboard and a dead button.
  const calls = { failed: 0, stops: 0 };
  const { deps, emit } = makeDeps();
  const updater = initAutoUpdate({
    ...deps,
    onInstallDispatched: () => {},
    onInstallFailed: () => { calls.failed += 1; },
    stopGateway: async () => { calls.stops += 1; },
  });
  emit("update-downloaded", { version: "9.9.9" });
  await updater.install();
  assert.strictEqual(calls.stops, 1);

  emit("error", new Error("Code signature at URL ... did not pass validation"));
  assert.strictEqual(calls.failed, 1, "host must be told the install failed");

  // `installing` was reset, so the user can click Install Update & Restart App again.
  await updater.install();
  assert.strictEqual(calls.stops, 2, "retry must reach stopGateway again");
});

test("a failure OUTSIDE an install does not fire onInstallFailed", async () => {
  const calls = { failed: 0 };
  const { deps, emit } = makeDeps();
  initAutoUpdate({ ...deps, onInstallFailed: () => { calls.failed += 1; } });
  emit("error", new Error("HttpError: 503"));
  assert.strictEqual(calls.failed, 0, "check/download failures must not trigger gateway recovery");
});

test("a throwing onInstallFailed cannot swallow the error state", async () => {
  const states = [];
  const { deps, emit } = makeDeps();
  const updater = initAutoUpdate({
    ...deps,
    onUpdateState: (s) => states.push(s.state),
    onInstallFailed: () => { throw new Error("host hook broke"); },
  });
  emit("update-downloaded", { version: "9.9.9" });
  await updater.install();
  emit("error", new Error("swap failed"));
  assert.ok(states.includes("error"), "the UI must still hear about the failure");
});

// --------------------------------------------------------------------------
// Windows session-end notice: shutdown/restart/logout must notify the user
// that an update is waiting, because before-quit is never emitted on those paths.
//
// WHY a notice, not a quitAndInstall: the NSIS installer extracts files with
// nsisunz::Unzip directly into $INSTDIR (no backup, no rollback). Windows gives
// each process a default shutdown window of a few seconds; a ~350 MB bundle copy
// takes far longer. A mid-install kill leaves $INSTDIR with a mix of old and new
// files — the app may not launch. A Windows toast notification is safe: it is
// handled by a system service, persists in Action Center across the session
// boundary, and the user sees it at next login.
// --------------------------------------------------------------------------

test("session-end shows a notification on win32 when an update is staged", async () => {
  const { deps, calls, emit, hasWindowOnce, fireWindowOnce } = makeDeps();
  deps.osPlatform = "win32";
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "2.0.0" });

  assert.ok(hasWindowOnce("session-end"), "session-end handler must be armed after update-downloaded on win32");

  fireWindowOnce("session-end");

  // Shows a notification; does NOT call quitAndInstall (no partial-install risk).
  assert.strictEqual(calls.quitAndInstall.length, 0, "session-end must NOT call quitAndInstall: partial NSIS install is unsafe");
  assert.ok(calls.notifications.length >= 1, "a notification must be shown to inform the user");
  assert.match(
    calls.notifications[calls.notifications.length - 1].body,
    /quit.*tray|tray.*quit|quit.*menu|menu.*quit/i,
    "notification must tell the user to quit from the tray or menu",
  );
});

test("session-end handler is NOT armed on darwin", async () => {
  const { deps, emit, hasWindowOnce } = makeDeps();
  // deps.osPlatform already "darwin"
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "2.0.0" });

  assert.ok(!hasWindowOnce("session-end"), "session-end must NOT be armed on darwin");
});

test("session-end respects auto-download opt-out: no notification when auto-download is off for auto-staged update", async () => {
  // sessionEndPendingNotice mirrors deferredInstallOnQuit: when the stage is
  // automatic AND the user turned off auto-download, the notification must not
  // fire — deferredInstallOnQuit would skip the install, so "quit from tray"
  // would send the user on a futile trip.
  const { deps, calls, emit, fireWindowOnce } = makeDeps();
  deps.osPlatform = "win32";
  let autoPref = true;
  deps.getAutoDownloadPreference = () => autoPref; // live ref so amend after init works
  initAutoUpdate(deps);

  // Trigger auto-download path (update-available with pref=true sets downloadWasAutomatic).
  emit("update-available", { version: "2.0.0" });
  autoPref = false; // user turns off auto-download
  emit("update-downloaded", { version: "2.0.0" }); // stagedWasAutomatic=true

  fireWindowOnce("session-end");
  assert.strictEqual(calls.notifications.length, 0, "session-end must not notify when auto-download is off for an auto-staged update");
});

test("session-end fires when update is staged; no notification when no update", async () => {
  const { deps, calls, emit, fireWindowOnce } = makeDeps();
  deps.osPlatform = "win32";
  initAutoUpdate(deps);
  // No update-downloaded: updateReady is false.
  // session-end handler is not even registered (no update-downloaded event);
  // firing does nothing.
  fireWindowOnce("session-end");
  assert.strictEqual(calls.notifications.length, 0, "no notification without a staged update");
});

test("retraction (update-not-available) disarms the session-end handler on win32", async () => {
  const { deps, emit, hasWindowOnce } = makeDeps();
  deps.osPlatform = "win32";
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "2.0.0" });

  assert.ok(hasWindowOnce("session-end"), "precondition: session-end armed after download");

  emit("update-not-available");

  assert.ok(!hasWindowOnce("session-end"), "session-end handler must be removed on retraction");
});

test("session-end notice and deferred before-quit install are independent", async () => {
  // session-end shows a notice; the user can still quit from tray (before-quit)
  // and get the full install — both handlers remain active concurrently.
  const { deps, calls, emit, hasWindowOnce, fireWindowOnce, fireAppOnce } = makeDeps();
  deps.osPlatform = "win32";
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "2.0.0" });

  // Simulate session-end while both handlers are armed.
  fireWindowOnce("session-end");
  assert.strictEqual(calls.quitAndInstall.length, 0, "session-end must not install");
  assert.ok(calls.notifications.length >= 1, "session-end must notify");

  // before-quit is still armed; firing it should install normally.
  // deferredInstallOnQuit uses an async IIFE that awaits the freshness gate and
  // then stopGateway before calling quitAndInstall, so drain several turns of
  // the event loop after firing.
  fireAppOnce("before-quit");
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.strictEqual(calls.quitAndInstall.length, 1, "before-quit after session-end must still install");
});

test("disarm removes the session-end listener from the window it was registered on, even after the main window changes", async () => {
  const { deps, emit, hasWindowOnce } = makeDeps();
  deps.osPlatform = "win32";
  // initAutoUpdate captures the provider once, so swap what it returns, not the provider.
  const firstWindowProvider = deps.getMainWindow;
  let windowNow = null;
  deps.getMainWindow = () => windowNow || firstWindowProvider();
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "2.0.0" });
  assert.ok(hasWindowOnce("session-end"), "precondition: session-end armed on the first window");

  // The main window is recreated: getMainWindow() now returns a different object.
  windowNow = { once: () => {}, removeListener: () => {}, isDestroyed: () => false };

  emit("update-not-available");
  assert.ok(!hasWindowOnce("session-end"), "the listener on the original window must be removed, not leaked");
});

// TEMPORAL contract, quit half. Squirrel.Mac stages the update INSIDE
// quitAndInstall() (about 40s for a real build), and ShipIt is armed only when
// `before-quit-for-update` fires. A quit that lands in between -- a second Cmd+Q,
// Dock or tray Quit, a window close, a logout -- kills the stage, and the app
// reopens on the OLD version with no error. Neither install path still listens
// for before-quit at that point, so the lane must hold those quits itself.

/** An app whose before-quit listeners are real, so a quit can be fired and observed. */
function makeQuitApp() {
  const listeners = [];
  const app = {
    isPackaged: true,
    getVersion: () => "1.0.0",
    quits: 0,
    exits: 0,
    on: (ev, fn) => { if (ev === "before-quit") listeners.push({ fn, once: false }); },
    once: (ev, fn) => { if (ev === "before-quit") listeners.push({ fn, once: true }); },
    removeListener: (ev, fn) => {
      if (ev !== "before-quit") return;
      const i = listeners.findIndex((l) => l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    exit: () => { app.exits += 1; },
    quit: () => { app.quits += 1; fireQuit(); },
  };
  /** Fire before-quit the way Electron does. Returns whether the quit was prevented. */
  function fireQuit() {
    let prevented = false;
    const event = { preventDefault: () => { prevented = true; } };
    const snapshot = listeners.slice();
    for (const l of snapshot) {
      if (l.once) listeners.splice(listeners.indexOf(l), 1);
    }
    for (const l of snapshot) l.fn(event);
    return prevented;
  }
  return { app, fireQuit, listenerCount: () => listeners.length };
}

function makeQuitDeps({ osPlatform = "darwin" } = {}) {
  const base = makeDeps();
  const quit = makeQuitApp();
  const failed = { count: 0 };
  base.deps.app = quit.app;
  base.deps.osPlatform = osPlatform;
  base.deps.onInstallFailed = () => { failed.count += 1; };
  return { ...base, ...quit, failed };
}

async function until(predicate) {
  for (let i = 0; i < 200 && !predicate(); i += 1) await new Promise((r) => setImmediate(r));
  assert.ok(predicate(), "condition never became true");
}

test("TEMPORAL: a quit while Squirrel stages a manual install is held, not obeyed", async () => {
  const { deps, calls, emit, fireQuit } = makeQuitDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();
  assert.strictEqual(calls.quitAndInstall.length, 1);

  assert.strictEqual(fireQuit(), true, "a quit before the installer takes over kills the stage -- it must be held");
  assert.strictEqual(fireQuit(), true, "a second quit in the same window must be held too");
  const notices = calls.notifications.filter((n) => n.title === "Finishing the update");
  assert.strictEqual(notices.length, 1, "say once why the app did not quit");
});

test("TEMPORAL: once the installer takes over, quits are no longer held", async () => {
  const { deps, emit, fireQuit, fireNative } = makeQuitDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();

  fireNative("before-quit-for-update");
  assert.strictEqual(fireQuit(), false, "the installer's own quit follows before-quit-for-update and must go through");
});

test("TEMPORAL: an installer failure after a held quit releases the hold and replays the quit", async () => {
  const { deps, emit, fireQuit, failed, app } = makeQuitDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();

  assert.strictEqual(fireQuit(), true);
  emit("error", new Error("Code signature at URL did not pass validation"));
  assert.strictEqual(failed.count, 1, "a manual install failure still restores the gateway");
  assert.strictEqual(app.quits, 1, "the quit the user asked for while staging must be honored, not swallowed");
});

test("TEMPORAL: an installer failure with no held quit keeps the app open and stops holding", async () => {
  const { deps, emit, fireQuit, app } = makeQuitDeps();
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();

  emit("error", new Error("Code signature at URL did not pass validation"));
  assert.strictEqual(app.quits, 0, "nobody asked to quit");
  assert.strictEqual(fireQuit(), false, "after the failure a quit must go through again");
});

test("TEMPORAL: a second Cmd+Q during the quit-time install's staging is held", async () => {
  const { deps, calls, emit, fireQuit } = makeQuitDeps();
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });

  assert.strictEqual(fireQuit(), true, "the first quit is held to run the install");
  await until(() => calls.quitAndInstall.length === 1);
  assert.strictEqual(fireQuit(), true, "a second quit while Squirrel stages would leave the old version");
});

test("TEMPORAL: a wedged installer does not leave an app that can never quit", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const { deps, emit, fireQuit, app } = makeQuitDeps();
    const u = initAutoUpdate(deps);
    emit("update-downloaded", { version: "1.1.0" });
    await u.install();

    assert.strictEqual(fireQuit(), true);
    mock.timers.tick(10 * 60 * 1000);
    assert.strictEqual(app.quits, 1, "after the bound the held quit is replayed");
    assert.strictEqual(fireQuit(), false, "and quits are no longer held");
  } finally {
    mock.timers.reset();
  }
});

test("off darwin the installer's own quit is never held", async () => {
  const { deps, emit, fireQuit } = makeQuitDeps({ osPlatform: "win32" });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();
  assert.strictEqual(fireQuit(), false, "NSIS and AppImage quit the app themselves to install");
});
