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
const { sessionCookieHeader } = require("../mochi-session-token");
const {
  hostsDashboard: sharedHostsDashboard,
  openDashboardSession: sharedOpenDashboardSession,
} = require("../dashboard-session");

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
  overlayMayBeNonActivatable,
  openPetWindow,
  closePetWindow,
  petWindowCount,
  rearmBlankedCompanionWindows,
  setOverlayLogger,
  setOverlayTarget,
  registerOverlayIpc,
  stopHitboxPoll,
} = require("./petOverlay");

const APP_NAME = "crew-companion";

/** Reconcile cadence. Fast enough that Enable feels immediate, cheap on loopback. */
const TICK_MS = 5_000

let backendUrl = "";
/**
 * Resolve a gateway credential as `{ value, viaCookie }`.
 *
 * Supplied by main.js as the SAME chain the dashboard window and Mochi use:
 * local-secret mint, then the SSH-fetched token of a configured remote crew,
 * then the session credential the dashboard window already holds. The local
 * mint alone is not enough: it deliberately refuses for a port a remote crew is
 * configured on and for any gateway this process did not spawn itself, and in
 * both cases the dashboard still signs in by another route. With the mint as the
 * only source the reconcile can never ask, reads "no credential" as unknown,
 * and leaves an enabled companion closed indefinitely.
 */
let fetchGatewayAuth = null;
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
 * Reuse is the normal path and a re-resolve the exception: only an actual auth
 * refusal invalidates the cache. A link token (local mint or ssh fetch) is
 * refused once its short first-load expiry passes, which costs one re-resolve
 * every few minutes; a borrowed session cookie lasts until the session expires.
 */
const NO_AUTH = Object.freeze({ value: "", viaCookie: false });
let cachedAuth = NO_AUTH;

/**
 * How long an EMPTY answer from the credential chain is trusted before asking again.
 *
 * The chain is not free when it finds nothing: with a remote crew whose ssh is
 * failing, every attempt starts an ssh process and posts a status line to the
 * dashboard window. Asking every 5s tick would do that a dozen times a minute
 * for a companion that cannot open anyway. A refusal-driven re-resolve (the
 * gateway rejected a credential we HAD) is not throttled.
 */
const EMPTY_AUTH_RETRY_MS = 30_000;
let emptyAuthAt = 0;

/**
 * The credential to probe with, resolving only when there is nothing usable cached.
 *
 * @param {boolean} forceMint Re-resolve even if a credential is cached — for the
 *   one retry after the gateway refused the cached one.
 * @returns {Promise<{value: string, viaCookie: boolean}>}
 */
async function authForProbe(forceMint) {
  if (!forceMint && cachedAuth.value) return cachedAuth;
  if (!forceMint && emptyAuthAt && Date.now() - emptyAuthAt < EMPTY_AUTH_RETRY_MS) {
    return NO_AUTH;
  }
  try {
    const auth = fetchGatewayAuth && (await fetchGatewayAuth());
    cachedAuth = auth && typeof auth.value === "string" && auth.value
      ? { value: auth.value, viaCookie: Boolean(auth.viaCookie) }
      : NO_AUTH;
  } catch {
    cachedAuth = NO_AUTH;
  }
  emptyAuthAt = cachedAuth.value ? 0 : Date.now();
  return cachedAuth;
}

/**
 * Point every companion window at this credential.
 *
 * A borrowed session credential is the dashboard window's own cookie, already in
 * the shared cookie jar the companion windows load with, so it is NOT put on the
 * page URL: its link-token `exp` froze when that session was first exchanged, and
 * as `?token=` it would be refused. The windows load bare and ride the cookie.
 */
function targetWindows(auth) {
  const pageToken = auth.viaCookie ? "" : auth.value;
  setOverlayTarget(backendUrl, pageToken, auth.viaCookie);
  setPanelTarget(backendUrl, pageToken);
  setGalleryTarget(backendUrl, pageToken);
}

/**
 * Say once that the companion cannot ask the gateway, and once when it can again.
 *
 * An enabled companion that cannot ask would otherwise leave nothing to look at
 * in any log.
 */
