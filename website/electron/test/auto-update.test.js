const { test } = require("node:test");
const assert = require("node:assert");
const {
  initAutoUpdate,
  channelForFlavor,
  channelForVersion,
  resolveChannel,
  buildFeedBase,
  configureUpdater,
  readExternallyManaged,
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

test("resolveChannel: no/invalid preference defaults to STABLE, not to the stamp", () => {
  // A stable release is PROMOTED, not rebuilt: the stable and insider downloads
  // of a promoted version are the same file carrying the same prerelease stamp,
  // so the stamp cannot say which feed to follow. Insider is an explicit opt-in.
  assert.strictEqual(resolveChannel("stable", ""), "stable");
  assert.strictEqual(resolveChannel("insider", undefined), "stable");
  assert.strictEqual(resolveChannel("stable", "nightly"), "stable"); // nightly is not a valid opt-in
  assert.strictEqual(resolveChannel("insider", "bogus"), "stable");
});

test("a promoted -insider.N build with no preference follows the STABLE feed", async () => {
  // The regression this exists for: promoting 0.3.0 publishes the insider
  // candidate's exact bytes to stable, so every stable install would otherwise
  // read its own version stamp and migrate itself onto the insider feed.
  const { deps, calls } = makeDeps({ appVersion: "0.3.0-insider.13" });
  deps.getChannelPreference = () => "";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/stable/"),
    `expected stable feed urls, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().channel, "stable");
});

test("an explicit insider preference still selects insider on promoted bytes", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.3.0-insider.13" });
  deps.getChannelPreference = () => "insider";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/insider/"),
    `expected insider feed urls, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().channel, "insider");
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

test("configureUpdater: autoInstallOnAppQuit=false on EVERY platform", () => {
  for (const osPlatform of ["darwin", "linux", "win32"]) {
    const updater = {};
    configureUpdater(updater, osPlatform);
    assert.strictEqual(updater.autoInstallOnAppQuit, false, osPlatform);
  }
  // Library default is TRUE, and it is unsafe on all three for two DIFFERENT
  // reasons. Off darwin, BaseUpdater.addQuitHandler() swaps the bundle on quit
  // without stopping the Python gateway. ON darwin the flag instead controls
  // when Squirrel is handed the zip -- and staging is what ARMS ShipIt, a
  // launchd job that swaps on any process death. Keeping it false is what makes
  // the gateway-before-swap ordering self-enforcing: Squirrel has no bytes until
  // quitAndInstall(), which is only reachable after an awaited stopGateway().
  const updater = {};
  configureUpdater(updater);
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
    // Bundle location seams. Default to a normal /Applications install so every
    // pre-existing test keeps arming the updater; the bundle-location guard
    // tests below drive these to the refused states.
    resourcesPath = "/Applications/Kiro Crew.app/Contents/Resources",
    bundleWritable = true,
    // Externally-managed verdict. null (the default) = not managed, decided
    // here so no test's outcome depends on the host filesystem.
    externallyManaged = null,
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
    resourcesPath,
    // Stubbed so the writable-vs-read-only axis is decided by the test, not by
    // whatever the host filesystem happens to allow.
    probeBundleWritable: () => bundleWritable,
    externallyManaged,
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  const emit = (ev, payload) => handlers[ev] && handlers[ev](payload);
  const stateNames = () => states.map((s) => s.state);
  return { deps, calls, handlers, emit, states, stateNames, appOnce, appRemoved };
}

// ---------------------------------------------------------------------------
// Logger wiring contract: a provided `log` dep must become autoUpdater.logger,
// verbatim. This is what routes electron-updater's own lifecycle/error output
// through the caller's sink -- if the assignment drifts, a packaged app's
// update diagnostics silently fall back to console and are lost.
// ---------------------------------------------------------------------------

test("initAutoUpdate wires the provided log dep as autoUpdater.logger", () => {
  const { deps } = makeDeps();
  initAutoUpdate(deps);
  assert.strictEqual(deps.autoUpdater.logger, deps.log);
});

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
// Dev (unpackaged) builds have no update lane, and must come back disabled
// WITHOUT touching the updater at all.
// ---------------------------------------------------------------------------

test("SUPPORTED_PLATFORMS is exactly {darwin, linux, win32}", () => {
  assert.deepStrictEqual([...SUPPORTED_PLATFORMS].sort(), ["darwin", "linux", "win32"]);
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

// A nightly-stamped version, kept because these cases were written against one.
// Windows now publishes on every known channel, so the choice no longer matters;
// the stable case is asserted separately below.
const WIN_NIGHTLY = "1.0.0-nightly.20260817t170500";

test("win32 initialises the updater (not disabled)", () => {
  const { deps, calls } = makeDeps({ osPlatform: "win32", appVersion: WIN_NIGHTLY });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "policy flags applied");
});

// autoInstallOnAppQuit stays false on every platform, and off darwin that flag
// is what keeps BaseUpdater from registering a quit handler. On win32 that
// matters more than on Linux: NsisUpdater's quit handler would spawn the NSIS
// installer while the Python gateway is still running, so the deliberate
// stop-gateway-then-install ordering in applyUpdateAndRestart is the only path
// that may install.
test("win32 never arms install-on-quit", () => {
  const { deps } = makeDeps({ osPlatform: "win32", appVersion: WIN_NIGHTLY });
  initAutoUpdate(deps);
  assert.strictEqual(deps.autoUpdater.autoInstallOnAppQuit, false);
});

// Stable now publishes Windows too, by promoting the verified bundle's installer
// rather than rebuilding it. Windows therefore carries no channel restriction of
// its own, and this case exists to keep that from silently regressing.
test("win32 on stable arms the updater like every other channel", () => {
  const { deps, calls } = makeDeps({ osPlatform: "win32", appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "policy flags applied");
});

// NOT tested here, deliberately: the disabled:"channel" branch in initAutoUpdate
// is currently UNREACHABLE. currentChannel() runs the preference through
// resolveChannel, which falls back to the version-stamped channel for anything it
// does not recognise, so it can only ever return a member of KNOWN_CHANNELS. The
// branch is kept as a fail-closed guard for the day a channel is added to
// KNOWN_CHANNELS before its publish lane exists -- arming an updater against a
// feed nobody wrote is the failure it prevents -- but a test would have to fake
// module state to reach it, and a test that can only pass by faking the thing
// under test is worse than an honest note.
//
// channelHasLane itself is NOT dead: manualDownloadUrl takes an arbitrary channel
// argument, and auto-update-errors.test.js covers it rejecting an unknown one.

// Every platform keeps every known channel.
test("darwin on stable keeps its lane", () => {
  const { deps, calls } = makeDeps({ osPlatform: "darwin", appVersion: "1.0.0" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1);
});

test("dev (unpackaged) build returns disabled:'dev'", () => {
  const { deps, calls } = makeDeps({ isPackaged: false });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "dev");
  assert.strictEqual(calls.setFeedURL.length, 0);
});

// ---------------------------------------------------------------------------
// Externally-managed marker (PEP 668 precedent). An operator/distro packager
// that owns the install's update lifecycle disables the updater outright: the
// feed is never contacted, the channel switcher loses its lane, and the About
// panel gets the marker's metadata to display instead.
// ---------------------------------------------------------------------------

test("externally-managed install returns disabled:'externally-managed' and never arms the updater", () => {
  const { deps, calls } = makeDeps({
    externallyManaged: { managedBy: "internal-registry", updateCommand: "pkgtool update kirocrew" },
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "externally-managed");
  assert.strictEqual(calls.setFeedURL.length, 0, "the feed must never be contacted");
  assert.strictEqual(calls.checkForUpdates, 0);
  assert.strictEqual(deps.autoUpdater.autoDownload, undefined, "policy flags must not be applied");
  // The whole disabled surface must stay callable (ipcMain invokes every key).
  assert.strictEqual(typeof u.check, "function");
  assert.strictEqual(typeof u.download, "function");
  assert.strictEqual(typeof u.install, "function");
  assert.strictEqual(typeof u.getInfo, "function");
});

test("externally-managed getInfo carries the marker metadata and kills the switcher", () => {
  const { deps } = makeDeps({
    appVersion: "1.0.0", // bare semver stamps as 'stable' -> switchable on a normal install
    externallyManaged: { managedBy: "internal-registry", updateCommand: "pkgtool update kirocrew" },
  });
  const info = initAutoUpdate(deps).getInfo();
  assert.strictEqual(info.managedBy, "internal-registry");
  assert.strictEqual(info.updateCommand, "pkgtool update kirocrew");
  assert.strictEqual(info.channelSwitchable, false,
    "a managed install has no lane the marker's owner reads");
});

test("a self-updating install reports empty managed metadata", () => {
  const { deps } = makeDeps({ appVersion: "1.0.0" });
  const info = initAutoUpdate(deps).getInfo();
  assert.strictEqual(info.managedBy, "");
  assert.strictEqual(info.updateCommand, "");
  assert.strictEqual(info.channelSwitchable, true);
});

test("externally-managed wins over the dev gate (intentional operator override)", () => {
  const { deps } = makeDeps({
    isPackaged: false,
    externallyManaged: { managedBy: "", updateCommand: "" },
  });
  assert.strictEqual(initAutoUpdate(deps).disabled, "externally-managed");
});

test("readExternallyManaged: absent marker -> null", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  assert.strictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), null);
});

test("readExternallyManaged: JSON marker carries metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({ managedBy: "internal-registry", updateCommand: "pkgtool update kirocrew" }),
  );
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "internal-registry",
    updateCommand: "pkgtool update kirocrew",
  });
});

