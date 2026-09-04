const { test } = require("node:test");
const assert = require("node:assert");
const {
  initAutoUpdate,
  channelForFlavor,
  channelForVersion,
  resolveChannel,
  isNewerVersion,
  shouldAutoOffer,
  buildFeedBase,
  configureUpdater,
  readExternallyManaged,
  canRewriteMarker,
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

// ---------------------------------------------------------------------------
// Downgrade-nag guard: allowDowngrade=true means the library reports
// "available" for ANY feed version that differs from the running one, so a
// build running AHEAD of its channel's published latest gets nagged to install
// an OLDER build. isNewerVersion + shouldAutoOffer are the direction gate that
// suppresses that automatic path while leaving deliberate channel switches and
// explicit downloads alone.
// ---------------------------------------------------------------------------

test("isNewerVersion: strictly-greater release core is newer, older/equal is not", () => {
  assert.strictEqual(isNewerVersion("0.5.0", "0.3.0"), true);
  assert.strictEqual(isNewerVersion("0.3.0", "0.5.0"), false); // the reported bug
  assert.strictEqual(isNewerVersion("1.0.0", "1.0.0"), false);
});

test("isNewerVersion understands the prerelease stamps this app ships", () => {
  // A prerelease sorts BELOW its release core; a higher core wins regardless.
  assert.strictEqual(isNewerVersion("0.3.0-insider.13", "0.3.0"), false);
  assert.strictEqual(isNewerVersion("0.3.0", "0.3.0-insider.13"), true);
  assert.strictEqual(isNewerVersion("0.5.0-nightly.20260801t000000", "0.3.0"), true);
});

test("isNewerVersion: unrankable input is null (fail-open, never a false 'not newer')", () => {
  assert.strictEqual(isNewerVersion("", "1.0.0"), null);
  assert.strictEqual(isNewerVersion("1.0.0", undefined), null);
});

test("shouldAutoOffer: same-channel downgrade is NOT offered (the fix)", () => {
  // Exactly the reported case: a stable-stamped 0.5.0 following the stable feed
  // whose latest published build is 0.3.0.
  assert.strictEqual(
    shouldAutoOffer({
      candidate: "0.3.0",
      current: "0.5.0",
      followedChannel: "stable",
      defaultChannel: "stable",
    }),
    false,
  );
});

test("shouldAutoOffer: same-channel upgrade IS offered", () => {
  assert.strictEqual(
    shouldAutoOffer({
      candidate: "0.6.0",
      current: "0.5.0",
      followedChannel: "stable",
      defaultChannel: "stable",
    }),
    true,
  );
});

test("shouldAutoOffer: a deliberate channel switch is exempt (followed != default lane)", () => {
  // The user's explicit preference moved this install OFF its default lane
  // (stable -> insider): landing on an older build of the chosen channel is the
  // intended, user-initiated outcome allowDowngrade exists for.
  assert.strictEqual(
    shouldAutoOffer({
      candidate: "0.3.0-insider.4",
      current: "0.5.0",
      followedChannel: "insider",
      defaultChannel: "stable",
    }),
    true,
  );
});

test("shouldAutoOffer: promoted-stable bytes are NOT read as a switch (byte-stamp trap)", () => {
  // A promoted stable build carries the insider stamp, but with no preference it
  // follows stable and its DEFAULT lane is also stable, so followed == default:
  // it is a same-channel install and a lower feed version must NOT be offered.
  // (Keying on the raw stamp — insider — would wrongly exempt it.)
  assert.strictEqual(
    shouldAutoOffer({
      candidate: "0.3.0",
      current: "0.5.0-insider.20",
      followedChannel: "stable",
      defaultChannel: "stable",
    }),
    false,
  );
});

test("shouldAutoOffer: an unrankable version is offered (fail-open, no silent hide)", () => {
  assert.strictEqual(
    shouldAutoOffer({
      candidate: "garbage",
      current: "0.5.0",
      followedChannel: "stable",
      defaultChannel: "stable",
    }),
    true,
  );
});

test("guard: a same-channel downgrade neither nags nor auto-downloads", async () => {
  // Reproduces the screenshots: stable-stamped 0.5.0 on the stable feed, feed
  // latest is 0.3.0, auto-download ON. The pre-fix behavior downloaded 0.3.0
  // and popped "Update ready"; the guard must report up to date instead.
  const seen = [];
  const { deps, calls, emit, states } = makeDeps({ appVersion: "0.5.0" });
  deps.getAutoDownloadPreference = () => true;
  deps.notifyUpdateFound = (v, o) => seen.push([v, o]);
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.3.0", releaseNotes: "older" });
  assert.strictEqual(calls.downloadUpdate, 0, "a same-channel downgrade must never auto-download");
  assert.deepStrictEqual(seen, [], "a same-channel downgrade must never fire the OS nudge");
  assert.ok(!states.some((s) => s.state === "found"), "must not surface a 'found' downgrade");
  assert.strictEqual(states.at(-1).state, "not-available", "reports up to date instead");
});

test("guard: a same-channel UPGRADE still auto-downloads (no regression)", async () => {
  const { deps, calls, emit, states } = makeDeps({ appVersion: "0.5.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.6.0", releaseNotes: "newer" });
  assert.strictEqual(calls.downloadUpdate, 1, "a real upgrade must still download");
  assert.ok(states.some((s) => s.state === "found"), "a real upgrade must still surface 'found'");
});

test("guard: a prerelease-stamped build ahead of stable is NOT nagged (byte-stamp trap)", async () => {
  // The regression both local reviewers caught: an insider-STAMPED build
  // (0.5.0-insider.20) with no channel preference follows the STABLE feed by
  // default. channelForVersion() reports 'insider' for the bytes, but the
  // install is a plain stable follower running ahead of the stable feed's 0.3.0.
  // Keying the switch exemption on the raw stamp would treat followed=stable !=
  // stamped=insider as a deliberate switch and re-open the downgrade nag; keying
  // it on the DEFAULT lane (also stable) correctly reads it as same-channel.
  const seen = [];
  const { deps, calls, emit, states } = makeDeps({ appVersion: "0.5.0-insider.20" });
  deps.getAutoDownloadPreference = () => true;
  deps.notifyUpdateFound = (v, o) => seen.push([v, o]);
  // No getChannelPreference override -> defaults to "" -> follows stable.
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.3.0", releaseNotes: "older stable" });
  assert.strictEqual(calls.downloadUpdate, 0, "a promoted/ahead build must not auto-download a downgrade");
  assert.deepStrictEqual(seen, [], "no OS nudge for a byte-stamp-only channel mismatch");
  assert.ok(!states.some((s) => s.state === "found"), "must not surface a 'found' downgrade");
  assert.strictEqual(states.at(-1).state, "not-available");
});

// The feed's answer is the ONLY honest input for "which lane are these bytes
// from", because promotion re-points the soaked candidate's file at stable
// without re-stamping it: the stable feed's release is literally
// `0.4.1-insider.1`, so channelForVersion() reports `insider` for a stable
// install. The display layer therefore reads `laneVersion` /
// `runningAheadOfLane` instead, and the About panel's version chip and
// "you are on a prerelease" note key on those. The auto-offer guard above is
// deliberately NOT keyed on them -- suppressing an unsolicited downgrade is a
// separate decision from labelling the running build honestly.
test("records what the followed lane publishes, so an ahead build is not mislabelled", async () => {
  const { deps, emit } = makeDeps({ appVersion: "0.5.0-insider.2" });
  deps.getChannelPreference = () => "stable";
  const u = initAutoUpdate(deps);
  // Before any check the answer is UNKNOWN, never "ahead": a boot-time panel
  // must not un-fold every promoted-stable version on a comparison never made.
  assert.strictEqual(u.getInfo().laneVersion, "");
  assert.strictEqual(u.getInfo().runningAheadOfLane, null);

  await u.check();
  // The stable lane's current release, which the direction gate then (correctly)
  // declines to auto-offer -- the version must survive that suppression.
  emit("update-available", { version: "0.4.1-insider.1" });
  assert.strictEqual(u.getInfo().laneVersion, "0.4.1-insider.1");
  assert.strictEqual(u.getInfo().runningAheadOfLane, true);
});

test("a lane that publishes exactly the running build reports not-ahead", async () => {
  // The promoted-stable population: prerelease-STAMPED bytes that ARE the stable
  // release. `update-not-available` fires because the feed matches the running
  // version, which is what makes this a definite false rather than an unknown.
  const { deps, emit } = makeDeps({ appVersion: "0.4.1-insider.1" });
  deps.getChannelPreference = () => "stable";
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-not-available", { version: "0.4.1-insider.1" });
  assert.strictEqual(u.getInfo().laneVersion, "0.4.1-insider.1");
  assert.strictEqual(u.getInfo().runningAheadOfLane, false);
});

test("a build running BEHIND its lane is not ahead of it", async () => {
  const { deps, emit } = makeDeps({ appVersion: "0.4.0-insider.14" });
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.4.1-insider.1" });
  assert.strictEqual(u.getInfo().runningAheadOfLane, false);
});

test("lifecycle payloads carry the lane pair, so a push-driven renderer agrees with getInfo", async () => {
  const { deps, emit, states } = makeDeps({ appVersion: "0.5.0-insider.2" });
  deps.getChannelPreference = () => "stable";
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.4.1-insider.1" });
  const last = states.at(-1);
  assert.strictEqual(last.laneVersion, "0.4.1-insider.1");
  assert.strictEqual(last.runningAheadOfLane, true);
});

test("a lane answer is dropped once the install follows a different channel", async () => {
  // Design review caught this: `update:set-channel` stores the preference and
  // returns getInfo() SYNCHRONOUSLY while its re-check is still in flight, so a
  // retained answer from the old lane gets paired with the new channel. On an
  // up-to-date insider build flipped to stable, a kept `runningAheadOfLane: false`
  // says "these bytes ARE the stable release" -- folding the chip to a version
  // that does not exist and suppressing the prerelease ask, i.e. the very bug the
  // lane pair exists to fix, for as long as the next check keeps failing.
  let preference = "insider";
  const { deps, emit } = makeDeps({ appVersion: "0.5.0-insider.2" });
  deps.getChannelPreference = () => preference;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-not-available", { version: "0.5.0-insider.2" });
  assert.strictEqual(u.getInfo().runningAheadOfLane, false, "insider lane publishes these bytes");

  // The switcher flips. No check has completed for stable yet.
  preference = "stable";
  assert.strictEqual(u.getInfo().laneVersion, "", "the insider answer does not describe stable");
  assert.strictEqual(u.getInfo().runningAheadOfLane, null, "unknown, never a stale false");
});

test("a lane answer is attributed to the lane whose feed was fetched, not to a later flip", async () => {
  // Mirror of shouldAutoOffer's feedChannel rule: if the preference changes while
  // a check is in flight, the answer that comes back describes the OLD lane.
  let preference = "stable";
  const { deps, emit } = makeDeps({ appVersion: "0.5.0-insider.2" });
  deps.getChannelPreference = () => preference;
  const u = initAutoUpdate(deps);
  await u.check(); // feed configured for stable
  preference = "insider"; // user flips mid-flight
  emit("update-available", { version: "0.4.1-insider.1" }); // ...stable's answer arrives
  assert.strictEqual(u.getInfo().laneVersion, "", "not reported as the insider lane's answer");
  assert.strictEqual(u.getInfo().runningAheadOfLane, null);
});

test("guard: an EXPLICIT channel switch off the default lane is still offered", async () => {
  // Stable-stamped build whose user explicitly picked insider: the preference
  // moves it off its default (stable) lane, so a lower insider build is a
  // deliberate switch and must still be offered.
  const { deps, calls, emit, states } = makeDeps({ appVersion: "0.5.0" });
  deps.getAutoDownloadPreference = () => true;
  deps.getChannelPreference = () => "insider";
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.4.0-insider.1", releaseNotes: "insider lane" });
  assert.strictEqual(calls.downloadUpdate, 1, "a deliberate channel switch must still download");
  assert.ok(states.some((s) => s.state === "found"), "a deliberate switch must surface 'found'");
});

test("guard: a channel flip mid-check does not authorize a stale-feed downgrade (TOCTOU)", async () => {
  // The GPT [BLOCK-MERGE] on #6011: an in-flight STABLE check, then the user
  // picks insider before the response lands. The candidate (0.3.0) came from
  // the stable feed the check configured; the guard must compare against THAT
  // captured lane (feedChannel), not the now-insider live preference. A live
  // read would make followedChannel=insider != default=stable, wrongly treat
  // the stable downgrade as a deliberate insider switch, and stage it.
  let pref = "";
  const { deps, calls, emit, states } = makeDeps({ appVersion: "0.5.0" });
  deps.getAutoDownloadPreference = () => true;
  deps.getChannelPreference = () => pref;
  const u = initAutoUpdate(deps);
  await u.check(); // configureFeed() captures feedChannel = "stable"
  pref = "insider"; // user flips the switcher while the check is in flight
  emit("update-available", { version: "0.3.0", releaseNotes: "stale stable feed" });
  assert.strictEqual(calls.downloadUpdate, 0, "a stale-feed downgrade must not download after a mid-check channel flip");
  assert.ok(!states.some((s) => s.state === "found"), "must not surface a 'found' downgrade");
  assert.strictEqual(states.at(-1).state, "not-available");
});

test("guard: a stage armed for a downgrade is discarded and disarmed", async () => {
  // A 0.3.0 stage carried over (from before the fix, or a race): a later check
  // that re-reports 0.3.0 on the same channel must drop it so it cannot install
  // on the next quit.
  const { deps, emit, states, appRemoved } = makeDeps({ appVersion: "0.5.0" });
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  // Force a staged state by driving the downloaded event directly.
  emit("update-downloaded", { version: "0.3.0" });
  assert.strictEqual(u.isReady(), true, "precondition: a stage exists");
  states.length = 0;
  emit("update-available", { version: "0.3.0" });
  assert.strictEqual(u.isReady(), false, "the downgrade stage must be discarded");
  assert.ok(
    appRemoved.some((r) => r.ev === "before-quit"),
    "the deferred-install-on-quit hook must be removed",
  );
  assert.strictEqual(states.at(-1).state, "not-available");
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

test("externally-managed BARE marker returns disabled:'externally-managed' and never arms the updater", () => {
  const { deps, calls } = makeDeps({
    externallyManaged: { managedBy: "internal-registry", updateCommand: "", checkCommand: "" },
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

// ---------------------------------------------------------------------------
// MANAGED AUTO-UPDATE (marker-driven). A marker that ALSO carries an
// updateCommand no longer disables the updater: it shells the marker's own
// commands to check and apply, never arming electron-updater or the feed. The
// commands come from the keystone-protected marker, so shelling them is trusted
// (like the Python security_policy update pins).
//
// child_process is required inside auto-update.js, so these tests stub
// child_process.spawn on the real module for the duration of the test.
// ---------------------------------------------------------------------------

const cpModule = require("node:child_process");

// Install a fake spawn that records the command and drives a scripted
// {code, out}. Returns a restore fn + the recorded command list.
function stubSpawn(script) {
  const commands = [];
  // Spawn OPTIONS per call, so a test can assert the hardened environment the
  // marker's command runs in and not only which command ran.
  const optsList = [];
  const orig = cpModule.spawn;
  const { EventEmitter } = require("node:events");
  cpModule.spawn = (command, opts) => {
    commands.push(command);
    optsList.push(opts);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    const spec = (typeof script === "function" ? script(command) : script) || {};
    const { code = 0, out = "", err = "", error = false, signal = null } = spec;
    // Emit asynchronously so listeners attached after spawn() still catch it.
    setImmediate(() => {
      // `error: true` models a spawn failure (ENOENT / no shell); a `signal`
      // with a null code models a timeout kill. Both must read as "could not
      // run", distinct from a normal non-zero exit.
      if (error) { child.emit("error", new Error("spawn failed")); return; }
      if (out) child.stdout.emit("data", Buffer.from(out));
      if (err) child.stderr.emit("data", Buffer.from(err));
      child.emit("close", error ? null : code, signal);
    });
    return child;
  };
  return { commands, optsList, restore: () => { cpModule.spawn = orig; } };
}

test("managed check() with updateCommand+checkCommand emits found with the printed version", async (t) => {
  const { deps, states } = makeDeps({
    osPlatform: "win32",
    externallyManaged: {
      managedBy: "internal-registry",
      updateCommand: "pkgtool update kirocrew",
      checkCommand: "pkgtool check kirocrew",
    },
  });
  // Sibling contract: exit 0 and stdout IS the version (the packager authors
  // checkCommand to print the target version alone).
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.5\n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined, "a marker with an updateCommand is NOT disabled");
  await u.check();
  assert.deepStrictEqual(commands, ["pkgtool check kirocrew"], "check must shell the checkCommand");
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "an available update must surface a 'found' state");
  assert.strictEqual(found.version, "0.5.0.5", "trimmed stdout is the version");
  assert.strictEqual(
    found.installHandoff,
    "automatic-relaunch",
    "managed Windows updates run the marker command and must not promise an NSIS window",
  );
});

test("managed check(): non-zero exit -> not-available (ran, nothing new)", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 1, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(states.some((s) => s.state === "not-available"), "a non-zero check is up-to-date");
  assert.ok(!states.some((s) => s.state === "found"));
});

test("managed check(): exit 0 but no version printed -> check error (not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "   \n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "an exit-0 empty check is a broken command, not 'latest'");
  assert.ok(!states.some((s) => s.state === "not-available"));
  assert.ok(!states.some((s) => s.state === "found"));
});

test("managed check(): command that cannot run (spawn error) -> check error (not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ error: true });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "a check that could not run is an error, not 'up to date'");
  assert.ok(!states.some((s) => s.state === "not-available"));
});

test("managed check(): a discovered update that later clears disarms the quit-apply", async (t) => {
  let phase = "found";
  const relaunches = [];
  const { deps, appOnce, appRemoved } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.app.relaunch = () => relaunches.push(true);
  const { restore } = stubSpawn(() =>
    phase === "found" ? { code: 0, out: "0.5.0.5" } : { code: 1, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check(); // discovers -> arms before-quit
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "first check arms the quit-apply");
  phase = "clear";
  await u.check(); // external manager applied/withdrew it -> nothing new
  assert.ok(appRemoved.some((r) => r.ev === "before-quit"), "the stale quit-apply must be removed");
  // The captured handler now runs, but disarm cleared foundVersion: a normal
  // quit must NOT relaunch into an update that is no longer pending.
  let prevented = false;
  quit.fn({ preventDefault: () => { prevented = true; } });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(relaunches.length, 0, "a cleared update must not relaunch on quit");
});

test("managed check(): no checkCommand -> check error (cannot discover, not 'latest')", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "" },
  });
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.deepStrictEqual(commands, [], "no checkCommand -> nothing is shelled");
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "check", "no way to check must surface an error, not a green 'latest'");
  assert.ok(!states.some((s) => s.state === "not-available"));
});

