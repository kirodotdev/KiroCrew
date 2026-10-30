// The desktop update lane follows the channel the policy requires (#24481).
// An enterprise build follows stable whatever Settings says; an oss build is
// unchanged. See runtime/update/policy-channel.js.
const test = require("node:test");
const assert = require("node:assert");
const { initAutoUpdate, resolveChannel } = require("../auto-update");
const { ENTERPRISE_UPDATE_CHANNEL, requiredUpdateChannelFor } = require("../runtime/update/policy-channel");
const { makeDeps } = require("./auto-update-test-helpers");

const feedUrls = (calls) => calls.setFeedURL.map((o) => o.url);

test("resolveChannel: a required channel replaces the preference", () => {
  assert.strictEqual(resolveChannel("stable", "insider", "stable"), "stable");
  assert.strictEqual(resolveChannel("insider", "", "stable"), "stable");
  assert.strictEqual(resolveChannel("stable", "stable", "insider"), "insider");
  assert.strictEqual(resolveChannel("nightly", "", "nightly"), "nightly");
});

test("resolveChannel: a required channel the build cannot follow has no lane", () => {
  assert.strictEqual(resolveChannel("nightly", "", "stable"), null);
  assert.strictEqual(resolveChannel("stable", "", "nightly"), null);
  assert.strictEqual(resolveChannel(null, "", "stable"), null);
});

test("resolveChannel: no required channel keeps the oss rules", () => {
  assert.strictEqual(resolveChannel("stable", "insider", ""), "insider");
  assert.strictEqual(resolveChannel("stable", "insider"), "insider");
  assert.strictEqual(resolveChannel("nightly", "stable", ""), "nightly");
});

test("the baked edition decides the required channel", () => {
  assert.strictEqual(ENTERPRISE_UPDATE_CHANNEL, "stable");
  assert.strictEqual(requiredUpdateChannelFor("enterprise")(), "stable");
  assert.strictEqual(requiredUpdateChannelFor("oss")(), "");
  // This checkout bakes no EDITION file, so it reads as oss.
  assert.strictEqual(requiredUpdateChannelFor()(), "");
});

test("enterprise: an insider preference still reads the stable feed, and the switcher is locked", async () => {
  const { deps, calls } = makeDeps({ appVersion: "1.0.0" });
  deps.getChannelPreference = () => "insider";
  deps.getRequiredChannel = requiredUpdateChannelFor("enterprise");
  const u = initAutoUpdate(deps);
  await u.check();
  assert.ok(calls.setFeedURL.length >= 1);
  assert.deepStrictEqual(new Set(feedUrls(calls)), new Set(["https://cdn.example.dev/feed/stable/"]));
  const info = u.getInfo();
  assert.strictEqual(info.channel, "stable");
  assert.strictEqual(info.channelSwitchable, false);
  assert.strictEqual(info.channelLockedByPolicy, true);
});

test("enterprise: a promoted insider-stamped build stays on stable", async () => {
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