test("readExternallyManaged: bare/unparsable marker still means managed", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, "EXTERNALLY-MANAGED"), "not json {");
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
  });
});

test("readExternallyManaged: degenerate markers (oversized, symlink, directory) mean managed, no metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  // Oversized: presence still wins, the body is never read into memory.
  const big = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(big, { recursive: true, force: true }));
  fs.writeFileSync(path.join(big, "EXTERNALLY-MANAGED"), "x".repeat(9000));
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: big }), {
    managedBy: "",
    updateCommand: "",
  });
  // Symlink (even dangling): lstat'ed, never followed — a link into a FIFO or
  // device must not be able to stall this startup-path read.
  const sym = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(sym, { recursive: true, force: true }));
  try {
    fs.symlinkSync(path.join(sym, "nowhere"), path.join(sym, "EXTERNALLY-MANAGED"));
    assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: sym }), {
      managedBy: "",
      updateCommand: "",
    });
  } catch (err) {
    // Ordinary Windows accounts may lack SeCreateSymbolicLinkPrivilege. Keep
    // the oversized and directory cases live, and omit only the setup this
    // host cannot perform; capable Windows hosts still exercise the assertion.
    if (process.platform !== "win32" || !["EPERM", "EACCES"].includes(err?.code)) {
      throw err;
    }
    t.diagnostic("symlink assertion omitted: host cannot create symlinks");
  }
  // Directory named like the marker: present = managed, nothing to parse.
  const dirCase = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dirCase, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dirCase, "EXTERNALLY-MANAGED"));
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dirCase }), {
    managedBy: "",
    updateCommand: "",
  });
});

