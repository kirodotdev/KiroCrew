import { memo, useState } from 'react'
import { Trans } from 'react-i18next'

import NoticeCard from './NoticeCard'
import { COMPOSE_SEND_EVENT, type ComposeSendDetail, type ComposeSendOutcome } from '../../components/chat/GrillCard'
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

/** What a server's button did: `pending` while its send is out, `done` once
 *  the text went out or into the chat box, `failed` when it did not go out. */
type Press = { state: 'pending' | 'done' } | { state: 'failed'; reason?: string }

/**
 * That row, drawn in the reader's language from `meta.servers` (the names it
 * shows) and `meta.more` (how many it leaves out). It names the chat way in:
 * a message that names the server. The row's English `content` is the
 * fallback for channel mirrors and SSE readers, and for a row whose meta
 * names no server.
 *
 * Each named server gets a "Sign in to <name>" button (#40501). A press sends
 * that text for the person as an ordinary message of its own, through the
 * page that serves `slot` (`COMPOSE_SEND_EVENT`, `queue`): it queues behind a
 * running turn, never steers into it, and takes nothing from the draft. So the
 * sign-in starts exactly the way the typed message starts it. A host that
 * serves no composer for `slot` (a crew window) takes no press, and the
 * button does nothing.
 */
export default memo(function McpSignInNeededNotice({
  content,
  meta,
  slot,
}: {
  content: string
  meta?: ChatMessage['meta']
  slot?: string
}) {
  useLanguageGeneration()
  // shortcut: kept per mounted row, so a row scrolled out of a virtualized list
  // and back offers its buttons again; a second press only sends the message
  // again, and a sign-in already live for the server starts no second one.
  const [presses, setPresses] = useState<Record<string, Press>>({})
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
  const press = (name: string, text: string) => {
    if (!slot) return
    const settle = (outcome: ComposeSendOutcome, reason?: string) =>
      setPresses(cur => ({ ...cur, [name]: outcome === 'refused' ? { state: 'failed', reason } : { state: 'done' } }))
    const detail: ComposeSendDetail = { text, slot, queue: true, result: null, onOutcome: settle }
    window.dispatchEvent(new CustomEvent(COMPOSE_SEND_EVENT, { detail }))
    if (detail.result === 'pending') setPresses(cur => ({ ...cur, [name]: { state: 'pending' } }))
    else if (detail.result === 'staged') setPresses(cur => ({ ...cur, [name]: { state: 'done' } }))
  }
  const failed = servers.map(name => presses[name]).find(p => p?.state === 'failed')
  const action = slot ? (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      {servers.map(name => {
        // Stored text as an i18nT value, never a key or a <Trans> value.
        const text = i18nT('pages.chatPage.mcp_sign_in_needed_send', { name })
        const state = presses[name]?.state
        return (
          <button
            key={name}
            type="button"
            data-testid="mcp-sign-in-needed-send"
            disabled={state === 'pending' || state === 'done'}
            className="break-all underline underline-offset-2 hover:text-text focus-visible:text-text disabled:opacity-50 disabled:no-underline disabled:cursor-default"
            onClick={() => press(name, text)}
          >
            {text}
          </button>
        )
      })}
      {failed?.state === 'failed' && (
        <span role="status" data-testid="mcp-sign-in-needed-failed" className="text-danger">
          {failed.reason
            ? i18nT('pages.chatPage.send_failed_with_error', { error: failed.reason })
            : i18nT('pages.chatPage.send_failed')}
        </span>
      )}
    </span>
  ) : undefined
  return <NoticeCard content={content} tone="warn" body={body} action={action} />
})
