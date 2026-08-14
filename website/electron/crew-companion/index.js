/**
 * index.js — the companion's window lifecycle, driven by the app's enabled state.
 *
 * `main.js` calls `initCrewCompanion` once at startup and `shutdownCrewCompanion` on
 * quit. Everything else is a reconcile tick: ask the gateway whether the app is on,
 * and make the windows match. That is why clicking Enable in the dashboard is
 * sufficient — nothing launches anything, so nothing can fail and be rolled back.
 *
 * THREE STATES, NOT TWO. The probe answers enabled, disabled, or *unknown*, and
 * unknown must leave every window exactly as it is. Treating a failed probe as
 * "disabled" is what makes a companion appear to crash and reappear every few
 * seconds during an ordinary gateway restart — the reference implementation carries
 * the same warning for the same reason.
 */

const { ipcMain } = require("electron");
const http = require("http");

const {
  closePanelWindow,
  registerPanelIpc,
  setPanelClosedHandler,
  setPanelLogger,
  setPanelTarget,
} = require("./panelWindow");
const {
  closeGalleryWindow,
  openGalleryWindow,
  registerGalleryIpc,
  setAppearanceChangedHandler,
  setGalleryLogger,
  setGalleryTarget,
  setGalleryOpenedHandler,
  setGalleryClosedHandler,
} = require("./galleryWindow");
const {
  broadcastToPets,
  openPetWindow,
  closePetWindow,
  petWindowCount,
  setOverlayLogger,
  setOverlayTarget,
  registerOverlayIpc,
  stopHitboxPoll,
} = require("./petOverlay");

const APP_NAME = "crew-companion";

/** Reconcile cadence. Fast enough that Enable feels immediate, cheap on loopback. */
const TICK_MS = 5_000

let backendUrl = "";
let fetchLocalToken = null;
let log = () => {};
let timer = null;
let reconciling = false;
/**
 * Resolve the dashboard window a notification should be opened in.
 *
 * Supplied by main.js, which owns window lifecycle; the same shape `initMochi`
 * takes its `getMainWindow` in. Absent when the companion is initialised without
 * it, in which case the CTA reports that it could not act rather than pretending.
 */
let getDashboardWindow = null;

/**
 * Ask the gateway whether the app is enabled.
 *
 * @returns {Promise<"enabled"|"disabled"|"unknown">}
 */
function probeEnabled(token) {
  return new Promise((resolve) => {
    const url = `${backendUrl}/api/apps?token=${encodeURIComponent(token)}`;
    const req = http.get(url, { timeout: 5_000 }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        if (res.statusCode !== 200) return resolve("unknown");
        try {
          const parsed = JSON.parse(body);
          const rows = Array.isArray(parsed) ? parsed : parsed.apps || [];
          const row = rows.find((a) => a && a.name === APP_NAME);
          // ABSENT is not DISABLED: an older gateway that does not ship this
          // builtin, or a response shape we did not expect, must not be read as
          // an instruction to tear the windows down.
          if (!row) return resolve("unknown");
          resolve(row.enabled ? "enabled" : "disabled");
        } catch {
          resolve("unknown");
        }
      });
    });
    req.on("timeout", () => {
      req.destroy();
      resolve("unknown");
    });
    req.on("error", () => resolve("unknown"));
  });
}

/**
 * Surface the dashboard for a notification's session.
 *
 * The overlay cannot do this itself: it is a full-display, non-focusable window
 * that knows nothing about the dashboard's windows, so — exactly like the panel's
 * `crew-companion:panel-open` — it asks the main process to raise the right one.
 * The route is the dashboard's own session deep link, the same `?sid=` the System
 * page's session rows and the app SDK build.
 *
 * An empty *slotKey* means the notification names no session (an approval raised
 * with no owning conversation carries `slot: ""`). The dashboard is still
 * surfaced — that is where the approvals surface lives — but nothing is routed,
 * because navigating away from whatever the user had open would claim to have
 * opened a session that was never identified.
 *
 * @param {string} slotKey dashboard slot key, or "" when there is no session.
 * @returns {boolean} true when the dashboard was surfaced; false when there was
 *   no window able to receive the request, so the caller can KEEP a notification
 *   it could not act on instead of dismissing it.
 */
function openDashboardSession(slotKey) {
  const win = getDashboardWindow && getDashboardWindow();
  if (!win || win.isDestroyed()) return false;
  // Resolved BEFORE the window is raised: a routing request that cannot be
  // delivered must fail whole, not leave the dashboard focused on the wrong
  // session while the overlay is told the session was opened.
  const view = win._mcView;
  const wc = view && view.webContents;
  if (slotKey && (!wc || wc.isDestroyed())) return false;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (slotKey) wc.send("navigate", `/chat?sid=${encodeURIComponent(slotKey)}`);
  return true;
}

