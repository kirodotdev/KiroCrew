/**
 * The one read behind "a server set to share is not shared": the Customize
 * rail badge (shell), the MCP Servers tab and the notice on the servers list
 * share it, so the counts and the notice cannot disagree. Kept apart from the notice so the shell
 * imports only this.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { GatewayStatus } from '../settings/mcpManagementModel'

/** Same key as MCP Management, so one cached read serves every reader. */
export const MCP_GATEWAY_STATUS_QUERY_KEY = ['mcpGatewayStatus'] as const

/** How often the open notice on the servers list re-reads gateway status. */
export const STATUS_POLL_MS = 30_000

/**
 * Servers config asks to share whose launch is refused, sorted by name.
 *
 * The gateway does the matching (`share_refused`): a refusal is recorded under
 * the rewriter's alias for a slash-named server, which the client cannot derive.
 */
export function refusedShares(status: GatewayStatus | undefined): string[] {
  if (!status?.enabled) return []
  return Object.keys(status.share_refused ?? {}).sort()
}

/**
 * True for a read refused by who is asking (403: not the owner, 404: a gateway
 * without the route). Those are permanent answers for this viewer, not a fault
 * of the sharing check, so readers stay silent on them and stop polling.
 * An expired session's 403 (`authRequired`) is not: the dashboard refreshes the
 * session, so retries and polling carry on.
 */
export function isPermanentRefusal(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  return error.status === 404 || (error.status === 403 && !error.authRequired)
}

/**
 * How often the shell re-reads on its own. The gateway records a refusal when
 * it starts, and the socket's connect handler re-reads this key then
 * (`hooks/useWebSocket.ts`), so this is only the net under a refusal recorded
 * after that connect.
 */
export const SHELL_POLL_MS = 5 * 60_000

/**
 * How many servers set to share wait for an approval; 0 until a read succeeds,
 * for a viewer who is not the owner, and while sharing is off.
 *
 * One status read costs the gateway a daemon ping, a config load and a read of
 * the approvals store, so the shell does not poll it at the notice's pace. The
 * slow poll stops only on a permanent refusal. It runs while sharing is off
 * too: sharing can be turned on from another tab, which this tab must notice.
 */
export function useMcpShareRefusedCount(): number {
  const { data } = useQuery<GatewayStatus>({
    queryKey: MCP_GATEWAY_STATUS_QUERY_KEY,
    queryFn: () => api.mcpGatewayStatus(),
    retry: (count, err) => !isPermanentRefusal(err) && count < 1,
    refetchInterval: query => (isPermanentRefusal(query.state.error) ? false : SHELL_POLL_MS),
  })
  return refusedShares(data).length
}
