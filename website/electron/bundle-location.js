"use strict";
//
// bundle-location.js — where is this .app running FROM, and can it be replaced?
//
// Pure, injectable classification extracted so it can be unit-tested without
// Electron (mirrors gateway-recovery.js / instance-guard.js / gateway-stop.js).
//
// PROBLEM: macOS lets a user run an app straight out of the mounted DMG, and
// Gatekeeper additionally runs a quarantined app from a randomized read-only
// App Translocation path. Both look normal at launch — the gateway still spawns,
// because every write target (logs, pycache, the data home) is redirected to
// ~/ — so nothing complains. What silently cannot work is the macOS in-place
// bundle swap: electron-updater does not install on macOS itself, it serves the
// downloaded zip over a loopback server and delegates to Electron's built-in
// autoUpdater (Squirrel.Mac), so `quitAndInstall()` still ends in ShipIt
// replacing the running .app. Neither a read-only DMG mount nor an ephemeral
// translocated copy is a bundle ShipIt can usefully replace, and
// electron-updater has no writability or translocation check of its own. The app
// therefore downloads updates forever and applies none, with no visible reason.
//
// WHY LOCATION ALONE IS NOT THE TEST: `/Volumes/` holds every non-boot mount,
// not just disk images — an external SSD or a network share lives there too and
// is perfectly writable, so refusing on the path prefix alone would disable
// updates (and nag) for a legitimately updatable install. The property that
// actually decides it is whether the directory CONTAINING the bundle can be
// written, since that is what ShipIt must modify to swap the .app. So the path
// yields a location, and writability decides the verdict.
//
// Translocation is the exception that needs no writability test: even if the
// ephemeral copy were writable, replacing it updates a temporary directory and
// leaves the user's real app untouched.
//
// LINUX is the same question with different answers: an AppImage replaces itself
// in the directory holding the image, so writability decides it there too, while
// a deb/rpm install is the package manager's to update and the app must not try.
// Those live in the classifyLinuxInstall() family at the bottom of this file,
// kept separate from the macOS bundle functions because the two platforms share
// no path shape — collapsing them would mean one function whose parameters only
// ever apply to half its callers.

// The randomized read-only mount point Gatekeeper uses for a quarantined app.
// Path shape: /private/var/folders/<x>/<y>/d/AppTranslocation/<UUID>/d/Foo.app
const TRANSLOCATION_MARKER = "/AppTranslocation/";
// Every non-boot mount: DMGs, external disks, network shares.
const VOLUME_PREFIX = "/Volumes/";
const APPLICATIONS_MARKER = "/Applications/";
const RESOURCES_SUFFIX = "/Contents/Resources";
// fpm installs a deb/rpm under this prefix, so a resourcesPath below it is a
// package install even when no other signal survived (an app relaunched without
// $APPIMAGE, or a build whose package-type file was not written).
const PACKAGE_PREFIX = "/opt/";

/**
 * Classify the filesystem location an app bundle is running from. Path-only —
 * no I/O, so it stays trivially testable. This reports WHERE, not whether the
 * bundle is replaceable; see canInstallUpdates().
 *
 * @param {string} bundlePath  a path INSIDE the bundle (process.resourcesPath
 *                             is what the shell has to hand) or the .app path
 * @param {object} [opts]
 * @param {string} [opts.platform=process.platform]
 * @returns {"applications"|"volume"|"translocated"|"other"|"unknown"}
 *   "translocated" — Gatekeeper's randomized ephemeral copy (quarantined app)
 *   "volume"       — under /Volumes: a DMG, but equally an external disk or
 *                    network share. Writability decides whether it can update.
 *   "applications" — installed normally
 *   "other"        — some other real location (e.g. ~/Desktop)
 *   "unknown"      — no usable path (never claim a location we cannot see)
 */
