import { createElement } from 'react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'
import {
  ScrollText,
  Code2,
  Webhook,
  ListChecks,
  Bot,
  Server,
  LayoutGrid,
} from 'lucide-react'

import { getBuiltinSurfaces } from '../../../surfaces/registry'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import type { ResourceProvider, Result } from '../types'

/**
 * Pages provider (Search Everywhere).
 *
 * Source of truth is the surface registry (`src/surfaces/registry.ts`) — the
 * very same `getBuiltinSurfaces()` list `App.tsx` renders the left rail from —
 * so newly registered rail destinations show up here for free and we never
 * duplicate the rail by hand.
 *
 * The rail does not cover every routed destination, however. A handful of
 * pages have routes in `App.tsx` but no rail surface (some are redirects into
 * Settings). Those are enumerated in {@link EXTRA_PAGES} below so they remain
 * reachable from the palette. This is the only hardcoded data here, and it is
 * deliberately the *non-rail* routes — adding a new rail surface still requires
 * zero changes to this file.
 *
 * Per the §2 Enter matrix, Pages are pure navigation targets: Enter navigates
 * and there is no ⌘Enter (new session) or ⌥Enter (preview) variant, so
 * `onCmdActivate` / `onAltActivate` are intentionally left unset.
 */

const PROVIDER_ID = 'pages'

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function inlineIcon(Icon: typeof LayoutGrid): ReactNode {
  return createElement(Icon, { className: 'lucide-inline' })
}

/** A navigable page candidate before scoring. */
interface PageEntry {
  /** Stable key (surface navId or the route for extras). */
  key: string
  title: string
  /** Optional secondary line (the route path). */
  subtitle?: string
  route: string
  icon: ReactNode
}

/**
 * Routed-but-not-in-rail destinations (see `App.tsx` route table). Kept here —
 * never the rail — so the rail stays sourced exclusively from the registry.
 * Routes that redirect (e.g. /mc-agents, /instances) still navigate to
 * the right place via the router.
 */
const EXTRA_PAGES: readonly PageEntry[] = [
  // The App Store surface is `hiddenFromNav` (it renders as the Apps-header
  // "Explore" accent link, not a rail row), so it must be listed here to
  // stay reachable from the palette.
  { key: 'apps', title: 'Explore', route: '/apps', icon: inlineIcon(LayoutGrid) },
  { key: 'logs', title: 'Logs', route: '/logs', icon: inlineIcon(ScrollText) },
  { key: 'developer', title: 'Developer', route: '/developer', icon: inlineIcon(Code2) },
  { key: 'hooks', title: 'Hooks', route: '/hooks', icon: inlineIcon(Webhook) },
  { key: 'tasks', title: 'Tasks', route: '/tasks', icon: inlineIcon(ListChecks) },
  { key: 'mc-agents', title: 'KiroCrew Agents', route: '/mc-agents', icon: inlineIcon(Bot) },
  { key: 'instances', title: 'Remote Crew', route: '/instances', icon: inlineIcon(Server) },
]

/**
 * Build the full candidate list: every rail surface from the registry plus the
 * extra routed pages. Deduped by route so a surface and an extra never collide
 * (registry wins). Computed fresh per search so newly registered surfaces are
 * always reflected.
 */
function collectPages(): PageEntry[] {
  const byRoute = new Map<string, PageEntry>()
  for (const s of getBuiltinSurfaces()) {
    byRoute.set(s.route, {
      key: s.navId,
      title: s.label,
      subtitle: s.route,
      route: s.route,
      icon: s.icon,
    })
  }
  for (const p of EXTRA_PAGES) {
    if (!byRoute.has(p.route)) {
      byRoute.set(p.route, { ...p, subtitle: p.subtitle ?? p.route })
    }
  }
  return Array.from(byRoute.values())
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Create a Pages provider bound to a router `navigate` function. Pure (no React
 * hooks) so it can be unit-tested by mocking the surfaces registry and passing
 * a stub navigate.
 */
export function createPagesProvider(navigate: NavigateFunction): ResourceProvider {
  return {
    id: PROVIDER_ID,
    label: 'Pages',
    icon: inlineIcon(LayoutGrid),
    search(query: string): Result[] {
      const results: Result[] = []
      for (const page of collectPages()) {
        const match = fuzzyMatch(query, page.title)
        if (!match) continue
        const route = page.route
        results.push({
          id: `${PROVIDER_ID}:${page.key}`,
          providerId: PROVIDER_ID,
          title: page.title,
          subtitle: page.subtitle,
          icon: page.icon,
          score: match.score,
          indices: match.indices,
          // Declarative §2 Enter action (/ task 27): Pages are pure
          // navigation targets — Enter navigates to `route`, and ⌘Enter has no
          // distinct behavior (the dispatcher ignores the modifier for this
          // kind). `onActivate` stays bound to `navigate(route)` as the
          // execution path the dispatcher reuses.
          enter: { kind: 'navigate', route },
          onActivate: () => navigate(route),
        })
      }
      results.sort(compareResults)
      return results
    },
  }
}

/**
 * React hook: a Pages provider wired to the app router. Memoized on `navigate`
 * so the provider identity is stable across renders.
 */
export function usePagesProvider(): ResourceProvider {
  const navigate = useNavigate()
  return useMemo(() => createPagesProvider(navigate), [navigate])
}
