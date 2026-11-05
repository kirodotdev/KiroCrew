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
 * Six surfaces render a model picker (ChatPage, ChatPane, chat-sidebar/bulkModelSwitch.tsx's bulk
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
 * and cannot corrupt the cached value: chat-sidebar/bulkModelSwitch.tsx's bulk switcher passes
 * `false` until its panel opens so merely rendering the sidebar does not spawn
 * kiro-cli. Other mounted observers still fetch normally — `enabled` gates who
 * *triggers* a fetch, not what lands in the cache.
 *
 * `backend` is the `models_backend` the server names for a session or crew on
 * another harness than the configured one: that harness's list gets its own key,
 * so it never replaces the configured list the other pickers read.
 */
type AvailableModelsOptions = { enabled?: boolean; backend?: string }

/** The cache key of the configured list, or of `backend`'s own list. */
export function modelsQueryKey(providerId: string, backend?: string): readonly unknown[] {
  return backend === undefined ? ['available-models', providerId] : ['available-models', providerId, backend]
}

export function useAvailableModelsQuery({ enabled, backend }: AvailableModelsOptions = {}) {
  const provider = useProvider()
  const isDegraded = useModelsDegraded(modelHealthKey(provider.id, backend))
  const query = useQuery({
    queryKey: modelsQueryKey(provider.id, backend),
    queryFn: async () => withAutoFirst(await provider.fetchAvailableModels(backend)),
    refetchInterval: modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  return {
    // Getters, not a `...query` spread. React Query re-renders an observer
    // only for the result fields it read, and a spread reads every field, so
    // each fetch's `isFetching`/`fetchStatus`/`dataUpdatedAt` flips re-rendered
    // ChatPage and every split pane even when the model list was unchanged.
    // A getter reads its field only when the caller does.
    get data() { return query.data ?? PLACEHOLDER },
    get error() { return query.error },
    get isError() { return query.isError },
    get isFetching() { return query.isFetching },
    get isSuccess() { return query.isSuccess },
    refetch: query.refetch,
    isDegraded,
  }
}

export function useAvailableModels(options: AvailableModelsOptions = {}): ModelInfo[] {
  return useAvailableModelsQuery(options).data
}
