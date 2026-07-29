/**
 * Desktop auto-update via Electron's native autoUpdater (Squirrel.Mac).
 *
 * Squirrel.framework + ShipIt are already in the signed app bundle, so this
 * only wires the updater to a feed and drives the install. The ONE
 * KiroCrew-specific concern vs. a plain Electron app: the bundled Python
 * gateway is a long-running child process, so it MUST be stopped gracefully
 * BEFORE Squirrel swaps the .app bundle — otherwise ShipIt can race the swap
 * and leave a half-replaced app. The graceful stopper is injected from main.js
 * (it calls POST /api/shutdown to flush state, then SIGTERM/SIGKILL).
 *
 * Pure helpers (channelForFlavor, buildFeedUrl) are dependency-free and tested
 * directly. initAutoUpdate takes electron modules + callbacks injected so it
 * stays testable without an Electron runtime.
 */

// Default update feed host: updates.crew.kiro.dev, the pointer hostname of
// the public distribution CDN (CloudFront + OAC over the kirocrew-updates
// bucket). The feed is a STATIC JSON file at <base>/<channel>/latest-mac.json
// written by CI after notarization; the artifact URLs inside it point at the
// byte hostname (download.crew.kiro.dev, CI's CLI_CDN_BASE). There is no
// 200/204 server endpoint: safeCheck() fetches the feed itself and compares
// versions CLIENT-SIDE, engaging Squirrel.Mac only when the feed version
// differs from the running app. (Squirrel treats any 200 feed response as
// "update available", so gating on the client compare is what prevents a
// re-download loop against a static file.)
const DEFAULT_FEED_BASE = "https://updates.crew.kiro.dev/feed";
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first
const FORCE_EXIT_AFTER_MS = 5 * 1000; // failsafe: guarantee exit after quitAndInstall
const FEED_TIMEOUT_MS = 15 * 1000;
const FEED_MAX_BYTES = 64 * 1024;

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
 *   (insider 0.2.0-insider.1 -> stable 0.1.0); safeCheck's compare gate
 *   deliberately engages on any version DIFFERENCE, so that works.
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
 * Build the static feed URL for a channel. Pure + testable.
 *
 * `cacheBust` appends a unique query param. Squirrel.Mac fetches the feed
 * through an NSMutableURLRequest with the default UseProtocolCachePolicy, so
 * it reads NSURLCache -- and a feed object served without Cache-Control gets
 * heuristically cached, making Squirrel resolve a stale entry (the version
 * already installed) while this module's own cacheless fetch sees the new
 * one. A per-check unique URL is not in any HTTP cache's key, so it defeats
 * NSURLCache regardless of the response headers. The origin-side fix is the
 * feed's `Cache-Control: public, max-age=300`; this is the client-side belt,
 * and it is not redundant -- a build already in the field cannot be given the
 * header fix retroactively, so the bust is what lets a poisoned client
 * recover. (The CDN edge is not involved either way: the feed/* behavior is
 * CACHING_DISABLED, so CloudFront always goes to origin.)
 *
 * @param {{base:string, channel:string, cacheBust?:string|number}} o
 * @returns {string}
 */
function buildFeedUrl({ base, channel, cacheBust }) {
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  const url = `${b}/${encodeURIComponent(channel)}/latest-mac.json`;
  if (cacheBust === undefined || cacheBust === null || cacheBust === "") return url;
  return `${url}?_=${encodeURIComponent(String(cacheBust))}`;
}

/**
 * Default feed fetcher: GET the static feed JSON. Injectable via
 * deps.fetchFeed for tests. Bounded body size + timeout; rejects on any
 * non-200 so callers surface a single error path. HTTPS everywhere;
 * plain HTTP is permitted ONLY for loopback hosts so the local update
 * harness (KIROCREW_UPDATE_FEED=http://127.0.0.1:PORT/...) works --
 * cleartext update metadata over a real network stays rejected.
 * @param {string} url
 * @returns {Promise<{version:string, url:string}>}
 */
function fetchFeedHttps(url) {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (err) {
      reject(err);
      return;
    }
    const isLoopback = ["127.0.0.1", "localhost", "[::1]", "::1"].includes(parsed.hostname);
    let mod;
    if (parsed.protocol === "https:") {
      mod = require("https");
    } else if (parsed.protocol === "http:" && isLoopback) {
      mod = require("http");
    } else {
      reject(new Error(`feed URL must be https (or http on loopback): ${parsed.protocol}//${parsed.hostname}`));
      return;
    }
    const req = mod.get(url, { headers: { "cache-control": "no-cache" } }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`feed HTTP ${res.statusCode}`));
        return;
      }
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        body += chunk;
        if (body.length > FEED_MAX_BYTES) req.destroy(new Error("feed response too large"));
      });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); } catch (err) { reject(err); }
      });
    });
    req.on("error", reject);
    req.setTimeout(FEED_TIMEOUT_MS, () => req.destroy(new Error("feed request timed out")));
  });
}

