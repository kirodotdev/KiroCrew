/**
 * Process-wide single-flight POST /api/auth/refresh.
 *
 * Both recovery paths call this:
 *  - the proactive scheduler + on-boot /api/auth/me recovery (useRefreshScheduler)
 *  - the reactive warm-path background-poll recovery (api/client checkSessionExpired)
 *
 * Funnelling through ONE single-flight means at most one refresh is ever in
 * flight regardless of which path detects the access-cookie lapse. Without it,
 * a cold reopen could fire the hook's /api/auth/me-403 refresh and the first
 * background-poll-403 refresh near-simultaneously — two POSTs presenting the
 * same pre-rotation refresh cookie. (The server tolerates that via its
 * multi-tab grace window, but de-duping removes the redundant POST entirely.)
 *
 * The outcome is logged here once per actual refresh (status only, never the
 * token/cookie) so BOTH paths emit a console breadcrumb — a recurrence is
 * visible in the browser console, not only from server-side rotation counts.
 */

export interface RefreshResult {
  ok: boolean
  status: number
  body: unknown
}

let _inFlight: Promise<RefreshResult> | null = null

export function refreshOnce(): Promise<RefreshResult> {
  if (_inFlight) return _inFlight
  _inFlight = (async (): Promise<RefreshResult> => {
    let status = 0
    let ok = false
    let body: unknown = null
    try {
      const resp = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
      status = resp.status
      ok = resp.ok
      try { body = await resp.json() } catch { /* no / invalid JSON body */ }
      if (ok) {
        // eslint-disable-next-line no-console -- intentional refresh breadcrumb (status only)
        console.info('[refresh] /api/auth/refresh -> 200 (session rotated)')
      } else if (status === 401) {
        // eslint-disable-next-line no-console -- intentional refresh breadcrumb (status only)
        console.warn('[refresh] /api/auth/refresh -> 401 (terminal: chain revoked / no refresh cookie)')
      } else {
        // eslint-disable-next-line no-console -- intentional refresh breadcrumb (status only)
        console.warn(`[refresh] /api/auth/refresh -> ${status} (transient; will retry on next trigger)`)
      }
    } catch {
      // eslint-disable-next-line no-console -- intentional refresh breadcrumb (status only)
      console.warn('[refresh] /api/auth/refresh network error (transient; will retry)')
    }
    return { ok, status, body }
  })()
  // Clear the single-flight slot once settled so the next lapse starts fresh.
  void _inFlight.finally(() => { _inFlight = null })
  return _inFlight
}

/** Test-only: clear the single-flight slot between cases. */
export function __resetRefreshOnceForTests(): void {
  _inFlight = null
}
