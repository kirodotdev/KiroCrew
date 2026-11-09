// The desktop update lane follows the channel the policy requires (#24481).
// An LTS build follows lts whatever Settings says (RFC D11); an oss build
// is unchanged. See runtime/update/policy-channel.js.
const test = require("node:test");
const assert = require("node:assert");
const { initAutoUpdate, resolveChannel } = require("../auto-update");
const { LTS_UPDATE_CHANNEL, requiredUpdateChannelFor } = require("../runtime/update/policy-channel");
const { makeDeps } = require("./auto-update-test-helpers");

const feedUrls = (calls) => calls.setFeedURL.map((o) => o.url);

test("resolveChannel: a required channel replaces the preference", () => {
  assert.strictEqual(resolveChannel("stable", "insider", "stable"), "stable");
  assert.strictEqual(resolveChannel("insider", "", "stable"), "stable");
  assert.strictEqual(resolveChannel("stable", "stable", "insider"), "insider");
  assert.strictEqual(resolveChannel("nightly", "", "nightly"), "nightly");
  assert.strictEqual(resolveChannel("stable", "insider", "lts"), "lts");
  assert.strictEqual(resolveChannel("insider", "stable", "lts"), "lts");
});

test("resolveChannel: a required channel the build cannot follow has no lane", () => {
  assert.strictEqual(resolveChannel("nightly", "", "stable"), null);
  assert.strictEqual(resolveChannel("stable", "", "nightly"), null);
  assert.strictEqual(resolveChannel(null, "", "stable"), null);
  assert.strictEqual(resolveChannel("nightly", "", "lts"), null);
});

test("resolveChannel: no required channel keeps the oss rules", () => {
  assert.strictEqual(resolveChannel("stable", "insider", ""), "insider");
  assert.strictEqual(resolveChannel("stable", "insider"), "insider");
  assert.strictEqual(resolveChannel("nightly", "stable", ""), "nightly");
});

test("the baked edition decides the required channel", () => {
  assert.strictEqual(LTS_UPDATE_CHANNEL, "lts");
  assert.strictEqual(requiredUpdateChannelFor("lts")(), "lts");
  assert.strictEqual(requiredUpdateChannelFor("oss")(), "");
  // This checkout bakes no EDITION file, so it reads as oss.
  assert.strictEqual(requiredUpdateChannelFor()(), "");
});

test("LTS: an insider preference reads the lts feed, never an oss one, and the switcher is locked", async () => {
  // release-lts.yml publishes lts, so the LTS app reads its own feed only.
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.getChannelPreference = () => "insider";
  deps.getRequiredChannel = requiredUpdateChannelFor("lts");
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, undefined);
  await u.check();
  assert.deepStrictEqual(new Set(feedUrls(calls)), new Set(["https://cdn.example.dev/feed/lts/"]));
  const info = u.getInfo();
  assert.strictEqual(info.channel, "lts");
  assert.strictEqual(info.channelSwitchable, false);
  assert.strictEqual(info.channelLockedByPolicy, true);
});

test("LTS: an older lts release is not auto-downloaded (lts is the default lane, not a switch)", async () => {
  const { deps, calls, emit, stateNames } = makeDeps({ appVersion: "0.14.0" });
  deps.getRequiredChannel = requiredUpdateChannelFor("lts");
  deps.getAutoDownloadPreference = () => true;
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "0.13.0" });
  await new Promise((r) => setImmediate(r));
  assert.strictEqual(calls.downloadUpdate, 0);
  assert.ok(!stateNames().includes("found"));
  assert.ok(!stateNames().includes("downloading"));
});

test("LTS: a promoted insider-stamped build stays on stable", async () => {
  const { deps, calls } = makeDeps({ appVersion: "0.3.0-insider.13" });
  deps.getChannelPreference = () => "insider";
  deps.getRequiredChannel = () => "stable";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.deepStrictEqual(new Set(feedUrls(calls)), new Set(["https://cdn.example.dev/feed/stable/"]));
});

test("a required insider channel is followed over a stable preference", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.getChannelPreference = () => "stable";
  deps.getRequiredChannel = () => "insider";
  const u = initAutoUpdate(deps);
  await u.check();
  assert.deepStrictEqual(new Set(feedUrls(calls)), new Set(["https://cdn.example.dev/feed/insider/"]));
  assert.strictEqual(u.getInfo().channel, "insider");
});

test("oss: the preference still picks the feed and the switcher stays open", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.getChannelPreference = () => "insider";
  deps.getRequiredChannel = requiredUpdateChannelFor("oss");
  const u = initAutoUpdate(deps);
  await u.check();
  assert.deepStrictEqual(new Set(feedUrls(calls)), new Set(["https://cdn.example.dev/feed/insider/"]));
  const info = u.getInfo();
  assert.strictEqual(info.channelSwitchable, true);
  assert.strictEqual(info.channelLockedByPolicy, false);
  assert.strictEqual(info.requiredChannel, "");
});

test("a nightly build asked to follow stable turns updates off and never reads a feed", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0-nightly.20261026" });
  deps.getRequiredChannel = () => "stable";
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "policy-channel");
  await u.check();
  assert.strictEqual(calls.setFeedURL.length, 0);
  assert.strictEqual(calls.checkForUpdates, 0);
  // It reports the lane it is on, names the required one, and links that
  // channel's app, the way out.
  const info = u.getInfo();
  assert.strictEqual(info.channel, "nightly");
  assert.strictEqual(info.requiredChannel, "stable");
  assert.match(info.downloadUrl, /\/desktop\/stable\/latest\//);
  assert.strictEqual(info.channelLockedByPolicy, true);
});

test("a production build asked to follow nightly turns updates off", () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.getRequiredChannel = () => "nightly";
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.disabled, "policy-channel");
  assert.strictEqual(calls.setFeedURL.length, 0);
});

test("a requirement that changes to one the build cannot follow stops the next check", async () => {
  const { deps, calls, states } = makeDeps({ appVersion: "1.0.0" });
  let required = "stable";
  deps.getRequiredChannel = () => required;
  const u = initAutoUpdate(deps);
  await u.check();
  const before = calls.setFeedURL.length;
  const checksBefore = calls.checkForUpdates;
  required = "nightly";
  await u.check();
  assert.strictEqual(calls.setFeedURL.length, before, "no feed is pointed at");
  assert.strictEqual(calls.checkForUpdates, checksBefore, "no feed is read");
  assert.strictEqual(states[states.length - 1].state, "error");
});

test("manualDownloadUrl: the lts channel names the LTS app's own files", () => {
  const { manualDownloadUrl } = require("../auto-update");
  assert.match(manualDownloadUrl("lts", "darwin", "arm64"), /\/desktop\/lts\/latest\/KiroCrew-LTS\.dmg$/);
  assert.match(manualDownloadUrl("lts", "linux", "x64", "deb"), /\/desktop\/lts\/latest\/KiroCrew-LTS-x86_64\.deb$/);
  assert.match(manualDownloadUrl("lts", "win32", "x64"), /\/desktop\/lts\/latest\/KiroCrew-LTS-Setup\.exe$/);
  // oss channels keep their published names.
  assert.match(manualDownloadUrl("stable", "darwin", "arm64"), /\/desktop\/stable\/latest\/KiroCrew\.dmg$/);
});
