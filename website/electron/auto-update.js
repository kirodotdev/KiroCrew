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

// The externally-managed marker, named after the PEP 668 precedent: a distro or
// enterprise packager that owns this install's update lifecycle drops this file
// into the packaged resources (beside `package-type` and `backend-dist`, the
// established outside-asar packager surface). Its PRESENCE is the whole signal;
// the JSON body only adds display metadata.
const EXTERNALLY_MANAGED_MARKER = "EXTERNALLY-MANAGED";
// Read cap for the marker and display caps for its fields. The marker is an
// operator/packager-owned local file, but this code runs synchronously during
// main-process startup: an unbounded read of a huge file (or a symlink into a
// FIFO/device) must not be able to stall or exhaust the app. An over-cap or
// non-regular entry still counts as MANAGED — presence is the signal — just
// with no metadata to show.
const EXTERNALLY_MANAGED_MAX_BYTES = 8192;
const MANAGED_BY_MAX_CHARS = 128;
const UPDATE_COMMAND_MAX_CHARS = 512;
const CHECK_COMMAND_MAX_CHARS = 512;

/**
 * Is this install's update lifecycle owned by an external package manager?
 *
 * Lookup order: the `KIROCREW_EXTERNALLY_MANAGED` env var (a path to a marker
 * file, or any other non-empty value to mark the install managed with no
 * metadata — the test-harness seam, mirroring `KIROCREW_UPDATE_FEED`, and
 * honored ONLY on an unpackaged build: in a packaged app one env var in the
 * launch environment would otherwise name the file whose body we execute), then
 * `<resourcesPath>/EXTERNALLY-MANAGED`. I/O-bearing and fully injectable, like
 * resolveLinuxInstall above.
 *
 * The marker body is optional JSON `{managedBy, updateCommand, checkCommand}`:
 * `managedBy` names the owning system for the About panel, `updateCommand` is
 * the command the panel offers to copy AND (when the managed auto-update path
 * is active) the command run to apply an update, and `checkCommand` is the
 * optional command run to discover whether an update is available. Every
 * degenerate marker — empty, unparsable,
 * over-cap, a directory, a symlink, a dangling symlink — still means MANAGED:
 * an operator who dropped SOMETHING at that name gets the safe behavior
 * (updater off) even when the metadata is wrong, never a silent fallback to
 * self-updating. Entries are `lstat`ed and only regular files are read, so a
 * symlink can never route this startup-path read into a FIFO or device.
 *
 * INTEGRITY: the metadata is only parsed when neither the marker nor its
 * directory is OWNED by this euid or writable by group/other (see
 * canRewriteMarker) — `updateCommand`/`checkCommand` are SHELLED, so a marker
 * anything running as this user could rewrite is a marker that names arbitrary
 * code to run. A rewritable marker still means MANAGED, just with no metadata:
 * the same degenerate shape as an empty body, which leaves the updater off and
 * nothing to execute. Windows always takes that answer (no POSIX owner to read).
 *
 * @param {object} [o]
 * @param {object} [o.env=process.env]
 * @param {string} [o.resourcesPath=process.resourcesPath]
 * @param {boolean} [o.isPackaged]  packaged app? gates the env-var seam off
 * @param {(p:string)=>boolean} [o.probeMarkerRewritable=canRewriteMarker]
 * @returns {{managedBy:string, updateCommand:string, checkCommand:string}|null} null when not managed
 */
function readExternallyManaged({
  env = process.env,
  resourcesPath = process.resourcesPath,
  // Is this a PACKAGED app? Only the env-var seam consults it, and only to
  // refuse. Resolved lazily so the module still loads outside Electron: a
  // runtime with no `electron.app` is by definition not the packaged desktop
  // app, which is exactly when the harness seam is allowed.
  isPackaged = (() => {
    try {
      const electronApp = require("electron").app;
      return !!(electronApp && electronApp.isPackaged);
    } catch {
      return false;
    }
  })(),
  // Marker-integrity probe, injected for the same reason as the other probes in
  // this module: assertable without a real read-only install directory.
  probeMarkerRewritable = canRewriteMarker,
} = {}) {
  let raw = null;
  let markerPath = "";
  try {
    const fs = require("fs");
    const path = require("path");
    // Present-but-unreadable (non-regular, over-cap, read error) = managed, no
    // metadata. Absent = null. Never follows a symlink into the read.
    const readMarkerAt = (p) => {
      let st;
      try {
        st = fs.lstatSync(p);
      } catch {
        return null; // absent
      }
      if (!st.isFile() || st.size > EXTERNALLY_MANAGED_MAX_BYTES) return "";
      try {
        return fs.readFileSync(p, "utf8");
      } catch {
        return "";
      }
    };
    // The env seam is a DEV/TEST affordance. In a packaged app the launch
    // environment (shell profile, launchd plist, .desktop file) is writable by
    // the user, so honoring it there would let one env var choose the file whose
    // body this process shells.
    const override = (!isPackaged && env && env.KIROCREW_EXTERNALLY_MANAGED) || "";
    if (override) {
      // A value that names a marker file reads it; any other non-empty value
      // (including a dangling path) marks the install managed with no metadata.
      markerPath = override;
      raw = readMarkerAt(override);
      if (raw === null) raw = "";
    } else {
      markerPath = path.join(resourcesPath || "", EXTERNALLY_MANAGED_MARKER);
      raw = readMarkerAt(markerPath);
      if (raw === null) return null;
    }
  } catch {
    // fs itself unavailable (non-node runtime): nothing to read, not managed.
    return null;
  }
  // Integrity gate: a marker this process could rewrite carries no authority,
  // so it is read as a bare marker (managed, no metadata). Deliberately BEFORE
  // the parse, so no attacker-chosen string reaches the fields at all.
  if (raw && probeMarkerRewritable(markerPath)) raw = "";
  let managedBy = "";
  let updateCommand = "";
  let checkCommand = "";
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      if (typeof parsed.managedBy === "string") {
        managedBy = parsed.managedBy.trim().slice(0, MANAGED_BY_MAX_CHARS);
      }
      if (typeof parsed.updateCommand === "string") {
        updateCommand = parsed.updateCommand.trim().slice(0, UPDATE_COMMAND_MAX_CHARS);
      }
      if (typeof parsed.checkCommand === "string") {
        checkCommand = parsed.checkCommand.trim().slice(0, CHECK_COMMAND_MAX_CHARS);
      }
    }
  } catch {
    // Presence alone is the signal; a bare marker means managed, no metadata.
  }
  return { managedBy, updateCommand, checkCommand };
}

