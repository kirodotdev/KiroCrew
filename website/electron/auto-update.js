/**
 * Desktop auto-update via electron-updater (macOS + Linux).
 *
 * WHY electron-updater instead of Electron's built-in autoUpdater: the built-in
 * updater covers only macOS (Squirrel.Mac) and Windows (Squirrel.Windows), and
 * requires us to hand-build the feed, the version compare and the publish
 * metadata. electron-updater generates that metadata at build time
 * (latest-mac.yml / latest-linux.yml), verifies sha512 fail-closed, adds Linux
 * support, and — on macOS — still drives Squirrel.Mac underneath, so the proven
 * atomic bundle swap is unchanged. See docs/guides/windows-install.md and issue #598.
 *
 * The ONE KiroCrew-specific concern vs. a plain Electron app is unchanged: the
 * bundled Python gateway is a long-running child process, so it MUST be stopped
 * gracefully BEFORE the app bundle is swapped — otherwise the swap races a live
 * child and can leave a half-replaced app. That is why autoInstallOnAppQuit is
 * forced OFF (see configureUpdater) and every install path goes through
 * stopGateway() first.
 *
 * Pure helpers (channelForFlavor, channelForVersion, resolveChannel,
 * buildFeedBase) are dependency-free and tested directly. initAutoUpdate takes
 * the electron + electron-updater surfaces injected so it stays testable
 * without an Electron runtime.
 */

// Default update feed host: updates.crew.kiro.dev, the pointer hostname of the
// public distribution CDN (CloudFront + OAC over the kirocrew-updates bucket).
//
// electron-updater's generic provider treats the configured URL as a DIRECTORY
// and resolves <base>/latest-mac.yml (macOS) or <base>/latest-linux.yml (Linux)
// from it. The artifact URLs inside those files are ABSOLUTE and point at the
// byte hostname (download.crew.kiro.dev), which is what preserves our
// pointer/bytes host split: `new URL(fileUrl, base)` ignores the base when
// fileUrl is absolute. That behaviour is structural but undocumented, so
// test/auto-update.test.js pins it against the real installed library — a
// version bump that changes it must fail CI, not strand installs in the field.
const {
  classifyBundleLocation,
  containingDirForBundle,
  canInstallUpdates,
  classifyLinuxInstall,
  containingDirForAppImage,
  canUpdateLinuxInstall,
  describeLinuxInstall,
} = require("./bundle-location");

// The Linux package formats this app ships. A format is BOTH the feed
// sub-directory a package install reads its channel file from AND the download
// extension its manual-reinstall link must use, so one set serves both: the
// format has to be known rather than assumed, because `package-type` is the only
// signal that names it and the resourcesPath fallback in classifyLinuxInstall()
// proves only that this IS a package. An unnamed format therefore stays empty,
// canUpdateLinuxInstall() refuses, and the download link falls back to the
// AppImage — instead of pointing an rpm install at deb bytes either way.
const LINUX_PACKAGE_EXTENSIONS = new Set(["deb", "rpm"]);

/**
 * Which Linux install shape is running, resolved from the three signals that
 * exist at runtime. I/O-bearing (it reads the package-type file), so it sits
 * here rather than in the pure bundle-location module, and every input is
 * injectable so tests never touch a real filesystem.
 *
 * @param {object} [o]
 * @param {object} [o.env=process.env]
 * @param {string} [o.resourcesPath=process.resourcesPath]
 * @returns {{kind:string, format:string, appImagePath:string}}
 */
function resolveLinuxInstall({ env = process.env, resourcesPath = process.resourcesPath } = {}) {
  const appImagePath = (env && env.APPIMAGE) || "";
  let packageType = "";
  try {
    const fs = require("fs");
    const path = require("path");
    packageType = fs.readFileSync(path.join(resourcesPath || "", "package-type"), "utf8").trim();
  } catch {
    // Absent on an AppImage and on any build whose target had no publish config
    // — the other two signals cover both, so this is a normal case, not a fault.
    packageType = "";
  }
  const kind = classifyLinuxInstall({ appImagePath, packageType, resourcesPath });
  const format = kind === "package" && LINUX_PACKAGE_EXTENSIONS.has(packageType) ? packageType : "";
  return { kind, format, appImagePath };
}

