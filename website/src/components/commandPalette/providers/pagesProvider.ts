import { createElement } from 'react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'
import {
  ScrollText,
  Code2,
  ArrowDownToLine,
  ListChecks,
  Bot,
  Server,
  LayoutGrid,
  Compass,
} from 'lucide-react'

import { getAdvertisedSurfaces, surfaceLabel } from '../../../surfaces/registry'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { readPreviewFlag } from '../../../utils/previewFlags'
import { EXTRA_PAGES, EXTRA_PAGE_TITLE_KEY } from './pagesData'
import { i18nT } from '../../../i18n/t'
import type { ResourceProvider, Result } from '../types'

/**
 * Pages provider (Search Everywhere).
 *
 * Source of truth is the surface registry (`src/surfaces/registry.ts`) — so newly
 * registered destinations show up here for free and we never duplicate the rail
 * by hand.
 *
 * This list is NO LONGER identical to the rail's. Both read
 * `getAdvertisedSurfaces()`, but `shell/nav/appRail.tsx` additionally drops a `pinnable`
 * surface the user has not promoted, so a promotable sub-item (an Agent
 * Capabilities tab) is searchable here whether or not it occupies a rail row.
 * That is deliberate: the palette answers "where can I go", and such a tab is
 * always reachable inside its host panel — only its rail row is opt-in.
 *
 * The rail does not cover every routed destination, however. A handful of
 * pages have routes in `App.tsx` but no rail surface (some are redirects into
 * Settings). Those are enumerated in `pagesData.ts` ({@link EXTRA_PAGES}) so they remain
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

/** Glyph per {@link EXTRA_PAGES} key (`pagesData.ts` holds the entries). */
const EXTRA_PAGE_ICON: Record<string, typeof LayoutGrid> = {
  apps: Compass,
  'apps-library': LayoutGrid,
  webhooks: ArrowDownToLine,
  logs: ScrollText,
  developer: Code2,
  tasks: ListChecks,
  'mc-agents': Bot,
  instances: Server,
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
  // `getAdvertisedSurfaces()`, not `getBuiltinSurfaces()`: a preview-gated
  // surface is not released yet, so it must not be reachable from Search
  // Everywhere either — the palette is a second front door to the rail, and
  // gating only the rail would leave the unpolished page one ⌘K away.
  for (const s of getAdvertisedSurfaces()) {
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
    // Same gate the `getAdvertisedSurfaces()` loop above applies to registry
    // surfaces: an unreleased page must not be one ⌘K away either.
    if (p.previewFlag && !readPreviewFlag(p.previewFlag)) continue
    if (!byRoute.has(p.route)) {
      byRoute.set(p.route, {
        key: p.key,
        route: p.route,
        icon: inlineIcon(EXTRA_PAGE_ICON[p.key]),
        title: i18nT(EXTRA_PAGE_TITLE_KEY[p.key]),
        subtitle: p.route,
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
