// The electron-updater feed lane: consent and auto-download, re-checks and
// the background poll, the strict install order, the update nudge, and the
// lifecycle edge cases around an install dispatch. The shared initAutoUpdate
// fixture lives in auto-update-test-helpers.js.

const { test } = require("node:test");
const assert = require("node:assert");
const { initAutoUpdate } = require("../auto-update");

const { makeDeps } = require("./auto-update-test-helpers");

// ---------------------------------------------------------------------------
// Consent flow with the electron-updater event shape. The library's
// autoDownload stays false on every path, so 'update-available' is always a
// DISCOVERY event; whether a download follows it is read per discovery from
// getAutoDownloadPreference(). The module defaults that dep to FALSE, so the
// cases below exercise the consent path with no extra wiring, and the
// auto-download cases further down opt in explicitly.
// ---------------------------------------------------------------------------

test("'update-available' surfaces 'found' and does NOT call downloadUpdate (discovery never downloads)", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  await u.check();
  assert.strictEqual(calls.checkForUpdates, 1);
  emit("update-available", { version: "1.1.0", releaseNotes: "Fixes things", releaseDate: "2026-07-28T00:00:00Z" });
  assert.strictEqual(calls.downloadUpdate, 0, "discovery must never download");
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "'found' state must be emitted");
  assert.strictEqual(found.version, "1.1.0");
  assert.strictEqual(found.notes, "Fixes things");
  assert.strictEqual(found.pubDate, "2026-07-28T00:00:00Z");
});

test("download() is the consent gate: it alone calls downloadUpdate", async () => {
  const { deps, calls, emit, stateNames } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.strictEqual(calls.downloadUpdate, 0);
  await u.download();
  assert.strictEqual(calls.downloadUpdate, 1);
  assert.ok(stateNames().includes("downloading"));
});

// ---------------------------------------------------------------------------
// Auto-download (the product default, wired from main.js). Discovery proceeds
// straight to a download; the INSTALL is still deferred to the next quit by the
// existing update-downloaded handler, so nothing swaps the bundle under a user
// mid-session. The two library flags are unchanged on this path -- that is the
// point, and it is asserted rather than assumed.
// ---------------------------------------------------------------------------

test("auto-download ON: 'update-available' downloads without a consent call", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0", releaseNotes: "Fixes things" });
  assert.strictEqual(calls.downloadUpdate, 1, "auto-download must fetch on discovery");
  // 'found' still precedes 'downloading': the renderer has to learn WHICH
  // version is coming before the progress card replaces the card naming it.
  const order = states.map((s) => s.state);
  assert.ok(
    order.indexOf("found") !== -1 && order.indexOf("found") < order.indexOf("downloading"),
    `'found' must be emitted before 'downloading', got ${JSON.stringify(order)}`,
  );
  assert.strictEqual(states.find((s) => s.state === "found").version, "1.1.0");
});

test("auto-download ON does NOT touch the two library policy flags", async () => {
  const { deps, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  // autoDownload=true would fetch inside checkForUpdates, bypassing the one
  // guarded entry point the preference can actually switch off.
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "library autoDownload must stay false");
  // autoInstallOnAppQuit=true is the dangerous one: on darwin it stages eagerly,
  // which ARMS ShipIt to swap the bundle on ANY exit -- including exits that
  // skip the gateway teardown -- and cannot be un-armed, so it also defeats
  // release retraction. Auto-download must never imply it.
  assert.strictEqual(deps.autoUpdater.autoInstallOnAppQuit, false, "auto-download must not arm install-on-quit");
});

test("auto-download ON: an already-staged version is not re-downloaded", async () => {
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  emit("update-downloaded", { version: "1.1.0" });
  assert.strictEqual(calls.downloadUpdate, 1);
  // The 4-hourly poll re-reports the same version for the rest of the session,
  // because the RUNNING version never changes. Without the staged-version
  // short-circuit this would re-fetch the same bytes every four hours.
  emit("update-available", { version: "1.1.0" });
  assert.strictEqual(calls.downloadUpdate, 1, "a staged version must not be re-downloaded");
});

test("auto-download ON: a superseding version replaces the stale stage", async () => {
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  emit("update-downloaded", { version: "1.1.0" });
  emit("update-available", { version: "1.2.0" });
  assert.strictEqual(calls.downloadUpdate, 2, "the newer build must be fetched, not the stale stage installed");
});

