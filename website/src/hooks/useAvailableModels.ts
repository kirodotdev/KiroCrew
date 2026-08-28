import { useQuery } from '@tanstack/react-query'

import { useProvider } from '../providers'
import { lastKnownBackend, servesAutoModel } from '../providers/adapters/acp'
import { modelListRefetchInterval, modelListScope } from '../providers/modelListHealth'
import { withAutoFirst } from '../providers/modelList'
import type { ModelInfo } from '../providers/types'

/** Auto-only list used before the first fetch resolves.
 *
 *  `description: ''` for the same reason as `withAutoFirst`: Auto's short label
 *  is a catalog key resolved where it renders, not an English literal living in
 *  a data module. */
const PLACEHOLDER: ModelInfo[] = [{ name: 'auto', description: '' }]

/** Nothing known yet. A separate frozen constant rather than a fresh `[]` per
 *  call: the return value is a hook result read into render paths and effect
 *  deps, so a new array identity on every render is a re-render loop waiting to
 *  happen. */
const EMPTY: ModelInfo[] = []

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
 * `slot` / `backend` scope the cache entry. A live chat — including
 * ChatSidebar's bulk switcher — passes the session's harness so the picker
 * does not list the *next* default after a backend save, and does not flash
 * Auto on a harness that does not serve it. Settings and other new-session
 * pickers pass the configured backend (or omit both, which is the kiro /
 * unknown-config key).
 */
export function useAvailableModels({
  enabled,
  slot,
  backend,
}: {
  enabled?: boolean
  slot?: string
  backend?: string | null
} = {}): ModelInfo[] {
  const provider = useProvider()
  const intendedBackend = backend ?? ''
  const scope = modelListScope(slot, intendedBackend)
  const { data } = useQuery({
    queryKey: ['available-models', provider.id, scope],
    queryFn: async () =>
      withAutoFirst(
        await provider.fetchAvailableModels(slot ? { slot, scope } : { scope }),
      ),
    refetchInterval: modelListRefetchInterval,
    ...(enabled === undefined ? {} : { enabled }),
  })
  // `data` is undefined only before the first fetch resolves for this key. The
  // placeholder is a SYNTHETIC Auto row, so it may only be offered on a backend
  // that serves `auto` — otherwise the picker's very first paint shows one row,
  // it is the only thing to pick, and the id is rejected at the wire. Everything
  // `fetchAvailableModels` does to avoid fabricating that row is undone here if
  // this is left unconditional, because this branch runs BEFORE any of it.
  if (data) return data
  // After a default-harness switch the last-known cache is still the previous
  // namespace. Showing Auto (or that namespace's rows) for the new key is the
  // flash this placeholder exists to prevent.
  if (intendedBackend && intendedBackend !== (lastKnownBackend() ?? '')) return EMPTY
  return servesAutoModel() ? PLACEHOLDER : EMPTY
}
