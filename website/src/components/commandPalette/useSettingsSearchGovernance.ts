import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { useUpdateSwitchesShown } from '../../utils/updateSwitches'
import { configuredAcpBackend, type AcpBackendConfig } from '../../api/acpBackend'
import { KIRO_SIGN_IN_BACKEND } from '../../pages/developer/kiroSignInLink'
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
  // The `['kirocrewConfig']` read naming the selected harness. The sign-in card
  // signs out Crew's own Kiro sign-in, which agents run as only on KAS; on any
  // other harness the entry would promise a sign-out of an account it does not
  // touch. Withheld only on a successful read that names another harness.
  // From cache only, on every surface: the shell already reads this at boot
  // (the credits pill), so a search adds no request of its own.
  const cfgQ = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    ...CACHED_ONLY,
  })
  const kiroSignInOffered = !cfgQ.isSuccess || configuredAcpBackend(cfgQ.data) === KIRO_SIGN_IN_BACKEND
  return useMemo(
    () => ({ decisionsEnabled, tipsEnabled, updateSwitches: { app, gateway }, kiroSignInOffered }),
    [decisionsEnabled, tipsEnabled, app, gateway, kiroSignInOffered],
  )
}
