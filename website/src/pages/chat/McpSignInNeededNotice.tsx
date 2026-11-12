import { memo } from 'react'
import { Trans } from 'react-i18next'

import NoticeCard from './NoticeCard'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/** The row a chat shows once when its MCP servers wait for a sign-in
 *  (`meta.kind === 'mcp_sign_in_needed'`, written by
 *  `src/kiro_crew/dashboard/chat_turn/mcp_session.py`). */
export function isMcpSignInNeededNotice(m: ChatMessage): boolean {
  return m.role === 'notice' && m.meta?.kind === 'mcp_sign_in_needed'
}

/**
 * That row, drawn in the reader's language from `meta.servers` (the names it
 * shows) and `meta.more` (how many it leaves out). It names the chat way in:
 * a message that names the server. The row's English `content` is the
 * fallback for channel mirrors and SSE readers, and for a row whose meta
 * names no server. It starts no sign-in by itself.
 */
export default memo(function McpSignInNeededNotice({
  content,
  meta,
}: {
  content: string
  meta?: ChatMessage['meta']
}) {
  useLanguageGeneration()
  const servers = Array.isArray(meta?.servers)
    ? meta.servers.filter((s): s is string => typeof s === 'string' && s !== '')
    : []
  if (!servers.length) return <NoticeCard content={content} />
  const more = typeof meta?.more === 'number' && meta.more > 0 ? meta.more : 0
  // Server names are stored text, so each one is a component's own child,
  // never a <Trans> value (website/docs/i18n-catalog.md).
  const body = servers.length === 1 && !more ? (
    <Trans
      i18nKey="pages.chatPage.mcp_sign_in_needed_single"
      components={{ name: <span>{servers[0]}</span> }}
    />
  ) : (
    <Trans
      i18nKey="pages.chatPage.mcp_sign_in_needed_several"
      components={{
        names: <>{fmtList([...servers, more ? i18nT('pages.chatPage.mcp_sign_in_needed_more', { count: more }) : ''])}</>,
        first: <span>{servers[0]}</span>,
      }}
    />
  )
  return <NoticeCard content={content} tone="warn" body={body} />
})