test("managed install() runs updateCommand then relaunch+exit", async (t) => {
  const relaunches = [];
  const exits = [];
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "pkgtool update kirocrew", checkCommand: "check" },
  });
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.install();
  assert.ok(commands.includes("pkgtool update kirocrew"), "install must shell the updateCommand");
  assert.ok(states.some((s) => s.state === "installing"));
  assert.strictEqual(relaunches.length, 1, "a successful install relaunches");
  assert.deepStrictEqual(exits, [0], "a successful install exits(0)");
});

test("managed install() failure emits an install-phase error and calls onInstallFailed", async (t) => {
  const failed = [];
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.onInstallFailed = () => failed.push(true);
  deps.app.relaunch = () => { throw new Error("must not relaunch on failure"); };
  const { restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 7, out: "boom" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  await u.install();
  assert.strictEqual(failed.length, 1, "onInstallFailed must fire on a non-zero apply");
  const err = states.find((s) => s.state === "error");
  assert.ok(err && err.phase === "install", "a failed apply emits an install-phase error");
});

test("managed auto-on-restart: pref true + found arms before-quit that runs updateCommand", async (t) => {
  const relaunches = [];
  const exits = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "pkgtool update kirocrew", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "pref true + found must arm a before-quit handler");
  const event = { preventDefault: () => {} };
  quit.fn(event);
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.ok(commands.includes("pkgtool update kirocrew"), "before-quit must run the updateCommand");
  assert.strictEqual(relaunches.length, 1);
  assert.deepStrictEqual(exits, [0]);
});