test("auto-download OFF is the opt-out and still only discovers", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => false;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.strictEqual(calls.downloadUpdate, 0, "the opt-out must hold");
  assert.ok(states.some((s) => s.state === "found"), "the nudge must survive the opt-out");
});

test("a throwing preference reader falls back to consent, not to downloading", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => { throw new Error("store unreadable"); };
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.strictEqual(calls.downloadUpdate, 0, "an unreadable preference must never read as consent");
  assert.ok(states.some((s) => s.state === "found"), "discovery must still be surfaced");
});

test("notifyUpdateFound is told which mode was chosen", async () => {
  const seen = [];
  const { deps, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  deps.notifyUpdateFound = (version, opts) => seen.push([version, opts]);
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  // main.js branches its notification copy on this: telling the user to go to
  // About and download, when the download is already running, is the one wrong
  // thing the nudge can say.
  assert.deepStrictEqual(seen, [["1.1.0", { autoDownload: true }]]);
});

test("getInfo reports the auto-download preference for the About toggle", () => {
  const { deps } = makeDeps({ appVersion: "1.0.0" });
  deps.getAutoDownloadPreference = () => true;
  assert.strictEqual(initAutoUpdate(deps).getInfo().autoDownload, true);
  const off = makeDeps({ appVersion: "1.0.0" });
  off.deps.getAutoDownloadPreference = () => false;
  assert.strictEqual(initAutoUpdate(off.deps).getInfo().autoDownload, false);
});

test("getInfo reports the actual OS and architecture in About", () => {
  for (const [osPlatform, osArch, expected] of [
    ["win32", "x64", "win32-x64"],
    ["darwin", "arm64", "darwin-arm64"],
    ["linux", "arm64", "linux-arm64"],
  ]) {
    const { deps } = makeDeps({ osPlatform });
    deps.osArch = osArch;
    assert.strictEqual(initAutoUpdate(deps).getInfo().platform, expected);
  }
});

test("download() with nothing discovered checks first instead of blind-downloading", async () => {
  const { deps, calls } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.download();
  assert.strictEqual(calls.downloadUpdate, 0, "no consent target yet -- must not download");
  assert.strictEqual(calls.checkForUpdates, 1, "must fall back to discovery");
});

test("'download-progress' surfaces 'downloading' with the percent", () => {
  const { deps, emit, states } = makeDeps();
  initAutoUpdate(deps);
  emit("download-progress", { percent: 42.5, bytesPerSecond: 1024 });
  const s = states.find((x) => x.state === "downloading");
  assert.ok(s, "'downloading' state must be emitted");
  assert.strictEqual(s.percent, 42.5);
});

test("'update-downloaded' surfaces 'downloaded' and arms install-on-quit", () => {
  const { deps, emit, states, appOnce } = makeDeps();
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "notes" });
  const s = states.find((x) => x.state === "downloaded");
  assert.ok(s, "'downloaded' state must be emitted");
  assert.strictEqual(s.version, "1.1.0");
  assert.strictEqual(s.notes, "notes");
  // UI-driven mode still installs on a natural quit if the user picks Later.
  assert.ok(appOnce.some((c) => c.ev === "before-quit"), "deferred install must be armed");
});

test("release-notes arrays ({version,note}[] feed shape) are flattened", () => {
  const { deps, emit, states } = makeDeps();
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: [{ note: "first" }, { note: "second" }] });
  const s = states.find((x) => x.state === "downloaded");
  assert.strictEqual(s.notes, "first\n\nsecond");
});

test("check failure surfaces 'error' instead of throwing", async () => {
  const { deps, emit, states, stateNames } = makeDeps();
  deps.autoUpdater.checkForUpdates = async () => { throw new Error("feed HTTP 403"); };
  const u = initAutoUpdate(deps);
  await u.check(); // must not reject
  assert.ok(stateNames().includes("error"));
  assert.ok(states.find((s) => s.state === "error").message.includes("feed HTTP 403"));
  // A later updater 'error' event is also surfaced.
  emit("error", new Error("boom"));
  assert.ok(states.filter((s) => s.state === "error").length >= 2);
});

