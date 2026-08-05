"use strict";
// Where-am-I-running classification, and whether Squirrel could replace this
// bundle. Two separate questions on purpose: the PATH tells you the location,
// but only WRITABILITY decides a /Volumes verdict — an external disk and a
// read-only disk image share that prefix. The contract: only states we can
// positively identify as un-swappable disable auto-update; everything
// ambiguous stays updatable so an unreadable path can never silently stop
// updates for the whole fleet.

const { test } = require("node:test");
const assert = require("node:assert");
const {
  classifyBundleLocation,
  containingDirForBundle,
  canInstallUpdates,
  shouldOfferRelocation,
  describeLocation,
} = require("../bundle-location");

const DARWIN = { platform: "darwin" };
const READ_ONLY = { bundleWritable: false };
const WRITABLE = { bundleWritable: true };

test("a mounted volume is 'volume' — the path alone does not condemn it", () => {
  const p = "/Volumes/KiroCrew Nightly/KiroCrew Nightly.app/Contents/Resources";
  assert.equal(classifyBundleLocation(p, DARWIN), "volume");
});

test("Gatekeeper App Translocation is 'translocated'", () => {
  const p = "/private/var/folders/ab/cd/d/AppTranslocation/DEAD-BEEF/d/KiroCrew.app/Contents/Resources";
  assert.equal(classifyBundleLocation(p, DARWIN), "translocated");
});

test("a normal install is 'applications'", () => {
  assert.equal(
    classifyBundleLocation("/Applications/KiroCrew Nightly.app/Contents/Resources", DARWIN),
    "applications",
  );
  assert.equal(
    classifyBundleLocation("/Users/someone/Applications/KiroCrew.app/Contents/Resources", DARWIN),
    "applications",
  );
});

test("an unusual but writable path is 'other', not a problem", () => {
  const p = "/Users/someone/Desktop/KiroCrew.app/Contents/Resources";
  assert.equal(classifyBundleLocation(p, DARWIN), "other");
  assert.equal(canInstallUpdates("other", READ_ONLY), true);
});

test("translocation wins over the volume prefix", () => {
  // A translocated copy of a volume-launched app carries both markers, and
  // "translocated" is the stricter, more accurate verdict.
  const p = "/Volumes/x/d/AppTranslocation/UUID/d/KiroCrew.app/Contents/Resources";
  assert.equal(classifyBundleLocation(p, DARWIN), "translocated");
});

test("non-darwin platforms do not get macOS-only verdicts", () => {
  assert.equal(classifyBundleLocation("C:\\Program Files\\KiroCrew", { platform: "win32" }), "other");
  assert.equal(classifyBundleLocation("/opt/kirocrew", { platform: "linux" }), "other");
});

test("a missing path is 'unknown' and stays updatable (fail-safe direction)", () => {
  // "couldn't look" must never be mistaken for "broken location" — that would
  // disable updates for users whose resourcesPath we simply failed to read.
  for (const bad of [undefined, null, "", 42]) {
    assert.equal(classifyBundleLocation(bad, DARWIN), "unknown");
  }
  assert.equal(canInstallUpdates("unknown", READ_ONLY), true);
  assert.equal(shouldOfferRelocation("unknown", READ_ONLY), false);
});

// ── Writability is what decides a /Volumes verdict ──────────────────────────

test("a WRITABLE volume (external disk, network share) can still update", () => {
  // Regression pin for the false positive: refusing on the /Volumes prefix
  // alone would nag and disable updates for a perfectly replaceable install.
  assert.equal(canInstallUpdates("volume", WRITABLE), true);
  assert.equal(shouldOfferRelocation("volume", WRITABLE), false);
  assert.equal(describeLocation("volume", WRITABLE), "");
});

test("a READ-ONLY volume (mounted disk image) cannot update", () => {
  assert.equal(canInstallUpdates("volume", READ_ONLY), false);
  assert.equal(shouldOfferRelocation("volume", READ_ONLY), true);
  assert.match(describeLocation("volume", READ_ONLY), /read-only disk image/);
});

test("an un-probed volume defaults to updatable", () => {
  // Default true: a caller that could not run the probe must not condemn.
  assert.equal(canInstallUpdates("volume"), true);
});

test("translocation is un-updatable even when writable", () => {
  // Swapping an ephemeral copy modifies a temp dir and leaves the real app on
  // the old version, so writability is irrelevant here.
  assert.equal(canInstallUpdates("translocated", WRITABLE), false);
  assert.equal(shouldOfferRelocation("translocated", WRITABLE), true);
  assert.match(describeLocation("translocated", WRITABLE), /App Translocation/);
});

// ── containingDirForBundle: the dir ShipIt must be able to write ────────────

test("containingDirForBundle strips Contents/Resources and the .app", () => {
  assert.equal(
    containingDirForBundle("/Applications/KiroCrew.app/Contents/Resources"),
    "/Applications",
  );
  assert.equal(
    containingDirForBundle("/Volumes/KiroCrew Nightly/KiroCrew Nightly.app/Contents/Resources"),
    "/Volumes/KiroCrew Nightly",
  );
});

test("containingDirForBundle refuses to guess on an unusable shape", () => {
  // Returning "" keeps a caller from probing (and judging) an unrelated dir.
  for (const bad of [undefined, null, "", 42, "relative/path", "/"]) {
    assert.equal(containingDirForBundle(bad), "", String(bad));
  }
});
