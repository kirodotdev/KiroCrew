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
 * registry. So anything that isn't a bare segment would register but never
 * resolve: `/reports/daily` (extra segment → matches `/reports`),
 * `/reports?daily` or `/reports#x` (the `?daily`/`#x` isn't in the pathname →
 * matches `/reports`), or whitespace/`.`/`..`. All redirect to chat — the
 * silent-vanish failure the seams guard against. The pattern therefore requires
 * a leading alphanumeric then only URL-path-safe chars (`A-Za-z0-9._~-`) and NO
 * second `/`, `?`, `#`, or whitespace; `.`/`..` are excluded by the mandatory
 * alphanumeric first char. A non-conforming route routes through
 * `reportSeamCollision` (fail-loud dev/test, warn-and-ignore prod), same as a
 * duplicate.
 *
 * An entry must also carry an `appId` — the owning app's `/api/apps` name — and
 * it is refused on the same terms. The appId is published as the page's app
 * identity and is used as a storage key and query-key prefix, so an entry with
 * no id (or an id outside `[a-z0-9-]`) is rejected rather than registered with
 * a namespace nothing can address.
 */
const _BUILTIN_ROUTE_RE = /^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/

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
    reportSeamCollision(
      'builtinRegistry',
      `route ${route} is not a single plain path segment ` +
        `(/^\\/[A-Za-z0-9][A-Za-z0-9._~-]*$/); BuiltinAppRoute can never ` +
        `resolve it — ignoring`,
    )
    return true
  }
  if (!isValidAppId(entry?.appId)) {
    reportSeamCollision(
      'builtinRegistry',
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
