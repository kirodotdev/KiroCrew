import { useQuery } from '@tanstack/react-query'

import { useProvider } from '../providers'
import { modelHealthKey, modelListRefetchInterval, useModelsDegraded } from '../providers/modelListHealth'
import { withAutoFirst } from '../providers/modelList'
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
 * `enabled` is the one option callers still control, because it is per-observer
 * and cannot corrupt the cached value: ChatSidebar's bulk switcher passes
 * `false` until its panel opens so merely rendering the sidebar does not spawn
 * kiro-cli. Other mounted observers still fetch normally — `enabled` gates who
 * *triggers* a fetch, not what lands in the cache.
 */
type AvailableModelsOptions = { enabled?: boolean; backend?: string | null }

/** The model-list cache key for a chat: its own backend pick's entry, or the
 *  configured backend's. Every reader of a chat's list builds the key here, so
 *  a reader cannot look up one key while the picker fills another. */
export function modelsQueryKey(providerId: string, backend?: string | null): readonly unknown[] {
  return typeof backend === 'string' ? ['available-models', providerId, backend] : ['available-models', providerId]
}

/**
 * `backend` is a chat's own backend pick (`''` is Kiro). A pick gets its OWN
 * cache entry, `['available-models', provider.id, backend]`, fetched from that
 * backend's model source on first mount -- one backend's list must never
 * overwrite another's for every reader. `null`/omitted keeps the two-part key and
 * the configured backend's list, exactly as before. The session-spawn refetch
 * (`useWebSocket`) invalidates by the `['available-models']` prefix, so a
 * backend whose first session just registered its models refreshes too.
 */
export function useAvailableModelsQuery({ enabled, backend }: AvailableModelsOptions = {}) {
  const provider = useProvider()
  const picked = typeof backend === 'string'
  const isDegraded = useModelsDegraded(modelHealthKey(provider.id, typeof backend === 'string' ? backend : undefined))
  const query = useQuery({
    queryKey: modelsQueryKey(provider.id, backend),
    queryFn: async () => withAutoFirst(await provider.fetchAvailableModels(picked ? backend : undefined)),
    refetchInterval: modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  return { ...query, data: query.data ?? PLACEHOLDER, isDegraded }
}

export function useAvailableModels(options: AvailableModelsOptions = {}): ModelInfo[] {
  return useAvailableModelsQuery(options).data
}