test("'update-not-available' surfaces 'not-available'", async () => {
  const { deps, emit, stateNames } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-not-available");
  assert.ok(stateNames().includes("not-available"));
});

// ---------------------------------------------------------------------------
// Re-check / re-click semantics: a manual check is never a silent no-op, and
// an in-flight download is never restarted underneath itself.
// ---------------------------------------------------------------------------

test("re-check with a staged download consults the feed and RE-SURFACES 'downloaded' when the stage is still latest (no dead button)", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "notes" });
  states.length = 0;
  await u.check();
  // The check MUST consult the feed even with a stage in hand -- short-circuiting
  // here would pin the user to a stale stage when a newer version ships
  // mid-session. What it must NOT do is re-download.
  assert.strictEqual(calls.checkForUpdates, 1);
  assert.strictEqual(calls.downloadUpdate, 0);
  // Feed still reports the staged version -> re-surface the install prompt.
  emit("update-available", { version: "1.1.0", releaseNotes: "notes" });
  const s = states.find((x) => x.state === "downloaded");
  assert.ok(s, "staged version must be re-surfaced");
  assert.strictEqual(s.version, "1.1.0");
  assert.strictEqual(calls.downloadUpdate, 0, "must not re-download an already-staged version");
});

test("a NEWER version discovered while one is staged supersedes the stale stage", async () => {
  const { deps, calls, emit, states, stateNames } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "old" });
  assert.strictEqual(u.isReady(), true);
  states.length = 0;
  await u.check();
  // Feed has moved on: 1.2.0 is now latest. The staged 1.1.0 must be discarded
  // and re-offered as a fresh find, NOT installed as if it were current.
  emit("update-available", { version: "1.2.0", releaseNotes: "new" });
  assert.strictEqual(u.isReady(), false, "stale stage must be discarded");
  const found = states.find((x) => x.state === "found");
  assert.ok(found, "the newer version must be surfaced as a fresh find");
  assert.strictEqual(found.version, "1.2.0");
  assert.ok(
    !stateNames().includes("downloaded"),
    "must not re-surface the superseded stage as installable",
  );
  // Consent now downloads the NEWER build.
  await u.download();
  assert.strictEqual(calls.downloadUpdate, 1);
});

// ---------------------------------------------------------------------------
// Background poll with a staged update. The supersede handling above is only
// reachable if a check actually RUNS while the stage is armed -- and the only
// check most users ever get is the background poll. A poll gated on
// !updateReady makes that path unreachable: the app sits on its stale stage
// for the rest of the session, the user installs a superseded build, and is
// re-prompted immediately after relaunch. These tests drive the REAL interval
// with node:test mock timers, so a regression on the timer wiring itself (not
// just on safeCheck's internals) fails here.
// ---------------------------------------------------------------------------

test("the background poll invokes checkForUpdates even while an update is staged", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  initAutoUpdate(deps);
  // Drain the 30s launch check first so the poll's contribution is isolated,
  // and flush its microtasks so safeCheck's `checking` flag is released.
  t.mock.timers.tick(30 * 1000);
  await new Promise((r) => setImmediate(r));
  // Stage an update: this is the state the old `if (!updateReady)` guard
  // silenced the poll in.
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "old" });
  const before = calls.checkForUpdates;
  t.mock.timers.tick(4 * 60 * 60 * 1000); // one full poll interval
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    calls.checkForUpdates,
    before + 1,
    "the poll must consult the feed with a stage armed -- skipping pins the user to the stale stage",
  );
});

test("poll-path supersede end-to-end: poll fires -> NEWER version found -> stage discarded ('found', not 'downloaded')", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { deps, calls, emit, states, stateNames } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  t.mock.timers.tick(30 * 1000); // drain the launch check
  await new Promise((r) => setImmediate(r));
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "old" });
  assert.strictEqual(u.isReady(), true, "precondition: an update is staged");
  states.length = 0;
  const before = calls.checkForUpdates;
  t.mock.timers.tick(4 * 60 * 60 * 1000); // the poll fires with the stage armed
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(calls.checkForUpdates, before + 1, "poll must reach the feed");
  // The feed answers with a NEWER version than the stage.
  emit("update-available", { version: "1.2.0", releaseNotes: "new" });
  assert.strictEqual(u.isReady(), false, "the stale stage must be discarded");
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "the newer version must be surfaced as a fresh find");
  assert.strictEqual(found.version, "1.2.0");
  assert.ok(
    !stateNames().includes("downloaded"),
    "the superseded stage must not be re-surfaced as installable",
  );
  assert.strictEqual(calls.downloadUpdate, 0, "discovery via the poll must never download");
});