async function reconcileOnce() {
  if (reconciling) return;
  reconciling = true;
  try {
    let token = "";
    try {
      token = (fetchLocalToken && (await fetchLocalToken())) || "";
    } catch {
      token = "";
    }
    if (!token) {
      // No credential means we cannot ask, which is unknown — not disabled.
      return;
    }

    setOverlayTarget(backendUrl, token);
    setPanelTarget(backendUrl, token);
    setGalleryTarget(backendUrl, token);
    const state = await probeEnabled(token);

    if (state === "unknown") return; // leave every window exactly as it is
    if (state === "disabled") {
      if (petWindowCount() > 0) {
        closePanelWindow();
        closeGalleryWindow();
        closePetWindow();
        log("crew-companion: disabled — overlays closed");
      }
      return;
    }
    if (petWindowCount() === 0) {
      openPetWindow();
      log("crew-companion: enabled — overlays opened");
    }
  } finally {
    reconciling = false;
  }
}

/**
 * Start following the app's enabled state.
 *
 * @param {{backendUrl: string, fetchLocalToken: () => Promise<string>, glog: (m: string) => void,
 *   getDashboardWindow?: () => (object | null)}} deps
 */
function initCrewCompanion(deps) {
  backendUrl = (deps && deps.backendUrl) || "";
  fetchLocalToken = deps && deps.fetchLocalToken;
  log = (deps && deps.glog) || (() => {});
  getDashboardWindow = (deps && deps.getDashboardWindow) || null;
  setOverlayLogger(log);
  setPanelLogger(log);
  setGalleryLogger(log);
  registerPanelIpc();
  registerGalleryIpc();
  // Switching avatars in the gallery window must reach the overlays, which are
  // separate windows and share nothing but the main process.
  setAppearanceChangedHandler(() => broadcastToPets("crew-companion:appearance-changed"));
  // Tell every companion overlay when the panel goes, so its click target and its
  // focusable flag both return to the closed state.
  setPanelClosedHandler(() => broadcastToPets("crew-companion:panel-closed"));
  // And when the avatar gallery opens / closes, so the companion holds still while
  // the user browses it and resumes wandering afterwards.
  setGalleryOpenedHandler(() => broadcastToPets("crew-companion:gallery-opened"));
  setGalleryClosedHandler(() => broadcastToPets("crew-companion:gallery-closed"));

  // The renderer reports the companion's, bubble's and menu's hitboxes; the main
  // process polls the cursor and toggles each overlay's click-through itself. This
  // replaces the old pointer-enter/leave `setInteractive` round-trip, whose latency
  // let a click on the companion body fall through to the window behind it.
  registerOverlayIpc();

  /**
   * Focus, granted only while the panel is open.
   *
   * The overlay is non-focusable by default so it never takes focus from the user's
   * real work, but a non-focusable window receives no keyboard events at all — and
   * the panel has a reminder input. Narrowing the grant to the panel's lifetime is
   * what keeps both properties: type into the panel, and never have the desktop
   * steal focus the rest of the time.
   */
  ipcMain.on("crew-companion:focusable", (event, focusable) => {
    const win = event.sender && require("electron").BrowserWindow.fromWebContents(event.sender);
    if (!win || win.isDestroyed()) return;
    win.setFocusable(Boolean(focusable));
    // setFocusable alone does not move focus; without this the panel opens focusable
    // but still unfocused, so the first keystroke goes to the previous app.
    if (focusable) win.focus();
  });

  /**
   * "Open session" on a waiting-on-you bubble.
   *
   * `handle`, not `on`, because the answer is what makes the CTA honest: the
   * overlay clears the notification only once the dashboard has actually been
   * surfaced, and a sticky approval bubble is the only pointer the user has back
   * to the blocked session. `removeHandler` first so a second init cannot throw
   * on the duplicate registration.
   */
  ipcMain.removeHandler("crew-companion:open-session");
  ipcMain.handle("crew-companion:open-session", (_event, slotKey) =>
    openDashboardSession(typeof slotKey === "string" ? slotKey : ""));

  if (timer) clearInterval(timer);
  timer = setInterval(() => void reconcileOnce(), TICK_MS);
  // A background poll must never be the reason a process cannot exit. Electron's
  // main process is kept alive by the app itself, so this timer has no business
  // holding the event loop open — and an un-unref'd one turns any early return
  // that skips shutdown into a process that hangs forever instead of exiting.
  timer.unref?.();
  void reconcileOnce();
}

function shutdownCrewCompanion() {
  closePanelWindow();
  closeGalleryWindow();
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  stopHitboxPoll();
  closePetWindow();
}

module.exports = {
  initCrewCompanion,
  shutdownCrewCompanion,
  // Exported for tests: they drive the tick directly rather than waiting 5s.
  reconcileOnce,
  probeEnabled,
  openDashboardSession,
};
