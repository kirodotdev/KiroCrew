/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux dependency.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 */
import { useState, useRef, useCallback, useEffect, type ReactNode } from 'react'
import { useImeGuard } from '../hooks/useImeGuard'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowUp, Loader2 } from 'lucide-react'
import ChatMessageList from './ChatMessageList'
import { useAppApi } from './index'
import type { ChatMessage } from '../types'

import { i18nT } from '../i18n/t'
export interface ChatEmbedProps {
  slotKey: string
  agent?: string
  placeholder?: string
  /**
   * Chrome-less rendering: drop the outer border/rounding/background and the
   * title strip, and make the input row transparent with no top border. Lets a
   * host page (e.g. the Spec Builder builtin) embed the chat flush inside its
   * own card. Defaults to false — existing embeds are unchanged.
   */
  frameless?: boolean
  /**
   * Jump the scroll to the bottom instantly on the first render (instead of the
   * default smooth scroll), then stay pinned to the bottom as content grows —
   * unless the user scrolls up more than 40px, which releases the pin until they
   * return to the bottom. Defaults to false — existing embeds keep the smooth
   * scroll-into-view behavior.
   */
  startAtBottom?: boolean
  /**
   * Send handler. When supplied, the composer routes through it INSTEAD of
   * `POST /api/chat`.
   *
   * The generic endpoint keys off `slotKey` alone and will CREATE the slot if it
   * is missing, with no app ownership and no project — so a stale tab (its spec
   * deleted elsewhere) could resurrect an unscoped session in which approved
   * tools run from the gateway's own directory. A host app that owns its slots
   * passes its own endpoint here, which can carry the app's identity checks and
   * refuse a stale send. Omitted, behaviour is unchanged.
   */
  onSend?: (message: string) => Promise<unknown> | void
  /**
   * Content rendered in normal flow directly ABOVE the composer, inside the
   * embed's own column, so it always sits on top of the input regardless of the
   * composer's height. A host uses this for a docked quote / reference bar
   * instead of absolutely positioning one over the transcript with a brittle
   * fixed offset that breaks whenever the composer's height changes.
   */
  aboveComposer?: ReactNode
}

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  title?: string
}

