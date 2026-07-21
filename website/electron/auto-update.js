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

// Default update feed host: the public distribution CDN (CloudFront + OAC
// over the kirocrew-updates bucket). The feed is a STATIC JSON file at
// <base>/<channel>/latest-mac.json written by CI after notarization; the
// artifact it points at lives under desktop/<channel>/<version>/ on the
// same CDN. There is no 200/204 server endpoint: safeCheck() fetches the
// feed itself and compares versions CLIENT-SIDE, engaging Squirrel.Mac only
// when the feed version differs from the running app. (Squirrel treats any
// 200 feed response as "update available", so gating on the client compare
// is what prevents a re-download loop against a static file.)
const DEFAULT_FEED_BASE = "https://d28nxu9if70cmc.cloudfront.net/feed";
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first
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
 * Build the static feed URL for a channel. Pure + testable.
 * @param {{base:string, channel:string}} o
 * @returns {string}
 */
function buildFeedUrl({ base, channel }) {
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  return `${b}/${encodeURIComponent(channel)}/latest-mac.json`;
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
  // the UI: stamped version wins, flavor is the unstamped-dev fallback.
  function currentChannel() {
    return channelForVersion(app.getVersion()) || channelForFlavor(getFlavor());
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
    return { version: app.getVersion(), channel: currentChannel(), platform, packaged: !!app.isPackaged };
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
  let installing = false;
  let quitHandled = false;

  function configureFeed() {
    const channel = currentChannel();
    const url = buildFeedUrl({ base: feedBase, channel });
    autoUpdater.setFeedURL({ url });
    log.info(`[update] feed: ${url}`);
    return url;
  }

  let checking = false;
  async function safeCheck() {
    if (checking || updateReady) return;
    checking = true;
    try {
      // CLIENT-SIDE version gate (see DEFAULT_FEED_BASE note): fetch the
      // static feed and only hand off to Squirrel when the version differs.
      // Squirrel would otherwise re-download on every check forever, since a
      // static 200 feed always reads as "update available" to it.
      const url = configureFeed(); // re-read flavor/channel each check
      emit("checking");
      const feed = await fetchFeed(url);
      if (!feed || typeof feed.version !== "string" || typeof feed.url !== "string") {
        throw new Error("feed missing version/url");
      }
      if (feed.version === app.getVersion()) {
        log.info(`[update] up to date (${feed.version})`);
        emit("not-available");
        return;
      }
      log.info(`[update] feed has ${feed.version} (running ${app.getVersion()}) — engaging Squirrel`);
      autoUpdater.checkForUpdates();
    } catch (err) {
      log.error("[update] check failed", err);
      emit("error", { message: String(err && err.message || err) });
    } finally {
      checking = false;
    }
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

  autoUpdater.on("error", (err) => { log.error("[update] error", err); emit("error", { message: String(err && err.message || err) }); });
  autoUpdater.on("checking-for-update", () => { log.info("[update] checking…"); emit("checking"); });
  autoUpdater.on("update-not-available", () => { log.info("[update] up to date"); emit("not-available"); });
  autoUpdater.on("update-available", () => { log.info("[update] available — downloading…"); emit("available"); });
  autoUpdater.on("update-downloaded", (_e, notes, name) => {
    updateReady = true;
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

  // Renderer-callable triggers (wired to ipcMain in main.js).
  return {
    check: () => safeCheck(),
    install: () => applyUpdateAndRestart(),
    getInfo,
    isReady: () => updateReady,
  };
}

module.exports = { initAutoUpdate, channelForFlavor, channelForVersion, buildFeedUrl, fetchFeedHttps };