/**
 * Wire Electron's autoUpdater. All Electron surfaces injected for testability.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {import("electron").AutoUpdater} deps.autoUpdater
 * @param {typeof import("electron").dialog} deps.dialog
 * @param {typeof import("electron").Notification} deps.Notification
 * @param {() => string} deps.getFlavor      - returns "beta" | "stable"
 * @param {() => Promise<void>} deps.stopGateway - graceful, awaitable gateway stop
 * @param {string} [deps.platform]           - e.g. "darwin-arm64"
 * @param {string} [deps.feedBase]           - override feed host
 * @param {(state:object) => void} [deps.onUpdateState] - if provided, the
 *   in-app UI drives the install prompt: state transitions are pushed here
 *   ({state, version, notes, channel}) and the native dialog is suppressed.
 *   Without it, the native dialog is the fallback prompt.
 * @param {{info:Function,warn:Function,error:Function}} [deps.log]
 * @returns {{check:Function, install:Function, getInfo:Function}} renderer-callable triggers
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
    feedBase = process.env.KIROCREW_UPDATE_FEED || DEFAULT_FEED_BASE,
    fetchFeed = fetchFeedHttps,
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

  // Squirrel is unavailable for unsigned / not-installed dev builds.
  if (!app.isPackaged) {
    log.info("[update] dev build — auto-update disabled");
    return { check: () => {}, install: async () => {}, getInfo, disabled: "dev" };
  }
  if (process.platform !== "darwin") {
    log.info("[update] non-darwin — auto-update disabled (Squirrel.Mac only)");
    return { check: () => {}, install: async () => {}, getInfo, disabled: "platform" };
  }

  let updateReady = false;
  let downloading = false; // Squirrel download/extract in flight
  let stagedVersion = null; // version name Squirrel has downloaded + staged
  let stagedNotes = "";
  let installing = false;
  let quitHandled = false;
  let feedNonce = 0; // monotonic, so two feed URLs are never byte-identical

  function configureFeed() {
    const channel = currentChannel();
    // Unique per call so neither NSURLCache (Squirrel's fetch) nor this
    // module's fetch can be served a previously-cached feed body. The
    // monotonic counter is NOT redundant with the timestamp: check() ->
    // download() (the consent flow) issues two configureFeed calls that can
    // land in the same millisecond, and a repeated URL is a cache HIT.
    feedNonce += 1;
    const url = buildFeedUrl({ base: feedBase, channel, cacheBust: `${Date.now()}-${feedNonce}` });
    autoUpdater.setFeedURL({ url, headers: { "Cache-Control": "no-cache" } });
    log.info(`[update] feed: ${url}`);
    return url;
  }

  let checking = false;
  let foundFeed = null; // last feed entry surfaced to the user, awaiting consent
  /**
   * Version of the update currently being fetched/held -- NOT the running
   * app's version. Every state the UI renders a version for must pass this
   * explicitly: emit() defaults `version` to app.getVersion() so the
   * check/not-available/error states report the running build, and a
   * "downloading" event that omitted it made the update card claim the app
   * was downloading the version already installed.
   */
  function pendingVersion() {
    return (foundFeed && foundFeed.version) || stagedVersion || app.getVersion();
  }
  async function safeCheck() {
    // NOTE: no updateReady short-circuit here. A check ALWAYS consults the
    // feed and reports state (macOS Software Update semantics) — the silent
    // `return` this replaces made the Check-for-updates button a dead no-op
    // once a download had been staged.
    if (checking) return;
    if (downloading) {
      // Squirrel is mid-download/extract. Re-engaging checkForUpdates() now
      // restarts its update flow and tears down the temp staging dir under
      // the in-flight extraction (observed in the field as
      // "ditto: Could not lstat .../update.XXXX/...: No such file or
      // directory"). Report progress instead; update-downloaded/error will
      // clear the flag and the next check proceeds normally.
      log.info("[update] check requested while download in flight — reporting progress");
      emit("downloading", { version: pendingVersion() });
      return;
    }
    checking = true;
    try {
      const url = configureFeed(); // re-read flavor/channel each check
      emit("checking");
      const feed = await fetchFeed(url);
      if (!feed || typeof feed.version !== "string" || typeof feed.url !== "string") {
        throw new Error("feed missing version/url");
      }
      if (feed.version === app.getVersion()) {
        log.info(`[update] up to date (${feed.version})`);
        foundFeed = null;
        emit("not-available");
        return;
      }
      if (updateReady && stagedVersion === feed.version) {
        // Latest is already downloaded + staged: re-surface the install
        // prompt instead of doing nothing (and instead of re-downloading).
        log.info(`[update] ${stagedVersion} already downloaded — awaiting install`);
        emit("downloaded", { version: stagedVersion, notes: stagedNotes });
        return;
      }
      // CONSENT GATE (macOS Software Update semantics): discovery never
      // downloads. Surface what was found — version, notes, publish date —
      // and wait for an explicit download() before engaging Squirrel.
      foundFeed = feed;
      log.info(`[update] found ${feed.version} (running ${app.getVersion()}) — awaiting user consent`);
      // Nudge hook: main.js shows a native notification pointing at
      // Settings > About (deduped there, once per version). Discovery-only —
      // download/install still require the explicit consent actions.
      if (typeof notifyUpdateFound === "function") {
        try { notifyUpdateFound(feed.version); } catch (err) { log.error("[update] notifyUpdateFound threw", err); }
      }
      emit("found", {
        version: feed.version,
        notes: typeof feed.notes === "string" ? feed.notes : "",
        pubDate: typeof feed.pub_date === "string" ? feed.pub_date : "",
      });
    } catch (err) {
      log.error("[update] check failed", err);
      emit("error", { message: String(err && err.message || err) });
    } finally {
      checking = false;
    }
  }

  /**
   * Explicit user consent: engage Squirrel to download (and stage) the
   * version last surfaced by safeCheck. Never called automatically.
   */
  async function startDownload() {
    if (downloading) { emit("downloading", { version: pendingVersion() }); return; }
    if (updateReady && foundFeed && stagedVersion === foundFeed.version) {
      emit("downloaded", { version: stagedVersion, notes: stagedNotes });
      return;
    }
    if (updateReady) {
      // A previously staged bundle was superseded by a newer find: drop the
      // stale stage so Squirrel re-downloads the newest instead of installing
      // an already-old build.
      log.info(`[update] staged ${stagedVersion} superseded — re-downloading`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
    }
    configureFeed();
    log.info("[update] user consented — engaging Squirrel download");
    downloading = true;
    emit("downloading", { version: pendingVersion() });
    autoUpdater.checkForUpdates();
  }

  // ShipIt aborts the bundle swap with "App Still Running Error" (Code=-9)
  // if ANY instance of the app is alive during its ~25s install window — and
  // the user silently relaunches into the OLD version. If anything blocks the
  // Electron quit (a renderer beforeunload, a lingering child holding the
  // process open), force-exit so this instance is guaranteed gone.
  function forceExitFailsafe(reason) {
    const t = setTimeout(() => {
      log.error(`[update] process still alive ${FORCE_EXIT_AFTER_MS}ms after quitAndInstall (${reason}) — forcing exit so ShipIt can swap`);
      try { app.exit(0); } catch { process.exit(0); }
    }, FORCE_EXIT_AFTER_MS);
    if (typeof t.unref === "function") t.unref();
  }

  async function applyUpdateAndRestart() {
    if (installing) return;
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
    autoUpdater.quitAndInstall();
    forceExitFailsafe("manual install");
  }

  // If the user chose "Later", install on the natural quit. before-quit can't
  // await async work, so preventDefault, stop the gateway, then quitAndInstall.
  function deferredInstallOnQuit(event) {
    if (quitHandled || !updateReady) return;
    quitHandled = true;
    event.preventDefault();
    (async () => {
      log.info("[update] deferred install on quit");
      try { await stopGateway(); } catch (err) { log.error("[update] stop on quit errored", err); }
      autoUpdater.quitAndInstall();
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

  autoUpdater.on("error", (err) => { downloading = false; log.error("[update] error", err); emit("error", { message: String(err && err.message || err) }); });
  autoUpdater.on("checking-for-update", () => { log.info("[update] checking…"); emit("checking"); });
  autoUpdater.on("update-not-available", () => { downloading = false; log.info("[update] up to date"); emit("not-available"); });
  autoUpdater.on("update-available", () => { downloading = true; log.info("[update] downloading…"); emit("downloading", { version: pendingVersion() }); });
  autoUpdater.on("update-downloaded", (_e, notes, name) => {
    downloading = false;
    // LAST LINE OF DEFENSE against a stale feed resolution. If Squirrel
    // resolved the version this app is already running, installing it is a
    // no-op that costs a full restart -- and because the running version
    // never changes, the next check re-offers it forever (observed as an
    // endless reinstall loop). Refuse the staged bundle instead of arming
    // the install, and report "up to date", which is what the user's
    // machine actually is. Never sets updateReady, so before-quit cannot
    // install it either.
    if (name && name === app.getVersion()) {
      log.error(`[update] feed resolved ${name}, already installed — refusing self-reinstall`);
      updateReady = false;
      stagedVersion = null;
      stagedNotes = "";
      foundFeed = null;
      emit("not-available");
      return;
    }
    updateReady = true;
    stagedVersion = name || null;
    stagedNotes = notes || "";
    log.info(`[update] downloaded ${name} — ${uiDriven ? "notifying UI" : "prompting"}`);
    emit("downloaded", { version: name || app.getVersion(), notes: notes || "" });
    if (uiDriven) {
      // In-app UI owns the prompt. Still install on a natural quit if the user
      // dismisses the modal with "Later" (mirrors the native dialog's deferral).
      app.once("before-quit", deferredInstallOnQuit);
    } else {
      promptInstall(name, notes);
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

module.exports = { initAutoUpdate, channelForFlavor, channelForVersion, resolveChannel, buildFeedUrl, fetchFeedHttps };
