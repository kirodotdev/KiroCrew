"use strict";

/**
 * dashboard-session.js: surface the dashboard window on a session, for a window
 * that has no handle on it (a desktop overlay, a kirocrew:// link).
 *
 * An overlay is a full-display, non-focusable window that knows nothing about the
 * dashboard's windows, so it asks the main process to raise the right one. The
 * route is the dashboard's own session deep link, the same `?sid=` the System
 * page's session rows and the app SDK build.
 */

const { cancelPendingTrayHide } = require("./hide-to-tray");

/**
 * True when *wc* currently hosts the dashboard itself.
 *
 * The `navigate` message is only listened for by the dashboard SPA. Every other
 * page this view can hold drops it silently: the boot and recovery splashes
 * (`loading.html`, loaded as `file://`), an error page, or a view still blank
 * mid-load. Sending into one of those and returning true would tell the caller
 * the session had been opened, and a caller dismisses a sticky notification on
 * that word, so the notification is lost and the session never opens.
 *
 * Matched on ORIGIN rather than on a full URL, because the dashboard's address
 * carries a `?token=` and any in-app route: same origin is exactly the property
 * that decides whether the SPA, and therefore the listener, is loaded.
 *
 * @param {object} wc the view's webContents.
 * @param {string} base the backend URL to match.
 * @returns {boolean} true only for a page served from the dashboard's origin.
 */
function hostsDashboard(wc, base) {
  if (!base) return false;
  let current = "";
  try {
    current = (wc.getURL && wc.getURL()) || "";
  } catch {
    return false; // torn down between the isDestroyed() check and here
  }
  try {
    return new URL(current).origin === new URL(base).origin;
  } catch {
    return false; // "", about:blank, or a file:// splash: not the dashboard
  }
}

/**
 * True when the view holds a page from some OTHER web origin.
 *
 * The complement of `hostsDashboard` is not one state but two, and only one of
 * them is a security problem:
 *
 * * the app's own local shell ("", `about:blank`, or the `file://` splash) is
 *   this window on its way to becoming the dashboard. Nothing foreign is
 *   displayed and nothing has been navigated away from.
 * * an `http(s)` origin that is not the backend's is a page the dashboard window
 *   is not supposed to be showing at all.
 *
 * `hostsDashboard` answers false for both, which is right for ROUTING (neither
 * can receive a `navigate`) and wrong for AUTHORIZATION (only the second means
 * the caller must be refused). This predicate isolates the second.
 *
 * @param {object} wc the view's webContents.
 * @param {string} base the backend URL to compare against.
 * @returns {boolean} true only for a committed non-backend web origin.
 */
function showsForeignOrigin(wc, base) {
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
    return false; // "", about:blank, or a relative shell path: not foreign
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  if (!base) return true; // a web page with no backend to compare against
  // Same rule as the routing check above, and the same rule the mint dials by, so
  // a window this app opened at the minted origin is never read as foreign while
  // the configured URL still spells the host `localhost`. A v6 spelling is NOT
  // covered: that is a different listener.
  return url.origin !== new URL(base).origin;
}

/**
 * Raise the dashboard window and route it to *slotKey*'s session.
 *
 * An empty *slotKey* means the caller names no session (an approval raised with
 * no owning conversation). The dashboard is still surfaced, since that is where
 * the approvals surface lives, but nothing is routed: navigating away from what
 * the user had open would claim to have opened a session never identified.
 *
 * @param {object|null} win the dashboard window to use (main.js resolves it).
 * @param {string} base the backend URL.
 * @param {string} slotKey dashboard slot key, or "" when there is no session.
 * @param {{ showApp?: () => void, focusApp?: () => void }} [surface] the same
 *   hooks the summon hotkey takes: `showApp` runs before the window is shown so
 *   macOS does not ignore `win.show()` for an app a fullscreen tray-close hid,
 *   and `focusApp` runs after it to bring the app in front.
 * @returns {boolean} true when the dashboard was surfaced AND ready to show it;
 *   false when nothing could receive the request, so the caller can KEEP a
 *   notification it could not act on instead of dismissing it.
 */
function openDashboardSession(win, base, slotKey, { showApp, focusApp } = {}) {
  if (!win || win.isDestroyed()) return false;
  const view = win._mcView;
  const wc = view && view.webContents;
  // Caller authorization and slot routing are SEPARATE questions, and only the
  // second one depends on naming a session. A missing or destroyed view, and a
  // view showing a foreign web origin, are refusals whether or not a slot was
  // named: gating this on a non-empty slotKey let a slot-less approval land on
  // whatever a focused secondary window happened to be showing and still answer
  // true.
  if (!wc || wc.isDestroyed() || showsForeignOrigin(wc, base)) return false;
  // Is the SPA, and therefore the approvals surface and the navigate listener,
  // actually there? The local shell is this window on its way to the dashboard,
  // so it is ours to raise but has nothing loaded yet.
  const ready = hostsDashboard(wc, base);
  // A request that NAMES a session must fail whole, before the window moves.
  if (slotKey && !ready) return false;
  // A user-intent show, like the tray's and the summon hotkey's: a close-to-tray
  // hide still pending would hide the window again right after this show.
  cancelPendingTrayHide(win);
  if (typeof showApp === "function") showApp();
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (typeof focusApp === "function") focusApp();
  // SURFACING and ACKNOWLEDGING are different answers, and the return value is
  // the second one. Raising a loading window is honest and useful; claiming the
  // session has somewhere to be acted on is not. So the window comes forward and
  // the caller's notification STAYS.
  if (!ready) return false;
  if (slotKey) wc.send("navigate", `/chat?sid=${encodeURIComponent(slotKey)}`);
  return true;
}

module.exports = { hostsDashboard, openDashboardSession, showsForeignOrigin };
