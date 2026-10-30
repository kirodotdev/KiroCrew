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
  DEFAULT_FEED_BASE,
  SUPPORTED_PLATFORMS,
  resolveMacDistArch,
  manualDownloadUrl,
  DOWNLOAD_BASE,
} = require("../auto-update");

const { makeDeps } = require("./auto-update-test-helpers");

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
// Single-arch macOS builds follow their OWN feed directory. electron-updater
// names the channel file from the platform alone on darwin (latest-mac.yml,
// no arch suffix), so feed/<channel>/<arch>/ -- the directory
// sign-and-notarize.yml's mac_variant legs write -- is the only seam, and the
// universal build (no stamp) must keep reading the channel root untouched.
// ---------------------------------------------------------------------------

test("a universal mac build (no stamp) reads the channel root, as before", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.macDistArch = "";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/stable/"),
    `expected the universal channel root, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().downloadUrl, `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew.dmg`);
});

test("an arm64-only mac build reads feed/<channel>/arm64/ and is offered its own DMG", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.macDistArch = "arm64";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/stable/arm64/"),
    `expected the arm64 feed directory, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().downloadUrl, `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-arm64.dmg`);
});

test("an x64-only mac build reads feed/<channel>/x64/ whatever the host arch says", async () => {
  // An x64-only app under Rosetta on Apple Silicon still reports the BUILD's
  // arch: the feed it follows is a property of the bytes on disk, not the CPU.
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.macDistArch = "x64";
  deps.osArch = "arm64";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(
    calls.setFeedURL.every((o) => o.url === "https://cdn.example.dev/feed/stable/x64/"),
    `expected the x64 feed directory, got: ${calls.setFeedURL.map((o) => o.url)}`,
  );
  assert.strictEqual(u.getInfo().downloadUrl, `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-x64.dmg`);
});

test("manualDownloadUrl: the mac DMG follows the BUILD arch, not the host arch", () => {
  assert.strictEqual(
    manualDownloadUrl("nightly", "darwin", "arm64", "", ""),
    `${DOWNLOAD_BASE}/desktop/nightly/latest/KiroCrew.dmg`,
  );
  assert.strictEqual(
    manualDownloadUrl("nightly", "darwin", "arm64", "", "arm64"),
    `${DOWNLOAD_BASE}/desktop/nightly/latest/KiroCrew-arm64.dmg`,
  );
  assert.strictEqual(
    manualDownloadUrl("nightly", "darwin", "arm64", "", "x64"),
    `${DOWNLOAD_BASE}/desktop/nightly/latest/KiroCrew-x64.dmg`,
  );
  // The stamp is mac-only: a Linux install never grows a mac suffix from it.
  assert.strictEqual(
    manualDownloadUrl("stable", "linux", "x64", "", "arm64"),
    `${DOWNLOAD_BASE}/desktop/stable/latest/KiroCrew-x86_64.AppImage`,
  );
});

test("resolveMacDistArch: the stamp, and nothing but the two known values", () => {
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => ({ desktopDistArch: "arm64" }) }), "arm64");
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => ({ desktopDistArch: "x64" }) }), "x64");
  // Unstamped = universal. So is anything the build never writes: an unknown
  // value or an unreadable package.json must fall back to the universal feed
  // (which every mac install can run), never throw on the update path.
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => ({}) }), "");
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => ({ desktopDistArch: "universal" }) }), "");
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => ({ desktopDistArch: "x86_64" }) }), "");
  assert.strictEqual(resolveMacDistArch({ readPackageJson: () => { throw new Error("boom"); } }), "");
  // The repo's own package.json carries no stamp: a dev run is universal.
  assert.strictEqual(resolveMacDistArch(), "");
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
  // The click re-confirms the feed first; wait for that hand-off so a download really
  // is in flight when the re-check below asks what is happening.
  await new Promise((r) => setImmediate(r));
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
// Characterization of the updater's owners. The facade keeps the policy
// (channels, feeds, markers, install-shape probes) and composes the lanes and
// the state reporter; these pin the observable shapes and orderings any move
// between those owners must keep.
// ---------------------------------------------------------------------------

const nodePath = require("node:path");
const nodeFs = require("node:fs");
const NodeModule = require("node:module");

const AUTO_UPDATE_EXPORTS = [
  "initAutoUpdate",
  "channelForFlavor",
  "channelForVersion",
  "resolveChannel",
  "isNewerVersion",
  "shouldAutoOffer",
  "buildFeedBase",
  "configureUpdater",
  "classifyError",
  "manualDownloadUrl",
  "resolveLinuxInstall",
  "resolveMacDistArch",
  "readExternallyManaged",
  "canRewriteMarker",
  "DEFAULT_FEED_BASE",
  "DOWNLOAD_BASE",
  "SUPPORTED_PLATFORMS",
];

