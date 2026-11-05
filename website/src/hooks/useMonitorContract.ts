import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import { AUTONUDGE_LOOPS_QUERY_KEY } from '../components/autoNudgeLoop'
import { resetRuntimeMonitorContract, setRuntimeMonitorContract } from '../monitoring/automation'

/** Shared cache key for the one runtime monitor-contract fetch. */
export const MONITOR_CONTRACT_QUERY_KEY = ['monitor-contract'] as const

/** The query-key prefixes whose cached results are read through
 * `normalizeAutomationRecord` / `isPullRequestWatchKind`, so a kind the contract
 * adds only shows its real state once these repaint. Matched by prefix, so a
 * per-slot key (`['session-automation', slot]`) is covered by its root. */
const CONTRACT_DEPENDENT_QUERY_KEYS: readonly (readonly unknown[])[] = [
  AUTONUDGE_LOOPS_QUERY_KEY,
  ['session-automation'],
  ['monitor-runtime-ceiling'],
]

/**
 * Fetch the gateway's runtime monitor contract once and install its kind list,
 * so a monitor kind an edition contributed at boot (`register_monitor_kind`)
 * is not shown as blocked in the dashboard.
 *
 * The dashboard bundle ships `website/src/monitoring/contract.json`, a snapshot
 * that lists only the built-in pull-request kinds; `automation.ts` treats a kind
 * absent from it as an unsupported, non-actionable record. This hook reads the
 * runtime `GET /api/monitor-contract` (the backend's `monitor_frontend_contract()`,
 * which reflects the registered kinds) and installs it through
 * `setRuntimeMonitorContract`, so the normalizer recognizes the contributed kind.
 *
 * It is deliberately fallback-safe. A gateway without the route (an older build)
 * answers 404; any other error is also non-fatal. In every failure case the
 * bundled contract stays in force, so behaviour is unchanged when the endpoint is
 * absent or unreachable, and unchanged when no kinds were contributed. Mount it
 * once near the app root.
 *
 * `onKindsChanged` runs when the installed kind set actually grows. The React
 * Query invalidation below repaints surfaces that read the query cache, but the
 * authoritative automation collection also lives in Redux (seeded by
 * `useAutomationSeed`), and a contributed-kind record seeded before the contract
 * arrived sits at `actionable: false` there. The owner of the seed passes its
 * `seedAutomations` here so the generation-fenced seed re-runs and re-normalizes
 * the Redux projection against the now-installed vocabulary.
 */
export function useMonitorContract(onKindsChanged?: () => void): void {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: MONITOR_CONTRACT_QUERY_KEY,
    queryFn: () => api.monitorContract(),
    // The registry is fixed at gateway boot; one fetch per session is enough.
    staleTime: Infinity,
    gcTime: Infinity,
    // One shot, no retry: a 404 (older gateway) or any other failure is a
    // non-fatal fall-through to the bundled contract, so retrying buys nothing
    // and would keep the shared query client "fetching" for the retry window.
    retry: false,
  })

  useEffect(() => {
    if (!data) return
    // Install the runtime kinds. The records already normalized and cached
    // (the autonudge list, the per-slot session automation, the monitor ceiling)
    // were normalized against the pre-install vocabulary, so a contributed-kind
    // record is sitting at "blocked" until something refetches it. When the set
    // actually changed, invalidate exactly those queries so they repaint at once
    // rather than on the next poll; when it did not change, touch nothing.
    if (setRuntimeMonitorContract(data)) {
      for (const queryKey of CONTRACT_DEPENDENT_QUERY_KEYS) {
        void queryClient.invalidateQueries({ queryKey })
      }
      // Re-normalize the Redux automation projection against the new vocabulary.
      onKindsChanged?.()
    }
    // On a 404 or any other error, leave the bundled contract in force; never
    // clear a value already installed from a prior successful fetch.
  }, [data, queryClient, onKindsChanged])

  // A 404 (older gateway) or any other fetch failure is non-fatal and leaves the
  // bundled contract in force; there is nothing for a caller to do about it, so
  // it is swallowed rather than surfaced.

  // Drop the runtime override when the last consumer unmounts, so a remount
  // re-reads it rather than trusting a value from a gateway that may have changed.
  useEffect(() => resetRuntimeMonitorContract, [])
}
