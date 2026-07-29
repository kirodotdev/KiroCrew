const { test } = require("node:test");
const assert = require("node:assert");
const {
  initAutoUpdate,
  channelForFlavor,
  channelForVersion,
  resolveChannel,
  buildFeedBase,
  configureUpdater,
  DEFAULT_FEED_BASE,
  SUPPORTED_PLATFORMS,
} = require("../auto-update");

// ---------------------------------------------------------------------------
// Pure channel helpers (unchanged surface from the hand-rolled updater).
// ---------------------------------------------------------------------------

test("channelForVersion: nightly stamp -> nightly feed", () => {
  assert.strictEqual(channelForVersion("0.1.0-nightly.20260721042000"), "nightly");
});

test("channelForVersion mirrors release.yml: any non-nightly prerelease -> insider", () => {
  assert.strictEqual(channelForVersion("0.1.0-insider.1"), "insider");
  assert.strictEqual(channelForVersion("1.2.3-rc.1"), "insider");
});

test("channelForVersion: bare semver -> stable, unstamped/missing -> null", () => {
  assert.strictEqual(channelForVersion("1.2.3"), "stable");
  assert.strictEqual(channelForVersion(undefined), null);
});

test("channelForFlavor maps beta -> insider", () => {
  assert.strictEqual(channelForFlavor("beta"), "insider");
});

test("channelForFlavor maps stable -> stable", () => {
  assert.strictEqual(channelForFlavor("stable"), "stable");
});

test("channelForFlavor defaults non-beta to stable", () => {
  assert.strictEqual(channelForFlavor(undefined), "stable");
  assert.strictEqual(channelForFlavor("anything"), "stable");
});

test("resolveChannel: nightly stamp is pinned -- preference ignored", () => {
  assert.strictEqual(resolveChannel("nightly", "stable"), "nightly");
  assert.strictEqual(resolveChannel("nightly", "insider"), "nightly");
  assert.strictEqual(resolveChannel("nightly", ""), "nightly");
});

test("resolveChannel: dev (null stamp) has no lane -- preference cannot conjure one", () => {
  assert.strictEqual(resolveChannel(null, "insider"), null);
  assert.strictEqual(resolveChannel(null, ""), null);
});

test("resolveChannel: production stamps follow the preference when set", () => {
  assert.strictEqual(resolveChannel("stable", "insider"), "insider");
  assert.strictEqual(resolveChannel("insider", "stable"), "stable");
});

test("resolveChannel: no/invalid preference falls back to the stamp", () => {
  assert.strictEqual(resolveChannel("stable", ""), "stable");
  assert.strictEqual(resolveChannel("insider", undefined), "insider");
  assert.strictEqual(resolveChannel("stable", "nightly"), "stable"); // nightly is not a valid opt-in
  assert.strictEqual(resolveChannel("insider", "bogus"), "insider");
});

// ---------------------------------------------------------------------------
// buildFeedBase: the generic-provider DIRECTORY url. The trailing slash is
// load-bearing -- `new URL("latest-mac.yml", base)` REPLACES the last path
// segment when base has no trailing slash, resolving the wrong channel.
// ---------------------------------------------------------------------------

