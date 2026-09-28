/**
 * Builtin App Component Registry
 *
 * Maps builtin app route paths to their lazy-loaded React components.
 * This enables auto-discovery: App.tsx doesn't need to hardcode routes
 * for each builtin app. When a new builtin app is added, just add an
 * entry here — no changes to App.tsx needed.
 *
 * Components are lazy-loaded so they don't bloat the initial bundle.
 */
import { lazy, type ComponentType } from 'react'
import { reportSeamCollision } from './seamCollision'
import { isValidAppId } from './appIdentity'

export type LazyComponent = React.LazyExoticComponent<ComponentType<Record<string, never>>>

/** One registered builtin page: the component to render, and the app it belongs to. */
export interface BuiltinAppEntry {
  readonly component: LazyComponent
  /**
   * The owning app's `/api/apps` name. `BuiltinAppRoute` publishes it as the
   * page's app identity, and the platform namespaces persisted view state and
   * cached data to it.
   *
   * Explicit data rather than derived from the route, because the two are not
   * the same thing: `/worlds` belongs to the app `agent-worlds`, so
   * `route.slice(1)` would mint `worlds` — not an app, agreed with by nothing
   * else on the platform. Since the appId becomes a storage-key and query-key
   * prefix, a wrong one is not a cosmetic slip: it is a permanent namespace
   * holding state no other reader can find.
   */
  readonly appId: string
}

/**
 * Registry mapping route paths (from app manifest ui.pages[].route)
 * to their lazy-loaded page components and owning app.
 *
 * To add a new builtin app:
 * 1. Create your page component in src/apps/{name}/ or src/pages/
 * 2. Add an entry here: '/route-path': { component: lazy(() => import(…)), appId: 'your-app' }
 * 3. Declare ui.pages in your app.json manifest
 * That's it — no App.tsx changes needed.
 *
 * `appId` must be the `name` from that app.json, and
 * `builtinRegistry.identity.test.ts` fails if it is not — including the
 * `/worlds` → `agent-worlds` case, where the route and the app name differ.
 */
export const BUILTIN_COMPONENT_REGISTRY: Record<string, BuiltinAppEntry> = {
  '/worlds': { component: lazy(() => import('../pages/WorldsPage')), appId: 'agent-worlds' },
  '/channels': { component: lazy(() => import('../pages/ChannelPage')), appId: 'channels' },
  '/auto-improvement': { component: lazy(() => import('./auto-improvement/AutoImprovementPage')), appId: 'auto-improvement' },
  '/auto-research': { component: lazy(() => import('./auto-research/ResearchLabPage')), appId: 'auto-research' },
  '/aws-control': { component: lazy(() => import('./aws-control/AwsControlPage')), appId: 'aws-control' },
  '/file-explorer': { component: lazy(() => import('./file-explorer/FileExplorerPage')), appId: 'file-explorer' },
  '/code-review-sage': { component: lazy(() => import('./code-review-sage/CodeReviewSagePage')), appId: 'code-review-sage' },
  '/workflows': { component: lazy(() => import('./workflows/WorkflowsPage')), appId: 'workflows' },
  '/dev-fleet': { component: lazy(() => import('../pages/DevFleetPage')), appId: 'dev-fleet' },
  '/issue-radar': { component: lazy(() => import('./issue-radar/IssueRadarPage')), appId: 'issue-radar' },
  '/meetings': { component: lazy(() => import('./meetings/MeetingsPage')), appId: 'meetings' },
  '/papyrus': { component: lazy(() => import('./papyrus/PapyrusPage')), appId: 'papyrus' },
  '/pptx-maker': { component: lazy(() => import('./pptx-maker/PptxMakerPage')), appId: 'pptx-maker' },
  '/ops-mission-control': { component: lazy(() => import('./ops-mission-control/OpsMissionControlPage')), appId: 'ops-mission-control' },
  '/design-critique': { component: lazy(() => import('./design-critique/DesignCritiquePage')), appId: 'design-critique' },
  '/crew-companion': { component: lazy(() => import('./crew-companion/CrewCompanionPage')), appId: 'crew-companion' },
  '/projects': { component: lazy(() => import('../pages/ProjectsPage')), appId: 'projects' },
  '/md-notebook': { component: lazy(() => import('./md-notebook/MdNotebookPage')), appId: 'md-notebook' },
  '/mochi': { component: lazy(() => import('./mochi/MochiPage')), appId: 'mochi' },
  '/spec-builder': { component: lazy(() => import('./spec-builder/SpecBuilderPage')), appId: 'spec-builder' },
  '/personal-shopper': { component: lazy(() => import('./personal-shopper/PersonalShopperPage')), appId: 'personal-shopper' },
  '/design-tweak': { component: lazy(() => import('./design-tweak/DesignTweakPage')), appId: 'design-tweak' },
  '/project-scaffolder': { component: lazy(() => import('./project-scaffolder/ProjectScaffolderPage')), appId: 'project-scaffolder' },
}

