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
// Latched true by suspendCrewCompanion() while an update install is stopping the
// gateway and quitting. The reconcile loop treats a gateway it cannot reach as
// "unknown" and LEAVES every window as-is, so without this latch the overlay
// would float orphaned over the vanished dashboard during the quit handoff — and
// a tick firing in the brief window where the gateway is still up (dispatch runs
// BEFORE stopGateway is awaited) could even reopen a just-closed overlay.
let suspended = false;
/**
 * The token this poll reuses across ticks.
 *
 * Minting one per tick is what this cache exists to stop. Every mint is a full
 * link->session exchange on the gateway: it registers a nonce in a bounded
 * 50-slot ring (so at this cadence the ring turned over every few minutes and
 * evicted OTHER pending one-time links — a phone-access QR among them, before
 * its own window had even lapsed), issued a fresh 30-day refresh chain, and
 * appended the consumed nonce to a persisted denylist. It also meant dozens of
 * live, full-privilege sign-in links existed at any moment purely as a
 * side-effect of asking whether an app is enabled.
 *
 * The token outlives the tick by hours, so reuse is the normal path and a
 * re-mint is the exception: only an actual auth refusal invalidates it.
 */
let cachedToken = "";

/**
 * The token to probe with, minting only when there is nothing usable cached.
 *
 * @param {boolean} forceMint Re-mint even if a token is cached — for the one
 *   retry after the gateway refused the cached one.
 * @returns {Promise<string>}
 */
async function tokenForProbe(forceMint) {
  if (!forceMint && cachedToken) return cachedToken;
  try {
    cachedToken = (fetchLocalToken && (await fetchLocalToken())) || "";
  } catch {
    cachedToken = "";
  }
  return cachedToken;
}

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
 * ``unauthorized`` is split out from ``unknown`` so a cached token that the
 * gateway has stopped accepting can be re-minted exactly once, instead of
 * either wedging the poll forever or going back to minting every tick.
 *
 * @returns {Promise<"enabled"|"disabled"|"unauthorized"|"unknown">}
 */