test("buildFeedBase emits the channel DIRECTORY with a trailing slash", () => {
  const url = buildFeedBase({ base: "https://cdn.example.dev/feed", channel: "insider" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/insider/");
  assert.ok(url.endsWith("/"), "trailing slash is load-bearing for the generic provider");
});

test("buildFeedBase strips trailing slashes from the base before appending", () => {
  const url = buildFeedBase({ base: "https://cdn.example.dev/feed///", channel: "stable" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/stable/");
});

test("buildFeedBase url-encodes the channel segment", () => {
  const url = buildFeedBase({ base: "https://cdn.example.dev/feed", channel: "a b" });
  assert.strictEqual(url, "https://cdn.example.dev/feed/a%20b/");
});

test("buildFeedBase defaults to the public pointer host (DEFAULT_FEED_BASE)", () => {
  assert.strictEqual(
    buildFeedBase({ channel: "nightly" }),
    "https://updates.crew.kiro.dev/feed/nightly/",
  );
  assert.strictEqual(DEFAULT_FEED_BASE, "https://updates.crew.kiro.dev/feed");
});

test("buildFeedBase THROWS for plain http on non-loopback hosts", () => {
  assert.throws(
    () => buildFeedBase({ base: "http://cdn.example.dev/feed", channel: "stable" }),
    /must be https/,
  );
  // A LAN address is not loopback either -- cleartext update metadata over a
  // real network stays rejected.
  assert.throws(
    () => buildFeedBase({ base: "http://192.168.1.10/feed", channel: "stable" }),
    /must be https/,
  );
});

test("buildFeedBase ALLOWS plain http on loopback (local update harness)", () => {
  assert.strictEqual(
    buildFeedBase({ base: "http://127.0.0.1:8099/feed", channel: "stable" }),
    "http://127.0.0.1:8099/feed/stable/",
  );
  assert.strictEqual(
    buildFeedBase({ base: "http://localhost:8099/feed", channel: "stable" }),
    "http://localhost:8099/feed/stable/",
  );
  assert.strictEqual(
    buildFeedBase({ base: "http://[::1]:8099/feed", channel: "stable" }),
    "http://[::1]:8099/feed/stable/",
  );
});

// ---------------------------------------------------------------------------
// configureUpdater: the four policy flags this app depends on. EVERY one
// differs from the electron-updater default; a regression on any of them
// re-introduces a bug class we already fixed.
// ---------------------------------------------------------------------------

test("configureUpdater: autoDownload=false (consent-first: discovery must never download)", () => {
  const updater = {};
  configureUpdater(updater);
  // Library default is TRUE: a background check would silently download
  // megabytes with no user action. Our UX is discover -> ask -> download.
  assert.strictEqual(updater.autoDownload, false);
});

test("configureUpdater: autoInstallOnAppQuit=false (gateway must stop before the bundle swap)", () => {
  const updater = {};
  configureUpdater(updater);
  // Library default is TRUE: it would swap the bundle on quit WITHOUT
  // stopping the bundled Python gateway -- the half-replaced-app race this
  // module exists to prevent. deferredInstallOnQuit() does it in order.
  assert.strictEqual(updater.autoInstallOnAppQuit, false);
});

test("configureUpdater: allowDowngrade=true (difference-based gate: retraction + channel switch-back)", () => {
  const updater = {};
  configureUpdater(updater);
  // Library default is FALSE (greater-than only). Our gate is DIFFERENCE
  // based: a feed repointed to an older version (retraction) or a stable
  // preference on an insider build (switch-back downgrade) must be offered.
  assert.strictEqual(updater.allowDowngrade, true);
});

test("configureUpdater: allowPrerelease=true (nightly/insider stamps are semver prereleases)", () => {
  const updater = {};
  configureUpdater(updater);
  // Library default is FALSE: every -nightly.<stamp> / -insider.N version is
  // a semver prerelease and would be invisible to its OWN channel's checks.
  assert.strictEqual(updater.allowPrerelease, true);
});

// ---------------------------------------------------------------------------
// CONTRACT with electron-updater internals: the generic provider resolves
// artifact urls via newUrlFromBase(fileUrl, base). Our pointer/bytes host
// split (updates.crew.kiro.dev pointers, download.crew.kiro.dev bytes) relies
// on the UNDOCUMENTED-but-structural behaviour that an ABSOLUTE file url
// ignores the base. A library upgrade that changes this must fail CI here,
// not strand installs in the field.
// ---------------------------------------------------------------------------

test("CONTRACT: absolute artifact urls pass through newUrlFromBase unchanged (pointer/bytes split)", () => {
  const { newBaseUrl, newUrlFromBase } = require("electron-updater/out/util");
  const base = newBaseUrl(buildFeedBase({ base: "https://updates.crew.kiro.dev/feed", channel: "nightly" }));
  const absolute = "https://download.crew.kiro.dev/desktop/nightly/0.1.0-nightly.20260728t112233/KiroCrew-arm64.dmg";
  // Base is on a DIFFERENT host than the artifact: the absolute url must win.
  assert.strictEqual(newUrlFromBase(absolute, base).href, absolute);
});

test("CONTRACT: relative channel-file names resolve under the feed base directory", () => {
  const { newBaseUrl, newUrlFromBase } = require("electron-updater/out/util");
  const base = newBaseUrl(buildFeedBase({ base: "https://updates.crew.kiro.dev/feed", channel: "nightly" }));
  assert.strictEqual(
    newUrlFromBase("latest-mac.yml", base).href,
    "https://updates.crew.kiro.dev/feed/nightly/latest-mac.yml",
  );
});

// ---------------------------------------------------------------------------
// initAutoUpdate fixture: fake electron-updater AppUpdater (EventEmitter-like,
// recording setFeedURL / checkForUpdates / downloadUpdate / quitAndInstall)
// plus fake electron app/dialog/Notification. Platform comes in through the
// injected osPlatform dep -- no process.platform mutation needed.
// ---------------------------------------------------------------------------

function makeDeps(opts = {}) {
  const {
    appVersion = "1.0.0",
    osPlatform = "darwin",
    isPackaged = true,
  } = opts;
  const calls = { setFeedURL: [], checkForUpdates: 0, downloadUpdate: 0, quitAndInstall: [] };
  const handlers = {};
  const states = [];
  const appOnce = [];
  const appRemoved = [];
  const autoUpdater = {
    setFeedURL: (o) => calls.setFeedURL.push(o),
    checkForUpdates: async () => { calls.checkForUpdates += 1; },
    downloadUpdate: async () => { calls.downloadUpdate += 1; },
    quitAndInstall: (...args) => calls.quitAndInstall.push(args),
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged,
      getVersion: () => appVersion,
      once: (ev, fn) => appOnce.push({ ev, fn }),
      removeListener: (ev, fn) => appRemoved.push({ ev, fn }),
      // Must exist: the force-exit failsafe timer (unref'd but still live)
      // calls app.exit(0) if the suite outlives it; without this stub it
      // would fall through to process.exit and kill the test runner.
      exit: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform,
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  const emit = (ev, payload) => handlers[ev] && handlers[ev](payload);
  const stateNames = () => states.map((s) => s.state);
  return { deps, calls, handlers, emit, states, stateNames, appOnce, appRemoved };
}

// ---------------------------------------------------------------------------
// #709 regression guard: every state that renders a version must report the
// PENDING one. emit() defaults `version` to app.getVersion(), so a
// "downloading" event that forgets to pass it makes the update card claim the
// app is downloading the build it is already running -- the exact symptom
// reported in the field. The electron-updater migration reintroduced this once
// already; these tests exist so it cannot happen a third time.
// ---------------------------------------------------------------------------

test("#709: 'downloading' after consent reports the PENDING version, not the running one", async () => {
  const { deps, emit, states } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  states.length = 0;
  await u.download();
  const downloading = states.filter((s) => s.state === "downloading");
  assert.ok(downloading.length > 0, "consent must surface a downloading state");
  for (const s of downloading) {
    assert.strictEqual(
      s.version,
      "1.1.0",
      `downloading reported ${s.version} (running 1.0.0) -- the card would claim the app is downloading the version already installed`,
    );
  }
});

test("#709: download-progress reports the PENDING version, not the running one", async () => {
  const { deps, emit, states } = makeDeps({ appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  states.length = 0;
  emit("download-progress", { percent: 42, bytesPerSecond: 1024 });
  const s = states.find((x) => x.state === "downloading");
  assert.ok(s, "progress must surface a downloading state");
  assert.strictEqual(s.version, "1.1.0");
  assert.strictEqual(s.percent, 42);
});

test("#709: an in-flight re-check reports the PENDING version, not the running one", async () => {
  const { deps, emit, states } = makeDeps({ appVersion: "1.0.0" });
  const pending = [];
  deps.autoUpdater.downloadUpdate = () => new Promise((resolve) => pending.push(resolve));
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  u.download(); // leave it in flight
  states.length = 0;
  await u.check(); // must report progress, with the pending version
  const s = states.find((x) => x.state === "downloading");
  assert.ok(s, "an in-flight re-check must report progress");
  assert.strictEqual(s.version, "1.1.0");
  pending.forEach((r) => r());
});

test("#709: states that describe the RUNNING build still report app.getVersion()", () => {
  // The counterpart guard: pendingVersion() must not leak into states that are
  // about the installed app, or "up to date" would name a version the user
  // does not have.
  const { deps, emit, states } = makeDeps({ appVersion: "1.0.0" });
  initAutoUpdate(deps);
  emit("update-not-available", { version: "1.0.0" });
  const s = states.find((x) => x.state === "not-available");
  assert.ok(s);
  assert.strictEqual(s.version, "1.0.0");
});

// ---------------------------------------------------------------------------
// #709's other two fixes are now structurally subsumed by the library rather
// than implemented here, so they are pinned where they actually live:
//   - cache-bust: electron-updater appends its own noCache query
//     (isAddNoCacheQuery), and MacUpdater serves Squirrel.Mac from a loopback
//     proxy, so NSURLCache is no longer in the feed path at all.
//   - same-version guard: isUpdateAvailable() returns false on
//     eq(latest, current) BEFORE the allowDowngrade branch.
// Both are asserted against the REAL installed library below, so a version
// bump that removes either fails CI instead of resurfacing the incident.
// ---------------------------------------------------------------------------

test("#709 contract: the library still refuses an equal version even with allowDowngrade", () => {
  const src = require("fs").readFileSync(
    require.resolve("electron-updater/out/AppUpdater.js"),
    "utf8",
  );
  const idx = src.indexOf("async isUpdateAvailable(");
  assert.ok(idx > 0, "isUpdateAvailable not found -- library layout changed");
  const body = src.slice(idx, idx + 1200);
  const eqAt = body.indexOf("eq)(latestVersion, currentVersion)");
  const downgradeAt = body.indexOf("allowDowngrade");
  assert.ok(eqAt > 0, "equal-version short-circuit is gone -- self-reinstall loop can return");
  assert.ok(
    downgradeAt === -1 || eqAt < downgradeAt,
    "the equal-version check must precede the allowDowngrade branch, or allowDowngrade=true would offer the running version",
  );
});

test("#709 contract: the library adds its own no-cache query when no headers are set", () => {
  const src = require("fs").readFileSync(
    require.resolve("electron-updater/out/AppUpdater.js"),
    "utf8",
  );
  assert.match(
    src,
    /get isAddNoCacheQuery\(\)/,
    "isAddNoCacheQuery is gone -- the client-side cache-bust that replaced our feedNonce no longer exists",
  );
});
// win32 has none yet (#598) and must come back disabled -- WITHOUT touching
// the updater at all. Dev (unpackaged) builds have no update lane either.
// ---------------------------------------------------------------------------

test("SUPPORTED_PLATFORMS is exactly {darwin, linux}", () => {
  assert.deepStrictEqual([...SUPPORTED_PLATFORMS].sort(), ["darwin", "linux"]);
});

test("darwin initialises the updater (not disabled)", () => {
  const { deps, calls } = makeDeps({ osPlatform: "darwin" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "policy flags applied");
});

test("linux initialises the updater (not disabled)", () => {
  const { deps, calls } = makeDeps({ osPlatform: "linux" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "policy flags applied");
});

test("win32 returns disabled:'platform' and never touches the updater", () => {
  const { deps, calls } = makeDeps({ osPlatform: "win32" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "platform");
  assert.strictEqual(calls.setFeedURL.length, 0);
  assert.strictEqual(deps.autoUpdater.autoDownload, undefined, "policy flags must not be applied");
  // The disabled surface must still be safely callable.
  assert.strictEqual(typeof u.check, "function");
  assert.strictEqual(typeof u.getInfo, "function");
});

test("dev (unpackaged) build returns disabled:'dev'", () => {
  const { deps, calls } = makeDeps({ isPackaged: false });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "dev");
  assert.strictEqual(calls.setFeedURL.length, 0);
});

// ---------------------------------------------------------------------------
// Consent flow with the electron-updater event shape. autoDownload=false makes
// 'update-available' a DISCOVERY event: surfacing it must never download.
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
// Channel wiring: the feed url follows the version-derived channel and the
// user's opt-in preference; nightly is pinned. setFeedURL always uses the
// generic provider with a trailing-slash directory url.
// ---------------------------------------------------------------------------

test("stamped nightly build points the FEED at nightly (no channel migration)", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.1.0-nightly.20260728t112233" });
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  for (const o of calls.setFeedURL) {
    assert.strictEqual(o.provider, "generic");
    assert.strictEqual(o.url, "https://cdn.example.dev/feed/nightly/");
  }
});

test("channel preference points the FEED at the opted-in channel", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.1.0-insider.3" });
  deps.getChannelPreference = () => "stable";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/stable/"),
    `expected stable feed urls, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().channel, "stable");
});

test("getInfo exposes switcher inputs: stamped lane, switchability, preference", () => {
  const { deps } = makeDeps({ appVersion: "0.1.0-insider.3" });
  deps.getChannelPreference = () => "stable";
  const u = initAutoUpdate(deps);
  const info = u.getInfo();
  assert.strictEqual(info.stampedChannel, "insider");
  assert.strictEqual(info.channelSwitchable, true);
  assert.strictEqual(info.channelPreference, "stable");
  assert.strictEqual(info.packaged, true);
});

test("nightly build reports not switchable and stays on nightly despite a preference", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.1.0-nightly.20260722233638" });
  deps.getChannelPreference = () => "stable"; // must be ignored
  const u = initAutoUpdate(deps);
  await u.check();
  assert.strictEqual(u.getInfo().channelSwitchable, false);
  assert.ok(
    calls.setFeedURL.every((o) => o.url.includes("/nightly/")),
    `expected nightly feed urls, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
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
