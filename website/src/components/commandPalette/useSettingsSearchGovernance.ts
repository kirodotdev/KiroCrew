import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { useUpdateSwitchesShown } from '../../utils/updateSwitches'
import type { SettingsSearchGovernance } from './settingsSearchCore'

/**
 * The governance answers every settings search reads (the command palette,
 * the Command Bar and the Settings page search), resolved once so the three
 * cannot offer different rows.
 *
 * Each read is the SAME query the governed surface itself renders from, so a
 * search and the page it lands on cannot disagree. A search that may fetch
 * re-reads an answer over 30 s old when it opens; with `fetch: false` it reads
 * only what is cached, from entries kept for the session. The updater's answer
 * is read from cache only everywhere (`useUpdateSubscription` seeds it at
 * boot). An unanswered or failed read is
 * not a denial: the row is withheld only once it is known to be absent.
 */
const FRESH_ENOUGH = { staleTime: 30_000 } as const
const CACHED_ONLY = { enabled: false, refetchOnMount: false, staleTime: Infinity, gcTime: Infinity } as const

export function useSettingsSearchGovernance(
  /** `false` reads only what is cached: the Command Bar's root issues no request. */
  { fetch = true }: { fetch?: boolean } = {},
): SettingsSearchGovernance {
  // The Decisions card's `['dashboardConfig']` read.
  const dashCfgQ = useQuery<{ decisions_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    ...(fetch ? FRESH_ENOUGH : CACHED_ONLY),
  })
  const decisionsEnabled = !dashCfgQ.isSuccess || dashCfgQ.data?.decisions_enabled === true
  // The `['tipsStatus']` read ChatPanel uses to drop its Discovery rail group.
  const tipsQ = useQuery<{ enabled_config: boolean }>({
    queryKey: ['tipsStatus'],
    queryFn: () => api.tipsStatus(),
    ...(fetch ? FRESH_ENOUGH : CACHED_ONLY),
  })
  const tipsEnabled = !tipsQ.isSuccess || tipsQ.data?.enabled_config !== false
  // The two About update switches, under the rule About draws them by.
  // The updater's answer from cache only, on every surface: a search open is no
  // reason for an IPC round trip, and About re-reads it when it is opened.
  const { app, gateway } = useUpdateSwitchesShown({ fetch: false })
  return useMemo(
    () => ({ decisionsEnabled, tipsEnabled, updateSwitches: { app, gateway } }),
    [decisionsEnabled, tipsEnabled, app, gateway],
  )
}
