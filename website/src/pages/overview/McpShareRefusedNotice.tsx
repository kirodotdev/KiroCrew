/**
 * The notice the MCP servers list shows when a server config asks to share is
 * not shared, because the gateway refused its launch until it is approved.
 *
 * The approval itself stays on the MCP Servers page's Sharing view: that page reads
 * the exact launch and the approve write is checked against it. This notice only
 * makes the refusal visible where people look, and links to that page, which the
 * router serves for anyone, with or without Developer Mode.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { GatewayStatus } from '../settings/mcpManagementModel'
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
  const names = refusedShares(status)
  if (names.length === 0) return null
  const review = i18nT('pages.overview.mcpTab.share_refused_review')
  // A refused share is a launch the gateway rejected, so it is an error surface:
  // the hand-off is on unless the host page holds staged edits not applied yet.
  return (
    <ErrorNotice
      className={className}
      message={i18nT('pages.overview.mcpTab.share_refused', { count: names.length, names: fmtList(names, { style: 'narrow' }) })}
      askAgent={!stagedChanges}
      actionPlacement="below"
      testId="mcp-share-refused"
      footer={
        <span className="text-muted">
          {i18nT('pages.overview.mcpTab.share_refused_restart')}{' '}
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
