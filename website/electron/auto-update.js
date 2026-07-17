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

// Default update feed host. The feed compares the client's current version
// against latest.json for this channel+platform and returns 200 (Squirrel JSON)
// or 204 (no update). See kirocrew-publish-lambda-spec.md for the contract.
const DEFAULT_FEED_BASE = "https://updates.kirocrew.dev/feed"; // placeholder host
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000; // every 4h while running
const LAUNCH_CHECK_DELAY_MS = 30 * 1000; // let startup settle first

/**
 * Map the build flavor ("beta" | "stable") to an update channel.
 * Beta builds track the fast "insider" channel (nightlies + internal testers);
 * stable builds track "stable". The public KiroCrew ships a single "stable"
 * flavor, so getFlavor is wired to a constant "stable" at the call site.
 * @param {"beta"|"stable"} flavor
 * @returns {"insider"|"stable"}
 */
function channelForFlavor(flavor) {
  return flavor === "beta" ? "insider" : "stable";
}

/**
 * Build the Squirrel.Mac feed URL. Pure + testable.
 * @param {{base:string, platform:string, channel:string, version:string}} o
 * @returns {string}
 */
function buildFeedUrl({ base, platform, channel, version }) {
  const b = (base || DEFAULT_FEED_BASE).replace(/\/+$/, "");
  const q = new URLSearchParams({ platform, channel, version });
  return `${b}?${q.toString()}`;
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
    onUpdateState = null,
    log = console,
  } = deps;

  // When the in-app UI is wired (onUpdateState provided), it owns the prompt;
  // the native dialog stays as the fallback for headless / no-renderer cases.
  const uiDriven = typeof onUpdateState === "function";
  function emit(state, extra = {}) {
    if (!uiDriven) return;
    try {
      onUpdateState({ state, channel: channelForFlavor(getFlavor()), version: app.getVersion(), ...extra });
    } catch (err) {
      log.error("[update] onUpdateState threw", err);
    }
  }
  function getInfo() {
    return { version: app.getVersion(), channel: channelForFlavor(getFlavor()), platform, packaged: !!app.isPackaged };
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
    const channel = channelForFlavor(getFlavor());
    const url = buildFeedUrl({
      base: feedBase,
      platform,
      channel,
      version: app.getVersion(),
    });
    autoUpdater.setFeedURL({ url });
    log.info(`[update] feed: ${url}`);
  }

  function safeCheck() {
    try {
      configureFeed(); // re-read flavor/channel each check
      autoUpdater.checkForUpdates();
    } catch (err) {
      log.error("[update] checkForUpdates threw", err);
      emit("error", { message: String(err && err.message || err) });
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
      title: "KiroCrew update ready",
      message: `KiroCrew ${versionName || ""} is ready to install.`.trim(),
      detail:
        (notes || "").slice(0, 500) +
        "\n\nKiroCrew will stop the local gateway, install the update, and relaunch.",
    });
    if (response === 0) {
      await applyUpdateAndRestart();
    } else {
      app.once("before-quit", deferredInstallOnQuit);
      try {
        new Notification({
          title: "Update deferred",
          body: "KiroCrew will finish updating the next time you quit.",
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
  setTimeout(safeCheck, LAUNCH_CHECK_DELAY_MS);
  setInterval(() => { if (!updateReady) safeCheck(); }, CHECK_INTERVAL_MS);

  // Renderer-callable triggers (wired to ipcMain in main.js).
  return {
    check: () => { emit("checking"); safeCheck(); },
    install: () => applyUpdateAndRestart(),
    getInfo,
    isReady: () => updateReady,
  };
}

module.exports = { initAutoUpdate, channelForFlavor, buildFeedUrl };
