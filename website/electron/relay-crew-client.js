"use strict";

const { dialTarget } = require("./local-token");

/**
 * Ask this shell's own gateway for a relay access token for one remote Crew.
 *
 * The gateway's token route admits only a loopback caller that presents the
 * gateway's local secret in `X-Local-Secret`, so only main can call it: a
 * renderer holds a session cookie, not the secret. The secret goes only to a
 * literal loopback origin (`dialTarget`, the same rule as the dashboard token
 * mint in local-token.js) and never follows a redirect.
 *
 * Never throws and never logs. Only an `ok: true` result carries the token
 * and the Crew URL, and the caller keeps both in main.
 */

const TOKEN_PATH = "/api/relay-viewer/token";
// The gateway may make two control plane calls of up to 15 s each.
const DEFAULT_TIMEOUT_MS = 35_000;
// The control plane client's typed refusal codes, passed through unchanged.
const REFUSAL_CODES = new Set([
  "SIGNED_OUT", "NOT_SET_UP", "CREW_ACCESS_NOT_GRANTED", "REMOTE_ACCESS_DISABLED",
  "ACCESS_DENIED", "CREW_OFFLINE", "THROTTLED", "INVALID_REQUEST",
  "SERVICE_ERROR", "UNREACHABLE", "BAD_ANSWER", "SIGN_IN_CHANGED",
]);

const refused = (code, retryAfter = null) => ({ ok: false, code, retryAfter });
const finiteOrNull = (n) => (Number.isFinite(n) ? n : null);

/**
 * @returns {Promise<{ok: true, grant: object, crewUrl: string, name: string}
 *   | {ok: false, code: string, retryAfter: number|null}>}
 */
async function requestRelayCrewToken({
  backendUrl, endpointId, signInMarker = null, getSecret, fetchFn = (url, init) => fetch(url, init),
  timeoutMs = DEFAULT_TIMEOUT_MS,
}) {
  const target = dialTarget(backendUrl);
  if (!target) return refused("UNAVAILABLE");
  let secret = "";
  try { secret = getSecret(backendUrl) || ""; } catch { /* treated as no secret */ }
  if (typeof secret !== "string" || !secret) return refused("UNAVAILABLE");

  let res;
  let body;
  try {
    res = await fetchFn(`${target.origin}${TOKEN_PATH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Local-Secret": secret },
      // The window's sign-in marker, on a renewal only: the gateway refuses
      // SIGN_IN_CHANGED when the vault now holds another account.
      body: JSON.stringify(typeof signInMarker === "string" && signInMarker ? { endpointId, signInMarker } : { endpointId }),
      redirect: "error",
      signal: AbortSignal.timeout(timeoutMs),
    });
    body = await res.json().catch(() => null);
  } catch {
    return refused("UNAVAILABLE");
  }
  if (res.status === 404) return refused("RELAY_VIEWER_OFF");
  if (res.status === 200) {
    if (!body || typeof body.accessToken !== "string" || !body.accessToken
      || !Number.isFinite(body.expiresAt) || typeof body.crewUrl !== "string") {
      return refused("BAD_ANSWER");
    }
    return {
      ok: true,
      grant: {
        accessToken: body.accessToken,
        expiresAt: body.expiresAt,
        renewInSeconds: finiteOrNull(body.renewInSeconds),
        serverDate: finiteOrNull(body.serverDate),
      },
      crewUrl: body.crewUrl,
      name: typeof body.name === "string" ? body.name : "",
      // Names the sign-in (not a secret). Kept out of the grant, so renewal never sees it.
      signInMarker: typeof body.signInMarker === "string" && body.signInMarker ? body.signInMarker : null,
    };
  }
  const code = body && typeof body.code === "string" ? body.code : "";
  if (!REFUSAL_CODES.has(code)) return refused("UNAVAILABLE");
  const retryAfter = code === "THROTTLED" && body.retryAfter > 0 && Number.isFinite(body.retryAfter)
    ? body.retryAfter : null;
  return refused(code, retryAfter);
}

module.exports = { requestRelayCrewToken, REFUSAL_CODES, TOKEN_PATH };
