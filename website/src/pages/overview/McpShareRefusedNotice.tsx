/**
 * The notice the MCP servers list shows when a server config asks to share is
 * not shared, because the gateway refused its launch until it is approved.
 *
 * The approval itself stays on Developer > MCP Management: that page reads the
 * exact launch and the approve write is checked against it. This notice only
 * makes the refusal visible where people look, and links to that page, which the
 * router serves whether or not Developer Mode puts it in the side rail.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import type { GatewayStatus } from '../settings/mcpManagementModel'

/** Developer > MCP Management, where a refused launch is approved. */
export const MCP_MANAGEMENT_PATH = '/developer?tab=mcp-pool'

/** How often the open notice re-reads gateway status. */
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
 * of the sharing check, so the notice stays silent on them and stops polling.
 * An expired session's 403 (`authRequired`) is not: the dashboard refreshes the
 * session, so retries and polling carry on.
 */
function isPermanentRefusal(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  return error.status === 404 || (error.status === 403 && !error.authRequired)
}

export default function McpShareRefusedNotice({
  className = '',
  stagedChanges = false,
}: {
  className?: string
  /**
   * The host page holds staged edits that leaving it would drop. The chat
   * hand-off is then off, and the MCP Management link opens a new tab so this
   * page, and its staged edits, stay where they are.
   */
  stagedChanges?: boolean
}) {
  // Same key as MCP Management, so one cached read serves both. Polled while
  // mounted: a gateway restart can record a new refusal behind an open tab, and
  // the shared client caches with no expiry. A permanent refusal stops the poll.
  const { data: status, isError, error } = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
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
  return (
    <div
      role="status"
      data-testid="mcp-share-refused"
      className={`p-3 rounded border border-[var(--warn)] bg-warn/20 text-[13px] ${className}`}
    >
      <div className="text-[var(--warn)]">
        <AlertTriangle className="lucide-inline" />{' '}
        {i18nT('pages.overview.mcpTab.share_refused', { count: names.length, names: names.join(', ') })}
      </div>
      <div className="mt-1 text-muted">
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
      </div>
    </div>
  )
}