test("managed auto-on-restart: pref false does NOT arm before-quit", async (t) => {
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => false;
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(!appOnce.some((r) => r.ev === "before-quit"),
    "pref off -> nothing automatic; manual Install still works");
});

test("managed auto-on-restart: pref flipped OFF between check and quit is honored at quit", async (t) => {
  let pref = true;
  const relaunches = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => pref;
  deps.app.relaunch = () => relaunches.push(true);
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "armed while pref was true");
  pref = false; // user toggled off before quitting
  let prevented = false;
  quit.fn({ preventDefault: () => { prevented = true; } });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(prevented, false, "pref read fresh at quit -> quit proceeds normally");
  assert.ok(!commands.includes("apply"), "the updateCommand must NOT run when pref is off at quit");
  assert.strictEqual(relaunches.length, 0);
});

test("managed auto-on-restart: a FAILED apply on quit exits without relaunching", async (t) => {
  const relaunches = [];
  const exits = [];
  const failed = [];
  const { deps, appOnce } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  deps.getAutoDownloadPreference = () => true;
  deps.onInstallFailed = () => failed.push(true);
  deps.app.relaunch = () => relaunches.push(true);
  deps.app.exit = (c) => exits.push(c);
  const { restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 7, out: "boom" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const quit = appOnce.find((r) => r.ev === "before-quit");
  assert.ok(quit, "pref true + found arms the quit-apply");
  quit.fn({ preventDefault: () => {} });
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.strictEqual(relaunches.length, 0, "a failed apply must NOT relaunch into an uninstalled version");
  assert.deepStrictEqual(exits, [0], "the quit is still honored (exit 0)");
  assert.strictEqual(failed.length, 1, "onInstallFailed fires on a failed quit-apply");
});

test("managed check(): the version comes from stdout only, ignoring stderr warnings", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { restore } = stubSpawn({ code: 0, out: "0.5.0.5\n", err: "WARNING: config deprecated\n" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  const found = states.find((s) => s.state === "found");
  assert.ok(found, "an exit-0 check with a version on stdout is 'found'");
  assert.strictEqual(found.version, "0.5.0.5", "a stderr warning must not leak into the version");
});

test("managed download() lights the Install action (downloaded) without applying", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const { commands, restore } = stubSpawn((cmd) =>
    cmd === "check" ? { code: 0, out: "0.5.0.5" } : { code: 0, out: "" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.download(); // no prior check -> discovers first
  const dl = states.find((s) => s.state === "downloaded");
  assert.ok(dl && dl.version === "0.5.0.5", "download surfaces 'downloaded' with the found version");
  assert.ok(!commands.includes("apply"), "download must NOT run the apply command");
});

test("managed updater auto-checks on launch + arms a poll (no user action needed)", async (t) => {
  const { deps, states } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" },
  });
  const realST = global.setTimeout;
  const realSI = global.setInterval;
  let launchCb = null;
  let pollArmed = false;
  // Capture the scheduling done DURING synchronous init; the managed updater
  // returns before the feed path, so these are its only timers. Restore the
  // real timers immediately after init, before running the captured callback.
  global.setTimeout = (fn) => { launchCb = fn; return { unref() {} }; };
  global.setInterval = () => { pollArmed = true; return { unref() {} }; };
  const { commands, restore } = stubSpawn({ code: 0, out: "0.5.0.6" });
  t.after(() => { global.setTimeout = realST; global.setInterval = realSI; restore(); });
  initAutoUpdate(deps);
  global.setTimeout = realST;
  global.setInterval = realSI;
  assert.strictEqual(typeof launchCb, "function", "a launch check must be scheduled automatically");
  assert.ok(pollArmed, "a background polling interval must be armed");
  launchCb();
  await new Promise((r) => setImmediate(() => setImmediate(r)));
  assert.deepStrictEqual(commands, ["check"], "the scheduled launch check shells checkCommand");
  const found = states.find((s) => s.state === "found");
  assert.ok(found && found.version === "0.5.0.6", "the background check discovers the update on its own");
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
  // probeMarkerRewritable: this test is about PARSING a trusted marker; the
  // integrity gate itself is covered by the writability tests below.
  assert.deepStrictEqual(readExternallyManaged({
    env: {},
    resourcesPath: dir,
    probeMarkerRewritable: () => false,
  }), {
    managedBy: "internal-registry",
    updateCommand: "pkgtool update kirocrew",
    checkCommand: "",
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
    checkCommand: "",
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
    checkCommand: "",
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
      checkCommand: "",
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
    checkCommand: "",
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
    JSON.stringify({ managedBy: "m".repeat(500), updateCommand: "c".repeat(2000), checkCommand: "k".repeat(2000) }),
  );
  const got = readExternallyManaged({
    env: {},
    resourcesPath: dir,
    probeMarkerRewritable: () => false,
  });
  assert.strictEqual(got.managedBy.length, 128);
  assert.strictEqual(got.updateCommand.length, 512);
  assert.strictEqual(got.checkCommand.length, 512);
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
    probeMarkerRewritable: () => false,
  });
  assert.deepStrictEqual(got, { managedBy: "harness", updateCommand: "", checkCommand: "" });
});

