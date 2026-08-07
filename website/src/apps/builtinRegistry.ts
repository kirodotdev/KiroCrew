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

export type LazyComponent = React.LazyExoticComponent<ComponentType<Record<string, never>>>

/**
 * Registry mapping route paths (from app manifest ui.pages[].route)
 * to their lazy-loaded page components.
 *
 * To add a new builtin app:
 * 1. Create your page component in src/apps/{name}/ or src/pages/
 * 2. Add an entry here: '/route-path': lazy(() => import('./path/to/Page'))
 * 3. Declare ui.pages in your app.json manifest
 * That's it — no App.tsx changes needed.
 */
export const BUILTIN_COMPONENT_REGISTRY: Record<string, LazyComponent> = {
  '/worlds': lazy(() => import('../pages/WorldsPage')),
  '/channels': lazy(() => import('../pages/ChannelPage')),
  '/auto-improvement': lazy(() => import('./auto-improvement/AutoImprovementPage')),
  '/auto-research': lazy(() => import('./auto-research/ResearchLabPage')),
  '/file-explorer': lazy(() => import('./file-explorer/FileExplorerPage')),
  '/code-review-sage': lazy(() => import('./code-review-sage/CodeReviewSagePage')),
  '/workflows': lazy(() => import('./workflows/WorkflowsPage')),
  '/dev-fleet': lazy(() => import('../pages/DevFleetPage')),
  '/issue-radar': lazy(() => import('./issue-radar/IssueRadarPage')),
  '/meetings': lazy(() => import('./meetings/MeetingsPage')),
  '/papyrus': lazy(() => import('./papyrus/PapyrusPage')),
  '/pptx-maker': lazy(() => import('./pptx-maker/PptxMakerPage')),
  '/ops-mission-control': lazy(() => import('./ops-mission-control/OpsMissionControlPage')),
  '/design-critique': lazy(() => import('./design-critique/DesignCritiquePage')),
  '/crew-companion': lazy(() => import('./crew-companion/CrewCompanionPage')),
  '/projects': lazy(() => import('../pages/ProjectsPage')),
  '/md-notebook': lazy(() => import('./md-notebook/MdNotebookPage')),
  '/mochi': lazy(() => import('./mochi/MochiPage')),
  '/spec-builder': lazy(() => import('./spec-builder/SpecBuilderPage')),
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
 */
const _BUILTIN_ROUTE_RE = /^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/

export function registerBuiltinComponents(entries: Record<string, LazyComponent>): void {
  for (const [route, component] of Object.entries(entries)) {
    if (!_BUILTIN_ROUTE_RE.test(route)) {
      reportSeamCollision(
        'builtinRegistry',
        `route ${route} is not a single plain path segment ` +
          `(/^\\/[A-Za-z0-9][A-Za-z0-9._~-]*$/); BuiltinAppRoute can never ` +
          `resolve it — ignoring`,
      )
      continue
    }
    if (route in BUILTIN_COMPONENT_REGISTRY) {
      reportSeamCollision('builtinRegistry', `route ${route} already registered; ignoring duplicate`)
      continue
    }
    BUILTIN_COMPONENT_REGISTRY[route] = component
  }
}

/**
 * Check if a route path has a registered builtin component.
 */
export function hasBuiltinComponent(route: string): boolean {
  return route in BUILTIN_COMPONENT_REGISTRY
}

/**
 * Get the lazy component for a builtin route, or undefined.
 */
export function getBuiltinComponent(route: string): LazyComponent | undefined {
  return BUILTIN_COMPONENT_REGISTRY[route]
}
