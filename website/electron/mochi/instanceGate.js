/**
 * instanceGate.js — the two decisions behind "can that instance host the pet".
 *
 * Pure, so both are testable without Electron or a tunnel (same split as
 * instance-guard.js). The effects — the HTTP request and the 60s cache — stay in
 * main.js; only the judgement lives here.
 */

const { SELF_INSTANCE } = require("./machineStore");

/**
 * Read `/api/apps` and answer whether Mochi is enabled.
 *
 * Handles BOTH payload shapes on purpose: a bare array and `{apps: [...]}`. That
 * variance is not hypothetical — assuming one shape is exactly how Mochi's MCP
 * settings panel silently showed nothing until the route was pinned down.
 *
 * @returns {boolean|null} null = the payload could not be understood at all
 *   (which is NOT the same as "disabled" — see `enabledOrTrust`)
 */
function parseMochiEnabled(payload) {
  const apps = Array.isArray(payload) ? payload : payload && payload.apps;
  if (!Array.isArray(apps)) return null;
  const mochi = apps.find((a) => a && a.name === "mochi");
  // The gateway answered and Mochi is not among its apps — a real "no", not a
  // non-answer: it genuinely cannot host the pet.
  if (!mochi) return false;
  return !!mochi.enabled;
}

/**
 * Turn a probe result into a decision, deciding what a NON-ANSWER means.
 *
 * A non-answer (timeout, unparseable body, transport error) must NOT read as
 * "disabled". The tunnel is already confirmed up by the time we ask, so one slow
 * or garbled reply would otherwise yank the pet back to the local instance —
 * and because appearance and chat history follow the instance, the user would
 * watch their pet turn into a different pet over a hiccup.
 *
 * Fail-OPEN is safe here precisely because it is not a security decision: if we
 * guess wrong the remote's own `_require_enabled` still refuses every call. The
 * cost of guessing wrong in this direction is an inert pet; the cost in the other
 * direction is the pet moving on its own for no reason.
 *
 * @param {boolean|null} probe
 * @returns {boolean}
 */
function enabledOrTrust(probe) {
  return probe === null ? true : probe;
}

/**
 * A tick found the HOST gateway's Mochi disabled. Does that mean tear the pet
 * down?
 *
 * Only when the pet has nowhere else to be. "Should there be a pet" and "whose
 * Mochi does it show" are two different questions, and the host gateway only
 * owns the first one FOR ITSELF: when the pet is showing instance X, everything
 * it needs is served by X — the page, the token, and every
 * `/api/apps/mochi/*` call. X having Mochi on is what makes the pet work, so the
 * host being switched off says nothing about whether that pet can continue.
 *
 * This is the whole point of the fix: a user who runs Mochi on a remote crew and
 * turns it off locally is telling us to stop MOCHI'S BACKEND WORK HERE (which
 * `on_shutdown` duly does — pollers, watchlist guard, stats), not to take away a
 * pet that is being served from somewhere else entirely.
 *
 * `self` still tears down, because then the host IS the thing being shown.
 *
 * @param {string} shownInstanceId what the pet is currently showing
 * @param {boolean} remoteStillUsable did this tick resolve that instance as live
 *   AND Mochi-enabled? A definite no falls back to self, which is disabled — so
 *   there is genuinely no pet to keep.
 * @returns {boolean}
 */
function hostDisabledMeansTeardown(shownInstanceId, remoteStillUsable) {
  if (typeof shownInstanceId !== "string" || !shownInstanceId.trim()) return true;
  if (shownInstanceId === SELF_INSTANCE) return true;
  return !remoteStillUsable;
}

module.exports = { parseMochiEnabled, enabledOrTrust, hostDisabledMeansTeardown };
