/**
 * instanceGate.js — the two decisions behind "can that instance host the pet".
 *
 * Pure, so both are testable without Electron or a tunnel (same split as
 * instance-guard.js). The effects — the HTTP request and the 60s cache — stay in
 * main.js; only the judgement lives here.
 */

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

module.exports = { parseMochiEnabled, enabledOrTrust };