// ---------------------------------------------------------------------------
// Marker INTEGRITY. The marker's updateCommand/checkCommand are shelled, and the
// background launch check fires them with no user action, so the marker is an
// execution trust root: one file write under a user-writable <resourcesPath>
// (Homebrew, `pip --user`, ~/Applications) would otherwise be arbitrary code
// execution in the desktop app. These exercise the REAL probe -- no injected
// seam -- because a test that only greps for the call would execute none of it.
// ---------------------------------------------------------------------------

// Root can chmod and rewrite anything, so the probe correctly answers
// "rewritable" for every path and a trusted-marker case has nothing to assert.
// Windows has no POSIX owner to read and is declared fail-closed.
const canTestOwnership = process.platform !== "win32"
  && typeof process.geteuid === "function" && process.geteuid() !== 0;

// A real path this account does NOT own and cannot chmod, used as the trusted
// marker case. Resolved from the filesystem rather than hard-coded so the test
// asserts only when its own precondition genuinely holds.
function foreignOwnedPath() {
  if (!canTestOwnership) return "";
  const fs = require("node:fs");
  const path = require("node:path");
  const euid = process.geteuid();
  for (const candidate of ["/usr/bin/env", "/bin/sh", "/usr/lib/os-release"]) {
    try {
      const st = fs.lstatSync(candidate);
      const dir = fs.lstatSync(path.dirname(candidate));
      if (st.uid !== euid && dir.uid !== euid
        && (st.mode & 0o022) === 0 && (dir.mode & 0o022) === 0) return candidate;
    } catch {
      // try the next candidate
    }
  }
  return "";
}

