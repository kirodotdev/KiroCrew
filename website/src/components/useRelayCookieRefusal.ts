import { useEffect, useState } from 'react'

/** Deadline for the pair of probe requests. A probe that runs out says nothing. */
const RELAY_PROBE_TIMEOUT_MS = 5000

/**
 * Whether something in front of the dashboard refuses the relayed browser view's
 * files because they come without a cookie (#23180).
 *
 * The view is framed in a sandbox without `allow-same-origin`, so its document has
 * an opaque origin. Its `crossorigin` script and stylesheet fetches then send no
 * cookies. A front door that checks a cookie on every request (an SSO or OAuth
 * proxy, a corporate tunnel) lets the frame's navigation through, because the
 * dashboard starts it and it carries the cookie, but refuses every asset. The view
 * never boots and the frame stays blank. The opaque frame sends the panel no
 * signal it could wait for, and the panel cannot read it.
 *
 * So the panel asks the question directly: it GETs the relay path once with
 * cookies and once without. The relay's own auth is the capability token in the
 * path, never a cookie, so with nothing in front both answers are the relay's.
 * Refused ONLY when the request with cookies reached the relay and the one
 * without was turned away the way a sign-in front door turns it away: a 401 or
 * 403, a network failure (a cross-origin sign-in redirect fails CORS), or a 2xx
 * redirected out of the relay path (a same-origin sign-in page). The relay
 * itself never answers 401/403 and never redirects outside its prefix. Any
 * other cookieless answer (a 404 from token rotation, a 429, a 5xx) is `false`, as
 * is a timeout or a failed credentialed request, so a working frame is never
 * unloaded for a fault a proxy did not cause.
 *
 * Runs once per `path` / `resetKey` while `enabled`, not on a timer: the relay
 * pays a token check and an ownership proof per request.
 */
export function useRelayCookieRefusal(path: string, enabled: boolean, resetKey = 0): boolean {
  const [refused, setRefused] = useState(false)
  useEffect(() => {
    setRefused(false)
    if (!path || !enabled) return
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), RELAY_PROBE_TIMEOUT_MS)
    // `relay`: a 2xx that stayed under the relay path (the relay's own
    // `/` -> `index.html` redirect does). `elsewhere`: a 2xx redirected out of
    // the relay path; the relay keeps every redirect under its prefix, so only a
    // front door (its sign-in page) sends a request there. `auth`: a 401/403.
    // `network`: the request threw. `other`: any other non-2xx.
    const probe = async (credentials: RequestCredentials): Promise<'relay' | 'elsewhere' | 'auth' | 'network' | 'other'> => {
      try {
        const res = await fetch(path, { credentials, cache: 'no-store', signal: ctrl.signal })
        void res.body?.cancel().catch(() => {})
        if (res.status === 401 || res.status === 403) return 'auth'
        if (!res.ok) return 'other'
        if (!res.redirected) return 'relay'
        const landed = new URL(res.url, window.location.href)
        return landed.origin === window.location.origin && landed.pathname.startsWith(path) ? 'relay' : 'elsewhere'
      } catch {
        return 'network'
      }
    }
    void Promise.all([probe('same-origin'), probe('omit')]).then(([withCookie, withoutCookie]) => {
      if (ctrl.signal.aborted) return
      setRefused(withCookie === 'relay' && (withoutCookie === 'auth' || withoutCookie === 'network' || withoutCookie === 'elsewhere'))
    }).finally(() => clearTimeout(timer))
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [path, enabled, resetKey])
  return refused
}