/**
 * Register additional builtin route → component mappings at runtime.
 *
 * This is the extension seam for a downstream edition that bundles its own
 * builtin pages: instead of editing (and re-diffing) this file on every upstream
 * sync, the edition calls this once from the extensions.ts composition root
 * (loaded before App mounts; routes resolve lazily on navigation, so this
 * registry does not need to be reactive). Existing entries are never
 * overwritten silently — a duplicate route is a no-op and logs a warning, so
 * the core's own registrations always win.
 *
 * A route must be a single, plain top-level path segment: `BuiltinAppRoute`
 * resolves the catch-all `/:builtinApp` from ONE path parameter, and only the
 * `location.pathname` — never the query or hash — is matched against the
 * registry. So anything that isn't a bare segment could never resolve as
 * written: `/reports/daily` (extra segment → asked for as `/reports`),
 * `/reports?daily` or `/reports#x` (the `?daily`/`#x` isn't in the pathname →
 * asked for as `/reports`), `/../reports` (the browser removes the dot segment →
 * asked for as `/reports`), or whitespace/`.`/`..`. Each is refused, and the
 * refusal is recorded under the segment it WOULD have been asked for, so visiting
 * that segment states the reason rather than silently vanishing into chat. The
 * pattern therefore requires a leading alphanumeric then only URL-path-safe
 * chars (`A-Za-z0-9._~-`) and NO second `/`, `?`, `#`, or whitespace; `.`/`..`
 * are excluded by the mandatory alphanumeric first char. A non-conforming route
 * routes through `reportSeamCollision` (fail-loud dev/test, warn-and-ignore
 * prod), same as a duplicate.
 *
 * An entry must also carry an `appId` — the owning app's `/api/apps` name — and
 * it is refused on the same terms. The appId is published as the page's app
 * identity and is used as a storage key and query-key prefix, so an entry with
 * no id (or an id outside `[a-z0-9-]`) is rejected rather than registered with
 * a namespace nothing can address.
 *
 * A refusal is recorded against the LEADING SEGMENT of its route, normalised the
 * way a browser normalises a path and then decoded the way react-router decodes
 * it, which is the only form `BuiltinAppRoute` can look up, so it can say WHY the
 * page is empty instead of redirecting to chat. A duplicate is not recorded: the
 * route still resolves to the winning entry, so there is no empty space to
 * explain.
 */
const _BUILTIN_ROUTE_RE = /^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/

/**
 * Two bases that differ only in host, for the origin test in `routePathname`.
 *
 * `.invalid` is reserved by RFC 2606, so neither can collide with a real
 * deployment's own origin.
 */
const _KEY_BASE = 'http://one.invalid/'
const _KEY_BASE_ALT = 'http://two.invalid/'

/**
 * The pathname a browser would send for this route, or undefined if the route
 * addresses no page on this origin.
 *
 * Derived through `URL` rather than by cutting the string at `?`/`#` by hand.
 * The hand-rolled cut was correct about the query and hash and wrong about
 * everything else a browser normalises first, one case at a time: `/../reports`
 * is fetched as `/reports`, and so are `/./reports` and `/%2e%2e/reports`. `URL`
 * is the normaliser the browser itself runs, so the whole family lands in one
 * step — the query/hash cut on a LITERAL `?`/`#`, dot-segment removal in all its
 * spellings, and percent-encoding of what a browser would encode — while leaving
 * `%2F`, `%3F`, `%23` and a malformed `%ZZ` in the pathname untouched for the
 * two-pass decode below, which is what the router does next.
 *
 * Two spellings must NOT resolve to a pathname, and both are checked rather than
 * assumed:
 *
 * 1. **No leading `/`.** `URL` would synthesise one, and letting `reports`
 *    become `/reports` would file one registration key's refusal where it
 *    answers for a different key.
 * 2. **An authority-relative route.** `//host/path` opens with a `/` but leaves
 *    this origin, so no page here is empty and there is nothing to explain. So
 *    do the spellings the URL parser treats identically — `/\host/path`, and a
 *    tab or newline smuggled between the two separators — which is why this is
 *    not a `startsWith('//')` test. Parsing against two bases decides it exactly:
 *    an authority taken from the ROUTE yields the same origin both times, while a
 *    path-relative route inherits each base and yields two different ones.
 */