test("readExternallyManaged: a marker in a USER-WRITABLE dir yields NO metadata", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({
      managedBy: "attacker",
      updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
      checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
    }),
  );
  // Still MANAGED (the updater stays off) but the commands are refused: this is
  // the historical bare-marker shape.
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

test("readExternallyManaged: chmod 0400 on an OWNED marker does not buy trust", (t) => {
  // The bypass the mode-bit version of this gate had: the owner can always
  // chmod +w back, so read-only-right-now is not provenance. An agent shell
  // plants the marker, makes it 0400 in a 0500 dir, and must still be refused.
  if (!canTestOwnership) {
    t.skip("needs a non-root POSIX host: root can rewrite anything");
    return;
  }
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => {
    fs.chmodSync(dir, 0o700);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const marker = path.join(dir, "EXTERNALLY-MANAGED");
  fs.writeFileSync(marker, JSON.stringify({
    managedBy: "attacker",
    updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
    checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
  }));
  fs.chmodSync(marker, 0o400); // "not writable" -- but still ours
  fs.chmodSync(dir, 0o500);    // ditto
  assert.deepStrictEqual(readExternallyManaged({ env: {}, resourcesPath: dir }), {
    managedBy: "",
    updateCommand: "",
    checkCommand: "",
  });
});

test("canRewriteMarker: a marker we do NOT own and cannot chmod is trusted", (t) => {
  // The positive half: without this the gate could refuse everything and still
  // pass every negative test. Asserted against a real foreign-owned path (the
  // shape a root-owned system install has) rather than a fabricated one.
  const foreign = foreignOwnedPath();
  if (!foreign) {
    t.skip("no foreign-owned path available on this host");
    return;
  }
  assert.strictEqual(canRewriteMarker(foreign), false,
    `${foreign} is owned by another account in a directory we cannot write`);
});

test("canRewriteMarker: ownership is what is probed, not the mode bits", (t) => {
  if (!canTestOwnership) {
    t.skip("needs a non-root POSIX host: root can rewrite anything");
    return;
  }
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => {
    fs.chmodSync(dir, 0o700);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const marker = path.join(dir, "EXTERNALLY-MANAGED");
  fs.writeFileSync(marker, "{}");
  // Every mode an owner can set still answers "rewritable", because chmod is
  // ours: 0600 (plainly writable), 0400 (read-only file), 0000 (no bits at all).
  for (const mode of [0o600, 0o400, 0o000]) {
    fs.chmodSync(marker, mode);
    assert.strictEqual(canRewriteMarker(marker), true, `mode ${mode.toString(8)} is still ours`);
  }
  fs.chmodSync(marker, 0o600);
  // A marker that is absent cannot have its provenance established either.
  assert.strictEqual(canRewriteMarker(path.join(dir, "nope")), true);
});

test("canRewriteMarker: an ACL-granted write is rewritable even at a safe mode", (t) => {
  // POSIX mode bits do not model ACLs, so ownership + mode alone would call a
  // root-owned 0755 dir carrying a `chmod +a`/setfacl grant for this user
  // "not rewritable". A two-uid ACL fixture is not constructible in a test (it
  // needs a directory this account does not own), so the arm is asserted as a
  // DECLARED property, the same way the win32 branch below is.
  const js = require("node:fs").readFileSync(require.resolve("../auto-update"), "utf8");
  const probe = js.slice(js.indexOf("function canRewriteMarker"));
  assert.match(probe.slice(0, probe.indexOf("\n}")),
    /fs\.accessSync\(target, fs\.constants\.W_OK\);\s*\n\s*return true;/,
    "the probe must ask the kernel, not only the mode bits");
  // And the arm must not over-refuse: a foreign-owned path with no grant to us
  // still reads as trusted (covered positively above, re-asserted here against
  // the same subject so a broadened arm cannot pass silently).
  const foreign = foreignOwnedPath();
  if (!foreign) {
    t.diagnostic("no foreign-owned path available to re-assert the trusted case");
    return;
  }
  assert.strictEqual(canRewriteMarker(foreign), false);
});

test("canRewriteMarker: Windows is fail-closed by declaration", () => {
  // No POSIX owner to read and access(W_OK) does not model ACLs, so there is no
  // honest verdict; every Windows marker is refused. Asserted as a DECLARED
  // property so it cannot silently become an accident.
  const js = require("node:fs").readFileSync(require.resolve("../auto-update"), "utf8");
  assert.match(js, /if \(process\.platform === "win32" \|\| typeof process\.geteuid !== "function"\) return true;/,
    "the win32 branch must return 'rewritable' before any stat is attempted");
  if (process.platform === "win32") {
    const os = require("node:os");
    assert.strictEqual(canRewriteMarker(require("node:path").join(os.tmpdir(), "EXTERNALLY-MANAGED")), true);
  }
});

test("readExternallyManaged: a PACKAGED app ignores KIROCREW_EXTERNALLY_MANAGED", (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "kc-res-"));
  t.after(() => {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(empty, { recursive: true, force: true });
  });
  const marker = path.join(dir, "custom-marker.json");
  fs.writeFileSync(marker, JSON.stringify({ managedBy: "env", updateCommand: "/bin/false" }));
  // The env var names a marker the probe would trust, yet a packaged app must
  // not consult it at all: the launch environment is user-writable.
  assert.strictEqual(
    readExternallyManaged({
      env: { KIROCREW_EXTERNALLY_MANAGED: marker },
      resourcesPath: empty,
      isPackaged: true,
      probeMarkerRewritable: () => false,
    }),
    null,
    "packaged: the env seam is off and the real resources dir has no marker",
  );
  // Unpackaged (the harness case) still honors it.
  assert.deepStrictEqual(
    readExternallyManaged({
      env: { KIROCREW_EXTERNALLY_MANAGED: marker },
      resourcesPath: empty,
      isPackaged: false,
      probeMarkerRewritable: () => false,
    }),
    { managedBy: "env", updateCommand: "/bin/false", checkCommand: "" },
  );
});