function classifyBundleLocation(bundlePath, { platform = process.platform } = {}) {
  if (typeof bundlePath !== "string" || bundlePath === "") return "unknown";
  // Only macOS has DMG-running and App Translocation. Elsewhere the concept
  // does not apply, and guessing from a Windows/Linux path would be noise.
  if (platform !== "darwin") return "other";
  // Order matters: a translocated copy of a volume-launched app carries both
  // markers, and "translocated" is the more accurate — and stricter — verdict.
  if (bundlePath.includes(TRANSLOCATION_MARKER)) return "translocated";
  if (bundlePath.startsWith(VOLUME_PREFIX)) return "volume";
  if (bundlePath.includes(APPLICATIONS_MARKER)) return "applications";
  return "other";
}

/**
 * The directory ShipIt must be able to write in order to replace this bundle:
 * the parent of the .app itself. Given a resourcesPath
 * (`/x/Foo.app/Contents/Resources`) that is `/x`.
 *
 * Path-only and defensive: returns "" when the shape is not recognized, so a
 * caller cannot accidentally probe an unrelated directory.
 *
 * @param {string} resourcesPath
 * @returns {string} containing directory, or "" if it cannot be derived
 */
function containingDirForBundle(resourcesPath) {
  // Absolute only: a relative path is not something we can meaningfully probe,
  // and resolving it against the process cwd would judge an unrelated directory.
  if (typeof resourcesPath !== "string" || !resourcesPath.startsWith("/")) return "";
  const idx = resourcesPath.lastIndexOf(RESOURCES_SUFFIX);
  const appPath = idx === -1 ? resourcesPath : resourcesPath.slice(0, idx);
  const sep = appPath.lastIndexOf("/");
  if (sep <= 0) return "";
  return appPath.slice(0, sep);
}

/**
 * Can Squirrel's in-place bundle swap succeed from this location?
 *
 * Fail-safe direction is deliberately "allow": only states we can positively
 * identify as un-swappable are refused, so a path we failed to read — or a
 * writability probe that could not run — never silently disables updates for
 * the whole fleet.
 *
 * @param {string} location  a classifyBundleLocation() result
 * @param {object} [opts]
 * @param {boolean} [opts.bundleWritable=true]  can the containing dir be
 *        written? Only consulted for "volume", where the path is ambiguous
 *        (read-only DMG vs writable external disk). Pass the result of an
 *        access(W_OK) probe; default true keeps an un-probed caller updatable.
 * @returns {boolean}
 */
function canInstallUpdates(location, { bundleWritable = true } = {}) {
  // An ephemeral copy is un-updatable regardless of writability: swapping it
  // modifies a temp dir and leaves the user's real app on the old version.
  if (location === "translocated") return false;
  if (location === "volume") return bundleWritable !== false;
  return true;
}

/**
 * Should the shell nag the user to move the app into /Applications? Only for
 * states that actually break updating — never for a merely-unusual location.
 *
 * @param {string} location
 * @param {object} [opts]
 * @param {boolean} [opts.bundleWritable=true]
 * @returns {boolean}
 */
function shouldOfferRelocation(location, { bundleWritable = true } = {}) {
  return !canInstallUpdates(location, { bundleWritable });
}

/**
 * User-facing explanation for a state that cannot self-update. Returns "" when
 * updates are fine, so callers can treat "" as "nothing to say".
 *
 * @param {string} location
 * @param {object} [opts]
 * @param {boolean} [opts.bundleWritable=true]
 * @returns {string}
 */
function describeLocation(location, { bundleWritable = true } = {}) {
  if (canInstallUpdates(location, { bundleWritable })) return "";
  if (location === "translocated") {
    return "macOS is running Kiro Crew from a temporary read-only copy (App Translocation), "
      + "so it cannot install its own updates.";
  }
  return "Kiro Crew is running from a read-only disk image, so it cannot install its own updates.";
}

/**
 * Which Linux install shape is this process running as?
 *
 * Linux ships two formats with opposite update stories, and the shell must tell
 * them apart before it arms an updater: an AppImage self-replaces by `mv`-ing a
 * new image over itself, so it needs its containing directory to be writable,
 * while a deb/rpm install is owned by the package manager and is updated with
 * privilege escalation the app does not have.
 *
 * Both signals are positive identifications rather than the absence of the
 * other, so a third format added later reports "unknown" instead of silently
 * inheriting AppImage semantics: `resources/package-type` is written by
 * electron-builder for its package targets, `$APPIMAGE` is exported by the
 * AppImage runtime and holds the image's own path, and a package install is the
 * only shape of ours that lands under /opt (fpm's fixed prefix).
 *
 * @param {object} [signals]
 * @param {string} [signals.appImagePath=""]  value of process.env.APPIMAGE
 * @param {string} [signals.packageType=""]   contents of resources/package-type
 * @param {string} [signals.resourcesPath=""] process.resourcesPath
 * @returns {"appimage"|"package"|"unknown"}
 */
