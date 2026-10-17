/**
 * Token retry logic for 403 auto-recovery after gateway restart.
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
 * The stale `token` is dropped: the caller appends a freshly fetched one.
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

module.exports = { createTokenRetryHandler, dashboardRetryPath };
