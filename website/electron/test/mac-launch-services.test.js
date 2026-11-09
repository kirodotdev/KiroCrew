"use strict";
// Keep macOS launching the version that is running (#33896). After a Toolbox
// side-by-side update the old KiroCrew.app lingers and LaunchServices keeps
// resolving it, so a cold launch opens the old version. On startup the running
// (newest) bundle re-registers itself with lsregister so the next cold launch
// resolves it. The decision and the command are pure and tested here; only the
// spawn is impure, and it is injected.

const { test } = require("node:test");
const assert = require("node:assert");
const {
  LSREGISTER_PATH,
  planReregistration,
  lsregisterArgs,
  reregisterBundleIfManaged,
} = require("../mac-launch-services");

const MAC_MANAGED = {
  platform: "darwin",
  managed: true,
  resourcesPath: "/Users/x/.toolbox/tools/kirocrew/1.2.3/payload/KiroCrew.app/Contents/Resources",
};
const BUNDLE = "/Users/x/.toolbox/tools/kirocrew/1.2.3/payload/KiroCrew.app";

test("lsregister path is a fixed absolute CoreServices path", () => {
  assert.ok(LSREGISTER_PATH.startsWith("/System/Library/Frameworks/CoreServices.framework/"));
  assert.ok(LSREGISTER_PATH.endsWith("/lsregister"));
});

test("a managed macOS install plans a re-register of its own bundle", () => {
  const plan = planReregistration(MAC_MANAGED);
  assert.deepEqual(plan, { register: true, appPath: BUNDLE, reason: "" });
});

test("force-register argv is -f <bundle>", () => {
  assert.deepEqual(lsregisterArgs(BUNDLE), ["-f", BUNDLE]);
});

test("skips off macOS", () => {
  const plan = planReregistration({ ...MAC_MANAGED, platform: "win32" });
  assert.equal(plan.register, false);
  assert.match(plan.reason, /not macOS/);
});

test("skips a non-managed install (self-updating / Applications)", () => {
  const plan = planReregistration({ ...MAC_MANAGED, managed: false });
  assert.equal(plan.register, false);
  assert.match(plan.reason, /externally-managed/);
});

test("skips when the bundle path is not derivable", () => {
  const plan = planReregistration({ ...MAC_MANAGED, resourcesPath: "/not/a/bundle" });
  assert.equal(plan.register, false);
  assert.match(plan.reason, /not derivable/);
});

test("reregisterBundleIfManaged spawns lsregister -f for a managed macOS install", () => {
  const calls = [];
  const res = reregisterBundleIfManaged({
    ...MAC_MANAGED,
    run: (cmd, args) => calls.push([cmd, args]),
  });
  assert.deepEqual(res, { registered: true, reason: "" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], LSREGISTER_PATH);
  assert.deepEqual(calls[0][1], ["-f", BUNDLE]);
});

test("reregisterBundleIfManaged spawns nothing off macOS", () => {
  const calls = [];
  const res = reregisterBundleIfManaged({
    ...MAC_MANAGED,
    platform: "linux",
    run: (cmd, args) => calls.push([cmd, args]),
  });
  assert.equal(res.registered, false);
  assert.equal(calls.length, 0);
});

test("reregisterBundleIfManaged spawns nothing for a non-managed install", () => {
  const calls = [];
  const res = reregisterBundleIfManaged({
    ...MAC_MANAGED,
    managed: false,
    run: (cmd, args) => calls.push([cmd, args]),
  });
  assert.equal(res.registered, false);
  assert.equal(calls.length, 0);
});

test("a throwing spawner is swallowed — startup is never taken down", () => {
  const res = reregisterBundleIfManaged({
    ...MAC_MANAGED,
    run: () => { throw new Error("boom"); },
  });
  assert.deepEqual(res, { registered: false, reason: "spawn failed" });
});
