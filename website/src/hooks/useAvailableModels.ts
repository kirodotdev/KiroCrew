import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import { useProvider } from '../providers'
import { modelListRefetchInterval } from '../providers/modelListHealth'
import { withAutoFirst, applyModelOrder } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'

/** The one config path this hook reads. Kept minimal on purpose: the full
 *  `/api/config/kirocrew` object is large and owned by the settings panels;
 *  the model list only needs the saved display order. */
type ModelOrderConfig = { agent?: { model_order?: string[] } }

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
 *
 * ## The ordering seam
 *
 * The user-authored display order (`agent.model_order`) is applied HERE, as a
 * `useMemo` over the cached list, and deliberately NOT inside the model
 * `queryFn`. Folding it into the fetcher would freeze the order into the React
 * Query cache until the next `--list-models` refetch (a `modelListRefetchInterval`
 * away), so a reorder in Settings ▸ Chat would not show until then. Re-mapping
 * the cached array in a `useMemo` instead means a config change reorders every
 * picker on the very next render, with no model refetch — `applyModelOrder` is
 * pure and touches neither the network nor the cache.
 *
 * The single-key/single-fetcher invariant above is about the MODEL list; the
 * order is read from a SEPARATE query key (`['kirocrewConfig']`) with the SAME
 * fetcher every settings panel already uses, so this adds one more subscriber to
 * that shared singleton, not a second fetcher. It is ungated by `enabled`
 * because reading config never spawns kiro-cli, and a picker that renders with
 * `enabled: false` still needs the order once its list resolves.
 */
export function useAvailableModels({ enabled }: { enabled?: boolean } = {}): ModelInfo[] {
  const provider = useProvider()
  const { data } = useQuery({
    queryKey: ['available-models', provider.id],
    queryFn: async () => withAutoFirst(await provider.fetchAvailableModels()),
    refetchInterval: modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  // Same key + fetcher as every settings consumer of the config: one shared
  // cache entry, one fetch. Read as data — see "The ordering seam" above.
  const { data: cfg } = useQuery<ModelOrderConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<ModelOrderConfig>,
  })
  const order = cfg?.agent?.model_order
  const list = data ?? PLACEHOLDER
  return useMemo(() => applyModelOrder(list, order ?? []), [list, order])
}

/**
 * True while the shared `['kirocrewConfig']` read is in a FAILED state, i.e.
 * the saved `agent.model_order` could not be loaded and every picker is
 * showing the backend's own order as a fallback.
 *
 * A separate hook rather than a widened `useAvailableModels` return so the 11
 * list consumers keep their array shape untouched; the one shared list
 * RENDERER (`ModelDropdownList`) subscribes to this and surfaces the state
 * once, through `ErrorNotice`, for every picker — mirroring how the model
 * list's own degradation is a shared-health subscription
 * (`useModelsDegraded`) rather than a per-host banner. React Query retries
 * the read on its own, so the state is self-clearing.
 */
export function useModelOrderLoadFailed(): boolean {
  const { isError } = useQuery<ModelOrderConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig() as Promise<ModelOrderConfig>,
  })
  return isError
}
