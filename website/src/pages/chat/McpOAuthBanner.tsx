import type { ReactNode } from 'react'
import { Lock, ExternalLink, CheckCircle, XCircle } from 'lucide-react'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
/**
 * Inline banner for kiro-cli MCP OAuth flow. `meta.completed` flips it to the
 * authenticated state; `meta.failed` flips it to the error state.
 */
function isSafeOAuthUrl(url: string): boolean {
  if (!url) return false
  const lower = url.toLowerCase()
  return lower.startsWith('https://') || lower.startsWith('http://')
}

/** Render an mcp_oauth message into a banner, or null if there's nothing to show. */
export function renderMcpOAuthMessage(m: ChatMessage): ReactNode {
  const serverName = (m.meta?.server_name as string) || ''
  const oauthUrl = (m.meta?.oauth_url as string) || ''
  const completed = !!m.meta?.completed
  const failed = !!m.meta?.failed
  const error = (m.meta?.error as string) || ''
  if (!oauthUrl && !completed && !failed) return null
  return (
    <McpOAuthBanner
      serverName={serverName}
      oauthUrl={oauthUrl}
      completed={completed}
      failed={failed}
      error={error}
    />
  )
}

export default function McpOAuthBanner({
  serverName,
  oauthUrl,
  completed,
  failed,
  error,
}: {
  serverName: string
  oauthUrl: string
  completed: boolean
  failed?: boolean
  error?: string
}) {
  const label = serverName || i18nT('pages.chat.mcpOAuthBanner.mcp_server')

  if (failed) {
    return (
      <div className="flex items-center gap-2.5 px-3.5 py-2.5 rounded-lg border border-[var(--danger)]/40 bg-[var(--danger)]/10 text-sm">
        <XCircle className="shrink-0 text-[var(--danger)] lucide-inline" />
        <span className="flex-1 text-[var(--text)]">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.authentication_failed')}{error ? `: ${error}` : '.'}
        </span>
      </div>
    )
  }

  if (completed) {
    return (
      <div className="flex items-center gap-2.5 px-3.5 py-2.5 rounded-lg border border-[var(--ok)]/40 bg-[var(--ok)]/10 text-sm">
        <CheckCircle className="shrink-0 text-[var(--ok)] lucide-inline" />
        <span className="flex-1 text-[var(--text)]">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.authenticated')}
        </span>
      </div>
    )
  }

  // Defense-in-depth: backend already validates, but never render a non-http(s) URL on <a href>.
  const safeUrl = isSafeOAuthUrl(oauthUrl) ? oauthUrl : ''
  if (!safeUrl) return null

  return (
    <div className="flex flex-col gap-2 px-3.5 py-3 rounded-lg border border-[var(--warn)]/40 bg-[var(--warn)]/10 text-sm">
      <div className="flex items-center gap-2.5">
        <Lock className="shrink-0 text-[var(--warn)] lucide-inline" />
        <span className="flex-1 text-[var(--text)] min-w-0 break-words">
          <span className="font-mono font-semibold">{label}</span> {i18nT('pages.chat.mcpOAuthBanner.requires_authentication')}
        </span>
      </div>
      <a
        href={safeUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center justify-center gap-1.5 self-start px-4 py-1.5 rounded-md text-[13px] font-semibold bg-[var(--accent)] text-[var(--accent-fg)] cursor-pointer hover:opacity-90 transition-opacity no-underline"
      >
        {i18nT('pages.chat.mcpOAuthBanner.authorize')} {label} <ExternalLink className="lucide-inline" size={13} />
      </a>
    </div>
  )
}
