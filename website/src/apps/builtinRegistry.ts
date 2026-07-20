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

type LazyComponent = React.LazyExoticComponent<ComponentType<Record<string, never>>>

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
  '/auto-research': lazy(() => import('./auto-research/ResearchLabPage')),
  '/file-explorer': lazy(() => import('./file-explorer/FileExplorerPage')),
  '/code-review-sage': lazy(() => import('./code-review-sage/CodeReviewSagePage')),
  '/workflows': lazy(() => import('./workflows/WorkflowsPage')),
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