test("re-check and re-click while a download is in flight report progress instead of restarting", async () => {
  const { deps, calls, emit, states, stateNames } = makeDeps();
  const pending = [];
  deps.autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return new Promise((resolve) => pending.push(resolve));
  };
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  const dl = u.download(); // in flight -- do not await yet
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(calls.downloadUpdate, 1);
  states.length = 0;
  // Impatient re-check AND re-click mid-download: neither may restart the
  // updater flow underneath the running download.
  await u.check();
  await u.download();
  assert.strictEqual(calls.checkForUpdates, 1);
  assert.strictEqual(calls.downloadUpdate, 1);
  assert.ok(stateNames().includes("downloading"));
  // Completion clears the flag and surfaces install.
  emit("update-downloaded", { version: "1.1.0" });
  assert.ok(stateNames().includes("downloaded"));
  pending.forEach((resolve) => resolve());
  await dl;
});

test("updater 'error' clears the in-flight download so consent can retry", async () => {
  const { deps, calls, emit, stateNames } = makeDeps();
  const pending = [];
  deps.autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return new Promise((resolve) => pending.push(resolve));
  };
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  const dl1 = u.download(); // in flight -- resolved at the end
  await new Promise((r) => setImmediate(r));
  emit("error", new Error("network dropped"));
  assert.ok(stateNames().includes("error"));
  // The flag is cleared: a new consent click re-engages the download.
  const dl2 = u.download();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(calls.downloadUpdate, 2);
  pending.forEach((resolve) => resolve());
  await Promise.all([dl1, dl2]);
});

// ---------------------------------------------------------------------------
// install(): STRICT ORDER -- stopGateway must complete BEFORE quitAndInstall.
// A live gateway child during the bundle swap can leave a half-replaced app.
// ---------------------------------------------------------------------------

test("install() awaits stopGateway BEFORE quitAndInstall (strict order)", async () => {
  const { deps, emit } = makeDeps();
  const events = [];
  deps.stopGateway = async () => {
    events.push("stopGateway:begin");
    // Real async gap: if install() failed to await, quitAndInstall would be
    // recorded between begin and done and the deepStrictEqual below fails.
    await new Promise((r) => setTimeout(r, 20));
    events.push("stopGateway:done");
  };
  deps.autoUpdater.quitAndInstall = (...args) => { events.push(`quitAndInstall(${args.join(",")})`); };
  const u = initAutoUpdate(deps);
  // install() now REQUIRES a staged update (an unstaged install would hit
  // MacUpdater's wait-for-Squirrel branch and be killed by the failsafe), so
  // stage one first -- this test is about the ORDER of the install steps.
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();
  assert.deepStrictEqual(events, [
    "stopGateway:begin",
    "stopGateway:done",
    // isSilent=false, isForceRunAfter=true: relaunch the app after the swap.
    "quitAndInstall(false,true)",
  ]);
});

test("install() proceeds to quitAndInstall even when stopGateway errors (still in order)", async () => {
  const { deps, emit } = makeDeps();
  const events = [];
  deps.stopGateway = async () => {
    events.push("stopGateway:threw");
    throw new Error("gateway already dead");
  };
  deps.autoUpdater.quitAndInstall = () => events.push("quitAndInstall");
  const u = initAutoUpdate(deps);
  // install() now REQUIRES a staged update (an unstaged install would hit
  // MacUpdater's wait-for-Squirrel branch and be killed by the failsafe), so
  // stage one first -- this test is about the ORDER of the install steps.
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();
  assert.deepStrictEqual(events, ["stopGateway:threw", "quitAndInstall"]);
});

