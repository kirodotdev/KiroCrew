import { createElement } from 'react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { NavigateFunction } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { Package } from 'lucide-react'

import { api } from '../../../api/client'
import AppIcon from '../../AppIcon'
import { getBuiltinIcon } from '../../../apps/builtinIcons'
import { appNavTargets } from '../../../appNav'
import type { AppNavRecord, AppNavTarget } from '../../../appNav'
import { fuzzyMatch, makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import { i18nT } from '../../../i18n/t'
import type { ResourceProvider, Result } from '../types'

/**
 * Apps provider for the Search Everywhere command palette.
 *
 * Backs the **Apps** tab: type an installed app's name and Enter opens it, so the
 * palette becomes a launcher rather than only a finder.
 *
 * Installed apps are NOT reachable through {@link usePagesProvider}: that provider
 * sources `getBuiltinSurfaces()`, which deliberately excludes surfaces flagged
 * `appOnly` because the left rail renders those itself from `GET /api/apps`. So
 * before this provider, every rail destination was searchable EXCEPT the apps —
 * the one group whose membership changes per install.
 *
 * Destinations come from the shared `appNav` derivation, the same functions the
 * rail uses, so the palette can never route an app somewhere the rail does not (or
 * offer to open a disabled one).
 *
 * Enter is the only binding: an app is a pure navigation target, exactly like a
 * page, so there is no ⌘Enter or ⌥Enter variant.
 */

const PROVIDER_ID = 'apps'

/**
 * Catalog KEY for the tab label, not the label itself: this const is evaluated at
 * module load, so an `i18nT()` here would freeze the boot language.
 *
 * Its own key rather than a reuse: the rail's "Apps" group heading is a literal
 * `SurfaceGroup` value in `App.tsx`, not a catalog entry, so there is nothing to
 * share with.
 */
const PROVIDER_LABEL_KEY = 'components.commandPalette.providers.appsProvider.apps'

/** Cache the app list briefly so retyping within one search is free. */
const APPS_STALE_MS = 30_000

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function fallbackIcon(): ReactNode {
  return createElement(Package, { className: 'lucide-inline' })
}

/**
 * Render *target*'s icon, mirroring the rail's fallback chain: custom top-level
 * `iconUrl`, then a page-relative `ui/` file (installed apps), then the builtin
 * lucide glyph, then the generic package icon.
 *
 * The builtin-glyph step is builtin-only on purpose — `iconName` comes from the
 * manifest, so looking it up for an installed app would render a builtin glyph for
 * any app whose `page.icon` happens to collide with one.
 *
 * Unlike the rail this does NOT warn-tint an orphaned app: the palette says so in
 * the row's subtitle instead, which reads at a glance without relying on colour.
 */
function appIcon(target: AppNavTarget): ReactNode {
  if (target.iconUrl) {
    return createElement(AppIcon, { iconUrl: target.iconUrl, icon: target.iconName, size: 16 })
  }
  if (target.pageIconUrl) {
    return createElement('img', {
      src: `/apps/${target.name}/ui/${target.pageIconUrl}`,
      alt: '',
      className: 'w-4 h-4 rounded-sm object-contain',
    })
  }
  return (target.builtin ? getBuiltinIcon(target.iconName) : undefined) ?? fallbackIcon()
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Injectable dependencies for {@link createAppsProvider}. Keeping the concrete
 * provider free of React hooks makes it unit-testable with a plain mock fetch +
 * stub navigate; {@link useAppsProvider} wires the real React-Query fetch and
 * router.
 */
export interface AppsProviderDeps {
  /** Fetch the installed-app list (React-Query-cached in the hook). */
  fetchApps: () => Promise<AppNavRecord[]>
  /** Navigate to an app's route (Enter). */
  navigate: NavigateFunction
}

/**
 * Build the Apps {@link ResourceProvider} from injected dependencies. Pure (no
 * hooks) so it can be exercised directly in tests.
 */
export function createAppsProvider(deps: AppsProviderDeps): ResourceProvider {
  const { fetchApps, navigate } = deps

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
    icon: fallbackIcon(),
    async search(query: string): Promise<Result[]> {
      const q = query.trim()
      // Let a failed fetch REJECT rather than resolving to []: "could not load
      // the app list" and "no apps are installed" are different states, and the
      // provider is not the layer that should collapse them.
      //
      // Rejecting is safe and matches the peer fetch-backed providers
      // (`artifactsProvider` awaits bare): the All aggregator already guards
      // each provider individually and contributes [] on failure, so a swallow
      // here is redundant for protecting the blended tab.
      //
      // It is NOT yet user-visible: `CommandPalette` reads only React Query's
      // `data`/`isLoading`, so a scoped tab still shows the ordinary empty
      // state on failure — a palette-wide gap affecting every provider,
      // tracked in #1928. Rejecting is what lets that fix reach apps for free
      // rather than having the error already discarded here.
      const apps: AppNavRecord[] = await fetchApps()

      const results: Result[] = []
      for (const target of appNavTargets(apps)) {
        // Empty query lists every app (the tab doubles as a launcher menu);
        // otherwise fuzzy-match the label the rail shows.
        const match = q ? fuzzyMatch(q, target.label) : null
        if (q && !match) continue
        results.push({
          id: `${PROVIDER_ID}:${target.name}`,
          providerId: PROVIDER_ID,
          title: target.label,
          subtitle: target.orphaned
            ? i18nT('components.commandPalette.providers.appsProvider.needs_migration')
            : target.route,
          icon: appIcon(target),
          score: match ? match.score : 0,
          indices: match ? match.indices : [],
          // Apps are pure navigation targets, like Pages: Enter navigates and
          // ⌘Enter has no distinct behavior (the dispatcher ignores the modifier
          // for this kind). `onActivate` is the execution path it reuses.
          enter: { kind: 'navigate', route: target.route },
          onActivate: () => navigate(target.route),
        })
      }
      results.sort(compareResults)
      return results
    },
  }
}

/**
 * React hook: an Apps provider wired to React-Query and the router.
 *
 * Per the `use-react-query` lint rule the fetch goes through React-Query on the
 * `['apps']` key — the SAME key `AppsPage` uses, so opening the palette on a page
 * that already listed apps costs no request. `fetchQuery` rather than `useQuery`
 * because a {@link ResourceProvider}'s `search` is an imperative call from the
 * palette, not a render-time subscription.
 */
export function useAppsProvider(): ResourceProvider {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  return useMemo(
    () =>
      createAppsProvider({
        fetchApps: () =>
          queryClient.fetchQuery<AppNavRecord[]>({
            queryKey: ['apps'],
            queryFn: () => api.listApps(),
            staleTime: APPS_STALE_MS,
          }),
        navigate,
      }),
    [navigate, queryClient],
  )
}
