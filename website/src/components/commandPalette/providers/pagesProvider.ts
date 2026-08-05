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

import { getBuiltinSurfaces, surfaceLabel } from '../../../surfaces/registry'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { i18nT } from '../../../i18n/t'
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

/**
 * Catalog KEY for the tab label. The `id` above is the tab's identity (and what
 * `providerById` / the registry look up); this is only display copy, so the two
 * are deliberately separate. Resolved in {@link createPagesProvider}, never here
 * — a module-scope `i18nT()` would freeze the boot language.
 */
const PROVIDER_LABEL_KEY = 'components.commandPalette.providers.pagesProvider.pages'

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
 *
 * Titles live in {@link EXTRA_PAGE_TITLE_KEY}, not here: the entry's `title` is
 * what the palette both DISPLAYS and fuzzy-matches against, so it has to be
 * resolved per search (see {@link collectPages}) rather than frozen at import.
 */
const EXTRA_PAGES: readonly Omit<PageEntry, 'title'>[] = [
  // The App Store surface is `hiddenFromNav` (it renders as the Apps-header
  // "Explore" accent link, not a rail row), so it must be listed here to
  // stay reachable from the palette.
  { key: 'apps', route: '/apps', icon: inlineIcon(LayoutGrid) },
  { key: 'logs', route: '/logs', icon: inlineIcon(ScrollText) },
  { key: 'developer', route: '/developer', icon: inlineIcon(Code2) },
  { key: 'hooks', route: '/hooks', icon: inlineIcon(Webhook) },
  { key: 'tasks', route: '/tasks', icon: inlineIcon(ListChecks) },
  { key: 'mc-agents', route: '/mc-agents', icon: inlineIcon(Bot) },
  { key: 'instances', route: '/instances', icon: inlineIcon(Server) },
]

/**
 * Catalog KEY for each {@link EXTRA_PAGES} title, by entry key.
 *
 * Flat `Record` of full literal keys, indexed inline at the `i18nT()` call, so
 * `scripts/check-i18n-keys.mjs` can resolve every member statically. Deliberately
 * NOT a `titleKey` field on the entries themselves: `i18nT(p.titleKey)` is a
 * member access the gate cannot resolve, and would add a second entry to
 * `dynamic-keys-baseline.json` — a ratchet that only goes down.
 */
const EXTRA_PAGE_TITLE_KEY: Record<string, string> = {
  apps: 'components.commandPalette.providers.pagesProvider.explore',
  logs: 'components.commandPalette.providers.pagesProvider.logs',
  developer: 'components.commandPalette.providers.pagesProvider.developer',
  hooks: 'components.commandPalette.providers.pagesProvider.hooks',
  tasks: 'components.commandPalette.providers.pagesProvider.tasks',
  'mc-agents': 'components.commandPalette.providers.pagesProvider.kirocrew_agents',
  instances: 'components.commandPalette.providers.pagesProvider.remote_crew',
}

/**
 * Build the full candidate list: every rail surface from the registry plus the
 * extra routed pages. Deduped by route so a surface and an extra never collide
 * (registry wins). Computed fresh per search so newly registered surfaces are
 * always reflected — which is also what makes it the right place to resolve
 * titles for the current language.
 */
function collectPages(): PageEntry[] {
  const byRoute = new Map<string, PageEntry>()
  for (const s of getBuiltinSurfaces()) {
    byRoute.set(s.route, {
      key: s.navId,
      // `surfaceLabel(s)`, not `s.label`: the registry's `label` is a frozen
      // English fallback and `labelKey` is the translated one. Reading `.label`
      // directly left every rail destination English in the palette while the
      // nav rail beside it was translated.
      title: surfaceLabel(s),
      subtitle: s.route,
      route: s.route,
      icon: s.icon,
    })
  }
  for (const p of EXTRA_PAGES) {
    if (!byRoute.has(p.route)) {
      byRoute.set(p.route, {
        ...p,
        title: i18nT(EXTRA_PAGE_TITLE_KEY[p.key]),
        subtitle: p.subtitle ?? p.route,
      })
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
    // A GETTER, not a plain call: the provider object is built inside a `useMemo`
    // whose deps do not include the language, so `label: i18nT(...)` would resolve
    // once and keep the pre-switch wording forever. `LanguageProvider` forces a
    // re-RENDER via `cloneElement` (it deliberately does NOT remount — see its own
    // comment rejecting `key={active}`), and a re-render does not recompute a memo.
    // An accessor moves the lookup to the consumer's render, where the tab strip
    // reads it. Satisfies `ResourceProvider.label: string`.
    get label() { return i18nT(PROVIDER_LABEL_KEY) },
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
          // Declarative §2 Enter action: Pages are pure
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
