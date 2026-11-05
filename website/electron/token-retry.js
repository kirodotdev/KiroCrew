/**
 * Token retry logic for 403 auto-recovery after gateway restart, and the
 * signed-in dashboard load that it and the boot path share.
 * Extracted for testability.
 */
function createTokenRetryHandler(refreshFn, maxRetries = 2) {
  let retries = 0;
  return async function onNavigate(httpCode) {
    if (httpCode === 403 && retries < maxRetries) {
      retries++;
      await refreshFn();
    } else if (httpCode === 200) {
      retries = 0;
    }
  };
}

/**
 * The path+query a 403 retry should re-request, derived from the URL that
 * actually failed rather than from the window's entry path.
 *
 * A window's entry path can carry a ONE-SHOT intent -- "/chat?new=1" mints a
 * blank session and the SPA immediately replaces the URL with "?sid=<slot>".
 * Replaying the entry path on a later 403 would mint a SECOND session and swap
 * out the one the user is looking at, so the retry has to follow the window.
 *
 * The stale `token` is dropped: the caller signs in with a freshly fetched one.
 * A URL this gateway does not serve (the local splash `file://`, `about:blank`)
 * leaves `fallback` standing, so the entry intent survives until the dashboard
 * itself has navigated.
 */
function dashboardRetryPath(navigatedUrl, backendUrl, fallback = "") {
  let target;
  try {
    target = new URL(navigatedUrl);
    if (target.origin !== new URL(backendUrl).origin) return fallback;
  } catch {
    return fallback;
  }
  target.searchParams.delete("token");
  const query = target.searchParams.toString();
  return `${target.pathname}${query ? `?${query}` : ""}`;
}

/**
 * How long the sign-in request may take before the page loads anyway. The boot
 * path starts the liveness monitor only after the load, so a gateway that
 * wedges mid-exchange must not hold it off forever.
 */
const EXCHANGE_TIMEOUT_MS = 10_000;

/**
 * Load a dashboard URL signed in with `token`, keeping the token out of the
 * page's address.
 *
 * Chromium writes a page's address beside every console line in chromium.log,
 * a file the app cannot filter. The gateway sets its session cookie on any
 * signed-in request that carries `?token=`, so that same request for the page is
 * sent once as a plain fetch through the window's own session, whose cookie jar
 * the page then loads with. A failed exchange still loads the bare URL; the
 * token is never put back.
 */
async function loadDashboardWithToken(webContents, url, token) {
  if (token) {
    const exchange = new URL(url);
    exchange.searchParams.set("token", token);
    try {
      const signal = AbortSignal.timeout(EXCHANGE_TIMEOUT_MS);
      const response = await webContents.session.fetch(exchange.toString(), { signal });
      await response.arrayBuffer();
    } catch {
      // The bare load below then shows the signed-out page.
    }
  }
  if (!webContents.isDestroyed()) webContents.loadURL(url);
}

module.exports = { createTokenRetryHandler, dashboardRetryPath, loadDashboardWithToken };
