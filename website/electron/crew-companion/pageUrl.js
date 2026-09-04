/**
 * pageUrl.js — build the URL for one of Crew Companion's windows.
 *
 * The windows are top-level pages loaded FROM the gateway. For the local gateway
 * that is the same origin the dashboard already authenticated, so its cookie is
 * present and no credential is needed on the URL.
 *
 * Deliberately NOT setting the cookie ourselves: that would mean reproducing half
 * the server's session setup (the access cookie is HttpOnly + SameSite=Lax with a
 * TTL, and a separate refresh cookie is issued alongside it). Handing a credential
 * on the query string lets the gateway establish both, exactly as it does for the
 * dashboard's own windows.
 */

/**
 * URL namespace for app-shipped standalone windows: two path segments after the
 * prefix, mirroring `src/apps/<app>/<name>.html` on disk, so the URL and the file
 * agree by construction. Keep in sync with `APP_WINDOW_URL_PREFIX` in
 * `dashboard/server.py` and `vite.config.ts`.
 */
const APP_WINDOW_PREFIX = "app-windows/crew-companion";

/**
 * @param {string} baseUrl gateway origin, e.g. http://localhost:5476
 * @param {string} page    window filename, e.g. "pet.html"
 * @param {string} [token] first-load credential; omit for the local gateway
 * @returns {string}
 */
function companionPageUrl(baseUrl, page, token) {
  const base = String(baseUrl || "").replace(/\/$/, "");
  const url = `${base}/${APP_WINDOW_PREFIX}/${page}`;
  // One place for "append it unless empty" — several copies would be several
  // chances for one to drift and silently 403.
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

module.exports = { companionPageUrl, APP_WINDOW_PREFIX };
