import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { useProvider } from '../providers'
import { modelListRefetchInterval, useModelsDegraded } from '../providers/modelListHealth'
import { isPricedMultiplier, withAutoFirst } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'

/** Auto-only list used before the first fetch resolves.
 *
 *  `description: ''` for the same reason as `withAutoFirst`: Auto's short label
 *  is a catalog key resolved where it renders, not an English literal living in
 *  a data module. */
const PLACEHOLDER: ModelInfo[] = [{ name: 'auto', description: '' }]

/**
 * THE model list. Every picker reads it through here.
 *
 * ## Why a hook and not six `useQuery` calls
 *
 * Six surfaces render a model picker (ChatPage, ChatPane, ChatSidebar's bulk
 * switcher, AgentsPage, Settings ▸ Chat, KiroCrewAgentsPage) and all six used
 * the SAME query key — deliberately, so kiro-cli's `--list-models` is spawned
 * once — while each declared its own `queryFn`. React Query stores one cache
 * entry per key and the fetching observer's options win, so with divergent
 * fetchers the array every picker reads is decided by *which surface fetched
 * last*.
 *
 * That was not theoretical. Three shapes were live at once: four surfaces
 * returned `withAutoFirst(models)`, Settings ▸ Chat returned a hand-built
 * `[{name:'auto',description:'Default'}, ...rest]` that discarded everything
 * the live Auto row carried, and KiroCrewAgentsPage returned the raw list with
 * no Auto-first ordering at all. Opening Settings ▸ Chat replaced the shared
 * cache with the stripped shape, so Auto's credit-multiplier badge vanished
 * from every other picker until one of them refetched — a flicker whose cause
 * is three files away from the symptom.
 *
 * One key with one fetcher makes that class of bug unrepresentable: a caller
 * cannot supply a shape, only read one.
 *
 * `backend` selects a separate catalog and cache for a member's harness.
 * `enabled` controls fetching per observer and cannot corrupt the cached value:
 * ChatSidebar's bulk switcher passes
 * `false` until its panel opens so merely rendering the sidebar does not spawn
 * kiro-cli. Other mounted observers still fetch normally — `enabled` gates who
 * *triggers* a fetch, not what lands in the cache.
 */
type AvailableModelsOptions = { enabled?: boolean; backend?: string }

export function useAvailableModelsQuery({ enabled, backend }: AvailableModelsOptions = {}) {
  const provider = useProvider()
  const isDegraded = useModelsDegraded(provider.id)
  const query = useQuery({
    queryKey: backend === undefined ? ['available-models', provider.id] : ['available-models', provider.id, backend],
    queryFn: async () => {
      if (backend === undefined) return withAutoFirst(await provider.fetchAvailableModels())
      const models = await api.models(backend)
      return withAutoFirst(models.map((m: {
        model_name: string
        description?: string
        context_window?: number
        context_window_tokens?: number
        rate_multiplier?: number
      }) => ({
        name: m.model_name,
        description: m.description || '',
        contextWindow: [m.context_window, m.context_window_tokens]
          .find(value => typeof value === 'number' && Number.isFinite(value) && value > 0),
        rateMultiplier: isPricedMultiplier(m.rate_multiplier) ? m.rate_multiplier : undefined,
      })))
    },
    refetchInterval: backend === undefined ? modelListRefetchInterval : query =>
      query.state.error || !query.state.data?.some(model => model.name !== 'auto') ? 8_000 : false,
    ...(enabled === undefined ? {} : { enabled }),
  })
  return { ...query, data: query.data ?? PLACEHOLDER, isDegraded: backend === undefined ? isDegraded : query.isError }
}

export function useAvailableModels(options: AvailableModelsOptions = {}): ModelInfo[] {
  return useAvailableModelsQuery(options).data
}