test("the facade exports exactly its seventeen names, in order", () => {
  assert.deepStrictEqual(Object.keys(require("../auto-update")), AUTO_UPDATE_EXPORTS);
});

test("the update runtime loads without Electron and without load-time effects", () => {
  const electronDir = nodePath.join(__dirname, "..");
  const updateFiles = [nodePath.join(electronDir, "auto-update.js")];
  const runtimeDir = nodePath.join(electronDir, "runtime", "update");
  const owners = nodeFs.readdirSync(runtimeDir).filter((name) => name.endsWith(".js")).sort();
  assert.deepStrictEqual(owners, [
    "feed-lane.js", "freshness-gate.js", "managed-lane.js", "policy-channel.js", "state-reporter.js",
  ]);
  // Who composes whom: the facade composes the two lanes and the reporter; the
  // feed lane composes its own freshness gate, which the facade never sees; the
  // IPC registrar reads the policy channel and hands it to the facade.
  const composedBy = {
    "feed-lane.js": { file: updateFiles[0], path: "./runtime/update/feed-lane" },
    "managed-lane.js": { file: updateFiles[0], path: "./runtime/update/managed-lane" },
    "state-reporter.js": { file: updateFiles[0], path: "./runtime/update/state-reporter" },
    "freshness-gate.js": { file: nodePath.join(runtimeDir, "feed-lane.js"), path: "./freshness-gate" },
    "policy-channel.js": {
      file: nodePath.join(electronDir, "ipc-registrar.js"),
      path: "./runtime/update/policy-channel",
    },
  };
  for (const owner of owners) {
    updateFiles.push(nodePath.join(runtimeDir, owner));
    const { file, path } = composedBy[owner];
    assert.match(
      nodeFs.readFileSync(file, "utf8"),
      new RegExp(`require\\("${path.replace(/[./]/g, "\\$&")}"\\)`),
      `${nodePath.basename(file)} must compose ${owner}`,
    );
    assert.doesNotMatch(
      nodeFs.readFileSync(nodePath.join(runtimeDir, owner), "utf8"),
      /__dirname|require\(\s*["'](?:\.\.\/\.\.\/)?auto-update["']\s*\)/,
      `${owner} must neither anchor on its own directory nor require the facade`,
    );
  }
  const cached = new Map();
  for (const file of updateFiles) {
    cached.set(file, require.cache[file]);
    delete require.cache[file];
  }
  const originalLoad = NodeModule._load;
  const timers = [];
  const originalSetTimeout = global.setTimeout;
  const originalSetInterval = global.setInterval;
  NodeModule._load = function load(request, ...rest) {
    if (request === "electron") throw new Error("electron must not load at module scope");
    return originalLoad.call(this, request, ...rest);
  };
  global.setTimeout = (...args) => { timers.push(args); return originalSetTimeout(() => {}, 0); };
  global.setInterval = (...args) => { timers.push(args); return originalSetInterval(() => {}, 1e9); };
  try {
    const fresh = require("../auto-update");
    assert.deepStrictEqual(Object.keys(fresh), AUTO_UPDATE_EXPORTS);
    assert.strictEqual(timers.length, 0, "loading arms no timer");
  } finally {
    NodeModule._load = originalLoad;
    global.setTimeout = originalSetTimeout;
    global.setInterval = originalSetInterval;
    for (const [file, entry] of cached) {
      if (entry) require.cache[file] = entry;
      else delete require.cache[file];
    }
  }
});

test("the baked marker defaults to EXTERNALLY-MANAGED beside main.js", (t) => {
  const looked = [];
  t.mock.method(nodeFs, "lstatSync", (target) => {
    looked.push(target);
    const error = new Error("absent");
    error.code = "ENOENT";
    throw error;
  });
  assert.strictEqual(
    readExternallyManaged({ env: {}, resourcesPath: "/virtual/resources", isPackaged: true }),
    null,
  );
  assert.deepStrictEqual(looked, [
    nodePath.join(__dirname, "..", "EXTERNALLY-MANAGED"),
    nodePath.join("/virtual/resources", "EXTERNALLY-MANAGED"),
  ]);
});

test("every lane hands back the handle shape its callers read", () => {
  const keys = (opts) => {
    const { deps } = makeDeps(opts);
    const handle = initAutoUpdate(deps);
    return { keys: Object.keys(handle), disabled: handle.disabled };
  };
  const stub = ["check", "download", "install", "getInfo", "disabled"];
  assert.deepStrictEqual(keys({ externallyManaged: { managedBy: "m", updateCommand: "" } }),
    { keys: stub, disabled: "externally-managed" });
  assert.deepStrictEqual(keys({ isPackaged: false }), { keys: stub, disabled: "dev" });
  assert.deepStrictEqual(keys({ osPlatform: "freebsd" }), { keys: stub, disabled: "platform" });
  assert.deepStrictEqual(keys({ appVersion: "1.0.0" }).keys, ["check", "download", "install", "getInfo", "isReady"]);

  const restoreTimers = (() => {
    const originalSetTimeout = global.setTimeout;
    const originalSetInterval = global.setInterval;
    global.setTimeout = () => ({ unref() {} });
    global.setInterval = () => ({ unref() {} });
    return () => {
      global.setTimeout = originalSetTimeout;
      global.setInterval = originalSetInterval;
    };
  })();
  try {
    assert.deepStrictEqual(
      keys({ externallyManaged: { managedBy: "m", updateCommand: "apply", checkCommand: "check" } }),
      { keys: ["check", "download", "install", "getInfo"], disabled: undefined },
    );
  } finally {
    restoreTimers();
  }

  assert.deepStrictEqual(
    keys({ resourcesPath: "/Volumes/Kiro Crew/Kiro Crew.app/Contents/Resources", bundleWritable: false }),
    { keys: stub, disabled: "volume" },
  );
});

test("getInfo and every lifecycle payload keep their key order", async () => {
  const { deps, states } = makeDeps();
  const u = initAutoUpdate(deps);
  assert.deepStrictEqual(Object.keys(u.getInfo()), [
    "version",
    "channel",
    "stampedChannel",
    "channelSwitchable",
    "channelLockedByPolicy",
    "requiredChannel",
    "channelPreference",
    "laneVersion",
    "runningAheadOfLane",
    "autoDownload",
    "managedBy",
    "updateCommand",
    "platform",
    "packaged",
    "downloadUrl",
    "lastState",
  ]);
  await u.check();
  assert.deepStrictEqual(Object.keys(states[0]), [
    "state",
    "channel",
    "version",
    "installHandoff",
    "laneVersion",
    "runningAheadOfLane",
  ]);
  assert.strictEqual(states[0].state, "checking");
});

test("the feed lane arms policy, events, feed and timers in one fixed order", () => {
  const order = [];
  const { deps } = makeDeps();
  const updater = {};
  for (const flag of ["autoDownload", "autoInstallOnAppQuit", "allowDowngrade", "allowPrerelease", "logger"]) {
    Object.defineProperty(updater, flag, {
      configurable: true,
      set(value) { order.push(`set ${flag}=${flag === "logger" ? "log" : value}`); },
      get() { return undefined; },
    });
  }
  updater.on = (event) => order.push(`on ${event}`);
  updater.setFeedURL = (options) => order.push(`setFeedURL ${options.url}`);
  const originalSetTimeout = global.setTimeout;
  const originalSetInterval = global.setInterval;
  global.setTimeout = (_fn, ms) => { order.push(`setTimeout ${ms}`); return { unref() {} }; };
  global.setInterval = (_fn, ms) => { order.push(`setInterval ${ms}`); return { unref() {} }; };
  try {
    initAutoUpdate({ ...deps, autoUpdater: updater });
  } finally {
    global.setTimeout = originalSetTimeout;
    global.setInterval = originalSetInterval;
  }
  assert.deepStrictEqual(order, [
    "set autoDownload=false",
    "set autoInstallOnAppQuit=false",
    "set allowDowngrade=true",
    "set allowPrerelease=true",
    "set logger=log",
    "on error",
    "on checking-for-update",
    "on update-not-available",
    "on update-available",
    "on download-progress",
    "on update-downloaded",
    "setFeedURL https://cdn.example.dev/feed/stable/",
    "setTimeout 30000",
    "setInterval 14400000",
  ]);
});

test("the managed lane arms only its launch check and poll, never the feed", () => {
  const order = [];
  const { deps } = makeDeps({
    externallyManaged: { managedBy: "m", updateCommand: "/usr/bin/apply", checkCommand: "/usr/bin/check" },
  });
  const updater = {
    on: (event) => order.push(`on ${event}`),
    setFeedURL: () => order.push("setFeedURL"),
  };
  const originalSetTimeout = global.setTimeout;
  const originalSetInterval = global.setInterval;
  global.setTimeout = (_fn, ms) => { order.push(`setTimeout ${ms}`); return { unref() { order.push("unref"); } }; };
  global.setInterval = (_fn, ms) => { order.push(`setInterval ${ms}`); return { unref() { order.push("unref"); } }; };
  try {
    initAutoUpdate({ ...deps, autoUpdater: updater });
  } finally {
    global.setTimeout = originalSetTimeout;
    global.setInterval = originalSetInterval;
  }
  assert.deepStrictEqual(order, ["setTimeout 30000", "setInterval 14400000", "unref", "unref"]);
});
