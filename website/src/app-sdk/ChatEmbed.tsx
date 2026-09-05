/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Transcript and send state live in useAppApi() + React Query; the composer
 * subtree (ChatInput) reads slot state from Redux, so a host mounts this under
 * the dashboard store as every in-tree app already does.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 *
 * Sending goes through the chat-core transport (`sendTurn`) over the app-sdk
 * wire, so the receipt contract is the shared one and the host app's
 * `allowedApiPaths` grant for `/api/chat` still gates the POST.
 *
 * The composer is the REAL native ChatInput (the same one ChatPage, ChatPane
 * and SideChat render), mounted inside a SlotProvider for the embedded slot
 * with its FAIL-CLOSED `embedded` flag: every capability that defaults on
 * for a first-class composer (typed command menus, prompt optimizer,
 * slot-approval chrome) is forced off, and the opt-in chrome (upload, voice,
 * agent/model/project) is simply not passed. A capability added to ChatInput
 * later must consult the flag before it defaults on, so nothing lights up
 * inside an app embed by convention.
 *
 * Why the flag exists, and the question to ask before granting any capability
 * prop here, is stated ONCE on `ChatInputProps.embedded` -- read it there.
 * `ChatEmbed.noDashboardClient.test.tsx` pins the invariant it protects.
 */
import { useRef, useState, useCallback, useEffect, useMemo, type ReactNode } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import ChatMessageList from './ChatMessageList'
import { useChatScrollFollow } from './useChatScrollFollow'
import { JumpToBottomButton } from './ChatScrollChrome'
import FollowUpBar from '../components/FollowUpBar'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { useChatConfig } from '../hooks/useChatConfig'
import { deriveFollowUpOptions } from './protocol'
import { useComposerDraft } from './useComposerDraft'
import { useAppApi } from './index'
import { appApiSendWire } from './appSendWire'
import { sendTurn, mintSendId } from '../chat-core/transport/sendTurn'
import { mergeRecoveredDraft } from '../utils/chatDrafts'
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

/** What the last send left behind on the transcript's tail. `seenCount` is the
 *  transcript length when the send STARTED and `sendId` the client-minted id
 *  stamped on the wire (`meta.sendId`, the same convention ChatPage and
 *  ChatPane use), so proof of delivery can be recognised as THIS message's own
 *  user row appearing past that point -- by identity, never by text, which a
 *  manual resend or a duplicate injection could share; `undo` is the composer
 *  before/after an unconfirmed restore, so that proof can take the text back
 *  out. */