function ChatEmbed({ slotKey, agent, placeholder, frameless, startAtBottom, onSend, aboveComposer }: ChatEmbedProps) {
  const api = useAppApi()
  const ime = useImeGuard()
  const [input, setInput] = useState('')
  const endRef = useRef<HTMLDivElement>(null)
  const scrollerRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')
  // When startAtBottom is on, we stick the scroller to the bottom until the
  // user scrolls up past the threshold; scrolling back down re-pins.
  const pinnedRef = useRef(true)

  const { data: slotData, refetch } = useQuery({
    queryKey: ['app-sdk-embed', slotKey],
    queryFn: () => api.get<ChatSlotData>('/api/chat/slots/' + encodeURIComponent(slotKey)),
    refetchInterval: (query) => {
      const running = query.state.data?.running ?? false
      return running ? 1000 : 5000
    },
  })

  const messages = slotData?.messages ?? []
  const running = slotData?.running ?? false
  const title = slotData?.title ?? ''

  // Track whether the user is parked at the bottom (startAtBottom mode only).
  useEffect(() => {
    if (!startAtBottom) return
    const el = scrollerRef.current
    if (!el) return
    const onScroll = () => {
      pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [startAtBottom, slotKey])

  // Auto-scroll when new messages arrive.
  const msgHash = messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0)
  useEffect(() => {
    if (msgHash === lastHashRef.current) return
    lastHashRef.current = msgHash
    if (startAtBottom) {
      // Instant jump on first paint + stick-to-bottom while streaming, unless
      // the user has scrolled up to read (pin released).
      const el = scrollerRef.current
      if (el && pinnedRef.current) el.scrollTop = el.scrollHeight
    } else {
      endRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [msgHash, startAtBottom])

  const sendMutation = useMutation({
    mutationFn: (msg: string) => {
      if (onSend) return Promise.resolve(onSend(msg))
      return api.post('/api/chat', { message: msg, slot: slotKey, agent: agent || '' })
        .catch((err) => {
          // POST /api/chat returns SSE — JSON parse fails, expected.
          if (err instanceof SyntaxError) return
          throw err
        })
    },
    onSettled: () => { void refetch() },
  })

  const send = useCallback(() => {
    const msg = input.trim()
    if (!msg) return
    setInput('')
    sendMutation.mutate(msg)
  }, [input, sendMutation])

  // Resolve a pending tool approval from inside the embed.
  //
  // Without this the group header rendered a dead "Approval needed" label with
  // no buttons: ChatMessageList only shows the Approve/Reject controls when an
  // onApprove handler is supplied, and the embed supplied none. An embedded
  // agent that hit a permission prompt was therefore unactionable and blocked
  // until the runner's timeout auto-rejected it.
  //
  // Routed through the SLOT approval endpoint, which is the only one that can
  // express all three decisions. /api/approvals/{id}/{action} accepts just
  // approve|reject, so mapping 'trust' onto it silently downgraded a Trust click
  // to a one-shot approve: the card said "Trusted" and the very next tool call
  // prompted again. POST /api/chat/slots/{slot}/approve carries the decision
  // verbatim plus the request_id, so trust sets the owner slot's policy.
  //
  // Requires the host app to grant '/api/chat' in its allowedApiPaths.
  const approveMutation = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: string }) =>
      api.post(`/api/chat/slots/${encodeURIComponent(slotKey)}/approve`, {
        action: decision,
        request_id: id,
      }),
    onSettled: () => { void refetch() },
  })

  const approve = useCallback(
    (approvalId: string, decision: string) => approveMutation.mutate({ id: approvalId, decision }),
    [approveMutation],
  )

  return (
    <div className={`flex flex-col h-full min-h-0 overflow-hidden ${frameless ? '' : 'border border-border rounded-lg bg-bg'}`}>
      {!frameless && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-card shrink-0">
          <span className={`w-2 h-2 rounded-full shrink-0 ${running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
          <span className="text-[13px] font-semibold text-text-strong truncate flex-1">{title || slotKey}</span>
          {agent && <span className="text-[10px] font-mono text-muted">{agent}</span>}
          {running && <span className="text-[10px] text-ok font-mono">{i18nT('appSdk.chatEmbed.streaming')}</span>}
        </div>
      )}

      <div ref={scrollerRef} className="flex-1 overflow-y-auto py-4 min-h-0">
        {messages.length === 0 && !running && (
          <div className="text-center text-muted text-[13px] py-10">{i18nT('appSdk.chatEmbed.session_ready_type_a_message_to_start')}</div>
        )}
        <ChatMessageList messages={messages} running={running} onApprove={approve} />
        <div ref={endRef} />
      </div>

      {aboveComposer && <div className="shrink-0">{aboveComposer}</div>}

      <div className={`flex items-center gap-2 px-3 py-2 shrink-0 ${frameless ? '' : 'border-t border-border bg-bg-subtle'}`}>
        <input
          type="text"
          aria-label={i18nT('appSdk.chatEmbed.chat_message')}
          className="flex-1 min-w-0 px-3 py-2 text-sm bg-bg-elevated border border-border rounded-md text-text outline-none focus-visible:border-accent transition-colors"
          value={input}
          onChange={e => setInput(e.target.value)}
          {...ime.bindComposition()}
          onKeyDown={e => {
            if (e.key !== 'Enter' || e.shiftKey) return
            // Rule 1: single-line input; the emptiness test stays outside the guard.
            if (ime.isComposing(e)) return
            if (input.trim()) { e.preventDefault(); send() }
          }}
          placeholder={running ? i18nT('appSdk.chatEmbed.agent_is_working') : (placeholder || i18nT('appSdk.chatEmbed.message'))}
          disabled={sendMutation.isPending}
        />
        <button
          className="p-2 rounded-md bg-accent text-accent-fg disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-80 transition-opacity"
          onClick={send}
          disabled={sendMutation.isPending || !input.trim()}
          title={i18nT('appSdk.chatEmbed.send')}
          aria-label={i18nT('appSdk.chatEmbed.send_message')}
        >
          {sendMutation.isPending ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>
    </div>
  )
}

export default ChatEmbed