// #10282: a feed check already in flight when the user clicks Install can return
// a NEWER build and fire an automatic supersede-download from the
// update-available handler WHILE the install dispatch is stopping the gateway.
// startDownload must refuse during the install window. Without the guard it sets
// `downloading = true` over the top of `installing`, and the post-stopGateway
// error handler then derives phase "download" instead of "install" and skips the
// install-phase abort that restores the stopped gateway -- a dead dashboard.
// This test FAILS on main (no guard: downloadUpdate runs) and passes with it.
test("an automatic download fired during an install dispatch is refused (#10282)", async () => {
  const { deps, calls, emit } = makeDeps();
  deps.getAutoDownloadPreference = () => true; // auto-download on
  // Park the install dispatch inside stopGateway with `installing` already set,
  // so the automatic download below arrives mid-install.
  let releaseStop;
  const stopEntered = new Promise((entered) => {
    deps.stopGateway = () => {
      entered();
      return new Promise((r) => { releaseStop = r; });
    };
  });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" }); // stage a build so install proceeds
  const installDone = u.install();                 // do NOT await: parks in stopGateway
  await stopEntered;                               // installing === true now
  // A check already in flight returns a NEWER build; its update-available
  // handler fires startDownload({automatic:true}) during the install window.
  emit("update-available", { version: "1.2.0" });
  assert.strictEqual(
    calls.downloadUpdate,
    0,
    "an automatic download must not start during an install dispatch -- it would corrupt the install-phase error attribution and leave the gateway stopped",
  );
  releaseStop();          // let the install dispatch finish cleanly
  await installDone;
});

test("install path arms a force-exit failsafe after quitAndInstall (app-still-running guard)", async () => {
  const { deps, emit } = makeDeps();
  const events = [];
  deps.app.exit = (code) => events.push(`exit:${code}`);
  deps.autoUpdater.quitAndInstall = () => events.push("quitAndInstall");
  // Capture the failsafe timer instead of waiting 5s of wall clock.
  const realSetTimeout = global.setTimeout;
  let failsafe = null;
  global.setTimeout = (fn, ms, ...rest) => {
    if (ms === 5000) { failsafe = fn; return { unref: () => {} }; }
    return realSetTimeout(fn, ms, ...rest);
  };
  try {
    const u = initAutoUpdate(deps);
  // install() now REQUIRES a staged update (an unstaged install would hit
  // MacUpdater's wait-for-Squirrel branch and be killed by the failsafe), so
  // stage one first -- this test is about the ORDER of the install steps.
  emit("update-downloaded", { version: "1.1.0" });
    await u.install();
  } finally {
    global.setTimeout = realSetTimeout;
  }
  assert.deepStrictEqual(events, ["quitAndInstall"]);
  assert.ok(failsafe, "failsafe timer must be armed");
  failsafe(); // simulate the app still being alive 5s later
  assert.deepStrictEqual(events, ["quitAndInstall", "exit:0"]);
});

// ---------------------------------------------------------------------------
// Update nudge: 'found' fires notifyUpdateFound (discovery-only); up-to-date
// and error paths never do. Once-per-version dedupe lives in main.js.
// ---------------------------------------------------------------------------

test("found fires notifyUpdateFound with the discovered version", async () => {
  const nudges = [];
  const { deps, emit } = makeDeps({ appVersion: "1.0.0" });
  deps.notifyUpdateFound = (v) => nudges.push(v);
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.deepStrictEqual(nudges, ["1.1.0"]);
});

test("up-to-date and failed checks never nudge", async () => {
  const nudges = [];
  const same = makeDeps({ appVersion: "1.0.0" });
  same.deps.notifyUpdateFound = (v) => nudges.push(v);
  const u1 = initAutoUpdate(same.deps);
  await u1.check();
  same.emit("update-not-available");
  const err = makeDeps({ appVersion: "1.0.0" });
  err.deps.notifyUpdateFound = (v) => nudges.push(v);
  err.deps.autoUpdater.checkForUpdates = async () => { throw new Error("offline"); };
  const u2 = initAutoUpdate(err.deps);
  await u2.check();
  assert.deepStrictEqual(nudges, []);
});

test("a throwing nudge callback does not break discovery ('found' still emitted)", async () => {
  const { deps, emit, stateNames } = makeDeps({ appVersion: "1.0.0" });
  deps.notifyUpdateFound = () => { throw new Error("boom"); };
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.ok(stateNames().includes("found"), `states: ${stateNames()}`);
});

// ---------------------------------------------------------------------------
// Review-round fixes. Each was a reachable defect found by the local review
// gate, so each gets a test that fails if the fix is undone.
// ---------------------------------------------------------------------------

