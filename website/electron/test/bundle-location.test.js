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
  classifyLinuxInstall,
  containingDirForAppImage,
  canUpdateLinuxInstall,
  describeLinuxInstall,
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

// --- Linux: two formats, opposite update stories -----------------------------
// Same shape of question as the macOS block above (where am I, and can I be
// replaced), but the AppImage's containing directory is what decides it while a
// package install is dpkg's to update. Every signal is a POSITIVE
// identification, so a format we have never seen reports "unknown" and stays
// updatable rather than inheriting AppImage semantics by default.

const IMAGE_READ_ONLY = { imageWritable: false };

test("the package-type file identifies a package install", () => {
  assert.equal(classifyLinuxInstall({ packageType: "deb" }), "package");
  assert.equal(classifyLinuxInstall({ packageType: "rpm" }), "package");
});

test("$APPIMAGE identifies an AppImage", () => {
  assert.equal(
    classifyLinuxInstall({ appImagePath: "/home/u/Applications/KiroCrew-x86_64.AppImage" }),
    "appimage",
  );
});

test("package-type wins over $APPIMAGE — it is the more authoritative signal", () => {
  assert.equal(
    classifyLinuxInstall({ appImagePath: "/home/u/K.AppImage", packageType: "deb" }),
    "package",
  );
});

test("an /opt resourcesPath is a package install even with no other signal", () => {
  // A deb-installed app relaunched from its desktop entry carries no $APPIMAGE,
  // and a build whose target had no publish config writes no package-type file.
  // Without this fallback such a launch would classify "unknown" and be handed
  // the AppImage self-replace path, which has no image to replace.
  assert.equal(classifyLinuxInstall({ resourcesPath: "/opt/KiroCrew/resources" }), "package");
});

test("no signal at all is 'unknown', never a guess", () => {
  assert.equal(classifyLinuxInstall(), "unknown");
  assert.equal(classifyLinuxInstall({}), "unknown");
  assert.equal(classifyLinuxInstall({ appImagePath: "", packageType: "   " }), "unknown");
});

test("the AppImage's containing directory is what must be writable", () => {
  assert.equal(containingDirForAppImage("/home/u/Applications/K.AppImage"), "/home/u/Applications");
});

test("a non-absolute AppImage path yields no directory to probe", () => {
  // Mirrors containingDirForBundle: resolving a relative path against the
  // process cwd would probe an unrelated directory and report on the wrong one.
  assert.equal(containingDirForAppImage("Apps/K.AppImage"), "");
  assert.equal(containingDirForAppImage(""), "");
  assert.equal(containingDirForAppImage("/K.AppImage"), "");
});

test("an AppImage in a directory it cannot write cannot self-update", () => {
  assert.equal(canUpdateLinuxInstall("appimage", IMAGE_READ_ONLY), false);
  assert.equal(canUpdateLinuxInstall("appimage", { imageWritable: true }), true);
});

test("a package install is updatable ONLY when its format is known", () => {
  // Each format reads its own feed directory, so the format is what makes an
  // update fetchable. Knowing only "this is a package" (the resourcesPath
  // fallback) is not enough: guessing would hand an rpm install the deb feed.
  assert.equal(canUpdateLinuxInstall("package", { packageFormat: "deb" }), true);
  assert.equal(canUpdateLinuxInstall("package", { packageFormat: "rpm" }), true);
  assert.equal(canUpdateLinuxInstall("package"), false);
  assert.equal(canUpdateLinuxInstall("package", { packageFormat: "" }), false);
  // Writability of /opt is irrelevant to a package install either way.
  assert.equal(canUpdateLinuxInstall("package", { packageFormat: "deb", imageWritable: false }), true);
});

test("an unrecognised install shape fails OPEN", () => {
  assert.equal(canUpdateLinuxInstall("unknown"), true);
  assert.equal(canUpdateLinuxInstall("unknown", IMAGE_READ_ONLY), true);
});

test("each un-updatable state explains itself in its own terms", () => {
  assert.equal(describeLinuxInstall("package", { packageFormat: "deb" }), "");
  assert.equal(describeLinuxInstall("appimage"), "");
  assert.equal(describeLinuxInstall("unknown"), "");

  const ro = describeLinuxInstall("appimage", IMAGE_READ_ONLY);
  assert.match(ro, /AppImage/);
  assert.match(ro, /cannot write/);

  // A package whose format could not be named points at the package manager
  // rather than blaming the filesystem.
  const pkg = describeLinuxInstall("package");
  assert.match(pkg, /package format/);
  assert.match(pkg, /package manager/);
  assert.doesNotMatch(pkg, /AppImage/);
});
