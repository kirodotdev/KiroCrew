"use strict";

/**
 * Borrow the gateway session the MAIN WINDOW already established, for
 * Mochi's poller — a plain Node process in the main process with no browser
 * session of its own.
 *
 * Two paths already cover how the shell gets a gateway credential: the local
 * secret (local-token.js, same machine) and an explicitly configured SSH
 * remote host (remote-token.js). Neither reaches a third topology: the
 * gateway and Electron are reachable over loopback-equivalent networking (a
 * WSL2 gateway and a Windows Electron shell, say) but sit on different
 * filesystems, so `os.homedir()` inside Electron never resolves to the
 * gateway's `.local_secret` — and there is nothing remote about the setup to
 * configure an SSH host for.
 *
 * The main window already authenticated — by whichever path worked for it —
 * the moment it first loaded successfully: the gateway's
 * token_auth_middleware sets an `mc_token_<port>` session cookie on that
 * response (httpOnly, hours-long `session_exp`; see
 * docs/system-specs/modules/security.md). This reads that cookie back out of
 * Electron's own cookie store so Mochi can present the SAME credential.
 *
 * FAILS CLOSED: a missing session/cookie API, an unparsable backendUrl, or no
 * matching cookie all resolve to "". This never mints or fabricates a
 * credential — it can only ever hand back one a genuine prior authentication
 * already produced, so an app that never authenticated stays unauthenticated.
 *
 * @param {{electronSession: {cookies: {get: Function}}, backendUrl: string}} deps
 * @returns {Promise<string>}
 */
async function borrowSessionToken({ electronSession, backendUrl }) {
  if (!electronSession || typeof electronSession.cookies?.get !== "function") return "";
  let port;
  try {
    port = new URL(backendUrl).port;
  } catch {
    return "";
  }
  if (!port) return "";
  try {
    const cookies = await electronSession.cookies.get({ url: backendUrl, name: `mc_token_${port}` });
    const value = Array.isArray(cookies) && cookies[0] && cookies[0].value;
    return typeof value === "string" ? value : "";
  } catch {
    return "";
  }
}

module.exports = { borrowSessionToken };