test("a feed reporting up-to-date DISARMS a staged update (retraction path)", () => {
  // Retraction repoints the feed at an older/other version. With a stage armed,
  // "no update" must discard it -- otherwise the WITHDRAWN build still installs
  // on the next quit, because deferredInstallOnQuit only checks updateReady.
  const { deps, emit, appOnce, appRemoved } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "withdrawn" });
  assert.strictEqual(u.isReady(), true, "precondition: an update is staged");
  assert.ok(appOnce.some((a) => a.ev === "before-quit"), "precondition: quit hook armed");

  emit("update-not-available", { version: "1.0.0" });
  assert.strictEqual(u.isReady(), false, "a retracted stage must be discarded");
  assert.ok(
    appRemoved.some((a) => a.ev === "before-quit"),
    "the before-quit install hook must be disarmed, or the withdrawn build installs on quit",
  );
});

test("install() with nothing staged is refused, so the force-exit failsafe is never armed", async () => {
  // MacUpdater.quitAndInstall() does NOT install when Squirrel has not yet
  // consumed the zip -- it registers a listener and waits. Arming
  // forceExitFailsafe there kills the process 5s later, mid-fetch, and the app
  // dies without swapping or relaunching.
  const { deps, calls, states } = makeDeps({ appVersion: "1.0.0" });
  const stopped = [];
  deps.stopGateway = async () => { stopped.push(1); };
  const u = initAutoUpdate(deps);
  await u.install();
  assert.strictEqual(calls.quitAndInstall.length, 0, "must not quitAndInstall with nothing staged");
  assert.strictEqual(stopped.length, 0, "must not stop the gateway for an install that cannot proceed");
  assert.ok(states.length > 0, "must report state rather than silently no-op");
});

test("install() proceeds once an update IS staged", async () => {
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0" });
  await u.install();
  assert.strictEqual(calls.quitAndInstall.length, 1);
});

test("BLOCKING-fix contract: package.json declares a publish entry so app-update.yml is emitted", () => {
  // electron-updater's downloadUpdate() -> getOrCreateDownloadHelper() awaits
  // configOnDisk -> readFile(app-update.yml). electron-builder only writes that
  // file when a publish config exists (its repository-info fallback resolves
  // null here). Without it, DISCOVERY works and every consented download throws
  // ENOENT -- a dead updater that no unit test with a fake autoUpdater can see.
  const pkg = require("../package.json");
  const publish = pkg.build && pkg.build.publish;
  assert.ok(Array.isArray(publish) && publish.length > 0, "build.publish must be a non-empty array");
  assert.strictEqual(publish[0].provider, "generic");
  assert.match(publish[0].url, /^https:\/\//, "baked publish url must be https");
});

test("the poll skips while an install is in flight (dispatched, gateway stopping)", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  // Hold the gateway stop open so the poll interval can fire inside the
  // dispatch window (installing === true, quitAndInstall not yet reached).
  let releaseGateway;
  deps.stopGateway = () => new Promise((resolve) => { releaseGateway = resolve; });
  const u = initAutoUpdate(deps);
  t.mock.timers.tick(30 * 1000); // drain the launch check
  await new Promise((r) => setImmediate(r));
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const installPromise = u.install(); // dispatch: blocks awaiting stopGateway
  await new Promise((r) => setImmediate(r));
  const before = calls.checkForUpdates;
  t.mock.timers.tick(4 * 60 * 60 * 1000); // poll interval elapses mid-install
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    calls.checkForUpdates,
    before,
    "a poll during an install dispatch must not consult the feed -- a check "
      + "failure in that window is classified as an install failure and would "
      + "trigger the host's gateway recovery during the bundle swap",
  );
  releaseGateway();
  await installPromise;
});