let reportedNoCredential = false;
function noteCredential(present) {
  if (!present && !reportedNoCredential) {
    reportedNoCredential = true;
    log("crew-companion: no gateway credential — cannot check whether the app is enabled");
  } else if (present && reportedNoCredential) {
    reportedNoCredential = false;
    log("crew-companion: gateway credential available again");
  }
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
function probeEnabled(auth) {
  const { value, viaCookie } = auth || NO_AUTH;
  return new Promise((resolve) => {
    let url = `${backendUrl}/api/apps`;
    const headers = {};
    if (viaCookie) {
      // A borrowed session credential is checked as the cookie it came from
      // (against its session expiry), exactly as the dashboard window sends it.
      const cookie = sessionCookieHeader(backendUrl, value);
      if (!cookie) return resolve("unknown");
      headers.Cookie = cookie;
    } else {
      url += `?token=${encodeURIComponent(value)}`;
    }
    const req = http.get(url, { timeout: 5_000, headers }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      // A body cut off mid-read emits error, never end.
      res.on("error", () => resolve("unknown"));
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
 * True when *wc* currently hosts the dashboard itself; see dashboard-session.js.
 * The kirocrew:// deep-link handler in main.js passes its own *base*: a link can
 * arrive before this module is initialised.
 */
function hostsDashboard(wc, base = backendUrl) {
  return sharedHostsDashboard(wc, base);
}

/**
 * Surface the dashboard for a notification's session; see dashboard-session.js.
 *
 * @param {string} slotKey dashboard slot key, or "" when there is no session.
 * @returns {boolean} true when the dashboard was surfaced and can show it.
 */
function openDashboardSession(slotKey) {
  return sharedOpenDashboardSession(getDashboardWindow && getDashboardWindow(), backendUrl, slotKey);
}

async function reconcileOnce() {
  if (suspended) return;
  if (reconciling) return;
  reconciling = true;
  try {
    const auth = await authForProbe(false);
    noteCredential(Boolean(auth.value));
    if (!auth.value) {
      // No credential means we cannot ask, which is unknown — not disabled.
      return;
    }

    targetWindows(auth);
    let state = await probeEnabled(auth);

    if (state === "unauthorized") {
      // The cached credential was refused. Re-resolve ONCE and retry; a second
      // refusal is left as unknown rather than retried in a loop, so a genuinely
      // broken credential path cannot turn this poll back into a mint-per-tick.
      cachedAuth = NO_AUTH;
      const reminted = await authForProbe(true);
      if (!reminted.value) return;
      targetWindows(reminted);
      state = await probeEnabled(reminted);
      if (state === "unauthorized") {
        cachedAuth = NO_AUTH;
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
    // A display overlay or hidden notification owner latched on an error document
    // is reloaded here and nowhere else: this tick's probe was just answered with
    // the credential set above, so the reload carries one the gateway accepts,
    // and a reload per 5s tick is the whole retry budget. "unknown" returned
    // before this line on purpose — a gateway that cannot answer the probe cannot
    // serve the page either. Freshly opened windows are still loading and have
    // nothing latched, so this is a no-op right after openPetWindow.
    const rearmed = rearmBlankedCompanionWindows();
    if (rearmed > 0) {
      log(`crew-companion: re-armed ${rearmed} window(s) hidden on an error document`);
    }
  } finally {
    reconciling = false;
  }
}

/**
 * Start following the app's enabled state.
 *
 * @param {{backendUrl: string,
 *   fetchGatewayAuth?: () => Promise<{value: string, viaCookie?: boolean}>,
 *   glog: (m: string) => void,
 *   getDashboardWindow?: () => (object | null)}} deps
 *   `fetchGatewayAuth` is the full credential chain main.js passes.
 */
function initCrewCompanion(deps) {
  suspended = false;
  backendUrl = (deps && deps.backendUrl) || "";
  if (deps && typeof deps.fetchGatewayAuth === "function") {
    fetchGatewayAuth = deps.fetchGatewayAuth;
  } else {
    fetchGatewayAuth = null;
  }
  reportedNoCredential = false;
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
    if (overlayMayBeNonActivatable()) {
      win.setFocusable(Boolean(focusable));
    } else if (!focusable) {
      // Windows keeps the overlay activatable (see overlayMayBeNonActivatable), so
      // hand focus back the way setFocusable(false) would have. setFocusable is not
      // called at all there: it also flips skipTaskbar, putting the overlay in the
      // taskbar.
      win.blur();
    }
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
  // Latch the loop as suspend does: a reconcile already past its awaits would
  // otherwise reopen the overlay after the quit closed it.
  suspended = true;
  closePanelWindow();
  closeGalleryWindow();
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  // Drop the reused credential with the poll that reused it, so a later
  // initCrewCompanion starts from a fresh mint instead of a token that may have
  // been minted against a gateway other than the one we will talk to.
  cachedAuth = NO_AUTH;
  emptyAuthAt = 0;
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
  hostsDashboard,
};