test("managed updater: a rewritable marker never arms the background check", async (t) => {
  // End-to-end wiring: the launch timer fires managedCheck() 30s after start
  // with no user action, so an unverified marker must never reach that path.
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ext-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(
    path.join(dir, "EXTERNALLY-MANAGED"),
    JSON.stringify({
      managedBy: "attacker",
      updateCommand: "/bin/sh -c 'touch /tmp/pwned'",
      checkCommand: "/bin/sh -c 'touch /tmp/pwned'",
    }),
  );
  // externallyManaged left UNSET so the module reads the real marker from disk.
  const { deps } = makeDeps({ resourcesPath: dir });
  delete deps.externallyManaged;
  const realST = global.setTimeout;
  const realSI = global.setInterval;
  let timerArmed = false;
  let pollArmed = false;
  global.setTimeout = (fn, ms) => { timerArmed = true; return realST(fn, ms); };
  global.setInterval = (fn, ms) => { pollArmed = true; return realSI(fn, ms); };
  const { commands, restore } = stubSpawn({ code: 0, out: "9.9.9" });
  t.after(() => { global.setTimeout = realST; global.setInterval = realSI; restore(); });
  const u = initAutoUpdate(deps);
  global.setTimeout = realST;
  global.setInterval = realSI;
  assert.strictEqual(u.disabled, "externally-managed",
    "a rewritable marker is managed-with-no-metadata, so the updater is off");
  assert.strictEqual(timerArmed, false, "no launch check may be scheduled");
  assert.strictEqual(pollArmed, false, "no background poll may be armed");
  // And the explicit surfaces are inert too.
  await u.check();
  await u.download();
  await u.install();
  assert.deepStrictEqual(commands, [], "no marker command may ever be shelled");
});