test("an installer failure that arrives while a check is in flight fires onInstallFailed and classifies as an install failure", async () => {
  // GPT round-7 finding: `checking` outranking `installing` in the phase
  // derivation labelled a genuine installer failure (observed live in the OTA
  // lane: a Squirrel signature rejection) as "check" whenever a check happened
  // to be in flight -- onInstallFailed never fired, and nothing restored the
  // gateway the dispatch had deliberately stopped.
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  let installFailedCalls = 0;
  deps.onInstallFailed = () => { installFailedCalls += 1; };
  // Hold the check open so it is still in flight when the install dispatches,
  // and hold the gateway stop open so the failure lands mid-dispatch.
  let rejectCheck;
  deps.autoUpdater.checkForUpdates = () => new Promise((_, reject) => { rejectCheck = reject; });
  let releaseGateway;
  deps.stopGateway = () => new Promise((resolve) => { releaseGateway = resolve; });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const checkPromise = u.check(); // checking = true, unresolved
  const installPromise = u.install(); // installing = true, awaiting stopGateway
  await new Promise((r) => setImmediate(r));
  // The installer path fails, delivered as the library's error EVENT
  // (electron-updater funnels every failure through one channel -- the phase
  // derivation is the only classifier).
  emit("error", new Error("Code signature at URL ... did not pass validation"));
  const errState = states.filter((s) => s.state === "error").pop();
  assert.ok(errState, "an error state must be emitted");
  assert.strictEqual(
    errState.phase,
    "install",
    "a failure while an install is dispatched must be reported as an install failure -- the gateway was stopped on purpose and only onInstallFailed restores it",
  );
  assert.strictEqual(installFailedCalls, 1, "host recovery must fire to restore the deliberately-stopped gateway");
  releaseGateway();
  await installPromise;
  assert.strictEqual(installFailedCalls, 1, "the dead dispatch must not run the recovery a second time");
  assert.strictEqual(calls.quitAndInstall.length, 0, "a dispatch whose install already failed must never reach quitAndInstall");
  rejectCheck(new Error("feed unreachable"));
  await checkPromise;
});

test("a check still in flight when the gateway has stopped aborts the install through the recovery path", async () => {
  // Companion to the precedence above: this abort is what guarantees no
  // install proceeds into quitAndInstall with a check outstanding, so a check
  // outcome -- a stage-invalidating response or a feed error -- can never land
  // in the middle of an actual bundle swap.
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  let installFailedCalls = 0;
  deps.onInstallFailed = () => { installFailedCalls += 1; };
  let resolveCheck;
  deps.autoUpdater.checkForUpdates = () => new Promise((resolve) => { resolveCheck = resolve; });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const checkPromise = u.check(); // checking = true, unresolved
  await u.install(); // gateway stops immediately; the check is still in flight
  assert.strictEqual(calls.quitAndInstall.length, 0, "an install must not commit while a check is in flight");
  assert.strictEqual(installFailedCalls, 1, "the abort must run the host recovery to bring the gateway back");
  const last = states[states.length - 1];
  assert.strictEqual(last.state, "error", "the renderer must learn the install did not proceed");
  assert.strictEqual(last.phase, "install", "the abort must use the install-error renderer contract");
  assert.strictEqual(last.code, "check-in-flight");
  // The dispatch is over and the stage survives: a retry once the check
  // settles must proceed.
  resolveCheck();
  await checkPromise;
  await u.install();
  assert.strictEqual(calls.quitAndInstall.length, 1, "a retry after the check settles must reach quitAndInstall");
});

test("a renderer-driven check during an install dispatch is refused, mirroring the poll gate", async () => {
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  let releaseGateway;
  deps.stopGateway = () => new Promise((resolve) => { releaseGateway = resolve; });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const installPromise = u.install();
  await new Promise((r) => setImmediate(r));
  const before = calls.checkForUpdates;
  await u.check();
  assert.strictEqual(
    calls.checkForUpdates,
    before,
    "a check during install activity must not consult the feed -- its failure would be classified as an install failure and fire gateway recovery mid-swap",
  );
  releaseGateway();
  await installPromise;
});

test("the poll also skips during a deferred install-on-quit (quitHandled, installing never set)", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { deps, calls, emit, appOnce } = makeDeps({ appVersion: "1.0.0" });
  // Hold the quit-path gateway stop open so the deferred install window stays
  // live while the poll interval elapses.
  deps.stopGateway = () => new Promise(() => {});
  initAutoUpdate(deps);
  t.mock.timers.tick(30 * 1000); // drain the launch check
  await new Promise((r) => setImmediate(r));
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  // Fire the deferred install exactly as app quit would: the before-quit
  // listener registered on update-downloaded.
  const quitHook = appOnce.find((h) => h.ev === "before-quit");
  assert.ok(quitHook, "update-downloaded must register the deferred quit install");
  quitHook.fn({ preventDefault: () => {} });
  await new Promise((r) => setImmediate(r));
  const before = calls.checkForUpdates;
  t.mock.timers.tick(4 * 60 * 60 * 1000);
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(
    calls.checkForUpdates,
    before,
    "a poll during a deferred install-on-quit must not consult the feed -- a "
      + "retraction there clears the stage under a dispatch that already "
      + "passed its guard",
  );
});

