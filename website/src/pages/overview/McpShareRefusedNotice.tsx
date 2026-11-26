/**
 * The line the MCP servers list shows when a server set to share is not shared
 * and the approval card above it does not list it (it cannot be approved there).
 *
 * The Sharing view shows each such server's reason and its Remove button. This
 * line only makes the servers visible on the Servers view, and links to the
 * Sharing view, which the router serves for anyone, with or without Developer Mode.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type McpManagedServer } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import StatusNotice from '../../components/StatusNotice'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { waitingForApproval, type GatewayStatus } from '../settings/mcpManagementModel'
import { MCP_GATEWAY_STATUS_QUERY_KEY, STATUS_POLL_MS, isPermanentRefusal, refusedShares } from './mcpShareRefusedQuery'

/** Customize → MCP Servers on its Sharing view, where a refused launch is approved. */
export const MCP_MANAGEMENT_PATH = '/capabilities?tab=mcp-servers&view=sharing'

// Re-exported so this path stays the import path its callers use.
export { STATUS_POLL_MS, refusedShares, isPermanentRefusal } from './mcpShareRefusedQuery'

export default function McpShareRefusedNotice({
  className = '',
  stagedChanges = false,
}: {
  className?: string
  /**
   * The host page holds staged edits that are not applied yet. The chat
   * hand-off is then off, and the MCP Management link opens a new tab so this
   * page, and its staged edits, stay in view.
   */
  stagedChanges?: boolean
}) {
  // Same key as MCP Management, so one cached read serves both. Polled while
  // mounted: a gateway restart can record a new refusal behind an open tab, and
  // the shared client caches with no expiry. A permanent refusal stops the poll.
  const { data: status, isError, error } = useQuery<GatewayStatus>({
    queryKey: MCP_GATEWAY_STATUS_QUERY_KEY,
    queryFn: () => api.mcpGatewayStatus(),
    retry: (count, err) => !isPermanentRefusal(err) && count < 1,
    refetchInterval: query => (isPermanentRefusal(query.state.error) ? false : STATUS_POLL_MS),
  })
  const { data: servers } = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })
  if (isError && !isPermanentRefusal(error)) {
    return (
      <ErrorNotice
        className={className}
        message={i18nT('pages.overview.mcpTab.share_status_failed')}
        askAgent={!stagedChanges}
        testId="mcp-share-status-error"
      />
    )
  }
  // A server the approval card above lists (an approvable launch) is answered
  // there; naming it again here only repeats the card. The rest (too many
  // launches, a launch with no approvable identity, a name the card cannot
  // match) is said once, and the Sharing view shows each one's own reason.
  const waiting = new Set(waitingForApproval(servers?.servers ?? [], status?.launch_refused).map(s => s.name))
  const names = refusedShares(status).filter(name => !waiting.has(name))
  if (names.length === 0) return null
  const review = i18nT('pages.overview.mcpTab.share_refused_review')
  // Nothing failed: the gateway is keeping these servers inside each chat, so
  // this is a warning, not an error.
  return (
    <StatusNotice
      className={className}
      message={i18nT('pages.overview.mcpTab.share_refused', { count: names.length, names: fmtList(names, { style: 'narrow' }) })}
      testId="mcp-share-refused"
      footer={
        <span className="block">
          {stagedChanges ? (
            <Link to={MCP_MANAGEMENT_PATH} className="text-accent hover:underline" target="_blank" rel="noopener noreferrer">
              {review}
            </Link>
          ) : (
            <Link to={MCP_MANAGEMENT_PATH} className="text-accent hover:underline">
              {review}
            </Link>
          )}
        </span>
      }
    />
  )
}
