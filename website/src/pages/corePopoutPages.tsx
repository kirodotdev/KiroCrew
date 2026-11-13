/**
 * Core pages that open in an app pop-out window, as a built-in app's page does.
 *
 * The app pop-out (`AppPopoutFrame`, the rail row's pop-out action) serves app
 * routes, which `BUILTIN_COMPONENT_REGISTRY` lists, and the core pages listed
 * here, whose Apps-group rows offer the same pop-out action.
 */
import { lazy, Suspense, type ComponentType, type LazyExoticComponent } from 'react'
import { hasBuiltinComponent } from '../apps/builtinRegistry'
import { ContentSkeleton } from '../components/ui'
import ErrorBoundary from '../components/ErrorBoundary'

/** A core page a pop-out window can render, and the catalog key of its window title. */
export interface CorePopoutPageEntry {
  readonly page: LazyExoticComponent<ComponentType>
  readonly labelKey: string
}

/** Route -> the page a pop-out window renders for it. */
export const CORE_POPOUT_PAGES: Readonly<Record<string, CorePopoutPageEntry>> = {
  '/projects': { page: lazy(() => import('./ProjectsPage')), labelKey: 'nav.task_runner' },
}

/** The entry for *route*, or `undefined` when it names no core pop-out page. */
export function corePopoutPage(route: string): CorePopoutPageEntry | undefined {
  return Object.hasOwn(CORE_POPOUT_PAGES, route) ? CORE_POPOUT_PAGES[route] : undefined
}

/** Whether *route* opens in an app pop-out window: a built-in app's page or a core page above. */
export function hasPopoutPage(route: string): boolean {
  return hasBuiltinComponent(route) || corePopoutPage(route) !== undefined
}

/** Renders *entry*'s page in a pop-out window, behind the boundary `BuiltinAppRoute` gives an app page. */
export function CorePopoutPage({ entry }: { entry: CorePopoutPageEntry }) {
  const Page = entry.page
  return (
    <ErrorBoundary>
      <Suspense fallback={<ContentSkeleton />}>
        <Page />
      </Suspense>
    </ErrorBoundary>
  )
}
