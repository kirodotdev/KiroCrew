/**
 * Desktop auto-update via electron-updater (macOS + Linux).
 *
 * WHY electron-updater instead of Electron's built-in autoUpdater: the built-in
 * updater covers only macOS (Squirrel.Mac) and Windows (Squirrel.Windows), and
 * requires us to hand-build the feed, the version compare and the publish
 * metadata. electron-updater generates that metadata at build time
 * (latest-mac.yml / latest-linux.yml), verifies sha512 fail-closed, adds Linux
 * support, and — on macOS — still drives Squirrel.Mac underneath, so the proven
 * atomic bundle swap is unchanged. See docs/windows-install.md and issue #598.
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
const DEFAULT_FEED_BASE = "https://updates.crew.kiro.dev/feed";
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall

/** Platforms with a working publish lane + updater. win32 lands with NSIS (#598). */
const SUPPORTED_PLATFORMS = new Set(["darwin", "linux"]);

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
 * @param {{base:string, channel:string}} o
 * @returns {string}
 * @throws {Error} on a non-HTTPS, non-loopback base
 */
function buildFeedBase({ base, channel }) {
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  const url = `${b}/${encodeURIComponent(channel)}/`;
  const parsed = new URL(url);
  const isLoopback = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
  if (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && isLoopback)) {
    throw new Error(`feed base must be https (or http on loopback): ${parsed.protocol}//${parsed.hostname}`);
  }
  return url;
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
 * - autoInstallOnAppQuit=false the default would swap the bundle on quit
 *                             WITHOUT stopping the Python gateway — exactly the
 *                             half-replaced-app race this module prevents.
 *                             deferredInstallOnQuit() does it in the right order.
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
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.allowDowngrade = true;
  autoUpdater.allowPrerelease = true;
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
    platform = "darwin-arm64",
    osPlatform = process.platform,
    feedBase = process.env.KIROCREW_UPDATE_FEED || DEFAULT_FEED_BASE,
    onUpdateState = null,
    log = console,
  } = deps;

  // When the in-app UI is wired (onUpdateState provided), it owns the prompt;
  // the native dialog stays as the fallback for headless / no-renderer cases.
  const uiDriven = typeof onUpdateState === "function";
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
    try {
      onUpdateState({ state, channel: currentChannel(), version: app.getVersion(), ...extra });
    } catch (err) {
      log.error("[update] onUpdateState threw", err);
    }
  }
  function getInfo() {
    const stamped = channelForVersion(app.getVersion());
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
  function pendingVersion() {
    return foundVersion || stagedVersion || app.getVersion();
  }

  function configureFeed() {
    const channel = currentChannel();
    const url = buildFeedBase({ base: feedBase, channel });
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
      log.error("[update] check failed", err);
      emit("error", { message: String((err && err.message) || err) });
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
      log.error("[update] download failed", err);
      emit("error", { message: String((err && err.message) || err) });
    }
  }

  // The bundle swap aborts if ANY instance of the app is alive during its
  // install window — and the user silently relaunches into the OLD version. If
  // anything blocks the Electron quit (a renderer beforeunload, a lingering
  // child holding the process open), force-exit so the swap can proceed.
  function forceExitFailsafe(reason) {
    const t = setTimeout(() => {
      log.error(`[update] process still alive ${FORCE_EXIT_AFTER_MS}ms after quitAndInstall (${reason}) — forcing exit so the install can swap the app`);
      try { app.exit(0); } catch { process.exit(0); }
    }, FORCE_EXIT_AFTER_MS);
    if (typeof t.unref === "function") t.unref();
  }

  // isSilent=false (no installer UI to suppress on these platforms),
  // isForceRunAfter=true so the user lands back in the app after the swap.
  function quitAndInstall() {
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
    downloading = false;
    log.error("[update] error", err);
    emit("error", { message: String((err && err.message) || err) });
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
  DEFAULT_FEED_BASE,
  SUPPORTED_PLATFORMS,
};
