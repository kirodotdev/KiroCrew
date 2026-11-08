/**
 * The one read behind "a settings file was changed outside the dashboard": the
 * Settings nav dot (shell), the Security tab dot, and the review cards on
 * Settings > Security all share this query, so the dot and the cards cannot
 * disagree. Kept apart from the cards so the shell imports only this.
 *
 * Refreshed by the owner WebSocket event `config_pending_changed` (and once per
 * reconnect) in `hooks/useWebSocket.ts`; there is no polling.
 */
import { useQuery } from '@tanstack/react-query'
import { api, type ConfigPendingData } from '../../api/client'
import { retryPolicy } from '../../api/queryClient'
import { isOwnerOnlyRefusal } from '../../components/sessionEnvironmentHooks'

export const CONFIG_PENDING_QUERY_KEY = ['config-pending'] as const

/** The highlight id the gateway's bell note links to
 *  (`/settings/security?highlight=security.config-pending`), and the cards'
 *  `data-setting-id`. Registered in `settingsManual.ts`. */
export const CONFIG_PENDING_SETTING_ID = 'security.config-pending'

export function useConfigPending() {
  return useQuery<ConfigPendingData>({
    queryKey: CONFIG_PENDING_QUERY_KEY,
    queryFn: api.configPending,
    // A viewer who is not the owner gets the same refusal every time.
    retry: (count, error) => !isOwnerOnlyRefusal(error) && retryPolicy(count, error),
  })
}

/** How many files wait for review; 0 until a read succeeds (and for a viewer
 *  who is not the owner). */
export function useConfigPendingCount(): number {
  return useConfigPending().data?.count ?? 0
}