test("readExternallyManaged: metadata fields are length-capped", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({ managedBy: "m".repeat(500), updateCommand: "c".repeat(2000) }),
  );
  const got = readExternallyManaged({ env: {}, resourcesPath: dir });
  assert.strictEqual(got.managedBy.length, 128);
  assert.strictEqual(got.updateCommand.length, 512);
});

test("readExternallyManaged: env override points at a marker file", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const marker = path.join(dir, "custom-marker.json");
  fs.writeFileSync(marker, JSON.stringify({ managedBy: "harness", updateCommand: "" }));
  const got = readExternallyManaged({
    env: { KIROCREW_EXTERNALLY_MANAGED: marker },
    resourcesPath: "/nonexistent",
  });
  assert.deepStrictEqual(got, { managedBy: "harness", updateCommand: "" });
});

// ---------------------------------------------------------------------------
// Bundle-location guard. The macOS install is an in-place .app replacement
// (MacUpdater -> Squirrel.Mac -> ShipIt), so a translocated copy or a read-only
// disk image can never apply an update. electron-updater has no such check of
// its own, so arming it there downloads every release and installs none.
// The DECISION logic is unit-tested in bundle-location.test.js; these assert the
// WIRING -- that a refused verdict returns the disabled surface and short-
// circuits before any updater state is touched.
// ---------------------------------------------------------------------------

