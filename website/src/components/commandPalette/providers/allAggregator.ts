import { createElement } from 'react'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import { Sparkles } from 'lucide-react'

import { makeScoreThenNameComparator } from '../../../utils/fuzzyMatch'
import type { ResourceProvider, Result } from '../types'
import { getProviders as getRegisteredProviders, getProvider } from './index'

/**
 * All aggregator (Search Everywhere).
 *
 * Backs the **All** tab. It is itself a {@link ResourceProvider}, but instead
 * of owning a data source it fans the query out to every *other* registered
 * provider, keeps the top-N hits per provider (so one chatty source can't
 * flood the blended list), then merges everything into a single list ordered
 * by score-descending, name-ascending (the same ordering every per-category
 * tab uses, via {@link makeScoreThenNameComparator}).
 *
 * Empty query is special-cased: rather than asking each provider for its
 * "everything" list, the All tab shows **recents** (recent sessions). This
 * matches the §2 design — the palette opens onto something useful before the
 * user types — and avoids N provider round-trips on every open.
 *
 * The concrete aggregator ({@link createAllAggregator}) takes its provider
 * source and recents source as injected dependencies so it stays free of React
 * hooks and is unit-testable with plain stubs; {@link useAllAggregator} wires
 * the real registry + a recents source derived from the Sessions provider.
 */

const PROVIDER_ID = 'all'
const PROVIDER_LABEL = 'All'

/** Default cap on how many hits each provider contributes to the blend. */
const DEFAULT_PER_PROVIDER_LIMIT = 5

/** Default number of recents shown on an empty query. */
const DEFAULT_RECENTS_LIMIT = 8

/** Icon convention: lucide element with `lucide-inline` (`use-lucide-icons` lint rule). */
function inlineIcon(): ReactNode {
  return createElement(Sparkles, { className: 'lucide-inline' })
}

const compareResults = makeScoreThenNameComparator<Result>(
  (r) => r.score,
  (r) => r.title,
)

/**
 * Injectable dependencies for {@link createAllAggregator}. Decoupling the
 * provider list and recents source from the registry/React keeps the merge
 * logic pure and directly testable.
 */
export interface AllAggregatorDeps {
  /** Source of the providers to fan out to. The All provider filters itself out. */
  getProviders: () => readonly ResourceProvider[]
  /**
   * Recents to show when the query is empty. Receives the recents limit so the
   * source can cap server fetches. Optional — when omitted, an empty query
   * yields an empty list.
   */
  getRecents?: (limit: number) => Promise<Result[]> | Result[]
  /** Max hits kept per provider before merging. Defaults to {@link DEFAULT_PER_PROVIDER_LIMIT}. */
  perProviderLimit?: number
  /** Max recents returned on an empty query. Defaults to {@link DEFAULT_RECENTS_LIMIT}. */
  recentsLimit?: number
}

/**
 * Build the All {@link ResourceProvider} from injected sources. Pure (no React
 * hooks) so it can be exercised directly in tests with stub providers.
 */
export function createAllAggregator(deps: AllAggregatorDeps): ResourceProvider {
  const {
    getProviders,
    getRecents,
    perProviderLimit = DEFAULT_PER_PROVIDER_LIMIT,
    recentsLimit = DEFAULT_RECENTS_LIMIT,
  } = deps

  return {
    id: PROVIDER_ID,
    label: PROVIDER_LABEL,
    icon: inlineIcon(),
    async search(query: string): Promise<Result[]> {
      const q = query.trim()

      // Empty query → recents, never a fan-out.
      if (q.length === 0) {
        if (!getRecents) return []
        const recents = await getRecents(recentsLimit)
        return recents.slice(0, recentsLimit)
      }

      // Fan out to every provider except this aggregator itself. A single
      // provider rejecting must not blank the All tab, so each search is
      // individually guarded and contributes [] on failure.
      const providers = getProviders().filter((p) => p.id !== PROVIDER_ID)

      const perProvider = await Promise.all(
        providers.map(async (p) => {
          try {
            return await p.search(q)
          } catch {
            return [] as Result[]
          }
        }),
      )

      // Cap each provider's contribution (top-N after its own ordering), then
      // blend everything and re-sort by score-desc, name-asc.
      const merged: Result[] = []
      for (const results of perProvider) {
        const top = [...results].sort(compareResults).slice(0, perProviderLimit)
        merged.push(...top)
      }
      merged.sort(compareResults)
      return merged
    },
  }
}

/**
 * React hook: an All aggregator wired to the live provider registry, with
 * recents sourced from the Sessions provider's empty-query output (which
 * returns recent sessions, fully wired to open / resume them — no duplicated
 * mapping here). Providers register themselves into the registry in later
 * steps; until then the aggregator simply has fewer sources to fan out to.
 *
 * @param opts.perProviderLimit - Override the per-provider cap (e.g. for a
 *   denser layout). Defaults to {@link DEFAULT_PER_PROVIDER_LIMIT}.
 * @param opts.recentsLimit - Override the recents count. Defaults to
 *   {@link DEFAULT_RECENTS_LIMIT}.
 */
export function useAllAggregator(opts?: {
  perProviderLimit?: number
  recentsLimit?: number
}): ResourceProvider {
  const perProviderLimit = opts?.perProviderLimit
  const recentsLimit = opts?.recentsLimit

  return useMemo(
    () =>
      createAllAggregator({
        getProviders: () => getRegisteredProviders(),
        getRecents: async (limit) => {
          const sessions = getProvider('sessions')
          if (!sessions) return []
          const recents = await sessions.search('')
          return recents.slice(0, limit)
        },
        perProviderLimit,
        recentsLimit,
      }),
    [perProviderLimit, recentsLimit],
  )
}