// Can THIS process rewrite the externally-managed marker?
//
// The marker's `updateCommand`/`checkCommand` are handed to a shell, so the
// marker's integrity is the whole boundary between "the packager that owns this
// install told us how to update" and "anything that can write one file told us
// what to run". A marker we can rewrite is one a prompt-injected agent shell
// running as this user can rewrite, so its metadata is refused.
//
// The question is OWNERSHIP, not the current mode bits. `access(W_OK)` answers
// "can I write this right now", and a POSIX owner can always `chmod +w` back —
// so on exactly the user-owned installs this exists to defend (Homebrew,
// `pip --user`, ~/Applications) an attacker would plant the marker, `chmod 0400`
// it, and be handed the trusted verdict. Provenance is what the metadata's
// authority rests on, so provenance is what is probed:
//
//   - a file or directory OWNED by this euid is rewritable (chmod is ours),
//   - a group- or world-writable one is rewritable by whoever else holds it, and
//   - one the KERNEL says we can write is rewritable however that was granted.
//
// The third arm is not redundant with the second: POSIX mode bits do not model
// ACLs, so a root-owned 0755 directory carrying a macOS `chmod +a` (or Linux
// setfacl) entry for this user is writable while every mode bit reads safe.
// access(W_OK) is the only check that sees that grant.
//
// Both the marker and its directory are checked, because either one controls the
// content: a writable directory allows replacing the file outright, and a file
// we own is rewritable even inside a directory we do not.
//
// Fail-CLOSED — the OPPOSITE direction to isBundleContainerWritable below. There
// a probe that cannot run must not disable updates; here a marker whose
// provenance cannot be established must not be executed. The cost of the safe
// answer is only "no metadata", which is the historical bare-marker behavior.
// Windows takes that answer UNCONDITIONALLY and by declaration: it has no POSIX
// owner to read, and `access(W_OK)` there does not model ACLs, so there is no
// honest verdict to give. A Windows install therefore never honors marker
// commands; see docs/build/desktop-app.md.
function canRewriteMarker(markerPath) {
  try {
    const fs = require("fs");
    const path = require("path");
    // No POSIX ownership to read: declared fail-closed (see note above).
    if (process.platform === "win32" || typeof process.geteuid !== "function") return true;
    const euid = process.geteuid();
    // root owns everything and can chmod anything, so nothing is un-rewritable.
    if (euid === 0) return true;
    for (const target of [markerPath, path.dirname(markerPath)]) {
      let st;
      try {
        st = fs.lstatSync(target);
      } catch {
        return true; // cannot establish provenance
      }
      if (st.uid === euid) return true;          // ours: chmod +w is ours too
      if ((st.mode & 0o022) !== 0) return true;  // group- or world-writable
      try {
        fs.accessSync(target, fs.constants.W_OK);
        return true;                             // ACL-granted write
      } catch {
        // Not writable by any grant the kernel knows about.
      }
    }
    return false;
  } catch {
    return true;
  }
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
// nightly.yml and both of release.yml's channels: insider publishes a fresh
// signed build, and stable republishes the promotion bundle's installer. That is
// every channel in KNOWN_CHANNELS, so Windows carries no channel restriction of
// its own and channelHasLane needs no win32 arm -- a separate set here would be a
// comment claiming a restriction that does not exist.
//
// If a channel ever loses its Windows lane, do NOT just delete the caller: a
// client resolving a channel nobody publishes fetches a feed that was never
// written, so every check 404s and the manual-download escape hatch is dead. Add
// the restriction back and report `disabled: "channel"` instead.
// test_the_updater_offers_exactly_the_channels_that_publish_windows fails until
// that is done, which is how this stays honest.
//
// Note this is a CHANNEL-level property, not a per-release one. The Windows
// promotion role is optional, so an individual stable release may carry no
// installer; the channel's feed still exists and still advertises the previous
// stable version, so there is nothing for the client to gate on.

/**
 * Whether this channel has a desktop publish lane at all.
 *
 * Platform-independent today: every KNOWN_CHANNELS channel publishes on all
 * three platforms. Takes no platform argument rather than an ignored one, so the
 * absence of a per-platform restriction is visible in the signature instead of
 * hidden in a branch that always returns true.
 */
function channelHasLane(channel) {
  return KNOWN_CHANNELS.has(channel);
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
 * - production builds (insider/stable stamps) follow the preference when set,
 *   and default to STABLE when it is not. Switching BACK can be a downgrade
 *   mid-cycle (insider 0.2.0-insider.1 -> stable 0.1.0), which is why
 *   allowDowngrade is enabled in configureUpdater.
 *
 * Why the unset default is stable rather than the stamp: a stable release is
 * PROMOTED, meaning the exact notarized candidate bytes are re-pointed at the
 * stable channel without a rebuild, so the stable download and the insider
 * download of a promoted version are the SAME FILE and carry the same
 * prerelease stamp (`0.3.0-insider.13`). The channel therefore cannot be a
 * property of the bytes, and reading it out of the version string sends every
 * promoted-stable install to the insider feed. It is a default plus an opt-in,
 * which is what this function's own contract above already describes.
 *
 * `channelForVersion` deliberately keeps classifying the BYTES (it is what the
 * About panel's "you are running prerelease bytes" note is keyed on); only the
 * followed feed is decoupled from it here.
 *
 * @param {"nightly"|"insider"|"stable"|null} stamped - channelForVersion(version)
 * @param {"insider"|"stable"|""|null|undefined} preference - user opt-in, falsy = default
 * @returns {"nightly"|"insider"|"stable"|null}
 */
function resolveChannel(stamped, preference) {
  if (stamped === "nightly") return "nightly";
  if (stamped === null) return null;
  if (preference === "insider" || preference === "stable") return preference;
  return "stable";
}

/**
 * Is `candidate` a STRICTLY NEWER version than `current`? Returns null when the
 * comparison cannot be made (either string unparseable, or semver unavailable).
 *
 * Uses electron-updater's own bundled `semver` so the ordering matches the
 * library's, and understands the prerelease stamps this app ships
 * (`0.3.0-insider.13`, `0.1.2-nightly.<ts>`). `require`d inline — like the
 * `fs`/`child_process` requires elsewhere in this module — so the pure helper
 * stays loadable outside an Electron runtime; a stubbed/absent semver yields
 * null (fail-open) rather than throwing.
 *
 * @param {string} candidate
 * @param {string} current
 * @returns {boolean|null}
 */
function isNewerVersion(candidate, current) {
  if (!candidate || !current) return null;
  let semver;
  try {
    semver = require("semver");
  } catch {
    return null;
  }
  const a = semver.valid(candidate) || semver.valid(semver.coerce(candidate));
  const b = semver.valid(current) || semver.valid(semver.coerce(current));
  if (!a || !b) return null;
  try {
    return semver.gt(a, b);
  } catch {
    return null;
  }
}

/**
 * Should a discovered version drive the AUTOMATIC update path — the background
 * download and the "update found" nudge?
 *
 * - YES for a genuine upgrade (candidate strictly newer than the running build).
 * - YES for ANY version when the FOLLOWED channel differs from the build's own
 *   DEFAULT (no-preference) channel: that is a deliberate channel switch — the
 *   user's stored preference has actively moved this install off the lane it
 *   would otherwise follow — where landing on an older build of the chosen
 *   channel is the intended, user-initiated outcome `allowDowngrade=true` exists
 *   for.
 * - NO for a same-channel version that is NOT newer than the running build.
 *   That is a build running AHEAD of what its own channel has published
 *   (locally-built or prerelease bytes ahead of the feed): from that channel's
 *   point of view there is nothing to install, so electron-updater's
 *   difference-based `update-available` (which fires for any version ≠ running,
 *   because allowDowngrade=true) would otherwise nag a DOWNGRADE. This is the
 *   bug this guard closes.
 *
 * **Why the switch signal is the DEFAULT lane, not the byte stamp.** A promoted
 * stable release ships the soaked insider candidate's exact bytes, so its
 * version keeps the insider stamp (`0.3.0-insider.13`) and `channelForVersion`
 * reports `insider` for a build that is, in fact, a stable install following the
 * stable feed with no deliberate switch. Keying the exemption on that raw stamp
 * (`followed=stable !== stamped=insider`) therefore fired for the ENTIRE
 * promoted-stable population — and for any prerelease-stamped build running ahead
 * of stable (`0.5.0-insider.20`) — re-opening the exact downgrade nag this guard
 * removes. So the comparison uses `defaultChannel = resolveChannel(stamped, "")`
 * (the lane with no preference, which folds promoted-insider bytes to stable),
 * and the exemption fires only when the FOLLOWED channel differs from THAT — i.e.
 * an explicit preference actually moved the install to a non-default lane.
 *
 * `allowDowngrade` stays true, so a real channel switch and any EXPLICIT user
 * download still roll the version; only the unsolicited auto-path is suppressed.
 *
 * Fail-open: an unrankable comparison (isNewerVersion → null) is treated as
 * offerable, so a version we cannot compare is never silently hidden.
 *
 * TRADE-OFF (deliberate, not accidental): a same-channel version RETRACTION —
 * the feed intentionally repointed to an older build — also reads as "not
 * newer, same channel" and is therefore no longer auto-applied. The client
 * cannot tell a retraction from an ahead-of-feed dev build by version alone, and
 * silently DOWNGRADING a user on the next restart is the more dangerous default,
 * so the safe direction is to not auto-act. A deliberate `retracted` feed flag
 * could re-enable that path explicitly in future.
 *
 * @param {{candidate:string, current:string, followedChannel:string, defaultChannel:(string|null)}} o
 * @returns {boolean}
 */
function shouldAutoOffer({ candidate, current, followedChannel, defaultChannel }) {
  if (followedChannel && defaultChannel && followedChannel !== defaultChannel) {
    return true;
  }
  const newer = isNewerVersion(candidate, current);
  if (newer === null) return true;
  return newer;
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
 * Computed HERE rather than in the renderer because the display-oriented
 * getInfo().platform value is not the updater's routing authority. osPlatform
 * and osArch are the native values used to select a published artifact.
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
  if (!channelHasLane(channel)) return null;
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
 * - autoDownload=false        electron-updater must never fetch from INSIDE
 *                             checkForUpdates. This is not the same question as
 *                             "may an update download without a click": that is
 *                             a policy read per discovery from
 *                             getAutoDownloadPreference(), and when it is on the
 *                             "update-available" handler calls startDownload()
 *                             itself. Keeping the library flag false is what
 *                             makes every download — automatic or consented —
 *                             pass through that one guarded function, so the
 *                             preference can actually turn it off and the
 *                             re-entrancy guards apply to both callers.
 *                             It also keeps discovery cheap on macOS: see the
 *                             autoInstallOnAppQuit note below for why staging,
 *                             not fetching, is the dangerous step there.
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
 * @param {string} [deps.osPlatform]           - process.platform override (tests)
 * @param {string} [deps.osArch]               - process.arch override (tests). Picks the
 *   per-arch Linux AppImage for the manual-reinstall link; darwin ignores it
 *   (the DMG is universal).
 * @param {string} [deps.platform]             - display platform override (tests);
 *   defaults to `${osPlatform}-${osArch}`
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
    // Whether discovery may proceed straight to a download without a click.
    // Read FRESH per event, like getChannelPreference, so toggling it in
    // Settings takes effect on the next check with no re-init.
    //
    // Defaults to FALSE, and that is deliberate: the module's fallback must be
    // the consent path, so a host that forgets to wire this loses the
    // convenience rather than silently downloading behind the user. The PRODUCT
    // default (on) lives in main.js where the preference store does, and
    // test/update-ipc-registration.test.js pins that wiring so it cannot
    // disappear unnoticed.
    getAutoDownloadPreference = () => false,
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
    osPlatform = process.platform,
    osArch = process.arch,
    platform = `${osPlatform}-${osArch}`,
    resourcesPath = process.resourcesPath,
    probeBundleWritable = isBundleContainerWritable,
    // Linux install shape + its AppImage writability probe, injected for the
    // same reason as probeBundleWritable: the verdict must be assertable in a
    // test without a real AppImage mount or a real /opt install.
    linuxInstall = null,
    probeAppImageWritable = isAppImageContainerWritable,
    // Externally-managed verdict, injected for the same reason as linuxInstall:
    // assertable in tests without a real marker file. undefined = read the
    // marker from disk; null = not managed; object = managed.
    externallyManaged = undefined,
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

  // Externally-managed verdict. Resolved once and BEFORE getInfo() is defined,
  // for the same temporal-dead-zone reason as `linux` above: the early-return
  // stub below hands getInfo out, and getInfo reports the marker's metadata.
  const managed = externallyManaged !== undefined
    ? externallyManaged
    : readExternallyManaged({ resourcesPath });

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
  // The version the FOLLOWED channel's feed last reported, and the channel it was
  // reported FOR. Recorded because promotion never re-stamps: the stable feed's
  // current release is literally `0.4.1-insider.1`, so `channelForVersion` cannot
  // tell a promoted-stable install from an insider one, and every surface that
  // asked the version string "which lane am I on" answered `insider` for the whole
  // promoted-stable population. The feed's own answer is the only honest input, so
  // it is kept for the display layer.
  let laneVersion = null;
  let laneChannel = null;
  /**
   * The lane pair, or UNKNOWN. Reported as unknown unless the recorded version
   * was read for the channel this install follows RIGHT NOW: a switch makes the
   * old lane's answer describe a lane nobody is on, and `update:set-channel`
   * returns `getInfo()` synchronously while its re-check is still in flight, so a
   * read-time comparison is what closes that window rather than clearing state on
   * an ordering assumption. Concretely, without it: flip insider -> stable on an
   * up-to-date insider build while the follow-up check cannot reach the feed
   * (offline), and a retained `runningAheadOfLane: false` tells the panel these
   * bytes ARE the stable release — folding the chip to a version that does not
   * exist and suppressing the prerelease ask, i.e. the very bug this pair exists
   * to fix. `null` means no usable answer and must never read as "ahead".
   */
  function laneSnapshot() {
    if (!laneVersion || laneChannel !== currentChannel()) {
      return { laneVersion: "", runningAheadOfLane: null };
    }
    return { laneVersion, runningAheadOfLane: isNewerVersion(app.getVersion(), laneVersion) };
  }
  /** Record what the lane just answered, attributed to the lane that answered. */
  function recordLaneVersion(version) {
    if (!version) return;
    laneVersion = version;
    // The channel the FETCH was configured for, not a live read: the preference
    // can flip while a check is in flight, and attributing that answer to the new
    // lane is the same mis-pairing `shouldAutoOffer` avoids with `feedChannel`.
    laneChannel = feedChannel || currentChannel();
  }
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
    const payload = {
      state,
      channel: currentChannel(),
      version: app.getVersion(),
      // The renderer cannot infer the real updater handoff from getInfo().platform:
      // packaged Linux variants and older bundles make that display field an
      // unreliable capability signal. Carry the handoff contract with every
      // lifecycle event so both ready surfaces can set the right expectation.
      // Externally managed Windows installs run the marker's update command
      // and relaunch directly; they never hand off to our NSIS installer.
      installHandoff: osPlatform === "win32" && !managed
        ? "windows-installer"
        : "automatic-relaunch",
      // Display inputs for the version chip and the prerelease note (see
      // laneSnapshot). Carried on every lifecycle payload as well as getInfo()
      // so a renderer driven by pushes alone never falls back to the
      // stamp-based guess this pair replaces -- and so a renderer that mounted
      // before the latest check does not keep rendering that older answer.
      ...laneSnapshot(),
      ...extra,
    };
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
      // (nightly is pinned; dev has no lane; an externally-managed install has
      // no lane the marker's owner reads), and the stored preference.
      stampedChannel: stamped,
      channelSwitchable: !managed && (stamped === "insider" || stamped === "stable"),
      channelPreference: getChannelPreference() || "",
      // What the FOLLOWED channel publishes, and whether these bytes are ahead
      // of it — i.e. that lane never shipped this build, so the install is not
      // on it. Both come from the feed rather than from `stampedChannel`, which
      // a promoted stable release makes unusable for the question (its bytes
      // keep the soaked candidate's insider stamp). "" / null until a check has
      // completed FOR THE CHANNEL THIS INSTALL FOLLOWS (see laneSnapshot).
      ...laneSnapshot(),
      // Current auto-download policy, so About renders the toggle from the
      // value the updater will actually act on rather than from its own copy
      // of the store. Read through the same guard as the event path: a
      // throwing reader reports "off", matching what would happen on discovery.
      autoDownload: (() => {
        try { return !!getAutoDownloadPreference(); } catch { return false; }
      })(),
      // Externally-managed metadata, both empty on a self-updating install.
      managedBy: managed ? managed.managedBy || "" : "",
      updateCommand: managed ? managed.updateCommand || "" : "",
      platform,
      packaged: !!app.isPackaged,
      // Escape hatch for a failed install (see manualDownloadUrl).
      downloadUrl: manualDownloadUrl(currentChannel(), osPlatform, osArch, linux.format),
      // Replay seed for a freshly mounted renderer (see lastEmittedState).
      lastState: lastEmittedState,
    };
  }

  // An operator or distro packager that dropped the EXTERNALLY-MANAGED marker
  // owns this install's update lifecycle: the external package manager replaces
  // the whole install, so a self-update would fight it (each overwriting the
  // other's bytes) and a feed check would compare against releases the owner
  // never ships. FIRST gate on purpose: the marker is an intentional operator
  // override, so it wins over every runtime detection below — the updater is
  // never armed and the feed is never contacted.
  if (managed) {
    // A BARE marker (present, but no updateCommand) means "someone else owns
    // updates and gave us nothing to run": keep the historical no-op behavior.
    if (!managed.updateCommand) {
      log.info(`[update] externally managed${managed.managedBy ? ` by ${managed.managedBy}` : ""} — auto-update disabled`);
      return { check: () => {}, download: async () => {}, install: async () => {}, getInfo, disabled: "externally-managed" };
    }

    // MANAGED AUTO-UPDATE (marker-driven): the marker carries the very
    // commands that own this install's lifecycle, so instead of arming
    // electron-updater or contacting the feed (which would fight the external
    // manager), we discover and apply updates by SHELLING the marker's own
    // commands.
    //
    // TRUST / HARDENING: reaching here means the marker's metadata already
    // passed the integrity gate in readExternallyManaged — neither the marker nor
    // its directory is owned by this euid or writable by group/other, so it is a
    // genuine packager artifact rather than a file a prompt-injected agent shell
    // could have planted. That gate is
    // what makes the commands trustworthy at all; the hardening below is about
    // the ENVIRONMENT they run in, not about the command string (see
    // runManagedCommand) — a narrowed system-only PATH so a planted shim on the
    // user's PATH cannot shadow a command, a CONSTRUCTED environment so nothing
    // the shell reads as code is inherited at all, cwd="/" (never the app or an
    // inherited dir), a
    // timeout, and bounded retained output. The
    // command still runs through a shell, so the writer MUST name absolute
    // binaries (a bare name will not resolve under the narrowed PATH); we NEVER
    // interpolate untrusted input. Platform-agnostic: the same path serves
    // macOS/Windows/Linux.
    log.info(`[update] externally managed${managed.managedBy ? ` by ${managed.managedBy}` : ""} — managed auto-update (self-contained commands)`);

    let foundVersion = null; // last version discovered by the checkCommand, awaiting apply
    let managedQuitArmed = false; // is a before-quit auto-apply handler installed?
    let managedInstalling = false; // an apply is in progress — pause the poll

    // Mirror emitError's renderer contract (emitError itself is defined further
    // down, after this early return, so it is out of scope here): a failure
    // WITH ITS PHASE so the card can distinguish check from install failures.
    const emitManagedError = (phase, err) => {
      const { code, detail, httpStatus } = classifyError(err);
      log.error(`[update] managed ${phase} failed (${code})`, err);
      emit("error", {
        phase,
        code,
        message: detail,
        ...(httpStatus === undefined ? {} : { httpStatus }),
      });
    };

    // Bound retained output so a chatty command cannot exhaust memory (we keep
    // DRAINING both streams either way), and cap how long an apply / check runs.
    const MANAGED_OUTPUT_CAP = 64 * 1024;
    const MANAGED_APPLY_TIMEOUT_MS = 30 * 60 * 1000; // 30 min ceiling for an apply
    const MANAGED_CHECK_TIMEOUT_MS = 45 * 1000; // a check must not hang the UI
    const MANAGED_VERSION_CAP = 128; // a version string is short; cap like the sibling
    // A narrowed, non-user-writable PATH: an agent-writable entry on the user's
    // own PATH cannot shadow a command. The marker's commands must name ABSOLUTE
    // binaries (a bare name will not resolve here) — mirrors CommandProvider.
    const managedPath = () =>
      process.platform === "win32"
        ? [
            `${process.env.SystemRoot || "C:\\Windows"}\\System32`,
            process.env.SystemRoot || "C:\\Windows",
          ].join(";")
        : "/usr/bin:/bin:/usr/sbin:/sbin";
    // The child's environment is CONSTRUCTED, not filtered.
    //
    // `shell: true` means a shell interprets the command, and a shell reads its
    // environment as code: the loader family (LD_*/DYLD_*), the interpreter
    // family (PYTHON*, NODE_OPTIONS), the startup files (BASH_ENV, ENV), the
    // tracing pair (SHELLOPTS plus a command-substituting PS4), word splitting
    // (IFS), and exported shell FUNCTIONS (BASH_FUNC_* — a function shadows a
    // command name outright, beating managedPath() rather than evading it).
    // That namespace is open-ended and differs by shell and by version, so no
    // denylist over it is provably complete; successive review rounds just find
    // the next name.
    //
    // Naming what the child DOES get inverts that: anything absent from this
    // list is gone by construction, so every present and future injection
    // variable is already handled and there is no enumeration to keep current.
    // The list carries what a packager's own updater plausibly needs — locale,
    // temp dir, proxy — and nothing a shell or an interpreter treats as code. A
    // packager needing more sets it inside its own command, which is the one
    // place that requirement is visible to whoever wrote it.
    //
    // HOME is deliberately NOT here. It is not shell-interpreted, but it is a
    // path an interpreter reads code from: Python derives its user-site
    // directory from HOME, so a planted ~/.local/lib/pythonX/site-packages/
    // sitecustomize.py executes on every `python` start. Passing HOME would
    // re-open the startup-injection class for any marker command that happens to
    // be a Python program, which is the class this whole construction closes.
    const MANAGED_ENV_PASSTHROUGH = [
      "USER", "LOGNAME", "TZ", "TMPDIR",
      "LANG", "LC_ALL", "LC_CTYPE",
      "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
      "http_proxy", "https_proxy", "no_proxy",
    ];
    // cmd.exe cannot start without these, so the win32 lane mirrors
    // managedPath()'s win32 branch rather than handing it a shell it cannot run.
    const MANAGED_ENV_PASSTHROUGH_WIN32 = [
      "SystemRoot", "SystemDrive", "windir", "COMSPEC",
      "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    ];
    const managedEnv = () => {
      const e = { PATH: managedPath() };
      const keys = process.platform === "win32"
        ? [...MANAGED_ENV_PASSTHROUGH, ...MANAGED_ENV_PASSTHROUGH_WIN32]
        : MANAGED_ENV_PASSTHROUGH;
      for (const k of keys) {
        if (process.env[k] !== undefined) e[k] = process.env[k];
      }
      return e;
    };

    // Run a marker command through the platform shell, resolving to
    // {code, out} (combined stdout+stderr, capped). Never rejects: spawn errors
    // and timeouts resolve with a non-zero code so callers treat them uniformly.
    // Hardened like the Python CommandProvider: narrowed PATH, cwd="/", a
    // timeout, and bounded retained output.
    const runManagedCommand = (command, { timeout } = {}) => new Promise((resolve) => {
      const cp = require("child_process");
      let out = "";        // combined stdout+stderr, for logging an apply
      let outStdout = "";  // stdout ONLY, for deriving the check's version
      let settled = false;
      // `failed` marks that the command could not be RUN to completion (spawn
      // error or timeout kill), as distinct from running and exiting non-zero.
      // The check path treats these differently: a run that exits non-zero is
      // "no update", but a command that could not run at all is an error.
      const done = (code, failed) => {
        if (!settled) {
          settled = true;
          resolve({ code: typeof code === "number" ? code : 1, out, stdout: outStdout, failed: !!failed });
        }
      };
      // Keep consuming BOTH streams (so the pipe never blocks the child) but
      // stop RETAINING once capped. stdout is captured separately because the
      // version is derived from stdout ONLY — a warning printed to stderr must
      // never be mistaken for the version.
      const capped = (s) => (s.length > MANAGED_OUTPUT_CAP ? s.slice(0, MANAGED_OUTPUT_CAP) : s);
      const onStdout = (d) => {
        const s = d.toString();
        if (out.length < MANAGED_OUTPUT_CAP) out = capped(out + s);
        if (outStdout.length < MANAGED_OUTPUT_CAP) outStdout = capped(outStdout + s);
      };
      const onStderr = (d) => {
        if (out.length < MANAGED_OUTPUT_CAP) out = capped(out + d.toString());
      };
      let child;
      try {
        // `command` is NOT user input: it is operator-controlled text from the
        // EXTERNALLY-MANAGED marker, and execution is hardened (narrowed system
        // PATH, cwd="/", bounded output, timeout). See the trust note above.
        // nosemgrep: javascript.lang.security.detect-child-process.detect-child-process
        child = cp.spawn(command, { // nosemgrep: javascript.lang.security.detect-child-process.detect-child-process
          shell: true,
          cwd: "/",
          env: managedEnv(),
          ...(timeout ? { timeout } : {}),
        });
      } catch (err) {
        log.error("[update] managed command spawn threw", err);
        return done(1, true);
      }
      if (child.stdout) child.stdout.on("data", onStdout);
      if (child.stderr) child.stderr.on("data", onStderr);
      child.on("error", (err) => { log.error("[update] managed command error", err); done(1, true); });
      // A timeout kill closes with a null exit code and a signal; treat that as
      // "could not run", not as a non-zero exit.
      child.on("close", (code, signal) => done(code, code === null && signal != null));
    });

    // The command run to APPLY an update, on quit and on explicit install.
    // Bounded by a ceiling timeout so a wedged package manager cannot hang quit.
    const runUpdateCommand = () => runManagedCommand(managed.updateCommand, { timeout: MANAGED_APPLY_TIMEOUT_MS });

    // Fresh-read the auto-download preference; a throwing reader fails toward
    // NOT auto-installing (same direction as the feed path's deferred handler).
    const autoDownloadOn = () => {
      try { return !!getAutoDownloadPreference(); } catch (err) {
        log.error("[update] getAutoDownloadPreference threw — treating as off", err);
        return false;
      }
    };

    // Auto-on-restart: apply the discovered update on the next natural quit.
    // Mirrors deferredInstallOnQuit — pref is re-read FRESH at quit time so a
    // toggle-off between discovery and quit is honored.
    const managedInstallOnQuit = (event) => {
      // Nothing pending (a later check cleared it, or it was already applied):
      // let the quit proceed normally — never relaunch into a withdrawn update.
      if (!foundVersion) {
        log.info("[update] managed quit handler fired with no pending update — not applying");
        return;
      }
      let stillOn = false;
      try { stillOn = !!getAutoDownloadPreference(); } catch (err) {
        log.error("[update] getAutoDownloadPreference threw on quit — not installing", err);
      }
      if (!stillOn) {
        log.info("[update] managed auto-download off at quit — not applying on quit");
        return;
      }
      event.preventDefault();
      (async () => {
        managedInstalling = true;
        emit("installing", { version: foundVersion });
        try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
        try { if (stopGateway) await stopGateway(); } catch (err) {
          log.error("[update] managed stop on quit errored", err);
        }
        log.info("[update] managed deferred install on quit — running update command");
        const { code } = await runUpdateCommand();
        if (code === 0) {
          app.relaunch();
        } else {
          // The apply failed; the user asked to quit, so honor that and exit
          // WITHOUT relaunching into a version that did not install.
          log.error(`[update] managed deferred install failed (exit ${code}) — quitting without relaunch`);
          try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
        }
        app.exit(0);
      })();
    };

    // Undo a quit-time auto-apply armed by an earlier check and forget the
    // discovered version. Called when a later check finds nothing pending, so a
    // normal quit does not relaunch into an update the external manager already
    // applied or withdrew — the feed path clears its deferred state for the
    // same reason.
    const disarmManagedQuit = () => {
      foundVersion = null;
      if (managedQuitArmed) {
        app.removeListener("before-quit", managedInstallOnQuit);
        managedQuitArmed = false;
      }
    };

    async function managedCheck() {
      emit("checking");
      if (!managed.checkCommand) {
        // The marker says how to APPLY an update but gives no way to DISCOVER
        // one. This is a check error, NOT a green "up to date": a silent
        // "latest" would hide every future update for this install forever.
        log.info("[update] managed: no checkCommand — cannot check for updates");
        emitManagedError("check", new Error("this managed install has no checkCommand"));
        return;
      }
      const { code, stdout, failed } = await runManagedCommand(managed.checkCommand, {
        timeout: MANAGED_CHECK_TIMEOUT_MS,
      });
      if (failed) {
        // Could not RUN the command (spawn error or timeout) — an error, not
        // "up to date". Mirrors the sibling CommandProvider, which returns an
        // error verdict for a check it could not execute.
        log.error("[update] managed check could not run");
        emitManagedError("check", new Error("managed check command failed to run"));
        return;
      }
      if (code !== 0) {
        // Ran and exited non-zero: no update available (sibling contract). Undo
        // any quit-time auto-apply armed by an earlier check that DID find one,
        // so a normal quit does not relaunch into a withdrawn/applied update.
        log.info(`[update] managed check: up to date (code=${code})`);
        disarmManagedQuit();
        emit("not-available");
        return;
      }
      // Sibling contract: exit 0 and stdout IS the version (trimmed, capped).
      // Derived from stdout ONLY so a stderr warning is never read as a version.
      const version = stdout.trim().slice(0, MANAGED_VERSION_CAP);
      if (!version) {
        // Exit 0 that prints NO version is a broken command, not an available
        // update: treating it as available would relaunch to the SAME version
        // forever. Fail the check rather than report "latest".
        log.error("[update] managed check: exit 0 but printed no version");
        emitManagedError("check", new Error("managed check command produced no version"));
        return;
      }
      foundVersion = version;
      log.info(`[update] managed check: update available -> ${version}`);
      emit("found", { version });
      // Auto-on-restart: if the user allows auto-download, arm a one-shot
      // before-quit handler that applies on the natural quit.
      if (autoDownloadOn() && !managedQuitArmed) {
        managedQuitArmed = true;
        app.once("before-quit", managedInstallOnQuit);
      }
    }

    async function managedDownload() {
      // Managed download+apply is ONE step (the updateCommand). "download" just
      // lights the UI Install action; it never applies. Discover first if the
      // UI raced the check.
      if (!foundVersion) {
        await managedCheck();
      }
      if (foundVersion) {
        emit("downloaded", { version: foundVersion });
      }
    }

    async function managedInstall() {
      managedInstalling = true;
      emit("installing", { version: foundVersion });
      try { if (onInstallDispatched) onInstallDispatched(); } catch { /* advisory */ }
      try { if (stopGateway) await stopGateway(); } catch (err) {
        log.error("[update] managed stop before install errored", err);
      }
      const { code } = await runUpdateCommand();
      if (code === 0) {
        log.info("[update] managed install succeeded — relaunching");
        app.relaunch();
        app.exit(0);
        return;
      }
      log.error(`[update] managed install failed (exit ${code})`);
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
      emitManagedError("install", new Error(`managed update command exited ${code}`));
    }

    // Auto-check on launch and on the same interval as the feed path, so a
    // managed install DISCOVERS updates without the user clicking Check
    // (auto-update is on by default). Background checks only discover — an
    // apply still requires the auto-download preference or an explicit install.
    // The poll skips windows where an apply is already in flight, and both
    // timers are unref'd so they never hold the process open (Electron quit,
    // tests).
    const managedLaunchTimer = setTimeout(() => {
      managedCheck().catch((err) => log.error("[update] managed launch check threw", err));
    }, LAUNCH_CHECK_DELAY_MS);
    const managedPollTimer = setInterval(() => {
      if (!managedInstalling) {
        managedCheck().catch((err) => log.error("[update] managed poll check threw", err));
      }
    }, CHECK_INTERVAL_MS);
    if (typeof managedLaunchTimer.unref === "function") managedLaunchTimer.unref();
    if (typeof managedPollTimer.unref === "function") managedPollTimer.unref();

    return {
      check: () => managedCheck(),
      download: () => managedDownload(),
      install: () => managedInstall(),
      getInfo,
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
  // A channel can lack a desktop publish lane entirely -- that is what
  // channelHasLane() records. No PLATFORM restricts channels today: every
  // KNOWN_CHANNELS channel publishes on all three, Windows included. Arming
  // the updater against a channel with no feed makes every check fail on a 404
  // and leaves the manual-download link pointing at nothing, so report it the
  // same way the dev and platform paths do -- About then shows "unavailable"
  // instead of a Check button that can only ever error.
  //
  // Evaluated once at init, while currentChannel() is read per check: switching
  // channels in Settings mid-session surfaces the ordinary failure card until
  // the next launch, which the UI already handles.
  if (!channelHasLane(currentChannel())) {
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
  // Was the staged build fetched by the auto-download policy rather than asked
  // for? It decides whether turning the preference OFF also disarms the
  // install-on-quit: a stage the user never requested must not land on a user
  // who has just declined auto-updates, while a stage they explicitly
  // downloaded stays armed because the preference is not what put it there.
  let stagedWasAutomatic = false;
  // Set when startDownload() is entered from the discovery handler, and read by
  // the update-downloaded handler -- the event carries no provenance of its own.
  let downloadWasAutomatic = false;
  let foundVersion = null; // last version surfaced to the user, awaiting consent
  let installing = false;
  let quitHandled = false;
  let checking = false;
  // The channel the LAST configureFeed() pointed the updater at. Captured at
  // check time because the update-available handler's direction gate must
  // compare the candidate against the channel its FEED was configured for, not
  // against a live currentChannel() read: the preference can flip mid-flight
  // (an in-flight stable check, then the user picks insider), and re-reading it
  // in the handler would treat a stable-feed downgrade as a deliberate insider
  // switch and stage it. Null until the first configureFeed().
  let feedChannel = null;

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
    // Record the channel this check's feed is configured for, so the
    // update-available handler compares the candidate against THIS lane rather
    // than a currentChannel() that may have changed since (see feedChannel).
    feedChannel = channel;
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
    if (installing || quitHandled) {
      // Install activity: the gateway is stopped on purpose and the process
      // is handing off to the platform installer. The poll timer already
      // skips this window (see pollTimer below); the renderer-driven path
      // must refuse for the same reasons — a check outcome here either races
      // the handoff or, because `installing` outranks `checking` in the error
      // handler's phase derivation, a feed failure would fire the host's
      // gateway recovery in the middle of the bundle swap.
      log.info("[update] check requested during install activity — skipping");
      return;
    }
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
   * Download the version last surfaced by safeCheck.
   *
   * Reached two ways: the user's explicit Download action, and — when
   * getAutoDownloadPreference() is on — automatically from the
   * "update-available" handler. Both enter here rather than through
   * electron-updater's own autoDownload flag, which stays false: routing every
   * download through one guarded function is what keeps the decision
   * inspectable, cancellable by preference, and identical on all platforms.
   *
   * Every early return below is load-bearing for the automatic caller, which
   * fires on a 4-hourly timer and can therefore re-enter: an in-flight download
   * is not restarted, an already-staged version is not re-fetched, and a call
   * with nothing discovered discovers instead of blind-downloading.
   */
  async function startDownload({ automatic = false } = {}) {
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
    log.info(`[update] downloading ${foundVersion}`);
    downloading = true;
    downloadWasAutomatic = automatic;
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
  // Windows deliberately uses isSilent=false. The assisted NSIS installer has
  // update-only hooks in build/installer.nsh that skip every decision page,
  // leave the native extraction progress visible, then relaunch and close on
  // completion. Passing /S hid that only useful feedback for several minutes,
  // making a healthy update look exactly like a crash. The installer also
  // converts /S back to this visible update mode for clients released before
  // this change, so the first upgrade into the fix is covered too.
  function notifyWindowsInstallHandoff() {
    if (osPlatform !== "win32") return;
    try {
      new Notification({
        title: "Installing Kiro Crew update",
        // Timing and automatic relaunch stay on the installer window that they
        // explain. The toast carries only the unique recovery instruction.
        body: "If Kiro Crew doesn’t reopen after the installer finishes, open it from the Start menu.",
      }).show();
    } catch { /* notifications optional */ }
  }

  function quitAndInstall() {
    notifyWindowsInstallHandoff();
    autoUpdater.quitAndInstall(false, true);
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
    // An install-phase failure can land while the gateway stops: the error
    // handler classifies it (installing outranks checking there), resets
    // `installing`, and runs the host recovery. This dispatch is already
    // dead — proceeding would install on a failure the user was just told
    // about, and aborting would run the recovery a second time.
    if (!installing) {
      log.info("[update] install failed while the gateway stopped — dispatch abandoned");
      return;
    }
    // Re-check the stage AFTER the await: a feed response already in flight
    // when the user clicked install can report a retraction or a newer build
    // while the gateway stops, and the update-available / update-not-available
    // handlers then discard the stage. Installing those bytes anyway would
    // ship a build the feed has withdrawn or superseded. A check STILL in
    // flight is the same hazard one step earlier: its response can invalidate
    // the stage the moment after this dispatch commits, and an error event it
    // produces during the bundle swap would be misattributed to the install
    // (see the phase derivation in the error handler). Aborting on `checking`
    // makes the dispatch itself the serialization point between checks and
    // installs: no check outcome — result or failure — can land past
    // quitAndInstall.
    if (!updateReady || checking) {
      log.info(
        !updateReady
          ? "[update] stage invalidated while the gateway stopped — aborting install and restoring"
          : "[update] check still in flight after the gateway stopped — aborting install and restoring",
      );
      installing = false;
      try { if (onInstallFailed) onInstallFailed(); } catch { /* advisory */ }
      // Use the install-error renderer contract, NOT a bare found/not-available:
      // the user just clicked Install Update & Restart App and is watching an install
      // surface -- a silent state swap reads as an unexplained cancel. The
      // error/install shape has an existing renderer contract (the About
      // card, and the in-place overlay failure state) that says the install
      // did not proceed and offers the way forward.
      emit("error", {
        phase: "install",
        code: !updateReady ? "stage-invalidated" : "check-in-flight",
        message: !updateReady
          ? "the staged update was withdrawn or superseded before the install could run"
          : "a feed check was still in flight when the install was ready to run",
        ...(foundVersion ? { version: foundVersion } : {}),
      });
      return;
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
    // The opt-out has to govern the update the user opted out BECAUSE OF.
    // Without this, the nudge says "downloading, will install on your next
    // quit", the user follows it to the toggle and switches it off, and the
    // stage lands anyway — the one outcome the toggle promises will not happen.
    // Only an AUTOMATIC stage is dropped: one the user downloaded on purpose
    // stays armed, because the preference is not what put it there.
    //
    // The bytes are kept either way. This disarms the install, it does not
    // discard the stage, so an explicit Install still applies it immediately
    // with nothing to re-download.
    if (stagedWasAutomatic) {
      let stillAuto = false;
      try {
        stillAuto = !!getAutoDownloadPreference();
      } catch (err) {
        // Unreadable preference: treat as opted OUT here. This is the same
        // fail-toward-consent direction as the discovery path, and on this path
        // it is the one that cannot surprise anyone -- the app quits as asked
        // and the stage is still there to install later.
        log.error("[update] getAutoDownloadPreference threw on quit — not installing", err);
      }
      if (!stillAuto) {
        log.info(`[update] auto-download off — leaving ${stagedVersion} staged instead of `
          + "installing on quit");
        return;
      }
    }
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
      // Same stage re-check as the manual path: a feed response in flight at
      // quit time can invalidate the stage while the gateway stops. The user
      // asked to QUIT, so skip the install and let the quit proceed. What
      // makes the re-entry safe is the LISTENER state, not `quitHandled`: a
      // retraction handler resets `quitHandled = false` and removes this
      // listener, and it was registered with app.once so it has already been
      // consumed -- either way no live before-quit hook re-prevents the quit,
      // so app.quit() exits normally without installing the withdrawn build.
      if (!updateReady) {
        log.info("[update] stage invalidated during quit — quitting without installing");
        // The user was told the update would finish on quit; explain why it
        // did not, or the still-old version at next launch reads as a failure.
        try {
          new Notification({
            title: "Update canceled",
            body: "The staged update was withdrawn or superseded, so it was not installed. You\u2019ll be offered the latest version next launch.",
          }).show();
        } catch { /* notifications optional */ }
        app.quit();
        return;
      }
      quitAndInstall();
      forceExitFailsafe("deferred install on quit");
    })();
  }

  async function promptInstall(versionName, notes) {
    const handoffDetail = osPlatform === "win32"
      ? "Installing can take several minutes. Kiro Crew will close, show Windows installation progress, and reopen automatically."
      : "Installing can take several minutes. Kiro Crew will close and reopen automatically when the update is complete.";
    const { response } = await dialog.showMessageBox({
      type: "info",
      buttons: ["Install Update & Restart App", "Later"],
      defaultId: 0,
      cancelId: 1,
      title: "Kiro Crew update ready",
      message: `Kiro Crew ${versionName || ""} is ready to install.`.trim(),
      detail:
        (notes || "").slice(0, 500) +
        `\n\n${handoffDetail}`,
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
    // from the operation actually in flight. Read the flags BEFORE clearing
    // `downloading`, or a mid-download failure would be reported as a check
    // failure. `installing` must outrank `checking`: once an install is
    // dispatched the gateway is stopped ON PURPOSE, and a genuine installer
    // failure (observed live in the OTA lane: a Squirrel signature rejection)
    // that arrives while a check happens to be in flight would otherwise be
    // labelled "check" — onInstallFailed never fires, nothing restores the
    // stopped gateway, and the app survives with a dead dashboard. The
    // converse misattribution is the recoverable one: a straddling check's
    // feed error killing the install runs the same onInstallFailed recovery
    // the post-stopGateway abort would run anyway — and that abort refuses to
    // reach quitAndInstall while `checking` is true, so no check outcome can
    // fire recovery in the middle of an actual bundle swap. The
    // `downloading`-before-`installing` precedence is long-standing behavior,
    // preserved as-is.
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
    // The feed's gate is DIFFERENCE-based (allowDowngrade=true), so "not
    // available" means the followed lane publishes exactly the running version:
    // record that, which is what makes the lane pair a definite not-ahead
    // instead of an unknown for the whole up-to-date population.
    recordLaneVersion(app.getVersion());
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
  // DISCOVERY, before any bytes move. electron-updater's autoDownload stays
  // false so it never fetches inside checkForUpdates; whether a download
  // follows is OUR decision, made here from the preference, so the automatic
  // and the consent paths share one guarded entry point (startDownload).
  autoUpdater.on("update-available", (info) => {
    foundVersion = (info && info.version) || null;
    // What the followed lane publishes, recorded BEFORE the direction gate
    // below can null `foundVersion` out. The suppressed case is precisely the
    // one the display layer needs it for: an insider build whose preference was
    // flipped to stable reaches here with the stable lane's OLDER release, is
    // (correctly) not auto-offered, and must still be able to say "stable
    // publishes 0.4.1; you are running bytes it never shipped" instead of
    // folding its version to a stable release that does not exist.
    recordLaneVersion(foundVersion);
    // Direction gate — the fix for the "update to an OLDER version" nag.
    // electron-updater fires this for ANY feed version that DIFFERS from the
    // running one, because allowDowngrade=true — so on a build running ahead of
    // its channel's published latest it reports a DOWNGRADE as available. When
    // this is a same-channel version that is not newer, suppress the automatic
    // path entirely: discard any stage armed for it, report up to date, and do
    // NOT download or nag. A deliberate channel switch (followed !== default
    // lane) is exempt, and explicit user downloads are unaffected.
    if (
      foundVersion &&
      !shouldAutoOffer({
        candidate: foundVersion,
        current: app.getVersion(),
        // The channel THIS candidate's feed was configured for, captured at
        // check time (feedChannel), NOT a live currentChannel() read. If the
        // preference flipped while this check was in flight, a live read would
        // pair the new channel with the OLD feed's candidate and wrongly treat
        // a stale-feed downgrade as a deliberate switch. Falls back to a live
        // read only before the first configureFeed() has run.
        followedChannel: feedChannel || currentChannel(),
        // The lane this build follows with NO preference. Folds a promoted
        // stable build's insider-stamped bytes back to stable, so only an
        // explicit preference that MOVES the install off its default lane reads
        // as a deliberate channel switch (see shouldAutoOffer).
        defaultChannel: resolveChannel(channelForVersion(app.getVersion()), ""),
      })
    ) {
      log.info(
        `[update] feed offers ${foundVersion} but running ${app.getVersion()} is not older `
          + "on the same channel — treating as up to date (suppressing downgrade nag)",
      );
      if (updateReady || stagedVersion) {
        // A downgrade staged before this guard existed (or by a race) must not
        // survive to install on the next quit.
        updateReady = false;
        stagedVersion = null;
        stagedNotes = "";
        quitHandled = false;
        app.removeListener("before-quit", deferredInstallOnQuit);
      }
      foundVersion = null;
      emit("not-available");
      return;
    }
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
      // Superseded: drop the stale stage so the next download takes the NEWEST
      // build rather than installing an already-old one.
      log.info(`[update] staged ${stagedVersion} superseded by ${foundVersion} — discarding stage`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
      app.removeListener("before-quit", deferredInstallOnQuit);
    }
    let autoDownload = false;
    try {
      autoDownload = !!getAutoDownloadPreference();
    } catch (err) {
      // A throwing preference reader must not cost the user the discovery
      // nudge, and it must not be read as consent either — fall back to the
      // consent path, which is the safe half.
      log.error("[update] getAutoDownloadPreference threw — treating as off", err);
    }
    log.info(`[update] found ${foundVersion} (running ${app.getVersion()}) — `
      + (autoDownload ? "auto-downloading" : "awaiting user consent"));
    // Nudge hook: main.js shows a native notification (deduped there, once per
    // version). Its copy differs by mode, so pass the mode rather than letting
    // main.js re-read the preference and risk disagreeing with this decision.
    if (typeof notifyUpdateFound === "function") {
      try { notifyUpdateFound(foundVersion, { autoDownload }); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
    }
    emit("found", {
      version: foundVersion,
      notes: notesFrom(info),
      pubDate: (info && info.releaseDate) || "",
    });
    // AFTER the "found" emit: the renderer must see the version it is about to
    // download, and startDownload() emits "downloading" over the top of it.
    // Fire-and-forget — startDownload owns its own error reporting, and this
    // handler is a synchronous event listener that cannot await.
    if (autoDownload) void startDownload({ automatic: true });
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
    stagedWasAutomatic = downloadWasAutomatic;
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
  // The poll must keep consulting the feed even while an update is STAGED
  // (see the note in safeCheck). Gating it on !updateReady would pin a
  // long-running session to its stale stage whenever a newer version ships
  // mid-session -- the supersede path in the update-available handler is only
  // reachable if some check actually runs. safeCheck() already owns the
  // staged case: re-surface when the stage is still latest, discard and
  // re-find when it is superseded.
  //
  // INSTALL ACTIVITY is the one state the poll must still skip, and there are
  // exactly two install entry points to cover: `installing` (the manual
  // Restart & Update dispatch) and `quitHandled` (the deferred install on a
  // natural quit, which never sets `installing`). In either window the
  // gateway is being stopped on purpose and the process is about to hand off
  // to the platform installer -- a check there is useless at best, and at
  // worst its outcome (an error event, or a retraction clearing the stage
  // under a dispatch that already passed its guard) races the handoff.
  // Staged-but-idle and installing are different states; only the latter is
  // unsafe to probe.
  const pollTimer = setInterval(() => { if (!installing && !quitHandled) safeCheck(); }, CHECK_INTERVAL_MS);
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
  isNewerVersion,
  shouldAutoOffer,
  buildFeedBase,
  configureUpdater,
  classifyError,
  manualDownloadUrl,
  resolveLinuxInstall,
  readExternallyManaged,
  canRewriteMarker,
  DEFAULT_FEED_BASE,
  DOWNLOAD_BASE,
  SUPPORTED_PLATFORMS,
};
