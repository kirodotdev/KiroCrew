import { appPathname, routerBasename } from '../lib/basePath'

/**
 * Where an app pop-out window lives in the URL, and how an app page is told
 * apart from every other dashboard route.
 *
 * Pure helpers only — no DOM, no channel — so the router bootstrap in
 * `main.tsx` can read them at document load, before any provider mounts, and
 * so the derivation is unit-testable. The BroadcastChannel coordination for
 * app pop-outs lives in `appPopout.ts`.
 *
 * ## Why the pop-out window carries a router basename
 *
 * The chat and artifact pop-outs render ONE component pinned to ONE URL
 * (`/popout/chat`, `/popout/artifact/<slug>`); nothing inside them navigates
 * by path. An app page is different: an app owns its own sub-path navigation
 * (`/aws-control/usage`), a "go to chat" exit (`navigate('/chat?sid=…')`), and
 * reads `useLocation()` to pick its view. Every one of those is spelled as an
 * absolute dashboard path inside the app, and none of it can be rewritten
 * without touching every app — including third-party ones the host never sees.
 *
 * So instead of rewriting the apps, the pop-out window mounts the router at
 * `<base>/popout/app`. The app then sees exactly the paths it sees in the main
 * dashboard (`/aws-control/usage`), while the document URL stays under
 * `/popout/app/…` for the whole life of the window — which is what keeps the
 * shell's load-time `isPopout` true across a reload, and what keeps a copied
 * URL re-opening as a pop-out rather than as a second full dashboard.
 */

/** Document-path prefix of an app pop-out window. */
export const APP_POPOUT_PREFIX = '/popout/app'

/**
 * Static two-segment App Store routes that `App.tsx` registers ahead of the
 * `/apps/:name` catch-all: `/apps/library` and the `/apps/-/...` prefix.
 * `detail` and `migrate` are not listed because their store routes need a
 * third segment, so bare `/apps/detail` names an installed app called `detail`,
 * as implied by the backend's `RESERVED_ROUTE_APP_NAMES` containing only
 * `library`.
 */
const APP_STORE_SEGMENTS = new Set(['library', '-'])

/**
 * The app an in-dashboard path is on, as the ROUTE the pop-out is keyed by, or
 * `null` when the path is not an app page.
 *
 *  - `/aws-control/usage` → `/aws-control` when `hasBuiltin('/aws-control')`.
 *    The first segment is the builtin's route (the `/:builtinApp/*` arm); the
 *    rest is the app's own sub-path and is not part of its identity.
 *  - `/apps/<name>` → `/apps/<name>`: an AppHost-routed installed app. Exactly
 *    two segments, because that is what the `/apps/:name` arm matches;
 *    `/apps/detail` therefore names the installed app `detail`.
 *  - `/apps`, `/apps/library`, `/apps/detail/x`, `/chat`, `/settings` → `null`.
 *
 * `hasBuiltin` is injected rather than imported so this module stays free of
 * the component registry (and its lazy page imports): the one caller that has
 * to decide on a live route passes `hasBuiltinComponent`.
 */
export function appRouteOf(pathname: string, hasBuiltin: (route: string) => boolean): string | null {
  const segs = pathname.split('/').filter(Boolean)
  if (segs.length === 0) return null
  if (segs[0] === 'apps') {
    return segs.length === 2 && !APP_STORE_SEGMENTS.has(segs[1]) ? `/apps/${segs[1]}` : null
  }
  const route = `/${segs[0]}`
  return hasBuiltin(route) ? route : null
}

/**
 * The app route an app pop-out DOCUMENT path names, or `null` when the path is
 * not under {@link APP_POPOUT_PREFIX} (or names no app at all).
 *
 *  - `/popout/app/aws-control/usage` → `/aws-control`
 *  - `/popout/app/apps/foo`          → `/apps/foo`
 *  - `/popout/chat/x`, `/popout/app` → `null`
 *
 * Unvalidated on purpose: this runs at document load, before the component
 * registry is consulted. Whether the route is a real app is the frame's call
 * (`appRouteOf` with the registry), which renders a not-found fallback with a
 * Return control for a bogus deep link rather than bouncing the window.
 */
export function appPopoutRouteOf(pathname: string): string | null {
  if (!pathname.startsWith(`${APP_POPOUT_PREFIX}/`)) return null
  const segs = pathname.slice(APP_POPOUT_PREFIX.length).split('/').filter(Boolean)
  if (segs.length === 0) return null
  return segs[0] === 'apps' && segs.length >= 2 ? `/apps/${segs[1]}` : `/${segs[0]}`
}

/**
 * `path` read the way React Router reads its params, so a route can be
 * compared with one rebuilt from `useParams()`.
 *
 * The router matches against the pathname decoded one segment at a time
 * (`decodeURIComponent`, with a decoded `/` put back as `%2F` so the segment
 * count holds), and leaves the whole path undecoded when any escape in it is
 * malformed. Each param it reads out of that then has every `%2F` turned into
 * `/`. So `/apps/a%20b` reads as `/apps/a b` and `/apps/foo%2Fbar` as
 * `/apps/foo/bar`. This mirrors `decodePath` and `matchPath` in react-router
 * 7; re-encoding the params instead would not, because `encodeURIComponent`
 * leaves `!~*'()` alone and writes its own hex case.
 *
 *  - `/apps/a%20b`    → `/apps/a b`
 *  - `/dev%2Dfleet`   → `/dev-fleet`
 *  - `/apps/%E0%A4%A` → `/apps/%E0%A4%A` (malformed: left as it is)
 */
export function routerParamForm(path: string): string {
  // `%2F`, spelled by the encoder so the escape and its case are the platform's.
  const encodedSlash = encodeURIComponent('/')
  let decoded: string
  try {
    decoded = path.split('/').map(seg => decodeURIComponent(seg).replace(/\//g, encodedSlash)).join('/')
  } catch {
    decoded = path
  }
  return decoded.replace(/%2F/g, '/')
}

/**
 * The router `basename` for the document loaded at `pathname`: the build's
 * base plus {@link APP_POPOUT_PREFIX} inside an app pop-out window, and the
 * ordinary build base everywhere else (`undefined` at the root, the router
 * default — see `routerBasename`).
 */
export function appPopoutRouterBasename(pathname: string, base: string): string | undefined {
  // `appPathname` hands back a path OUTSIDE the base unchanged; such a document
  // is not the dashboard's, so it is not a pop-out window of it either.
  const underBase = base === '' || pathname === base || pathname.startsWith(`${base}/`)
  if (underBase && appPopoutRouteOf(appPathname(pathname, base))) return `${base}${APP_POPOUT_PREFIX}`
  return routerBasename(base)
}

/**
 * The pop-out window URL path for an app route: `/popout/app/aws-control`,
 * `/popout/app/apps/foo`. Root-relative and base-less; the caller adds the
 * build base and origin.
 */
export function appPopoutPath(route: string): string {
  return `${APP_POPOUT_PREFIX}${route}`
}
