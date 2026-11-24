import { memo, useState } from 'react'
import { Trans } from 'react-i18next'

import NoticeCard from './NoticeCard'
import ErrorNotice from '../../components/ErrorNotice'
import { COMPOSE_SEND_EVENT, type ComposeSendDetail, type ComposeSendOutcome } from '../../components/chat/GrillCard'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import { fmtList, withReason } from '../../i18n/format'
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
 *  the text went out or into the chat box, `unconfirmed` when the answer was
 *  lost (it may have gone out, so the button stays held against a second
 *  send), `failed` when it did not go out. */
type Press = { state: 'pending' | 'done' | 'unconfirmed' } | { state: 'failed'; reason?: string }

const LINK = 'break-all underline underline-offset-2 hover:text-text focus-visible:text-text disabled:opacity-50 disabled:no-underline disabled:cursor-default'

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
 * sign-in starts exactly the way the typed message starts it. Up to two
 * servers get inline buttons; three or more share one "Sign in" menu. A press
 * no page takes (a crew window serves no composer for `slot`) says nothing
 * went out, and a refused one says why, both through `ErrorNotice`.
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
  // A press no page took (a crew window serves no composer for `slot`).
  const [unhandled, setUnhandled] = useState(false)
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
    setUnhandled(false)
    const settle = (outcome: ComposeSendOutcome, reason?: string) =>
      setPresses(cur => ({
        ...cur,
        [name]: outcome === 'refused' ? { state: 'failed', reason } : { state: outcome === 'indeterminate' ? 'unconfirmed' : 'done' },
      }))
    const detail: ComposeSendDetail = { text, slot, queue: true, result: null, onOutcome: settle }
    window.dispatchEvent(new CustomEvent(COMPOSE_SEND_EVENT, { detail }))
    if (detail.result === 'pending') setPresses(cur => ({ ...cur, [name]: { state: 'pending' } }))
    else if (detail.result === 'staged') setPresses(cur => ({ ...cur, [name]: { state: 'done' } }))
    // No page serves this chat's composer (a crew window): nothing went out.
    else setUnhandled(true)
  }
  // Stored text as an i18nT value, never a key or a <Trans> value.
  const sendText = (name: string) => i18nT('pages.chatPage.mcp_sign_in_needed_send', { name })
  const held = (name: string) => { const st = presses[name]?.state; return st === 'pending' || st === 'done' || st === 'unconfirmed' }
  const unconfirmed = servers.some(name => presses[name]?.state === 'unconfirmed')
  const failedName = servers.find(name => presses[name]?.state === 'failed')
  const failed = failedName ? presses[failedName] : undefined
  // At most two inline buttons; three or more servers share one menu.
  const action = !slot ? undefined : servers.length <= 2 ? (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      {servers.map(name => (
        <button
          key={name}
          type="button"
          data-testid="mcp-sign-in-needed-send"
          disabled={held(name)}
          className={LINK}
          onClick={() => press(name, sendText(name))}
        >
          {sendText(name)}
        </button>
      ))}
    </span>
  ) : (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" data-testid="mcp-sign-in-needed-menu" className={LINK}>
          {i18nT('pages.chatPage.mcp_sign_in_needed_menu')}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-w-[min(24rem,90vw)]">
        {servers.map(name => (
          <DropdownMenuItem
            key={name}
            data-testid="mcp-sign-in-needed-send"
            disabled={held(name)}
            className="break-all"
            onSelect={() => press(name, sendText(name))}
          >
            {sendText(name)}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
  return (
    <>
      <NoticeCard content={content} tone="warn" body={body} action={action} />
      {/* No hand-off: the fix is to press again, not to ask the agent. */}
      {failedName && failed?.state === 'failed' && (
        <ErrorNotice
          message={withReason(failed.reason, i18nT('components.grillCard.send_failed'))}
          variant="inline"
          askAgent={false}
          onDismiss={() => setPresses(cur => { const next = { ...cur }; delete next[failedName]; return next })}
          testId="mcp-sign-in-needed-failed"
        />
      )}
      {unconfirmed && (
        <ErrorNotice
          message={i18nT('components.grillCard.unconfirmed')}
          variant="inline"
          askAgent={false}
          testId="mcp-sign-in-needed-unconfirmed"
        />
      )}
      {unhandled && (
        <ErrorNotice
          message={i18nT('components.grillCard.not_sent')}
          variant="inline"
          askAgent={false}
          onDismiss={() => setUnhandled(false)}
          testId="mcp-sign-in-needed-unhandled"
        />
      )}
    </>
  )
})