function routePathname(route: string): string | undefined {
  if (!route.startsWith('/')) return undefined
  try {
    const parsed = new URL(route, _KEY_BASE)
    if (parsed.origin !== new URL(route, _KEY_BASE_ALT).origin) return parsed.pathname
  } catch {
    // `new URL` throws on an unparseable authority (`//[bad`) — another route
    // that addresses nothing here. The throw must not escape: this runs inside
    // `registerBuiltinComponents`, where it would abandon every later entry.
  }
  return undefined
}

/**
 * react-router's `decodePath`, in the one shape this caller needs: the pathname's
 * segments, decoded.
 *
 * Kept as an ARRAY rather than a rejoined string for the reason the router itself
 * re-encodes a decoded `/` back to `%2F` — a `/` that came out of an escape must
 * not invent a segment boundary the URL did not have. An array already says where
 * the boundaries are, so nothing has to be re-encoded and undone.
 *
 * The single try/catch is behaviour, not an implementation detail. A malformed
 * escape anywhere in the pathname leaves the ENTIRE pathname undecoded, so
 * `/x%2Dy/z%ZZ` is asked for with its `%2D` intact. Decoding one segment in
 * isolation would normalise a key the router never asks for. The throw must not
 * propagate either: this runs inside `registerBuiltinComponents`, where it would
 * abandon every entry after the offending one.
 */
function decodeSegmentsLikeRouter(pathname: string): string[] {
  const segments = pathname.split('/')
  try {
    return segments.map((segment) => decodeURIComponent(segment))
  } catch {
    return segments
  }
}

/**
 * The key `BuiltinAppRoute` will ask for, given the string an entry registered.
 *
 * A refusal is recorded so the route can explain an empty page, and that lookup
 * runs on `` `/${builtinApp}` `` — ONE resolved path parameter. So the key has to
 * be what the ROUTER produces, not what was registered, or the reason is filed
 * where nothing can read it and the route vanishes into chat exactly as it did
 * before the record existed. That is the whole invariant, and it has three parts,
 * each mirroring a step react-router actually performs:
 *
 * 1. normalise the route to the pathname a browser would send: the query and hash
 *    never reach the matcher, and neither does a dot segment;
 * 2. decode the pathname's segments the way `decodePath` does, and keep the FIRST,
 *    which is the only one `:builtinApp` captures;
 * 3. apply `matchPath`'s own second pass, `(value || '').replace(/%2F/g, '/')` on
 *    that captured param — which is why a double-encoded `%252F` resolves all the
 *    way to `/`, and why the match is uppercase-only, as the router's is.
 *
 * So `/reports/daily`, `/reports?daily` and `/reports#x` are all asked for as
 * `/reports`, and so is `/../reports`; `/reports%2Ddaily` as `/reports-daily`;
 * `/reports%2Fdaily` as `/reports/daily`, a `/` that stays INSIDE the one param. A
 * decoded `?` or `#` (`%3F`, `%23`) stays inside it too, which is why step 1 cuts
 * on a LITERAL one.
 *
 * A route with no leading `/` does NOT get one synthesised, and neither does an
 * authority-relative one keep its pathname: no URL on this origin produces
 * either spelling, so there is no empty page asking to be explained, and
 * inventing `/reports` for a registration of `reports` would let one key's
 * refusal answer for a different key. Both fall back to the raw string, a key
 * nothing can request.
 */
function builtinRouteKey(route: string): string {
  const pathname = routePathname(route)
  if (pathname === undefined) return route
  const [, leading = ''] = decodeSegmentsLikeRouter(pathname)
  // A parsed URL's pathname always opens with `/`, so the key's leading separator
  // is carried from it rather than re-spelled.
  return pathname.slice(0, 1) + leading.replace(/%2F/g, '/')
}

/**
 * Why a route holds nothing, when its registration was REFUSED.
 *
 * Keyed by the route the ROUTER will ask for, which `builtinRouteKey` derives —
 * not the string the entry registered with. Local to this module on purpose: a
 * refusal is only worth remembering where there is a surface to show it on, and
 * this is the one seam whose refused keys stay navigable. `seamCollision.ts`
 * keeps the shared fail-loud/degrade-safe policy and no store, so the other
 * ~20 callers pay nothing for a record they have nowhere to render.
 */
const _refusals = new Map<string, string>()

/**
 * The reason a builtin route was refused, or undefined if none was.
 *
 * `BuiltinAppRoute` calls this on its miss path, to tell "nobody ever registered
 * this" apart from "someone did, and it was rejected". The two want opposite
 * handling: the first is a typo or a stale link and belongs on the chat
 * redirect, the second is a defect in a shipped bundle and has to be readable by
 * whoever is looking at the empty page.
 */
