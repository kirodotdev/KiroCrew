"use strict";
//
// mac-launch-services.js — keep macOS launching the version that is running.
//
// PROBLEM (#33896): a Builder Toolbox install lays every version down in its own
// versioned directory and keeps the previous ones on disk
// (`~/.toolbox/tools/kirocrew/<ver>/payload/KiroCrew.app`). After an update the
// new bundle exists, but the OLD `KiroCrew.app` directory is still a valid,
// readable bundle. macOS LaunchServices — which is what the Dock, Spotlight and
// double-clicking in Finder all resolve a launch through — keeps its earlier
// registration, so a COLD launch opens the old version. Nothing in the app ever
// re-registers the new bundle, so the only user recovery is to delete the old
// versioned directories (uninstall + reinstall).
//
// This is NOT the in-app "Restart to update" relaunch #22774 already fixed with
// the marker relaunchPath: that is one process re-exec'ing itself, this is a
// fresh launch that never touches the app's own code until LaunchServices has
// already chosen a bundle.
//
// FIX: the one moment the app can influence that choice is while the NEW version
// is running — so on startup it force-registers its OWN bundle with
// LaunchServices (`lsregister -f <this .app>`), which makes the running (newest)
// bundle the one LaunchServices resolves for the next cold launch. Best-effort
// and fail-soft: registration is a hint to the OS, never a gate on startup, so
// every failure path resolves to a skip rather than throwing.
//
// macOS only, and only for an externally-managed (Toolbox) install: a normal
// /Applications install is registered by the OS when it is placed there, and a
// self-updating build replaces its bundle in place so the path never changes.
// Pure decision + command-building here; the thin spawner is the only impure
// part and it is injected for the test.

const { appBundlePath } = require("./bundle-location");

// LaunchServices' registration tool, pinned absolute. Never resolved from PATH
// (a planted shim on the user's PATH would win), and composed from POSIX
// components joined with an explicit "/" so the constant is identical on every
// platform the module is merely imported on — the same discipline
// launch_macos.py pins `/usr/bin/open` with.
const LSREGISTER_PATH = [
  "", "System", "Library", "Frameworks", "CoreServices.framework",
  "Versions", "A", "Frameworks", "LaunchServices.framework",
  "Versions", "A", "Support", "lsregister",
].join("/");

/**
 * Should this launch re-register its bundle with LaunchServices?
 *
 * Yes only when: this is macOS, the install is externally managed (a Toolbox /
 * side-by-side layout, where old bundles linger and compete), and the running
 * bundle path is derivable. Anything else — another platform, a self-updating
 * or /Applications install, an undiscoverable bundle path — is a skip, because
 * re-registering there is either a no-op the OS already did or a path we cannot
 * name.
 *
 * @param {object} o
 * @param {string} [o.platform]       process.platform
 * @param {boolean} [o.managed]       is this an externally-managed install?
 * @param {string} [o.resourcesPath]  process.resourcesPath
 * @returns {{register: boolean, appPath: string, reason: string}}
 */
function planReregistration({ platform, managed, resourcesPath } = {}) {
  if (platform !== "darwin") return { register: false, appPath: "", reason: "not macOS" };
  if (!managed) return { register: false, appPath: "", reason: "not an externally-managed install" };
  const appPath = appBundlePath(resourcesPath);
  if (!appPath) return { register: false, appPath: "", reason: "bundle path not derivable" };
  return { register: true, appPath, reason: "" };
}

/**
 * The lsregister argv that force-registers a bundle. `-f` forces a (re)register
 * of the named path, making it LaunchServices' answer for that bundle id.
 *
 * @param {string} appPath  an absolute .app path
 * @returns {string[]}
 */
function lsregisterArgs(appPath) {
  return ["-f", appPath];
}

/**
 * Best-effort: on a managed macOS install, force-register the running bundle so
 * the next cold launch from the Dock/Spotlight/Finder opens this (newest)
 * version rather than a lingering old one. Never throws and never blocks: the
 * spawn is detached and its outcome is only logged.
 *
 * @param {object} deps
 * @param {string} [deps.platform=process.platform]
 * @param {boolean} deps.managed            is this an externally-managed install?
 * @param {string} [deps.resourcesPath=process.resourcesPath]
 * @param {(cmd:string, args:string[]) => void} [deps.run]  injected spawner;
 *        defaults to a fire-and-forget child_process.execFile.
 * @param {(m:string) => void} [deps.log]
 * @returns {{registered: boolean, reason: string}}
 */
function reregisterBundleIfManaged(deps = {}) {
  const {
    platform = process.platform,
    managed,
    resourcesPath = process.resourcesPath,
    run,
    log,
  } = deps;
  const note = (m) => { try { if (log) log(m); } catch { /* ignore */ } };
  let plan;
  try {
    plan = planReregistration({ platform, managed, resourcesPath });
  } catch (e) {
    note(`lsregister plan failed: ${e && e.message}`);
    return { registered: false, reason: "plan failed" };
  }
  if (!plan.register) {
    note(`lsregister skipped: ${plan.reason}`);
    return { registered: false, reason: plan.reason };
  }
  const spawn = typeof run === "function"
    ? run
    : (cmd, args) => {
        // Fire-and-forget, exactly like the Linux cache refreshers: an absolute
        // fixed-system binary, no shell, outcome never awaited.
        try { require("child_process").execFile(cmd, args, () => {}); } catch { /* ignore */ }
      };
  try {
    spawn(LSREGISTER_PATH, lsregisterArgs(plan.appPath));
    note(`lsregister -f ${plan.appPath}`);
    return { registered: true, reason: "" };
  } catch (e) {
    note(`lsregister spawn failed: ${e && e.message}`);
    return { registered: false, reason: "spawn failed" };
  }
}

module.exports = {
  LSREGISTER_PATH,
  planReregistration,
  lsregisterArgs,
  reregisterBundleIfManaged,
};
