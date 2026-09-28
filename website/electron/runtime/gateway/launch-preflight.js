"use strict";

const { findKirocrewBin } = require("../../find-bin");
const { resolveGatewayPath } = require("../../mac-env");
const { launchBlockingBundleParts } = require("../../bundle-integrity");
const { describeSandboxProfileNeed } = require("../../sandbox-profile");

/**
 * What the host offers a gateway launch, asked before anything is spawned:
 * which backend binary the candidate list resolves to right now, whether that
 * bundle is complete enough to start, which tree ships agents/ and skills/,
 * whether the agent sandbox will need an AppArmor profile, which PATH entries
 * a GUI-launched macOS app should recover, and whether this app's own
 * executable is still there to relaunch.
 *
 * Every answer is computed fresh on each call (findKirocrewBin does live
 * access() checks), which is what lets a stale-bundle respawn pick up a backend
 * swapped in at the same path. `dirname` is the Electron app directory the
 * supervisor was given, never this module's own location.
 */
function createLaunchPreflight({
  fs,
  os,
  path,
  execFileSync,
  processObj,
  dirname,
  isWindows: IS_WIN,
  log: glog,
  warn: userWarn,
}) {
  /**
   * Can app.relaunch() still find something to re-exec? Electron relaunches
   * this process's own executable (process.execPath; inside the .app bundle on
   * a packaged macOS build), so the bundle being pruned out from under a
   * running app is visible as that path no longer existing. A swapped bundle
   * leaves a new executable at the same path and reads as relaunchable.
   */
  function canRelaunchThisApp() {
    const target = processObj.execPath;
    if (typeof target !== "string" || !target) return false;
    try {
      fs.accessSync(target, fs.constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }

  /** The backend launcher the candidate list names right now. */
  function resolveGatewayBin() {
    return findKirocrewBin(
      fs,
      os,
      path,
      processObj.resourcesPath,
      dirname,
      processObj.arch,
      IS_WIN,
    );
  }

  // Resolve the tree that ships agents/ and skills/. Packaged resources keep it
  // beside electron/; source checkouts keep it at the repo root two levels up.
  function resolveProjectDir() {
    const candidates = [
      path.resolve(dirname, ".."),
      path.resolve(dirname, "..", ".."),
    ];
    for (const candidate of candidates) {
      try {
        if (
          fs.existsSync(path.join(candidate, "agents"))
          && fs.existsSync(path.join(candidate, "skills"))
        ) {
          return candidate;
        }
      } catch { /* try the next candidate */ }
    }
    return path.resolve(dirname, "..");
  }

  /**
   * Re-ask, without spawning anything, whether the launcher would refuse the
   * bundled backend right now. Resolves the binary afresh the way spawnGateway
   * does (findKirocrewBin probes live), so a launcher that only lands mid-way
   * through extraction is picked up too. null = not resolvable to a bundled
   * tree yet, which the dialog reads as "still installing, count unknown".
   */
  function probeLaunchBlockingParts() {
    const bin = resolveGatewayBin();
    return launchBlockingBundleParts(fs, path, bin);
  }

  // AppImage processes receive no AppArmor profile automatically. Log the
  // exact remedy before launch; a failure here is diagnostic-only and must not
  // block the gateway.
  function warnSandboxProfileNeed(bin) {
    try {
      const need = describeSandboxProfileNeed({
        platform: processObj.platform,
        env: processObj.env,
        readSysctl: (file) => fs.readFileSync(file, "utf8"),
        cliBin: bin,
      });
      if (need) {
        userWarn(`WARN agent sandbox will fail closed: ${need.reason}`);
        userWarn(`HINT run this in a terminal (needs sudo), then restart the app: ${need.command}`);
      }
    } catch (error) {
      userWarn(`WARN sandbox profile check failed: ${error.message}`);
    }
  }

  // GUI-launched macOS apps receive launchd's minimal PATH. Append only the
  // user's launchd-domain additions so an existing resolution can never be
  // shadowed; other platforms and empty additions leave PATH untouched.
  function recoverLaunchdPath(basePath) {
    const gatewayPath = resolveGatewayPath({
      execFileSync,
      platform: processObj.platform,
      basePath,
    });
    if (gatewayPath) {
      glog(`PATH recovered from launchd domain: +${gatewayPath.added.length} dir(s) appended`);
    }
    return gatewayPath;
  }

  return {
    canRelaunchThisApp,
    resolveGatewayBin,
    resolveProjectDir,
    probeLaunchBlockingParts,
    warnSandboxProfileNeed,
    recoverLaunchdPath,
  };
}

module.exports = { createLaunchPreflight };