function classifyLinuxInstall({ appImagePath = "", packageType = "", resourcesPath = "" } = {}) {
  if (typeof packageType === "string" && packageType.trim() !== "") return "package";
  if (typeof appImagePath === "string" && appImagePath !== "") return "appimage";
  if (typeof resourcesPath === "string" && resourcesPath.startsWith(PACKAGE_PREFIX)) {
    return "package";
  }
  return "unknown";
}

/**
 * The directory an AppImage must be able to write in order to replace itself:
 * the one holding the image. Mirrors containingDirForBundle()'s defensiveness —
 * "" for anything that is not an absolute path, so no caller probes an
 * unrelated directory.
 *
 * @param {string} appImagePath
 * @returns {string}
 */
function containingDirForAppImage(appImagePath) {
  if (typeof appImagePath !== "string" || !appImagePath.startsWith("/")) return "";
  const sep = appImagePath.lastIndexOf("/");
  if (sep <= 0) return "";
  return appImagePath.slice(0, sep);
}

/**
 * Can this Linux install apply its own update?
 *
 * Three different reasons to say no or yes:
 *
 * - "appimage" depends on the filesystem: it `mv`s a new image over itself and
 *   so needs the containing directory writable.
 * - "package" needs its FORMAT to be known, because each format reads its own
 *   feed directory. The resourcesPath fallback in classifyLinuxInstall() proves
 *   only that this IS a package, not which kind — and guessing would hand an rpm
 *   install the deb feed (or an AppImage), so an unnamed format is refused.
 * - "unknown" fails open, in the same direction as canInstallUpdates(): a signal
 *   we could not read must not disable updates fleet-wide.
 *
 * @param {string} kind  a classifyLinuxInstall() result
 * @param {object} [opts]
 * @param {boolean} [opts.imageWritable=true]  can the AppImage's containing dir
 *        be written? Only consulted for "appimage".
 * @param {string} [opts.packageFormat=""]  resolved package format ("deb"/"rpm").
 *        Only consulted for "package".
 * @returns {boolean}
 */
function canUpdateLinuxInstall(kind, { imageWritable = true, packageFormat = "" } = {}) {
  if (kind === "appimage") return imageWritable !== false;
  if (kind === "package") return typeof packageFormat === "string" && packageFormat !== "";
  return true;
}

/**
 * User-facing explanation for a Linux install that cannot self-update, or ""
 * when it can — so callers can treat "" as "nothing to say", exactly like
 * describeLocation().
 *
 * @param {string} kind
 * @param {object} [opts]
 * @param {boolean} [opts.imageWritable=true]
 * @param {string} [opts.packageFormat=""]
 * @returns {string}
 */
function describeLinuxInstall(kind, { imageWritable = true, packageFormat = "" } = {}) {
  if (canUpdateLinuxInstall(kind, { imageWritable, packageFormat })) return "";
  if (kind === "package") {
    return "Kiro Crew cannot tell which package format this install came from, so it "
      + "cannot fetch the right update. Update it through your package manager.";
  }
  return "Kiro Crew cannot write to the folder holding its AppImage, so it cannot replace "
    + "itself with an update. Move the AppImage somewhere you own, such as ~/Applications.";
}

module.exports = {
  classifyBundleLocation,
  containingDirForBundle,
  canInstallUpdates,
  shouldOfferRelocation,
  describeLocation,
  classifyLinuxInstall,
  containingDirForAppImage,
  canUpdateLinuxInstall,
  describeLinuxInstall,
  TRANSLOCATION_MARKER,
  VOLUME_PREFIX,
  PACKAGE_PREFIX,
};