interface SendTail {
  role: 'error' | 'notice'
  content: string
  seenCount: number
  sendId: string
  undo?: { before: string; after: string }
}

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
  const { followUpOptions } = useMemo(
    () => deriveFollowUpOptions(messages, running),
    [messages, running]
  )

  /** The composer's draft behaviour, owned by the chat SDK rather than by this file —
   *  see useComposerDraft's own docs. Picking a follow-up option edits the draft
   *  (matching every other surface) instead of sending immediately. */
  const { draft, setDraft, picked, toggleOption } =
    useComposerDraft({ followUpOptions })

  /** What the last send left behind on the transcript's tail, if anything:
   *  an `error` row for a send that never went out (the same role ChatPage and
   *  ChatPane append into their slot), or a `notice` row for one whose
   *  delivery could not be confirmed. This embed owns no store, and the slot
   *  detail it polls has no record of a message the server never took, so the
   *  row lives here. Cleared when the next send starts. */
  const [sendTail, setSendTail] = useState<SendTail | null>(null)
  const shownMessages = useMemo<ChatMessage[]>(
    () => sendTail == null ? messages : [...messages, { role: sendTail.role, content: sendTail.content, cls: '' }],
    [messages, sendTail],
  )
  /** Latest rendered transcript length and draft, for the async send path to
   *  read at settle time instead of a value closed over at render. */
  const messageCountRef = useRef(0)
  messageCountRef.current = messages.length
  const draftRef = useRef(draft)
  draftRef.current = draft
  // An "unconfirmed" notice answers the question "did it go out?". Proof is a
  // USER row whose `meta.sendId` is THIS send's id, appended past the length
  // the transcript had WHEN THE SEND STARTED (not when the deadline fired --
  // by then the poll may already have shown it). Identity, not text: a user
  // row with the same words could be a manual resend or a duplicate
  // injection, and retiring on it would withdraw the wrong send's text. Only
  // that proof retires the notice and takes back the text it handed back --
  // and only if the composer still holds exactly what the restore produced;
  // anything typed or edited since stays. Transcript growth from anything
  // else (a prior turn still streaming, a cron or sub-agent injecting into
  // the slot, an older backend that drops meta) is NOT proof: the send may
  // still be undelivered, so the notice and the restored text both stay --
  // withdrawing the text on unproven delivery would re-create the silent loss
  // this surface is being fixed for. An `error` tail is never retired here:
  // nothing arriving later makes a refused send less refused.
  useEffect(() => {
    if (sendTail?.role !== 'notice' || messages.length <= sendTail.seenCount) return
    const landed = messages.slice(sendTail.seenCount).some(m => m.role === 'user' && m.meta?.sendId === sendTail.sendId)
    if (!landed) return
    setSendTail(null)
    const undo = sendTail.undo
    if (undo) setDraft(prev => (prev === undo.after ? undo.before : prev))
  }, [messages, sendTail, setDraft])

  // startAtBottom follow is owned by useChatScrollFollow (attached below).
  // Non-startAtBottom embeds keep the message-arrival smooth scroll: it fires
  // on NEW MESSAGES only (not on content growth) and deliberately scrolls
  // regardless of position — a top-anchored embed announcing each reply. The
  // send-tail row (a failed or unconfirmed send, below) counts as a new
  // message here: it is the one row that must never land below the fold
  // unannounced, or a failed send looks sent again.
  const msgHash = messages.length + ':' + (messages[messages.length - 1]?.content?.length || 0)
    + ':' + (sendTail ? sendTail.role + sendTail.content.length : '')
  useEffect(() => {
    if (startAtBottom) return
    if (msgHash === lastHashRef.current) return
    lastHashRef.current = msgHash
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [msgHash, startAtBottom])

  /** Hand an undelivered message back to the composer. The user may have typed
   *  more since the send cleared it; `mergeRecoveredDraft` owns the rule for
   *  keeping both, for every recovery site in the app -- its paragraph-break
   *  join renders as such because the composer is ChatInput's textarea, the
   *  same field every other recovery site writes into. Returns the composer
   *  value before and after, so an "unconfirmed" restore can be undone later
   *  if delivery is proven and the user has not touched it since. */
  const restoreIntoComposer = useCallback((text: string): { before: string; after: string } => {
    const before = draftRef.current
    const after = mergeRecoveredDraft(before, text)
    setDraft(after)
    return { before, after }
  }, [setDraft])

  const wire = useMemo(() => appApiSendWire(api, agent), [api, agent])

  /** The user's chat settings (send key), the same live setting the main
   *  composer reads; local settings only, no fetch. */
  const chatConfig = useChatConfig()

  // Receipt semantics live in the chat-core transport: `sendTurn` owns the
  // abort deadline and the shared classification, reached here through the
  // app-sdk wire so the host app's `allowedApiPaths` grant still gates the
  // POST. This embed only decides how to REACT per status:
  // - `refused` / `transport-error`: nothing was sent. Say so on the
  //   transcript and hand the text back. The old path reported neither -- it
  //   swallowed the SSE stream's parse error as success and never consumed
  //   `sendMutation.isError`, so a failed send looked sent and the text was
  //   gone.
  // - `response-late`: the deadline fired with no receipt either way. ChatPane
  //   leaves this alone because its optimistic bubble is still on screen; this
  //   embed keeps no bubble and the composer has already cleared, so the
  //   user's text has NO visible copy left -- the case the transport contract
  //   names as the one a caller may recover. Hand the text back and say the
  //   delivery is unconfirmed (a notice, not a failure: the turn may well be
  //   running, and the poll will show it if so).
  // - `unknown`: a 2xx was received -- the server has the message and only
  //   the receipt was mangled. Restoring would invite a duplicate; do nothing.
  // - `dispatched` / `queued`: the server has the message. The poll below
  //   renders it -- this embed keeps no optimistic bubble to confirm.
  // A host-supplied `onSend` keeps its own endpoint and its rejection proves
  // nothing about delivery (a host may have posted and lost the answer), so
  // it is treated like `response-late`: unconfirmed, text handed back, never
  // reported as a failure.
  //
  // Only a composer submit has anything to hand back: an option send never
  // consumed the draft, so restoring the option label would CLOBBER a draft
  // the user can re-click the chip for any time (the same gate ChatPane and
  // ChatPage keep). The unconfirmed notice says so in its own words for a
  // chip send -- "re-pick the option" -- instead of claiming a restore that
  // did not happen.
  const sendMutation = useMutation({
    // `msg` is the trimmed wire text; `draftText` is what the composer held,
    // whitespace and all -- a recovery must give back what the user typed, not
    // what the wire carried.
    mutationFn: async ({ msg, draftText, override, seenCount, sendId }: { msg: string; draftText: string; override: boolean; seenCount: number; sendId: string }) => {
      const fail = (reason?: string) => {
        // FRAMED, not bare: a raw backend reason ("slot agent mismatch") reads
        // as the agent erroring mid-work, not as "your request never went
        // out" -- and this surface has no optimistic bubble to anchor it.
        // Same core-owned framing key App.tsx's feedback send uses. Without a
        // reason the only remaining cause is the transport itself (the wire
        // names every server-side refusal), so that row states the cause too
        // instead of a bare "Send failed".
        setSendTail({
          role: 'error',
          content: (reason
            ? i18nT('pages.chatPage.send_failed_with_error', { error: reason })
            : i18nT('pages.chatPage.send_failed_connection')) as string,
          seenCount,
          sendId,
        })
        if (!override) restoreIntoComposer(draftText)
      }
      const unconfirmed = () => {
        const undo = override ? undefined : restoreIntoComposer(draftText)
        setSendTail({
          role: 'notice',
          content: i18nT(override ? 'pages.chatPage.delivery_unconfirmed_option' : 'pages.chatPage.delivery_unconfirmed') as string,
          seenCount,
          sendId,
          undo,
        })
      }
      if (onSend) {
        try {
          await onSend(msg)
        } catch {
          unconfirmed()
        }
        return
      }
      const receipt = await sendTurn({ message: msg, slot: slotKey, meta: { sendId }, wire })
      if (receipt.status === 'refused' || receipt.status === 'transport-error') fail(receipt.reason)
      if (receipt.status === 'response-late') unconfirmed()
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
    const draftText = override ?? draft
    const msg = draftText.trim()
    if (!msg || sendMutation.isPending) return
    setSendTail(null)
    if (override == null) setDraft('')
    // Transcript length NOW and a fresh id: together the yardstick for "did the
    // poll show THIS send".
    sendMutation.mutate({ msg, draftText, override: override != null, seenCount: messageCountRef.current, sendId: mintSendId() })
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
        <ChatMessageList messages={shownMessages} running={running} onApprove={approve} onApproveBatch={approveBatch} canTrust />
        {/* The send-tail row is announced visually by the new-message scroll;
            a screen-reader user would otherwise only notice the button
            re-enabling. A polite live region carries the same text. */}
        <div role="status" aria-live="polite" className="sr-only">{sendTail?.content ?? ''}</div>
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
            picked={picked}
            onSelect={toggleOption}
            onSend={text => send(text)}
          />
        </div>
      )}

      {/* The real composer under the fail-closed `embedded` flag (see the
          file header). No onStop/onSteer: this embed must not stop or steer
          the slot's turn, so while the agent runs the plain Send stays and a
          send simply queues server-side (the same `queued` receipt as before
          the swap). `sending`, not `disabled`, while a POST is in flight:
          the button acknowledges the click with a spinner and refuses a
          second fire, while the field stays live (ChatInput's `disabled`
          would announce "Stopping..."). The user's send-key setting is the
          same one the main composer honours -- read from local settings,
          not fetched, so the no-dashboard-client invariant holds. */}
      <div className={`shrink-0 ${frameless ? '' : 'border-t border-border bg-bg-accent'}`}>
        <SlotProvider slotId={slotKey}>
          <ChatInput
            embedded
            value={draft}
            onChange={setDraft}
            onSend={() => send()}
            sending={sendMutation.isPending}
            sendOnEnter={chatConfig.sendOnEnter}
            isRunning={running}
            placeholder={running ? i18nT('appSdk.chatEmbed.agent_is_working') : (placeholder || i18nT('appSdk.chatEmbed.message'))}
            inputAriaLabel={i18nT('appSdk.chatEmbed.chat_message')}
          />
        </SlotProvider>
      </div>
    </div>
  )
}

export default ChatEmbed