export function builtinRefusalReason(route: string): string | undefined {
  return _refusals.get(route)
}

/**
 * Record why a route is empty, then report the refusal.
 *
 * The ORDER is the contract: the record is written BEFORE
 * `reportSeamCollision`, whose dev/test branch throws, so a dev/test run that
 * aborts registration leaves the same record a production run leaves. First
 * refusal for a key wins — a second report describes a retry, not the reason the
 * key is empty.
 *
 * It exists as one function rather than two calls per arm so the message cannot
 * be recorded and reported as two different strings, and so each message stays
 * INSIDE the reporting call it belongs to: the i18n lint exempts a
 * `report…`-prefixed callee's argument as a developer diagnostic, which is the
 * same exemption `reportSeamCollision` itself relies on and the same class as
 * the config's `warnContributionSkipped` entry — a refused contribution naming
 * the field that failed, addressed to whoever authored it. Assigning the message
 * to a local first would take it out of that exemption and the gate would read a
 * registration diagnostic as user copy.
 */
function reportRefusal(route: string, message: string): void {
  const key = builtinRouteKey(route)
  if (!_refusals.has(key)) _refusals.set(key, message)
  reportSeamCollision('builtinRegistry', message)
}

/**
 * Report and refuse a registration that could never work, or return false.
 *
 * Applied at the runtime seam only. The core table above is developer-authored
 * code whose appIds `builtinRegistry.identity.test.tsx` already holds to
 * `isValidAppId`, so checking it again at import time would be a second
 * enforcement point over compile-time constants; this guards the one caller that
 * takes input the compiler never saw.
 *
 * It REPORTS rather than returning a reason, which keeps the refusal and its
 * diagnostic together — a caller cannot refuse an entry and forget to say why —
 * and keeps each message inside the `reportSeamCollision(` call it belongs to,
 * where the i18n gate already recognises it as a developer diagnostic rather
 * than user copy.
 *
 * The route rule and the appId rule are deliberately different charsets: a route
 * is a URL path segment (`/Reports`, `/my_app` and `/a.b` all resolve), while an
 * appId is a storage key and is held to `[a-z0-9-]` — see `appIdentity.ts` for why.
 */
function refuseBadEntry(route: string, entry: BuiltinAppEntry | undefined): boolean {
  if (!_BUILTIN_ROUTE_RE.test(route)) {
    reportRefusal(
      route,
      `route ${route} is not a single plain path segment ` +
        `(/^\\/[A-Za-z0-9][A-Za-z0-9._~-]*$/); BuiltinAppRoute can never ` +
        `resolve it — ignoring`,
    )
    return true
  }
  if (!isValidAppId(entry?.appId)) {
    // `reportRefusal` normalises through `builtinRouteKey`, a no-op on this arm
    // today: it is only reached once the route matched `_BUILTIN_ROUTE_RE`, which
    // admits a single plain segment. Going through the same path anyway keeps the
    // "recorded under the key the router asks for" invariant true if the two arms
    // are ever reordered.
    reportRefusal(
      route,
      `route ${route} declares appId ${JSON.stringify(entry?.appId)}, which is not a ` +
        `valid app id (non-empty, /^[a-z0-9-]+$/). The appId becomes a storage key and a ` +
        `query-key prefix, so it cannot be taken on trust — ignoring`,
    )
    return true
  }
  return false
}

export function registerBuiltinComponents(entries: Record<string, BuiltinAppEntry>): void {
  for (const [route, entry] of Object.entries(entries)) {
    if (refuseBadEntry(route, entry)) continue
    if (route in BUILTIN_COMPONENT_REGISTRY) {
      reportSeamCollision('builtinRegistry', `route ${route} already registered; ignoring duplicate`)
      continue
    }
    BUILTIN_COMPONENT_REGISTRY[route] = entry
  }
}

/**
 * Check if a route path has a registered builtin component.
 */
export function hasBuiltinComponent(route: string): boolean {
  return route in BUILTIN_COMPONENT_REGISTRY
}

/**
 * Get the component + owning app for a builtin route, or undefined.
 *
 * Replaces the former `getBuiltinComponent`, which returned the component
 * alone. The rename is deliberate rather than a shim: a caller left on the old
 * name would receive `{ component, appId }` where it expected a lazy component
 * and render nothing at all, so a compile error is strictly better than an
 * invisible blank page.
 */
export function getBuiltinApp(route: string): BuiltinAppEntry | undefined {
  return BUILTIN_COMPONENT_REGISTRY[route]
}
