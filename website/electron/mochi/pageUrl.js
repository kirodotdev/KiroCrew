/**
 * pageUrl.js — build the URL for one of Mochi's four windows.
 *
 * All four (pet, panel, settings, avatar) are top-level windows loaded FROM the
 * gateway whose Mochi they show. For the local gateway that is the same origin
 * the dashboard already authenticated, so its cookie is present and no token is
 * needed. For a REMOTE instance — reached on an ssh -L forwarded loopback port —
 * there is no cookie yet, so the first load carries `?token=`, which is what
 * makes the gateway set the port-keyed session cookies (`mc_token_<port>` plus
 * its refresh companion) for that origin.
 *
 * WHY NOT SET THE COOKIE OURSELVES: it would mean reproducing half of the
 * server's session setup — the access cookie is HttpOnly + SameSite=Lax with a
 * TTL, and a separate refresh cookie is issued alongside it. Handing the token
 * on the query string lets the gateway establish both, exactly as it does for
 * the dashboard's own connection windows and for the embedded instance panes.
 *
 * Kept as its own tiny module so the rule lives in ONE place: four copies of
 * "append the token unless it's empty" is four chances for one of them to drift
 * and silently 403 on a remote instance only.
 */

/**
 * URL namespace for app-shipped standalone windows. Two path segments after the
 * prefix, mirroring `src/apps/<app>/<name>.html` on disk — keep in sync with
 * `APP_WINDOW_URL_PREFIX` in `dashboard/server.py` and `vite.config.ts`.
 *
 * The window name alone is passed in (`"pet.html"`, not `"mochi-pet.html"`)
 * because the app segment is not the caller's to choose.
 */
const APP_WINDOW_PREFIX = "app-windows/mochi";

/**
 * @param {string} baseUrl gateway origin, e.g. http://localhost:5476
 * @param {string} page    window filename, e.g. "pet.html"
 * @param {string} [token] first-load token; omit/empty for the local gateway
 * @returns {string}
 */
function mochiPageUrl(baseUrl, page, token) {
  const base = String(baseUrl || "").replace(/\/$/, "");
  const url = `${base}/${APP_WINDOW_PREFIX}/${page}`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

module.exports = { mochiPageUrl, APP_WINDOW_PREFIX };