test("a stage invalidated while the gateway stops aborts the manual install and restores the gateway", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "1.0.0" });
  let installFailedCalls = 0;
  deps.onInstallFailed = () => { installFailedCalls += 1; };
  let releaseGateway;
  deps.stopGateway = () => new Promise((resolve) => { releaseGateway = resolve; });
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const installPromise = u.install(); // passes its updateReady guard, blocks on stopGateway
  await new Promise((r) => setImmediate(r));
  // A feed response that was in flight at click time now reports a
  // retraction: the handler discards the stage mid-dispatch.
  emit("update-not-available");
  releaseGateway();
  await installPromise;
  assert.strictEqual(calls.quitAndInstall.length, 0, "an invalidated stage must never reach quitAndInstall");
  assert.strictEqual(installFailedCalls, 1, "the abort must run the host recovery to bring the gateway back");
  const last = states[states.length - 1];
  assert.strictEqual(last.state, "error", "the renderer must learn the install did not proceed");
  assert.strictEqual(last.phase, "install", "the abort must use the install-error renderer contract, not a silent state swap");
});

test("a stage invalidated during a deferred quit-install quits without installing", async () => {
  const { deps, calls, emit, appOnce } = makeDeps({ appVersion: "1.0.0" });
  let quitCalls = 0;
  deps.app.quit = () => { quitCalls += 1; };
  let releaseGateway;
  deps.stopGateway = () => new Promise((resolve) => { releaseGateway = resolve; });
  initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const quitHook = appOnce.find((h) => h.ev === "before-quit");
  assert.ok(quitHook, "update-downloaded must register the deferred quit install");
  quitHook.fn({ preventDefault: () => {} });
  await new Promise((r) => setImmediate(r));
  emit("update-not-available"); // retraction lands while the gateway stops
  releaseGateway();
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(calls.quitAndInstall.length, 0, "the withdrawn build must not install on quit");
  assert.strictEqual(quitCalls, 1, "the quit the user asked for must still proceed");
});


test("a genuine install failure after a straddling check settles still fires recovery", async () => {
  const { deps, calls, emit } = makeDeps({ appVersion: "1.0.0" });
  let installFailedCalls = 0;
  deps.onInstallFailed = () => { installFailedCalls += 1; };
  let rejectCheck;
  deps.autoUpdater.checkForUpdates = () => new Promise((_, reject) => { rejectCheck = reject; });
  deps.stopGateway = async () => {};
  const u = initAutoUpdate(deps);
  emit("update-downloaded", { version: "1.1.0", releaseNotes: "n" });
  const checkPromise = u.check(); // straddles the dispatch
  const installPromise = u.install();
  await new Promise((r) => setImmediate(r));
  // The gateway stopped with the check still in flight: the dispatch aborts
  // through the recovery path rather than committing under an unsettled check.
  await installPromise;
  assert.strictEqual(calls.quitAndInstall.length, 0, "the dispatch must not commit under an unsettled check");
  assert.strictEqual(installFailedCalls, 1, "the abort must restore the gateway");
  // With no install in flight, the straddling check's own failure is a plain
  // check failure -- it must NOT fire recovery again.
  rejectCheck(new Error("feed unreachable"));
  await checkPromise;
  emit("error", new Error("feed unreachable"));
  assert.strictEqual(installFailedCalls, 1, "a check failure outside an install must not fire recovery");
  // A retry now commits, and a LATER genuine installer failure in that
  // dispatch classifies as `install` and fires recovery -- the flag was armed.
  await u.install();
  assert.strictEqual(calls.quitAndInstall.length, 1, "the retry must commit once the check has settled");
  emit("error", new Error("Squirrel could not validate the update"));
  assert.strictEqual(installFailedCalls, 2, "recovery must remain armed for a real install failure after the check settles");
});