function probeEnabled(token) {
  return new Promise((resolve) => {
    const url = `${backendUrl}/api/apps?token=${encodeURIComponent(token)}`;
    const req = http.get(url, { timeout: 5_000 }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        // The credential, not the answer, is what went stale — say so, so the
        // caller re-mints rather than treating it as "cannot tell".
        if (res.statusCode === 401 || res.statusCode === 403) {
          return resolve("unauthorized");
        }
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
 * True when *wc* currently hosts the dashboard itself.
 *
 * The `navigate` message is only listened for by the dashboard SPA. Every other
 * page this view can hold drops it silently: the boot and recovery splashes
 * (`loading.html`, loaded as `file://`), an error page, or a view still blank
 * mid-load. Sending into one of those and returning true would tell the overlay
 * the session had been opened, and the caller dismisses a sticky notification on
 * that word — so the notification is lost and the session never opens.
 *
 * Matched on ORIGIN rather than on a full URL, because the dashboard's address
 * carries a `?token=` and any in-app route: same origin is exactly the property
 * that decides whether the SPA — and therefore the listener — is loaded.
 *
 * @param {object} wc the view's webContents.
 * @returns {boolean} true only for a page served from the dashboard's origin.
 */
function hostsDashboard(wc) {
  if (!backendUrl) return false;
  let current = "";
  try {
    current = (wc.getURL && wc.getURL()) || "";
  } catch {
    return false; // torn down between the isDestroyed() check and here
  }
  try {
    return new URL(current).origin === new URL(backendUrl).origin;
  } catch {
    return false; // "", about:blank, or a file:// splash — not the dashboard
  }
}

/**
 * True when the view holds a page from some OTHER web origin.
 *
 * The complement of :func:`hostsDashboard` is not one state but two, and only
 * one of them is a security problem:
 *
 * * the app's own local shell -- "", `about:blank`, or the `file://` splash --
 *   is this window on its way to becoming the dashboard. Nothing foreign is
 *   displayed and nothing has been navigated away from.
 * * an `http(s)` origin that is not the backend's is a page the dashboard
 *   window is not supposed to be showing at all.
 *
 * `hostsDashboard` answers false for both, which is right for ROUTING (neither
 * can receive a `navigate`) and wrong for AUTHORIZATION (only the second means
 * the caller must be refused). This predicate isolates the second.
 *
 * @param {object} wc the view's webContents.
 * @returns {boolean} true only for a committed non-backend web origin.
 */
function showsForeignOrigin(wc) {
  let current = "";
  try {
    current = (wc.getURL && wc.getURL()) || "";
  } catch {
    return false; // torn down; the isDestroyed() check owns that case
  }
  let url;
  try {
    url = new URL(current);
  } catch {
    return false; // "", about:blank, or a relative shell path — not foreign
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  if (!backendUrl) return true; // a web page with no backend to compare against
  try {
    return url.origin !== new URL(backendUrl).origin;
  } catch {
    return true;
  }
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
  // Caller authorization and slot routing are SEPARATE questions, and only the
  // second one depends on naming a session.
  //
  // Unconditional: a missing or destroyed view, and a view showing a foreign web
  // origin, are both refusals whether or not a slot was named. Gating the whole
  // check on a non-empty slotKey let a slot-less approval land on whatever a
  // focused secondary window happened to be showing and still answer true — so
  // the overlay dismissed a sticky notification it had never acted on while the
  // gateway stayed blocked. `getDashboardWindow` cannot prevent this on its own:
  // it takes the focused window whenever it merely HAS an `_mcView`.
  if (!wc || wc.isDestroyed() || showsForeignOrigin(wc)) return false;
  // Is the SPA — and therefore the approvals surface and the navigate listener —
  // actually there? The local shell ("", about:blank, the file:// splash) is
  // this window on its way to the dashboard, so it is ours to raise but has
  // nothing loaded yet.
  const ready = hostsDashboard(wc);
  // A request that NAMES a session must fail whole, before the window moves: a
  // route that cannot be delivered must not leave the dashboard focused while
  // the overlay is told the session was opened.
  if (slotKey && !ready) return false;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  // SURFACING and ACKNOWLEDGING are different answers, and the return value is
  // the second one — the overlay dismisses a sticky notification on true.
  // Raising a loading window is honest and useful; claiming the approval has
  // somewhere to be acted on is not, because the surface that would show it has
  // not loaded. Answering true here dropped the user's only pointer to work the
  // gateway is still blocked on. So the window comes forward and the
  // notification STAYS.
  if (!ready) return false;
  if (slotKey) wc.send("navigate", `/chat?sid=${encodeURIComponent(slotKey)}`);
  return true;
}

async function reconcileOnce() {
  if (suspended) return;
  if (reconciling) return;
  reconciling = true;
  try {
    let token = await tokenForProbe(false);
    if (!token) {
      // No credential means we cannot ask, which is unknown — not disabled.
      return;
    }

    setOverlayTarget(backendUrl, token);
    setPanelTarget(backendUrl, token);
    setGalleryTarget(backendUrl, token);
    let state = await probeEnabled(token);

    if (state === "unauthorized") {
      // The cached token was refused. Re-mint ONCE and retry; a second refusal
      // is left as unknown rather than retried in a loop, so a genuinely broken
      // credential path cannot turn this poll back into a mint-per-tick.
      cachedToken = "";
      token = await tokenForProbe(true);
      if (!token) return;
      setOverlayTarget(backendUrl, token);
      setPanelTarget(backendUrl, token);
      setGalleryTarget(backendUrl, token);
      state = await probeEnabled(token);
      if (state === "unauthorized") {
        cachedToken = "";
        return;
      }
    }

    // Re-check AFTER the awaited probes: the top-of-function latch cannot stop a
    // reconcile that was already in flight (past that check, awaiting the gateway)
    // when suspendCrewCompanion() ran. Without this, that in-flight tick would
    // resume here and reopen the overlay we just closed for the update quit.
    if (suspended) return;
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

  // "Turn off companion" (the pet's context menu) disables the app over HTTP — which
  // updates the dashboard's Apps page too — and then asks us to close the overlay AT
  // ONCE, so it does not linger until the next reconcile tick. The renderer only
  // sends this after the disable POST succeeds, so the app is already disabled by
  // the time we close and the reconcile then keeps it closed. (A reconcile tick
  // that was already in flight and read "enabled" just before the disable landed
  // could reopen the overlay for a single tick; it self-heals on the next tick.)
  ipcMain.on("crew-companion:turn-off", () => {
    closePanelWindow();
    closeGalleryWindow();
    closePetWindow();
    log("crew-companion: turned off by user — overlays closed immediately");
  });


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
  // Drop the reused credential with the poll that reused it, so a later
  // initCrewCompanion starts from a fresh mint instead of a token that may have
  // been minted against a gateway that is no longer the one we will talk to.
  cachedToken = "";
  stopHitboxPoll();
  closePetWindow();
}

/**
 * Close the overlays for an in-flight update install WITHOUT tearing the app
 * down. Unlike shutdownCrewCompanion this keeps the reconcile timer, IPC
 * handlers and cached token intact, so an install that FAILS resumes cleanly via
 * resumeCrewCompanion() with no re-init (re-running initCrewCompanion would stack
 * a second `crew-companion:focusable`/`turn-off` listener). The latch stops the
 * still-running loop from reopening what we just closed.
 */
function suspendCrewCompanion() {
  suspended = true;
  closePanelWindow();
  closeGalleryWindow();
  closePetWindow();
  log("crew-companion: overlays closed for update install");
}

/** Undo suspendCrewCompanion() when an update install did not proceed. The loop
 * reopens the overlays on its next tick once the gateway is reachable again. */
function resumeCrewCompanion() {
  if (!suspended) return;
  suspended = false;
  log("crew-companion: update install did not proceed — resuming");
  void reconcileOnce();
}

module.exports = {
  initCrewCompanion,
  shutdownCrewCompanion,
  suspendCrewCompanion,
  resumeCrewCompanion,
  // Exported for tests: they drive the tick directly rather than waiting 5s.
  reconcileOnce,
  probeEnabled,
  openDashboardSession,
};