test("translocated bundle returns disabled:'translocated' and never arms the updater", () => {
  const { deps, calls } = makeDeps({
    resourcesPath: "/private/var/folders/ab/cd/d/AppTranslocation/UUID/d/Kiro Crew.app/Contents/Resources",
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "translocated");
  assert.strictEqual(calls.setFeedURL.length, 0);
  assert.strictEqual(deps.autoUpdater.autoDownload, undefined, "policy flags must not be applied");
  // The whole disabled surface must stay callable: main.js invokes every one of
  // these from an ipcMain handler, so a missing key is a renderer-visible crash.
  assert.strictEqual(typeof u.check, "function");
  assert.strictEqual(typeof u.download, "function");
  assert.strictEqual(typeof u.install, "function");
  assert.strictEqual(typeof u.getInfo, "function");
});

test("read-only volume returns disabled:'volume' and never arms the updater", () => {
  const { deps, calls } = makeDeps({
    resourcesPath: "/Volumes/Kiro Crew 1.0.0/Kiro Crew.app/Contents/Resources",
    bundleWritable: false,
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "volume");
  assert.strictEqual(calls.setFeedURL.length, 0);
  assert.strictEqual(deps.autoUpdater.autoDownload, undefined, "policy flags must not be applied");
});

test("WRITABLE volume still arms: an external disk is not a read-only image", () => {
  // Regression guard on the /Volumes prefix being too broad. An app on an
  // external SSD or a network share lives under /Volumes and ShipIt can replace
  // it, so refusing on the path alone would strand a legitimately updatable
  // install with no updates and a boot-time nag.
  const { deps, calls } = makeDeps({
    resourcesPath: "/Volumes/External SSD/Kiro Crew.app/Contents/Resources",
    bundleWritable: true,
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
  assert.strictEqual(deps.autoUpdater.autoDownload, false, "policy flags applied");
});

test("guard is macOS-only: a linux /Volumes-shaped path still arms", () => {
  // classifyBundleLocation() returns "other" off darwin, so deb/rpm installs --
  // which update through the package manager, not an in-place swap -- are never
  // refused. AppImage shares the writability requirement but needs its own
  // detection; see the comment in auto-update.js.
  const { deps, calls } = makeDeps({
    osPlatform: "linux",
    resourcesPath: "/Volumes/whatever/Kiro Crew.app/Contents/Resources",
    bundleWritable: false,
  });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  assert.ok(calls.setFeedURL.length >= 1, "feed must be configured at init");
});

test("an unreadable bundle path fails safe to updatable", () => {
  // Never claim a location we cannot see: a probe that cannot run must not be
  // read as "un-updatable", or one unreadable path disables updates fleet-wide.
  const { deps } = makeDeps({ resourcesPath: "" });
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
});

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
