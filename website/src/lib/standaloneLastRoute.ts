/**
 * Reopen the last page in the installed (Home Screen / standalone) web app.
 *
 * iOS and installed-PWA launchers open the manifest `start_url` (`/`), not the
 * page the person added to the Home Screen, so a teammate who only uses one
 * crewmate's chat lands on Sessions at every launch (#29497). While the
 * dashboard runs in `display-mode: standalone`, every route it shows is
 * recorded here; a standalone launch at a bare `/` replaces the URL with that
 * route before the router mounts.
 *
 * A normal browser tab neither records nor restores. The stored value is
 * re-validated on read: it must be a same-origin, root-relative dashboard
 * route (never `//host`, an absolute URL, a pop-out, an embed or an API path),
 * and credential-looking query parameters and the hash are never kept.
 */
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { appPathname, withBase } from './basePath'
import { isEmbeddedPane } from './embedded'

/** localStorage key holding the last route shown in the standalone app. */
export const STANDALONE_LAST_ROUTE_KEY = 'mc-standalone-last-route'

const MAX_ROUTE_LENGTH = 2048
/** Routes that belong to another window or are not pages to reopen. */
const EXCLUDED_PREFIXES = ['/popout', '/embed', '/worlds-popout', '/api']
/** Query parameters that may carry a credential; never stored or replayed. */
const SENSITIVE_PARAM = /token|auth|secret|password|key|code|ticket|session_id/i
const PROBE_ORIGIN = 'https://standalone.invalid'

/** True when the document runs as an installed web app, not in a browser tab. */
export function isStandaloneDisplay(): boolean {
  if (typeof window === 'undefined' || isEmbeddedPane()) return false
  try {
    if (window.matchMedia?.('(display-mode: standalone)').matches) return true
  } catch {
    /* matchMedia unavailable — fall through to the iOS flag */
  }
  return (navigator as unknown as { standalone?: boolean }).standalone === true
}

function isExcluded(pathname: string): boolean {
  return EXCLUDED_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`))
}

/**
 * The route to keep for `raw` (a router-relative `path?query`), or null when
 * it is not a reopenable same-origin dashboard page.
 */
export function sanitizeStandaloneRoute(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw.length === 0 || raw.length > MAX_ROUTE_LENGTH) return null
  // Root-relative only: `//host` and `/\host` are protocol-relative escapes.
  if (!raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\')) return null
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null
  let url: URL
  try {
    url = new URL(raw, PROBE_ORIGIN)
  } catch {
    return null
  }
  if (url.origin !== PROBE_ORIGIN) return null
  const pathname = url.pathname
  if (pathname === '/' || pathname.startsWith('//') || isExcluded(pathname)) return null
  for (const name of [...url.searchParams.keys()]) {
    if (SENSITIVE_PARAM.test(name)) url.searchParams.delete(name)
  }
  return `${pathname}${url.search}`
}

/** Record the route the standalone app is showing. A no-op in a browser tab. */
export function recordStandaloneRoute(route: string): void {
  if (!isStandaloneDisplay()) return
  if (isExcluded(appPathname())) return
  const clean = sanitizeStandaloneRoute(route)
  if (clean !== null) safeSetItem(STANDALONE_LAST_ROUTE_KEY, clean)
}

/**
 * On a standalone launch at a bare `/`, replace the URL with the last recorded
 * route. Must run before the router mounts. Returns the route it restored, or
 * null when it left the URL alone.
 */
export function restoreStandaloneLastRoute(): string | null {
  if (!isStandaloneDisplay()) return null
  const { search, hash } = window.location
  if (appPathname() !== '/' || search !== '' || hash !== '') return null
  const route = sanitizeStandaloneRoute(safeGetItem(STANDALONE_LAST_ROUTE_KEY))
  if (route === null) return null
  try {
    window.history.replaceState(window.history.state, '', withBase(route))
  } catch {
    return null
  }
  return route
}
