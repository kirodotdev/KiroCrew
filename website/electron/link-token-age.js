"use strict";

/**
 * When a cached gateway credential must be re-resolved before the gateway
 * would refuse it.
 *
 * The shell's credential chain (main.js fetchMochiGatewayAuth) answers a minted
 * LINK token for a local-secret or SSH credential (`viaCookie: false`). The
 * gateway accepts one sent as `?token=` for LINK_WINDOW_SECS (300 s) after it
 * was minted, then refuses it with 403 `token expired`. Each refusal is a
 * `denied` security-event record nobody forged, so a client that reuses one
 * credential across polls re-resolves it once it is LINK_TOKEN_REFRESH_MS old
 * instead of learning the expiry from that refusal. The 401/403 re-resolve
 * each caller already has stays as the fallback for an early refusal.
 *
 * The age is wall-clock because the gateway's `exp` is: a forward jump past the
 * window re-mints, and a backward jump (negative age) re-mints too, since the
 * credential's real age is then unknown. A borrowed session cookie
 * (`viaCookie: true`) is checked against `session_exp` instead and is never
 * aged out here.
 */

// Below the gateway's 300 s LINK_WINDOW_SECS with a minute of slack for a slow
// mint, an SSH round trip or a request in flight.
const LINK_TOKEN_REFRESH_MS = 240_000;

/**
 * @param {{value?: string, viaCookie?: boolean} | null} auth the cached credential
 * @param {number} resolvedAt wall-clock ms when it was resolved (stamped before the mint)
 * @param {number} now wall-clock ms
 * @returns {boolean} true when a link token must be resolved again before use
 */
function linkTokenIsStale(auth, resolvedAt, now) {
  if (!auth || !auth.value || auth.viaCookie) return false;
  const age = now - resolvedAt;
  return age < 0 || age >= LINK_TOKEN_REFRESH_MS;
}

module.exports = { LINK_TOKEN_REFRESH_MS, linkTokenIsStale };
