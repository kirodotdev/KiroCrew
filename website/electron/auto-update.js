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
 *
 * The policy stays here: channels and their publish lanes, the feed and
 * download URLs, the update-policy flags, the externally-managed marker and the
 * install-shape probes. initAutoUpdate gates on them and then composes one of
 * two lanes from runtime/update/: the electron-updater feed lane, or the
 * marker-driven managed lane, both reporting through one state reporter.
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
const { createUpdateReporter } = require("./runtime/update/state-reporter");
const { createManagedLane } = require("./runtime/update/managed-lane");
const { createFeedLane } = require("./runtime/update/feed-lane");

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
 * the BAKED marker `<app code>/EXTERNALLY-MANAGED` (inside app.asar, next to
 * this file — placed there at build time by `packaging/build-desktop.sh` when
 * `KIROCREW_MANAGED_INSTALL_MARKER` names one; read on a PACKAGED build only,
 * since in a dev checkout that directory is writable source), then the LOOSE marker
 * `<resourcesPath>/EXTERNALLY-MANAGED` a repackager drops beside the app.
 * I/O-bearing and fully injectable, like resolveLinuxInstall above.
 *
 * The two on-disk shapes differ in WHO put the file there, which is what its
 * authority rests on. The loose marker is a post-build affordance for a distro
 * packager, so it is gated on provenance (below). The baked marker is part of
 * the application's own code: it ships in the same archive as main.js and this
 * module, so anyone positioned to rewrite it is already positioned to rewrite
 * the code that reads it, and no ownership probe can add anything to that. It
 * is therefore trusted as code is trusted — on every platform, Windows
 * included — and it outranks a loose marker when both exist, because a
 * build-time declaration by the edition that produced the binary is a stronger
 * statement than a file dropped next to it afterwards. On macOS the baked
 * marker is additionally sealed by codesign for free.
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
 * INTEGRITY (loose marker only): the metadata is only parsed when neither the
 * marker nor its directory is OWNED by this euid or writable by group/other (see
 * canRewriteMarker) — `updateCommand`/`checkCommand` are SHELLED, so a marker
 * anything running as this user could rewrite is a marker that names arbitrary
 * code to run. A rewritable marker still means MANAGED, just with no metadata:
 * the same degenerate shape as an empty body, which leaves the updater off and
 * nothing to execute. Windows always takes that answer for a loose marker (no
 * POSIX owner to read); a baked marker is not probed on any platform.
 *
 * @param {object} [o]
 * @param {object} [o.env=process.env]
 * @param {string} [o.resourcesPath=process.resourcesPath]
 * @param {boolean} [o.isPackaged]  packaged app? gates the env-var seam off
 * @param {(p:string)=>boolean} [o.probeMarkerRewritable=canRewriteMarker]
 * @param {string} [o.bakedMarkerPath]  where the in-code marker lives; defaults
 *   to `EXTERNALLY-MANAGED` beside this module (inside app.asar when packaged)
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
  // The in-code marker. `__dirname` is inside app.asar in a packaged build
  // (Electron's fs shim reads through the archive), and the module directory
  // in a dev checkout, where the file simply does not exist.
  bakedMarkerPath = require("path").join(__dirname, EXTERNALLY_MANAGED_MARKER),
} = {}) {
  let raw = null;
  let markerPath = "";
  // Which shape was found. Only a LOOSE marker is subject to the provenance
  // probe below; a baked one is code (see the doc comment).
  let loose = false;
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
      // Treated like a loose marker: the harness is exercising that path.
      markerPath = override;
      loose = true;
      raw = readMarkerAt(override);
      if (raw === null) raw = "";
    } else {
      // Baked first: a build-time declaration outranks a file dropped later.
      // PACKAGED builds only. The baked path is `__dirname/EXTERNALLY-MANAGED`,
      // and in a dev checkout `__dirname` is a plain writable source directory,
      // not an archive: a file there has none of the provenance the trust rests
      // on, and the managed lane below arms its launch timer before the
      // dev-disable gate. An unpackaged run reads no baked marker; the env seam
      // above is the harness's route.
      markerPath = isPackaged && bakedMarkerPath ? bakedMarkerPath : "";
      raw = markerPath ? readMarkerAt(markerPath) : null;
      if (raw === null) {
        markerPath = path.join(resourcesPath || "", EXTERNALLY_MANAGED_MARKER);
        loose = true;
        raw = readMarkerAt(markerPath);
        if (raw === null) return null;
      }
    }
  } catch {
    // fs itself unavailable (non-node runtime): nothing to read, not managed.
    return null;
  }
  // Integrity gate: a LOOSE marker this process could rewrite carries no
  // authority, so it is read as a bare marker (managed, no metadata).
  // Deliberately BEFORE the parse, so no attacker-chosen string reaches the
  // fields at all. A baked marker skips the probe: it is code, and its
  // provenance is the application's own.
  if (raw && loose && probeMarkerRewritable(markerPath)) raw = "";
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
// honest verdict to give. A Windows install therefore never honors a LOOSE
// marker's commands; a packager that needs them there bakes the marker into the
// app at build time, where this probe does not apply (see readExternallyManaged
// and docs/build/desktop-app.md).
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

// The narrowed, non-user-writable PATH every marker command runs under: an
// agent-writable entry on the user's own PATH cannot shadow a command. The
// marker's commands must name ABSOLUTE binaries (a bare name will not resolve
// here) — mirrors CommandProvider. Read per call, so it follows SystemRoot.
const managedPath = () =>
  process.platform === "win32"
    ? [
        `${process.env.SystemRoot || "C:\\Windows"}\\System32`,
        process.env.SystemRoot || "C:\\Windows",
      ].join(";")
    : "/usr/bin:/bin:/usr/sbin:/sbin";

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
  // The channel, lane pair, lifecycle pushes and replayable info payload both
  // lanes report through. Created BEFORE any gate for the same temporal-dead-
  // zone reason as `linux` and `managed`: every stub below hands getInfo out.
  const reporter = createUpdateReporter({
    app,
    getFlavor,
    getChannelPreference,
    getAutoDownloadPreference,
    onUpdateState,
    uiDriven,
    log,
    osPlatform,
    osArch,
    platform,
    managed,
    linux,
    channelForFlavor,
    channelForVersion,
    resolveChannel,
    isNewerVersion,
    manualDownloadUrl,
  });
  const { currentChannel, getInfo } = reporter;

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

    // MANAGED AUTO-UPDATE (marker-driven): the marker's own commands discover
    // and apply updates instead of electron-updater and the feed.
    return createManagedLane({
      managed,
      app,
      emit: reporter.emit,
      getInfo,
      getAutoDownloadPreference,
      stopGateway,
      onInstallDispatched,
      onInstallFailed,
      classifyError,
      managedPath,
      launchCheckDelayMs: LAUNCH_CHECK_DELAY_MS,
      checkIntervalMs: CHECK_INTERVAL_MS,
      log,
    });
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

  return createFeedLane({
    app,
    autoUpdater,
    dialog,
    Notification,
    getAutoDownloadPreference,
    notifyUpdateFound,
    stopGateway,
    onInstallDispatched,
    onInstallFailed,
    osPlatform,
    linux,
    nativeAutoUpdater,
    feedBase,
    uiDriven,
    log,
    reporter,
    buildFeedBase,
    classifyError,
    shouldAutoOffer,
    resolveChannel,
    channelForVersion,
    launchCheckDelayMs: LAUNCH_CHECK_DELAY_MS,
    checkIntervalMs: CHECK_INTERVAL_MS,
  });
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