// Can the AppImage replace itself, i.e. is the directory HOLDING the image
// writable? AppImageUpdater stages the new image beside the old one and `mv`s it
// over the original, so the containing directory — not the mounted, read-only
// squashfs the app runs from — is what must be writable. Same fail-safe TRUE as
// isBundleContainerWritable: a probe that cannot run must not read as
// "un-updatable".
function isAppImageContainerWritable(appImagePath) {
  const dir = containingDirForAppImage(appImagePath);
  if (!dir) return true;
  try {
    const fs = require("fs");
    fs.accessSync(dir, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

// Can the macOS installer write the directory holding our .app (i.e. replace
// the bundle)? electron-updater does NOT install on macOS itself: MacUpdater
// serves the downloaded .zip over a loopback HTTP server and delegates to
// Electron's built-in autoUpdater (Squirrel.Mac), so the install still ends in
// ShipIt swapping the .app in place — which needs the CONTAINING directory to
// be writable. Verified against electron-updater 6.8.9 `out/MacUpdater.js`.
//
// `fs` is required lazily, matching this module's style of pulling Node builtins
// inside the function that needs them rather than at load time.
// Fail-safe TRUE: a probe that cannot run must never be read as "un-updatable",
// or one unreadable path would disable updates for everyone.
function isBundleContainerWritable(resourcesPath) {
  const dir = containingDirForBundle(resourcesPath);
  if (!dir) return true;
  try {
    const fs = require("fs");
    fs.accessSync(dir, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

const DEFAULT_FEED_BASE = "https://updates.crew.kiro.dev/feed";
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall

/**
 * Platforms with a working publish lane + updater.
 *
 * win32 is packaged as NSIS and driven by NsisUpdater, which reads `latest.yml`
 * from the same per-channel feed directory the other platforms use and verifies
 * the downloaded installer's Authenticode signature fail-closed. Both of its
 * prerequisites are in place: publish-windows.yml writes that feed, and it
 * refuses to publish an installer whose signature or publisher does not verify,
 * so the fail-closed check cannot be handed bytes it will reject.
 */
const SUPPORTED_PLATFORMS = new Set(["darwin", "linux", "win32"]);

// Byte host for human (manual) downloads -- deliberately the same CDN the
// updater pulls from, so a manual reinstall lands on identical artifacts.
const DOWNLOAD_BASE = "https://download.crew.kiro.dev";
// Channels with a desktop publish lane. "dev" has none.
const KNOWN_CHANNELS = new Set(["nightly", "insider", "stable"]);
// Channels with a WINDOWS publish lane. publish-windows.yml is wired into
// nightly.yml and release.yml's insider path only: stable republishes the
// immutable promotion bundle, whose artifact roles do not include a Windows
// installer, so no stable Windows feed or installer exists to point at.
//
// Without this set a Windows user who selects Stable resolves
// feed/stable/latest.yml and desktop/stable/latest/KiroCrew-Setup.exe, neither
// of which is published: every check fails and the manual-download escape hatch
// is a 404. Offering nothing is the honest answer until stable has a lane.
const WINDOWS_CHANNELS = new Set(["nightly", "insider"]);

/** Whether this platform publishes artifacts for this channel. */
function channelHasLane(channel, osPlatform) {
  if (!KNOWN_CHANNELS.has(channel)) return false;
  if (osPlatform === "win32") return WINDOWS_CHANNELS.has(channel);
  return true;
}

/**
 * Map the build flavor ("beta" | "stable") to an update channel. Retained
 * for the internal beta flavor and as the fallback when the running version
 * carries no channel marker.
 * @param {"beta"|"stable"} flavor
 * @returns {"insider"|"stable"}
 */
function channelForFlavor(flavor) {
  return flavor === "beta" ? "insider" : "stable";
}

/**
 * Derive the update channel from the running version. CI stamps the app
 * version per channel (nightly.yml: <base>-nightly.<stamp>; release.yml:
 * tag-derived), so the version itself says which feed this build must
 * track. MUST mirror release.yml's tag-to-channel rule: "-nightly." is
 * nightly, any OTHER prerelease suffix (-insider.N, -rc.N, ...) is
 * insider, bare semver is stable. Without this, a nightly/insider build
 * would check the stable feed, see a differing version, and silently
 * migrate the user onto stable.
 * @param {string} version
 * @returns {"nightly"|"insider"|"stable"|null} null when unstamped (dev)
 */
function channelForVersion(version) {
  if (!version || typeof version !== "string") return null;
  if (version.includes("-nightly.")) return "nightly";
  if (version.includes("-")) return "insider";
  return "stable";
}

/**
 * Resolve the EFFECTIVE update channel from the build stamp + the user's
 * channel preference (the Settings > About switcher).
 *
 * Rules (stable ⇄ insider opt-in design):
 * - nightly-stamped builds are PINNED to nightly: the nightly app is a
 *   separate side-by-side install, and honoring a preference here would
 *   migrate the dev app onto a production channel.
 * - unstamped (dev, stamped === null) builds have no update lane; the
 *   preference cannot conjure one.
 * - production stamps (insider/stable) follow the preference when set,
 *   else their own stamp. Switching BACK can be a downgrade mid-cycle
 *   (insider 0.2.0-insider.1 -> stable 0.1.0), which is why allowDowngrade
 *   is enabled in configureUpdater.
 *
 * @param {"nightly"|"insider"|"stable"|null} stamped - channelForVersion(version)
 * @param {"insider"|"stable"|""|null|undefined} preference - user opt-in, falsy = follow stamp
 * @returns {"nightly"|"insider"|"stable"|null}
 */
function resolveChannel(stamped, preference) {
  if (stamped === "nightly") return "nightly";
  if (stamped === null) return null;
  if (preference === "insider" || preference === "stable") return preference;
  return stamped;
}

/**
 * Build the per-channel feed DIRECTORY url for the generic provider. Pure +
 * testable.
 *
 * The trailing slash is load-bearing: the provider resolves the channel file
 * with `new URL("latest-mac.yml", base)`, and without a trailing slash the
 * last path segment is replaced rather than appended (".../feed/nightly" would
 * resolve to ".../feed/latest-mac.yml" — the wrong channel, or a 404).
 * electron-updater's newBaseUrl() also normalises this, but emitting it here
 * keeps the contract explicit and independent of that internal.
 *
 * Enforces HTTPS, with plain HTTP allowed ONLY for loopback so the local
 * update harness (KIROCREW_UPDATE_FEED=http://127.0.0.1:PORT/feed) works;
 * cleartext update metadata over a real network stays rejected.
 *
 * `variant` adds one path segment below the channel, which is how a Linux
 * package install reaches its OWN channel file: electron-updater derives the
 * file NAME from platform and arch with no hook to change it, so two formats
 * cannot share a directory without one overwriting the other's metadata.
 * Separating them by directory leaves that derivation — including the
 * `-arm64` suffix — completely untouched.
 *
 * @param {{base:string, channel:string, variant?:string}} o
 * @returns {string}
 * @throws {Error} on a non-HTTPS, non-loopback base
 */
function buildFeedBase({ base, channel, variant = "" }) {
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  const tail = variant ? `${encodeURIComponent(variant)}/` : "";
  const url = `${b}/${encodeURIComponent(channel)}/${tail}`;
  const parsed = new URL(url);
  const isLoopback = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopback)) {
    throw new Error(`feed base must be https (or http on loopback): ${parsed.protocol}//${parsed.hostname}`);
  }
  return url;
}

/**
 * Human download permalink for a channel + platform, or null when there is no
 * publish lane (dev builds, Windows until publish-windows.yml lands).
 *
 * Why the UI needs this: an update that downloads but fails to APPLY leaves the
 * user with no next step -- the card simply re-offers the same update after
 * relaunch (observed in the field on 0.1.2-nightly.20260729t073648). Reinstalling
 * over the top is the supported recovery and is non-destructive: user data lives
 * in the KiroCrew home directory, never inside the app bundle.
 *
 * Computed HERE rather than in the renderer because the renderer has no
 * trustworthy platform: getInfo()'s `platform` field is a display string that
 * main.js does not currently supply, so it reports its "darwin-arm64" default on
 * every OS. osPlatform is the real one.
 *
 * Paths are the documented mutable "latest" aliases (max-age=300).
 *
 * @param {string} channel    resolved update channel
 * @param {string} osPlatform process.platform value
 * @param {string} [osArch]   process.arch value; defaults to the running arch
 * @param {string} [linuxFormat] resolved package format ("deb"/"rpm"), or "" for
 *        an AppImage / unknown shape
 * @returns {string|null}
 */
function manualDownloadUrl(channel, osPlatform, osArch = process.arch, linuxFormat = "") {
  if (!channelHasLane(channel, osPlatform)) return null;
  // The mac DMG is universal, so darwin needs no arch. Linux has no universal
  // binary: publish-linux.yml publishes one artifact per arch per format under
  // the basenames below, so handing a user the wrong one is an immediate
  // "cannot execute binary file" — or, for a package, one dpkg/rpm refuses.
  // An arch with no published lane returns null rather than guessing x86_64.
  // The format must match how they installed: offering an AppImage to someone
  // whose files are managed by a package manager invites two parallel installs,
  // so every recognised package format keeps its own extension and only an
  // AppImage (or a shape we could not name) falls back to the image.
  const linuxArch = { x64: "x86_64", arm64: "aarch64" }[osArch];
  const linuxExt = LINUX_PACKAGE_EXTENSIONS.has(linuxFormat) ? linuxFormat : "AppImage";
  // A published artifact FILENAME, not prose: the joined form is what
  // publish-linux.yml writes to the CDN, and the arch and extension are
  // interpolated because there are now six (arch, format) pairs to name.
  const linuxFile = linuxArch ? `KiroCrew-${linuxArch}.${linuxExt}` : null; // brand-ok
  // Windows ships x64 only. build-windows.yml has no arm64 leg, and Windows has
  // exactly one channel file whatever the arch (electron-updater appends an arch
  // suffix for linux alone), so a second arch means another entry in the same
  // latest.yml rather than another feed.
  const windowsFile = { x64: "KiroCrew-Setup.exe" }[osArch];
  const file = osPlatform === "darwin"
    ? "KiroCrew.dmg"
    : osPlatform === "linux"
      ? linuxFile || null
      : osPlatform === "win32"
        ? windowsFile || null
        : null;
  if (!file) return null;
  return `${DOWNLOAD_BASE}/desktop/${channel}/latest/${file}`;
}

/**
 * Apply the update-policy flags this app REQUIRES. Every one of these differs
 * from the electron-updater default, and each maps to a decision we already
 * made deliberately — so they are set in one audited place rather than
 * scattered:
 *
 * - autoDownload=false        consent-first UX: discovery must never download.
 *                             The default (true) would download megabytes on a
 *                             background check with no user action.
 * - autoInstallOnAppQuit=false FALSE ON EVERY PLATFORM, for two different
 *                             reasons -- electron-updater gives this one flag
 *                             two unrelated meanings:
 *
 *                             • Linux/Windows (AppImageUpdater/NsisUpdater
 *                               extend BaseUpdater): it means what the name
 *                               says. BaseUpdater.addQuitHandler() installs on
 *                               quit WITHOUT stopping the Python gateway.
 *                               deferredInstallOnQuit() does it in order.
 *
 *                             • macOS (MacUpdater extends AppUpdater, NOT
 *                               BaseUpdater, so electron-updater registers no
 *                               quit handler at all): it decides WHEN Squirrel
 *                               is handed the zip. That is NOT merely a latency
 *                               choice, because Squirrel.Mac arms the installer
 *                               at STAGE time, not at install time:
 *                               SQRLUpdater's prepareUpdateForInstallation
 *                               writes ShipItState.plist and LAUNCHES ShipIt,
 *                               a launchd job that waits on our pid and swaps
 *                               the bundle as soon as we die -- by any exit,
 *                               including a crash, Force Quit or logout.
 *                               Electron documents the consequence: "a
 *                               successfully downloaded update will always be
 *                               applied the next time the application starts."
 *                               quitAndInstall() only flips
 *                               launchAfterInstallation and terminates.
 *
 *                               Keeping this false is therefore what makes the
 *                               gateway-before-swap ordering SELF-ENFORCING:
 *                               Squirrel cannot swap because it does not have
 *                               the bytes until quitAndInstall(), which is only
 *                               reachable after an awaited stopGateway(). There
 *                               is no API to un-arm ShipIt once armed, so
 *                               eager staging would also defeat retraction --
 *                               a withdrawn build would still install on quit.
 *
 *                               Cost of this choice: the ~350MB loopback pull
 *                               happens inside quitAndInstall(), so the handoff
 *                               is slow. forceExitFailsafe() is gated on
 *                               before-quit-for-update precisely because of
 *                               that. Making staging eager safely needs an
 *                               "armed" flag that is never cleared plus an
 *                               awaited stopGateway() on EVERY quit path.
 *
 * - allowDowngrade=true       our update gate is DIFFERENCE-based, not
 *                             greater-than: a feed repointed to an older
 *                             version must be offered. This is what makes
 *                             channel switch-back and version RETRACTION work.
 * - allowPrerelease=true      every nightly (-nightly.<stamp>) and insider
 *                             (-insider.N) stamp is a semver prerelease and
 *                             would otherwise be invisible to its own channel.
 *
 * @param {object} autoUpdater electron-updater AppUpdater
 */
function configureUpdater(autoUpdater) {
  autoUpdater.autoDownload = false;
  // Never true. See the note above: on darwin this is a staging-time switch and
  // staging is what arms ShipIt, so flipping it hands Squirrel a licence to swap
  // the bundle on ANY exit -- including exits that skip our gateway teardown.
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.allowDowngrade = true;
  autoUpdater.allowPrerelease = true;
}

/**
 * Classify an updater failure into a STABLE code the renderer can translate,
 * plus a short detail string.
 *
 * Why a code instead of a message: the pre-migration client hand-rolled its
 * fetch and so produced its own curated text ("feed HTTP 404", "feed request
 * timed out"). electron-updater owns fetching now, and its exceptions are
 * written for developers reading logs -- HttpErrors are multi-line dumps, and a
 * checksum mismatch is a digest comparison no user can act on. Emitting a code
 * keeps the user-facing wording in the renderer where it can be localized,
 * instead of shipping English from the main process (#736).
 *
 * `detail` is the first line only, length-capped: enough to disambiguate two
 * failures of the same class without pasting a stack into a settings panel.
 * The full error still goes to the log.
 *
 * @param {unknown} err
 * @returns {{code:string, detail:string, httpStatus?:number}}
 */
function classifyError(err) {
  const raw = String((err && err.message) || err || "");
  const code = (err && err.code) || "";
  const status = err && (err.statusCode || err.status);
  const detail = raw.split("\n")[0].slice(0, 200);

  // Order matters: check the specific signals before the generic HTTP one,
  // since a 404 on the channel file is far more actionable than "HTTP 404".
  if (code === "ERR_UPDATER_CHANNEL_FILE_NOT_FOUND" || /Cannot find channel/i.test(raw)) {
    return { code: "no-release", detail };
  }
  if (code === "ERR_UPDATER_NO_CHECKSUM" || /sha512|checksum/i.test(raw)) {
    return { code: "integrity", detail };
  }
  if (/ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENETUNREACH|socket hang up|timed? ?out/i.test(`${code} ${raw}`)) {
    return { code: "offline", detail };
  }
  if (typeof status === "number") {
    return { code: "server", detail, httpStatus: status };
  }
  if (code === "ERR_UPDATER_INVALID_UPDATE_INFO" || /ENOENT/i.test(`${code} ${raw}`)) {
    return { code: "misconfigured", detail };
  }
  return { code: "unknown", detail };
}

/**
 * Wire electron-updater. All Electron surfaces injected for testability.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {object} deps.autoUpdater            - electron-updater AppUpdater
 * @param {typeof import("electron").dialog} deps.dialog
 * @param {typeof import("electron").Notification} deps.Notification
 * @param {() => string} deps.getFlavor        - returns "beta" | "stable"
 * @param {() => Promise<void>} deps.stopGateway - graceful, awaitable gateway stop
 * @param {string} [deps.platform]             - display arch, e.g. "darwin-arm64"
 * @param {string} [deps.osPlatform]           - process.platform override (tests)
 * @param {string} [deps.osArch]               - process.arch override (tests). Picks the
 *   per-arch Linux AppImage for the manual-reinstall link; darwin ignores it
 *   (the DMG is universal).
 * @param {string} [deps.resourcesPath]        - process.resourcesPath override
 *   (tests). Used only to classify where the bundle runs FROM, so a
 *   translocated / read-only-volume install can be refused an update lane.
 * @param {(p:string) => boolean} [deps.probeBundleWritable] - writability probe
 *   for the bundle's containing directory. Injected because the real one does
 *   filesystem I/O: a test cannot make /Volumes/X writable, so without a seam
 *   the "writable external disk still updates" case is unassertable.
 * @param {object} [deps.nativeAutoUpdater]     - Electron's native autoUpdater, observed
 *   for `before-quit-for-update` to know the installer took over (tests inject a stub)
 * @param {string} [deps.feedBase]             - override feed host
 * @param {(state:object) => void} [deps.onUpdateState] - if provided, the
 *   in-app UI drives the install prompt: state transitions are pushed here
 *   ({state, version, notes, channel}) and the native dialog is suppressed.
 * @param {{info:Function,warn:Function,error:Function}} [deps.log]
 * @returns {{check:Function, download:Function, install:Function, getInfo:Function}}
 */
function initAutoUpdate(deps) {
  const {
    app,
    autoUpdater,
    dialog,
    Notification,
    getFlavor,
    getChannelPreference = () => "",
    notifyUpdateFound = null,
    stopGateway,
    // Host hook: an install is now in flight, so a gateway that stops answering
    // is INTENTIONAL. main.js uses it to disarm the liveness watchdog, which
    // otherwise resurrects the gateway mid-swap. Optional (absent in tests).
    onInstallDispatched = null,
    // Host hook: the install FAILED after dispatch (Squirrel error at handoff
    // time). The gateway was stopped on purpose and recovery was disarmed, so
    // without this the user is left in a live app whose dashboard is dead until
    // they relaunch by hand. main.js re-arms recovery and respawns the gateway.
    onInstallFailed = null,
    platform = "darwin-arm64",
    osPlatform = process.platform,
    osArch = process.arch,
    resourcesPath = process.resourcesPath,
    probeBundleWritable = isBundleContainerWritable,
    // Linux install shape + its AppImage writability probe, injected for the
    // same reason as probeBundleWritable: the verdict must be assertable in a
    // test without a real AppImage mount or a real /opt install.
    linuxInstall = null,
    probeAppImageWritable = isAppImageContainerWritable,
    // Electron's NATIVE autoUpdater, used only to observe
    // `before-quit-for-update` -- the signal that the platform installer has
    // actually taken over (see forceExitFailsafe). electron-updater drives it
    // internally on macOS; we never call it. Resolved lazily so the module still
    // loads outside an Electron runtime (tests), where it is simply absent.
    nativeAutoUpdater = (() => {
      try { return require("electron").autoUpdater || null; } catch { return null; }
    })(),
    feedBase = process.env.KIROCREW_UPDATE_FEED || DEFAULT_FEED_BASE,
    onUpdateState = null,
    log = console,
  } = deps;

  // Linux install shape. Resolved once, and BEFORE getInfo() is defined: the
  // early-return stubs below hand getInfo out, so a renderer could call it
  // before a later declaration initialised — a temporal dead zone crash on the
  // one path that exists to report a problem gracefully. The signals cannot
  // change while the process lives, and re-reading package-type per check would
  // add a synchronous file read to a path that runs every four hours.
  const linux = osPlatform === "linux"
    ? (linuxInstall || resolveLinuxInstall({ resourcesPath }))
    : { kind: "", format: "", appImagePath: "" };

  // When the in-app UI is wired (onUpdateState provided), it owns the prompt;
  // the native dialog stays as the fallback for headless / no-renderer cases.
  const uiDriven = typeof onUpdateState === "function";
  // Last lifecycle payload handed to the UI. Pushed state dies with the
  // renderer: the post-install-failure recovery path reloads the window, and a
  // fresh mount that only ever LISTENS would render as if nothing happened --
  // the failure card (and its Retry) silently vanish. getInfo() carries this
  // back out so the renderer can replay it on mount, which keeps the boot path
  // untouched (the renderer already requests the info payload).
  let lastEmittedState = null;
  // Single channel resolver used for the feed AND everything reported to
  // the UI. Read the preference FRESH on every call: configureFeed() runs
  // per check, so a Settings channel switch takes effect on the next check
  // with no re-init. Flavor stays the unstamped-dev display fallback.
  function currentChannel() {
    const stamped = channelForVersion(app.getVersion());
    return resolveChannel(stamped, getChannelPreference()) || channelForFlavor(getFlavor());
  }
  function emit(state, extra = {}) {
    if (!uiDriven) return;
    const payload = { state, channel: currentChannel(), version: app.getVersion(), ...extra };
    // Remembered even when the push below throws: a renderer that missed the
    // push is exactly the one the getInfo() replay exists to catch up.
    lastEmittedState = payload;
    try {
      onUpdateState(payload);
    } catch (err) {
      log.error("[update] onUpdateState threw", err);
    }
  }
  function getInfo() {
    const stamped = channelForVersion(app.getVersion());
    // Observability for the replay path: without this line a replayed state is
    // indistinguishable from a live emit in the log, so a report of "the
    // failure card came back / didn't come back" has no evidence to read.
    if (lastEmittedState) {
      log.info(`[update] getInfo carrying replay seed: ${lastEmittedState.state}`
        + (lastEmittedState.phase ? ` (phase ${lastEmittedState.phase})` : ""));
    }
    return {
      version: app.getVersion(),
      channel: currentChannel(),
      // Switcher inputs: the build's own lane, whether this build may switch
      // (nightly is pinned; dev has no lane), and the stored preference.
      stampedChannel: stamped,
      channelSwitchable: stamped === "insider" || stamped === "stable",
      channelPreference: getChannelPreference() || "",
      platform,
      packaged: !!app.isPackaged,
      // Escape hatch for a failed install (see manualDownloadUrl).
      downloadUrl: manualDownloadUrl(currentChannel(), osPlatform, osArch, linux.format),
      // Replay seed for a freshly mounted renderer (see lastEmittedState).
      lastState: lastEmittedState,
    };
  }

  // Updating requires an installed, signed bundle (macOS code signature
  // validation is mandatory for Squirrel.Mac; Linux AppImage needs the
  // AppImage runtime), so dev builds have no update lane.
  if (!app.isPackaged) {
    log.info("[update] dev build — auto-update disabled");
    return { check: () => {}, download: async () => {}, install: async () => {}, getInfo, disabled: "dev" };
  }
  if (!SUPPORTED_PLATFORMS.has(osPlatform)) {
    log.info(`[update] ${osPlatform} — auto-update disabled (no publish lane yet)`);
    return { check: () => {}, download: async () => {}, install: async () => {}, getInfo, disabled: "platform" };
  }
  // A platform can have a lane on some channels and not others: Windows
  // publishes nightly and insider but not stable (see WINDOWS_CHANNELS). Arming
  // the updater against a channel with no feed makes every check fail on a 404
  // and leaves the manual-download link pointing at nothing, so report it the
  // same way the dev and platform paths do -- About then shows "unavailable"
  // instead of a Check button that can only ever error.
  //
  // Evaluated once at init, while currentChannel() is read per check: switching
  // channels in Settings mid-session surfaces the ordinary failure card until
  // the next launch, which the UI already handles.
  if (!channelHasLane(currentChannel(), osPlatform)) {
    log.info(`[update] ${osPlatform} has no ${currentChannel()} publish lane — auto-update disabled`);
    return { check: () => {}, download: async () => {}, install: async () => {}, getInfo, disabled: "channel" };
  }
  // The macOS install is an IN-PLACE replacement of the running .app:
  // electron-updater's MacUpdater hands the downloaded zip to Electron's
  // built-in autoUpdater (Squirrel.Mac) over a loopback server, and ShipIt
  // swaps the bundle. From a Gatekeeper App Translocation copy, or a read-only
  // disk image, there is no bundle it can usefully replace — so arming the
  // updater means downloading every release and failing the swap forever, with
  // nothing surfaced to the user. electron-updater has no check of its own here
  // (6.8.9 has no writability, /Volumes or translocation probe anywhere), so
  // refuse up front. A /Volumes path is NOT refused on its own: an external disk
  // or network share lives there too and is perfectly replaceable, so the
  // verdict rests on whether the bundle's containing directory is writable.
  //
  // macOS only, by construction: classifyBundleLocation() returns "other" for
  // every non-darwin platform, so this is a no-op on Linux. Linux asks the same
  // question through its own signals, immediately below, because the two
  // platforms agree on nothing but the question: an AppImage self-replaces via
  // `mv` into dirname($APPIMAGE) and so shares the writability requirement,
  // while a deb install is handed to dpkg behind an elevation prompt and needs
  // no writable directory at all.
  // ... and carry the reason out as `disabled`, exactly like the dev/platform
  // paths above: main.js merges it into the info payload it hands the renderer,
  // so About shows "unavailable" instead of a live Check button that no-ops.
  const bundleLocation = classifyBundleLocation(resourcesPath, { platform: osPlatform });
  const bundleWritable = probeBundleWritable(resourcesPath);
  if (!canInstallUpdates(bundleLocation, { bundleWritable })) {
    log.info(`[update] running from ${bundleLocation} (writable=${bundleWritable}) — auto-update `
      + "disabled (the installer cannot replace the bundle; move the app to /Applications)");
    return {
      check: () => {},
      download: async () => {},
      install: async () => {},
      getInfo,
      disabled: bundleLocation,
    };
  }

  if (osPlatform === "linux") {
    const imageWritable = linux.kind === "appimage"
      ? probeAppImageWritable(linux.appImagePath)
      : true;
    if (!canUpdateLinuxInstall(linux.kind, { imageWritable, packageFormat: linux.format })) {
      const reason = linux.kind === "package" ? "linux-package-unknown-format" : "appimage-readonly";
      log.info(`[update] auto-update disabled (${reason}): `
        + describeLinuxInstall(linux.kind, { imageWritable, packageFormat: linux.format }));
      return {
        check: () => {},
        download: async () => {},
        install: async () => {},
        getInfo,
        disabled: reason,
      };
    }
    log.info(`[update] linux install: ${linux.kind}${linux.format ? ` (${linux.format})` : ""}`);
  }

  configureUpdater(autoUpdater);
  autoUpdater.logger = log;

  let updateReady = false;
  let downloading = false;
  let stagedVersion = null; // version electron-updater has downloaded + staged
  let stagedNotes = "";
  let foundVersion = null; // last version surfaced to the user, awaiting consent
  let installing = false;
  let quitHandled = false;
  let checking = false;

  /**
   * Version of the update currently being fetched/held -- NOT the running
   * app's version. Every state the UI renders a version for must pass this
   * explicitly: emit() defaults `version` to app.getVersion() so the
   * check/not-available/error states report the running build, and a
   * "downloading" event that omitted it made the update card claim the app
   * was downloading the version already installed (fixed in #709; preserved
   * here through the electron-updater migration).
   */
  /**
   * Emit a failure WITH ITS PHASE. Without the phase the renderer cannot tell a
   * discovery failure from a download failure, so it labelled every error
   * "Couldn't check for updates" and unmounted the update card -- a user who
   * clicked Download saw a complaint about checking and lost the version they
   * had just consented to (#735).
   *
   * A download-phase failure also carries the pending version, so the card can
   * stay on screen and offer a retry instead of vanishing.
   *
   * @param {"check"|"download"|"install"} phase
   * @param {unknown} err
   */
  function emitError(phase, err) {
    const { code, detail, httpStatus } = classifyError(err);
    log.error(`[update] ${phase} failed (${code})`, err);
    emit("error", {
      phase,
      code,
      message: detail,
      ...(httpStatus === undefined ? {} : { httpStatus }),
      ...(phase === "download" ? { version: pendingVersion() } : {}),
    });
  }

  function pendingVersion() {
    return foundVersion || stagedVersion || app.getVersion();
  }

  function configureFeed() {
    const channel = currentChannel();
    // A package install reads its channel file from a per-format subdirectory,
    // so the two Linux formats never overwrite each other's metadata.
    const url = buildFeedBase({ base: feedBase, channel, variant: linux.format });
    autoUpdater.setFeedURL({ provider: "generic", url });
    log.info(`[update] feed: ${url}`);
    return url;
  }

  /**
   * DISCOVERY ONLY. With autoDownload=false, checkForUpdates() fetches the
   * channel file, compares versions (difference-based via allowDowngrade) and
   * emits update-available / update-not-available WITHOUT downloading. The
   * download requires the explicit download() consent call below.
   */
  async function safeCheck() {
    if (checking) return;
    if (downloading) {
      // A download is in flight. Re-entering the check would restart the
      // updater's flow underneath the running download; report progress
      // instead. update-downloaded/error clears the flag.
      log.info("[update] check requested while download in flight — reporting progress");
      emit("downloading", { version: pendingVersion() });
      return;
    }
    if (updateReady && stagedVersion) {
      // NOTE: deliberately NOT a short-circuit. A check must ALWAYS consult
      // the feed, even with a version already staged, because a NEWER version
      // can ship mid-session — returning early here would pin the user to the
      // stale stage until they installed or restarted. The update-available
      // handler distinguishes "the staged one is still latest" (re-surface the
      // install prompt) from "the stage is superseded" (drop it and re-find).
      log.info(`[update] ${stagedVersion} staged — checking whether it is still latest`);
    }
    checking = true;
    try {
      configureFeed(); // re-read flavor/channel each check
      emit("checking");
      await autoUpdater.checkForUpdates();
    } catch (err) {
      emitError("check", err);
    } finally {
      checking = false;
    }
  }

  /**
   * Explicit user consent: download the version last surfaced by safeCheck.
   * Never called automatically — this is the whole point of autoDownload=false.
   */
  async function startDownload() {
    if (downloading) { emit("downloading", { version: pendingVersion() }); return; }
    if (updateReady && stagedVersion) {
      emit("downloaded", { version: stagedVersion, notes: stagedNotes });
      return;
    }
    if (!foundVersion) {
      // Nothing discovered yet (e.g. UI raced the first check). Discover
      // first; the user can consent once "found" is surfaced.
      log.info("[update] download requested with nothing found — checking first");
      await safeCheck();
      return;
    }
    log.info(`[update] user consented — downloading ${foundVersion}`);
    downloading = true;
    emit("downloading", { version: pendingVersion() });
    try {
      await autoUpdater.downloadUpdate();
    } catch (err) {
      downloading = false;
      emitError("download", err);
    }
  }

  // Force-exit failsafe — ONLY safe once the platform's installer has actually
  // taken over.
  //
  // Why this is event-gated and not a plain timer: on macOS the expensive work
  // happens INSIDE quitAndInstall(), not before it. Because
  // autoInstallOnAppQuit=false (deliberately -- see configureUpdater),
  // electron-updater withholds the downloaded zip from Squirrel until install
  // time, so quitAndInstall() returns immediately while Squirrel is still
  // fetching ~350MB from the loopback proxy, unpacking it and verifying its
  // signature. A 5s app.exit(0) lands in the middle of that: the staged app is
  // left on disk, ShipIt is never armed, and the user relaunches into the OLD
  // version with no error shown. Observed in the field on
  // 0.1.2-nightly.20260729t073648.
  //
  // The pre-migration client was safe with the same 5s constant because it drove
  // Squirrel directly: "update-downloaded" then meant Squirrel had ALREADY
  // staged the bundle, so quitAndInstall() was a millisecond-scale handoff. The
  // migration changed what that event means; the timer did not notice.
  //
  //
  // `before-quit-for-update` is emitted by Electron's native autoUpdater when
  // the install is genuinely armed and the app is being torn down for it -- the
  // only signal that proves the handoff happened. Until it fires, exiting can
  // only destroy the update. On darwin the failsafe therefore stays DISARMED
  // and Squirrel quits the app itself; the original hazard it guarded (a
  // renderer beforeunload or lingering child blocking the quit, letting ShipIt
  // abort with "App Still Running Error" Code=-9) is handled by exiting only
  // AFTER that event.
  function forceExitFailsafe(reason) {
    const arm = () => {
      const t = setTimeout(() => {
        log.error(`[update] still alive ${FORCE_EXIT_AFTER_MS}ms after the installer took over (${reason}) — forcing exit so the swap can proceed`);
        try { app.exit(0); } catch { process.exit(0); }
      }, FORCE_EXIT_AFTER_MS);
      if (typeof t.unref === "function") t.unref();
    };

    // The native updater is the one that emits this; electron-updater's
    // BaseUpdater re-emits it for the platforms it installs itself.
    const native = nativeAutoUpdater;
    if (native && typeof native.once === "function") {
      native.once("before-quit-for-update", () => {
        log.info(`[update] installer took over (${reason}) — arming the exit failsafe`);
        arm();
      });
      return;
    }
    // No native updater surface to listen on (tests, unexpected platform):
    // fall back to the timer rather than losing the guarantee entirely.
    arm();
  }

  // isForceRunAfter=true so the user lands back in the app after the swap.
  //
  // isSilent is platform-dependent, and on Windows it decides whether this is an
  // automatic update at all. NsisUpdater passes /S only when isSilent, and the
  // installer is assisted (nsis.oneClick=false), so isSilent=false shows the
  // full NSIS wizard: the app would quit and then sit waiting for the user to
  // click through a setup dialog, which is not the silent swap macOS and Linux
  // perform. macOS and Linux have no installer UI to suppress, and passing
  // isSilent there would change which relaunch flag BaseUpdater honours, so the
  // flag is set only for win32.
  function quitAndInstall() {
    autoUpdater.quitAndInstall(osPlatform === "win32", true);
  }

  async function applyUpdateAndRestart() {
    if (installing) return;
    // REQUIRE a staged update. Without this guard an install() dispatched
    // before the download finished reaches MacUpdater.quitAndInstall()'s
    // squirrelDownloadedUpdate === false branch, which does NOT install --
    // it registers a listener and waits for Squirrel to fetch the update from
    // the loopback proxy. forceExitFailsafe would then kill the process 5s
    // later, mid-fetch, and the app dies without swapping or relaunching.
    // Once a stage exists, Squirrel has already consumed the zip and
    // quitAndInstall proceeds immediately, so the failsafe is safe to arm.
    if (!updateReady) {
      log.info("[update] install requested with nothing staged — ignoring");
      emit(foundVersion ? "found" : "not-available", foundVersion ? { version: foundVersion } : {});
      return;
    }
    installing = true;
    // Tell the renderer the install is UNDERWAY before anything goes silent:
    // the gateway is about to be stopped on purpose, and without this state
    // the dashboard renders the stoppage as an outage (offline pill, failed
    // requests) while the swap is still staging. On a failed handoff the
    // 'error' emit (phase "install") replaces this state, which is what
    // clears the renderer's installing overlay.
    emit("installing", { version: stagedVersion });
    // BEFORE stopGateway, or the watchdog can win the race and respawn the
    // gateway into the middle of the bundle swap.
    try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
    // STRICT ORDER: stop the gateway and await its exit, THEN quitAndInstall.
    // A live gateway child during the bundle swap can leave a half-replaced app.
    log.info("[update] stopping gateway before install");
    try {
      await stopGateway();
    } catch (err) {
      log.error("[update] gateway stop errored (continuing to install)", err);
    }
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] gateway down — quitAndInstall");
    quitAndInstall();
    forceExitFailsafe("manual install");
  }

  // If the user chose "Later", install on the natural quit. This is OUR
  // implementation rather than autoInstallOnAppQuit=true precisely because the
  // gateway must be stopped first; before-quit can't await async work, so
  // preventDefault, stop the gateway, then quitAndInstall.
  function deferredInstallOnQuit(event) {
    if (quitHandled || !updateReady) return;
    quitHandled = true;
    event.preventDefault();
    (async () => {
      // Same signal as the manual path: the window can stay visible for
      // several seconds while the gateway stops and the installer stages the
      // bundle, and the renderer must not read that silence as an outage.
      emit("installing", { version: stagedVersion });
      // No onInstallDispatched here: this handler only runs from before-quit,
      // where main.js has already set isQuitting -- the watchdog is covered.
      log.info("[update] deferred install on quit");
      try { await stopGateway(); } catch (err) { log.error("[update] stop on quit errored", err); }
      quitAndInstall();
      forceExitFailsafe("deferred install on quit");
    })();
  }

  async function promptInstall(versionName, notes) {
    const { response } = await dialog.showMessageBox({
      type: "info",
      buttons: ["Restart & Update", "Later"],
      defaultId: 0,
      cancelId: 1,
      title: "Kiro Crew update ready",
      message: `Kiro Crew ${versionName || ""} is ready to install.`.trim(),
      detail:
        (notes || "").slice(0, 500) +
        "\n\nKiro Crew will stop the local gateway, install the update, and relaunch.",
    });
    if (response === 0) {
      await applyUpdateAndRestart();
    } else {
      app.once("before-quit", deferredInstallOnQuit);
      try {
        new Notification({
          title: "Update deferred",
          body: "Kiro Crew will finish updating the next time you quit.",
        }).show();
      } catch { /* notifications optional */ }
    }
  }

  /** releaseNotes is string | {version,note}[] | null depending on the feed. */
  function notesFrom(info) {
    const n = info && info.releaseNotes;
    if (typeof n === "string") return n;
    if (Array.isArray(n)) return n.map((e) => (e && e.note) || "").filter(Boolean).join("\n\n");
    return "";
  }

  autoUpdater.on("error", (err) => {
    // The library funnels every failure through one event, so derive the phase
    // from what we were doing. Read the flags BEFORE clearing `downloading`, or
    // a mid-download failure would be reported as a check failure.
    const phase = downloading ? "download" : installing ? "install" : "check";
    downloading = false;
    if (phase === "install") {
      // The dispatch is over: allow a retry (updateReady is still true -- the
      // zip is still staged) and tell the host to bring the gateway back.
      // Observed live in the OTA lane: a Squirrel signature rejection lands
      // here; without recovery the app survives with a dead dashboard.
      installing = false;
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
    }
    emitError(phase, err);
  });
  autoUpdater.on("checking-for-update", () => { log.info("[update] checking…"); emit("checking"); });
  autoUpdater.on("update-not-available", () => {
    downloading = false;
    foundVersion = null;
    // Clear the STAGED state too, not just the found state. The feed reporting
    // "no update" while something is staged is exactly the retraction path
    // (a feed repointed to the running version) and the channel-switch-back
    // path -- and a stage left armed here would still install the withdrawn or
    // wrong-channel build on the next quit, because deferredInstallOnQuit only
    // checks updateReady. Disarm the quit hook as well or the listener
    // survives to fire against a stage we just invalidated.
    if (updateReady) {
      log.info(`[update] feed reports up to date -- discarding staged ${stagedVersion}`);
    }
    updateReady = false;
    stagedVersion = null;
    stagedNotes = "";
    quitHandled = false;
    app.removeListener("before-quit", deferredInstallOnQuit);
    log.info("[update] up to date");
    emit("not-available");
  });
  // CONSENT GATE: with autoDownload=false this fires on DISCOVERY, before any
  // bytes move. Surface what was found and wait for an explicit download().
  autoUpdater.on("update-available", (info) => {
    foundVersion = (info && info.version) || null;
    // A stage is only useful if it is still the latest thing on the feed.
    // Because the RUNNING version never changes mid-session, the updater
    // reports "available" for the staged version too — so the comparison
    // below is what separates the two cases.
    if (updateReady && stagedVersion) {
      if (foundVersion === stagedVersion) {
        log.info(`[update] ${stagedVersion} already downloaded — awaiting install`);
        emit("downloaded", { version: stagedVersion, notes: stagedNotes });
        return;
      }
      // Superseded: drop the stale stage so consent re-downloads the NEWEST
      // build rather than installing an already-old one.
      log.info(`[update] staged ${stagedVersion} superseded by ${foundVersion} — discarding stage`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
      app.removeListener("before-quit", deferredInstallOnQuit);
    }
    log.info(`[update] found ${foundVersion} (running ${app.getVersion()}) — awaiting user consent`);
    // Nudge hook: main.js shows a native notification pointing at
    // Settings > About (deduped there, once per version). Discovery-only —
    // download/install still require the explicit consent actions.
    if (typeof notifyUpdateFound === "function") {
      try { notifyUpdateFound(foundVersion); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
    }
    emit("found", {
      version: foundVersion,
      notes: notesFrom(info),
      pubDate: (info && info.releaseDate) || "",
    });
  });
  autoUpdater.on("download-progress", (p) => {
    // New capability vs. the hand-rolled updater: real progress, so the card
    // can show a percentage instead of an indeterminate "downloading".
    emit("downloading", {
      version: pendingVersion(),
      percent: p && typeof p.percent === "number" ? p.percent : undefined,
      bytesPerSecond: p && p.bytesPerSecond,
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    updateReady = true;
    downloading = false;
    stagedVersion = (info && info.version) || null;
    stagedNotes = notesFrom(info);
    log.info(`[update] downloaded ${stagedVersion} — ${uiDriven ? "notifying UI" : "prompting"}`);
    emit("downloaded", { version: stagedVersion || app.getVersion(), notes: stagedNotes });
    if (uiDriven) {
      // In-app UI owns the prompt. Still install on a natural quit if the user
      // dismisses the modal with "Later" (mirrors the native dialog's deferral).
      app.once("before-quit", deferredInstallOnQuit);
    } else {
      promptInstall(stagedVersion, stagedNotes);
    }
  });

  configureFeed();
  const launchTimer = setTimeout(safeCheck, LAUNCH_CHECK_DELAY_MS);
  const pollTimer = setInterval(() => { if (!updateReady) safeCheck(); }, CHECK_INTERVAL_MS);
  // Timers must never hold the process open (Electron quit, tests).
  if (typeof launchTimer.unref === "function") launchTimer.unref();
  if (typeof pollTimer.unref === "function") pollTimer.unref();

  // Renderer-callable triggers (wired to ipcMain in main.js). Background
  // timers only ever DISCOVER (safeCheck emits "found") — downloading
  // requires the explicit download() consent call.
  return {
    check: () => safeCheck(),
    download: () => startDownload(),
    install: () => applyUpdateAndRestart(),
    getInfo,
    isReady: () => updateReady,
  };
}

module.exports = {
  initAutoUpdate,
  channelForFlavor,
  channelForVersion,
  resolveChannel,
  buildFeedBase,
  configureUpdater,
  classifyError,
  manualDownloadUrl,
  resolveLinuxInstall,
  DEFAULT_FEED_BASE,
  DOWNLOAD_BASE,
  SUPPORTED_PLATFORMS,
};
