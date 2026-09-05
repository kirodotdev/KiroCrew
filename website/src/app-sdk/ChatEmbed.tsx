/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux dependency.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 */
import { useRef, useCallback, useEffect, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowUp, Loader2 } from 'lucide-react'
import ChatMessageList from './ChatMessageList'
import { useChatScrollFollow } from './useChatScrollFollow'
import { JumpToBottomButton } from './ChatScrollChrome'
import FollowUpBar from '../components/FollowUpBar'
import { deriveFollowUpOptions } from './protocol'
import { useComposerDraft } from './useComposerDraft'
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

/** Stable empty transcript. A fresh `[]` fallback would be a new identity on every
 *  render, so `deriveFollowUpOptions` below would re-run (and hand FollowUpBar a new
 *  options array) on every render of an embed whose poll has not answered yet. */
const EMPTY_MESSAGES: ChatMessage[] = []

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  title?: string
}

function ChatEmbed({ slotKey, agent, placeholder, frameless, startAtBottom, onSend, aboveComposer }: ChatEmbedProps) {
  const api = useAppApi()
  const endRef = useRef<HTMLDivElement>(null)
  const lastHashRef = useRef('')
  // startAtBottom mode delegates stick-to-bottom follow to the shared hook
  // (same FollowController semantics as ChatPane and the main chat): RO-driven
  // re-pin on growth AND collapse, released only by a genuine user scroll up.
  // `enabled` is the explicit mode switch — refs stay attached in both modes,
  // and a disabled hook is fully inert (no mount pin, no ResizeObserver), so a
  // top-anchored embed is never yanked by a resize. Non-startAtBottom embeds
  // keep their own contract below — a deliberate smooth scroll to each NEW
  // MESSAGE regardless of position.
  const follow = useChatScrollFollow({ resetKey: slotKey, enabled: !!startAtBottom })
  const scrollerRef = follow.scrollerRef

  const { data: slotData, refetch } = useQuery({
    queryKey: ['app-sdk-embed', slotKey],
    queryFn: () => api.get<ChatSlotData>('/api/chat/slots/' + encodeURIComponent(slotKey)),
    refetchInterval: (query) => {
      const running = query.state.data?.running ?? false
      return running ? 1000 : 5000
    },
  })

  const messages = slotData?.messages ?? EMPTY_MESSAGES
  const running = slotData?.running ?? false
  const title = slotData?.title ?? ''

  /** Derived from the same helper the main chat and side panel use, so "options only
   *  after the answer settles" and "a later user message clears them" behave identically
   *  here too — an agent's follow-up choices should never be silently dropped just
   *  because the surface embedding them is thinner.
   *
   *  `followUpIsPlan` is DELIBERATELY dropped here (#6057): this embed is not a
   *  plan-capable host, so a plan-shaped chip stays on the composer-draft path
   *  instead of dispatching POST /api/chat/slots/{slot}/plan-action. Why that is
   *  a recorded exclusion rather than a live mis-dispatch:
   *  - The slot-detail payload this embed polls carries no `mode` field, so the
   *    embed structurally lacks the orchestrator-mode gate the dispatch path
   *    requires (ChatPane/ChatPage read the slot record's mode before
   *    dispatching; there is no equivalent source here).
   *  - Exposure is narrow: `api_chat_slot_detail` runs
   *    `_deny_cross_app_slot_access`, so an app-token embed 404s on any foreign
   *    or unscoped slot. That proves "not another surface's slot", not "never a
   *    plan-bearing slot" — an app could create and embed its own
   *    orchestrator-mode slot, which is exactly why the missing mode field
   *    above, not the ownership guard, carries the exclusion.
   *  - On hosts that DO dispatch, the `isPlanAction` allowlist keeps
   *    non-protocol plan-shaped labels on the composer path; this file never
   *    consults it because it never dispatches.
   *  SideChat makes the same exclusion, silently — it also destructures only
   *  `followUpOptions`, with no record there. If dashboard-token embeds ever
   *  need working plan chips, the parity option is wiring `usePlanActionMutation`
   *  plus a mode source into this file — a product decision, not an oversight.
   *  Pinned by the plan-exclusion test in src/test/ChatEmbed.test.tsx. */
  const { followUpOptions, followUpRecommended } = useMemo(
    () => deriveFollowUpOptions(messages, running),
    [messages, running]
  )

  /** The composer's draft behaviour, owned by the chat SDK rather than by this file —
   *  see useComposerDraft's own docs. Picking a follow-up option edits the draft
   *  (matching every other surface) instead of sending immediately. */
  const { draft, setDraft, picked, toggleOption, composition, submitOnEnter } =
    useComposerDraft({ followUpOptions })

  // startAtBottom follow is owned by useChatScrollFollow (attached below).
  // Non-startAtBottom embeds keep the message-arrival smooth scroll: it fires
  // on NEW MESSAGES only (not on content growth) and deliberately scrolls
  // regardless of position — a top-anchored embed announcing each reply.
  const msgHash = messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0)
  useEffect(() => {
    if (startAtBottom) return
    if (msgHash === lastHashRef.current) return
    lastHashRef.current = msgHash
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
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

  /** `override` carries the text a follow-up chip's send arrow supplies (double-click
   *  or the send segment); without it the draft is the source of truth. Every call
   *  site wraps this in an arrow, so a click event can never arrive here as the
   *  override — mirrors SideChat's send(). Guarded on `sendMutation.isPending` so a
   *  chip's send arrow (unlike the composer's own Send button) can't fire a second
   *  turn before the first settles. Only a composer submit owns the composer's
   *  text — an override send carries its own text, so clearing the draft here would
   *  throw away a draft the user has not sent yet. */
  const send = useCallback((override?: string) => {
    const msg = (override ?? draft).trim()
    if (!msg || sendMutation.isPending) return
    if (override == null) setDraft('')
    sendMutation.mutate(msg)
  }, [draft, setDraft, sendMutation])

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

  // mutateAsync, not mutate: the returned promise carries a failed POST to the
  // approval row's rollback (CollapsibleToolGroup.submitDecision catches it and
  // restores the buttons). mutate() returns void, so a failed POST would leave
  // the row optimistically resolved while the agent stays parked on the
  // undelivered decision, with no retry path.
  const approve = useCallback(
    (approvalId: string, decision: string) => approveMutation.mutateAsync({ id: approvalId, decision }),
    [approveMutation],
  )

  // Batch resolver (Req 4.1-4.4): apply one decision to every pending approval
  // in a group. Each id goes through the SAME slot-scoped approve endpoint the
  // single path uses (POST /api/chat/slots/{slot}/approve with request_id) —
  // Task 4 mandates the slot-scoped path for batches, never the bare id-scoped
  // one-shot resolve (which matches slot futures by bare id with no session
  // check). Uses allSettled, NOT a fail-fast loop: a call whose verdict changed
  // between surfacing and resume (Req 4.3-4.4) is surfaced as an excluded
  // rejection instead of aborting the batch with earlier ids already approved.
  // Rejects (so the row rolls back) only if EVERY call failed; a partial
  // success settles as resolved and refetch reconciles the still-pending rows.
  const approveBatch = useCallback(
    async (approvalIds: string[], decision: string) => {
      const results = await Promise.allSettled(
        approvalIds.map(id => api.post(`/api/chat/slots/${encodeURIComponent(slotKey)}/approve`, {
          action: decision,
          request_id: id,
        })),
      )
      void refetch()
      const rejected = results.filter(r => r.status === 'rejected')
      if (rejected.length === approvalIds.length) throw (rejected[0] as PromiseRejectedResult).reason
      return results
    },
    [api, slotKey, refetch],
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

      <div ref={scrollerRef} onScroll={follow.onScroll} className="flex-1 overflow-y-auto py-4 min-h-0">
        <div ref={follow.contentRef}>
        {messages.length === 0 && !running && (
          <div className="text-center text-muted text-[13px] py-10">{i18nT('appSdk.chatEmbed.session_ready_type_a_message_to_start')}</div>
        )}
        {/* canTrust: this embed's approve routes through the slot approve
            endpoint (above), which records standing trust — the one mount
            allowed to offer the tier (#5434). */}
        <ChatMessageList messages={messages} running={running} onApprove={approve} onApproveBatch={approveBatch} canTrust />
        <div ref={endRef} />
        </div>
      </div>

      {startAtBottom && (
        <div className="relative">
          <JumpToBottomButton visible={!follow.isAtBottom && messages.length > 0} onClick={follow.scrollToBottom} />
        </div>
      )}

      {aboveComposer && <div className="shrink-0">{aboveComposer}</div>}

      {followUpOptions.length > 0 && (
        <div className={`shrink-0 px-3 ${frameless ? '' : 'bg-bg-accent'}`}>
          <FollowUpBar
            options={followUpOptions}
            recommended={followUpRecommended}
            picked={picked}
            onSelect={toggleOption}
            onSend={text => send(text)}
          />
        </div>
      )}

      <div className={`flex items-center gap-2 px-3 py-2 shrink-0 ${frameless ? '' : 'border-t border-border bg-bg-accent'}`}>
        <input
          type="text"
          {...composition}
          aria-label={i18nT('appSdk.chatEmbed.chat_message')}
          className="flex-1 min-w-0 px-3 py-2 text-sm bg-bg-elevated border border-border rounded-md text-text outline-none focus-visible:border-accent transition-colors"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => submitOnEnter(e, () => send())}
          placeholder={running ? i18nT('appSdk.chatEmbed.agent_is_working') : (placeholder || i18nT('appSdk.chatEmbed.message'))}
          disabled={sendMutation.isPending}
        />
        <button
          className="p-2 rounded-md bg-accent text-accent-fg disabled:opacity-40 disabled:cursor-not-allowed hover:opacity-80 transition-opacity"
          onClick={() => send()}
          disabled={sendMutation.isPending || !draft.trim()}
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