// Everything a shell reads as code. The managed command's environment is
// CONSTRUCTED, so none of these can reach it whether or not it is named here --
// this list is the adversary's side of the contract, not the implementation's.
const SHELL_CODE_VARS = [
  "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "PYTHONUSERBASE",
  "PYTHONWARNINGS", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
  "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "BASH_ENV", "ENV", "SHELLOPTS", "PS4",
  "IFS", "NODE_OPTIONS", "BASHOPTS", "PERL5OPT", "RUBYOPT",
  "BASH_FUNC_check%%", "BASH_FUNC_apply()", "BASH_FUNC_grep%%",
  // Not shell-interpreted, but an interpreter reads code from it: Python's
  // user-site dir comes from HOME, so a planted sitecustomize.py runs on every
  // `python` start.
  "HOME",
];

test("managed command env is CONSTRUCTED: nothing the shell reads as code is inherited", async (t) => {
  // A narrowed PATH stops a planted shim shadowing a command NAME. These go
  // further: an exported shell function shadows the name outright, PS4 under
  // SHELLOPTS=xtrace runs a command substitution before the command does, and
  // the loader family makes even a trusted absolute binary load planted code.
  // Because the env is built by naming what is ALLOWED, this also covers the
  // names nobody has thought of yet.
  const saved = new Map(SHELL_CODE_VARS.map((k) => [k, process.env[k]]));
  for (const k of SHELL_CODE_VARS) process.env[k] = "() { echo pwned; }";
  const savedProbe = process.env.KC_TEST_UNLISTED_PROBE;
  const savedLang = process.env.LANG;
  process.env.KC_TEST_UNLISTED_PROBE = "an-unlisted-variable";
  process.env.LANG = "C.UTF-8";
  t.after(() => {
    for (const [k, v] of saved) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    if (savedProbe === undefined) delete process.env.KC_TEST_UNLISTED_PROBE;
    else process.env.KC_TEST_UNLISTED_PROBE = savedProbe;
    if (savedLang === undefined) delete process.env.LANG;
    else process.env.LANG = savedLang;
  });
  const { deps } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "/usr/bin/apply", checkCommand: "/usr/bin/check" },
  });
  const { optsList, restore } = stubSpawn({ code: 0, out: "0.5.0.5" });
  t.after(restore);
  const u = initAutoUpdate(deps);
  await u.check();
  assert.strictEqual(optsList.length, 1, "the check shelled exactly one command");
  const env = optsList[0].env;
  for (const k of SHELL_CODE_VARS) {
    assert.ok(!(k in env), `${k} must not reach the managed command`);
  }
  assert.ok(!Object.keys(env).some((k) => k.startsWith("BASH_FUNC_")),
    "no exported shell function may survive");
  // Constructed, not filtered: an UNLISTED variable is absent because it was
  // never copied. This is the assertion that holds against the next name.
  assert.ok(!("KC_TEST_UNLISTED_PROBE" in env),
    "an unlisted variable must be absent by construction, not by denylist");
  // The declared pass-through still arrives, or a packager's updater breaks.
  assert.strictEqual(env.LANG, "C.UTF-8", "declared pass-through vars must survive");
  // The rest of the hardened environment is unchanged.
  assert.ok(env.PATH && !env.PATH.includes(require("node:os").homedir()),
    "PATH stays the narrowed system one");
  assert.strictEqual(optsList[0].cwd, "/");
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
