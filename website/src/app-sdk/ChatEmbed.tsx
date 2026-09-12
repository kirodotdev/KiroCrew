/**
 * ChatEmbed — embeddable chat widget using KiroCrew's native rendering.
 *
 * Uses ChatMessageList (shared with ChatPage) for message rendering.
 * Manages its own state via useAppApi() + React Query. No Redux dependency.
 *
 * State management: polling via useQuery refetchInterval.
 * Poll faster during streaming (1s), slower when idle (5s).
 *
 * The poll is BOUNDED (chat-core P5-e): it asks the slot-detail endpoint for
 * the newest `EMBED_PAGE_LIMIT` rows, and the transcript's "load earlier" bar
 * widens that window by a page per press, up to the handler's own ceiling. An
 * unbounded poll re-read a whole 10 MB thread every second while it ran; the
 * rows are virtualized too, so the DOM cost no longer grows with history
 * either. Incremental (since-cursor) polling is the recorded follow-up.
 *
 * Sending goes through the chat-core transport (`sendTurn`) over the app-sdk
 * wire, so the receipt contract is the shared one and the host app's
 * `allowedApiPaths` grant for `/api/chat` still gates the POST.
 */
import { useRef, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { ArrowUp, Loader2 } from 'lucide-react'
import ChatMessageList, { type VirtualTranscriptHandle } from './ChatMessageList'
import ErrorNotice from '../components/ErrorNotice'
import { JumpToBottomButton } from './ChatScrollChrome'
import FollowUpBar from '../components/FollowUpBar'
import { deriveFollowUpOptions } from './protocol'
import { useComposerDraft } from './useComposerDraft'
import { useAppApi, useAppInfo } from './index'
import { appApiSendWire } from './appSendWire'
import { sendTurn, mintSendId } from '../chat-core/transport/sendTurn'
import { mergeRecoveredDraft } from '../utils/chatDrafts'

/** ChatEmbed's own merge rule: the shared `mergeRecoveredDraft` join, with its
 *  paragraph break flattened to one space, because this composer is a
 *  single-line `<input>` and a browser silently drops newlines assigned to an
 *  input's value. Used for every restore AND for `retireSendTail`'s replay of
 *  later restores, so the two can never disagree on what the composer holds. */
const mergeIntoInputDraft = (before: string, text: string): string =>
  mergeRecoveredDraft(before, text).replace(/\s*\n+\s*/g, ' ')
import { drainEmbedRecovery, stashEmbedRecovery, subscribeEmbedRecovery, type PendingEmbedRecovery } from './embedSendRecovery'
import { retireSendTail, type SendTail } from './embedSendTails'
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

/** Each send's transcript-tail record is `SendTail` (embedSendTails.ts): the
 *  row to show and `sendId`, the client-minted id stamped on the wire
 *  (`meta.sendId`, the same convention ChatPage and ChatPane use), so proof of
 *  delivery can be recognised as THIS message's own user row appearing -- by
 *  identity ONLY, never by text, which a manual resend or a duplicate
 *  injection could share. The server keeps the id on every path a send can
 *  take: a dispatched send persists it on its row directly, and a send that
 *  was queued behind a busy slot carries it on the queue entry so the drain
 *  writes it onto the row (#8853). Records are kept per send and retired
 *  independently, so one late send's proof can never be masked by another's
 *  recovery landing after it. */

/** Whether a polled user row is THIS send's own, by identity. A plain row
 *  carries `meta.sendId`; a row the drain merged from several queued sends
 *  (`merge_queued_messages`) names every send it stands for in `meta.sendIds`,
 *  and membership there is the same proof. Anything else -- no id, another
 *  id, an older backend that drops `meta` -- is not. */
function isOwnUserRow(m: ChatMessage, sendId: string): boolean {
  if (m.role !== 'user') return false
  const meta = m.meta
  if (!meta) return false
  if (meta.sendId === sendId) return true
  const many = meta.sendIds
  return Array.isArray(many) && many.includes(sendId)
}

/** Minimal shape of the chat-slot payload consumed by this embed. */
interface ChatSlotData {
  messages?: ChatMessage[]
  running?: boolean
  title?: string
  /** Older rows exist beyond the bounded page. */
  has_more?: boolean
}

/** Rows per page of the bounded poll — the slot-detail handler's own default,
 *  so an embed asks for exactly what an unqualified read would have returned
 *  had it been bounded. Exported for tests. */
export const EMBED_PAGE_LIMIT = 200
/** The handler clamps `limit` here; a wider ask is silently this. */
export const EMBED_PAGE_LIMIT_MAX = 500

function ChatEmbed({ slotKey, agent, placeholder, frameless, startAtBottom, onSend, aboveComposer }: ChatEmbedProps) {
  const api = useAppApi()
  // The parking scope for receipts that outlive this instance: this app's
  // embeds for this slot (see embedSendRecovery).
  const appName = useAppInfo().name
  const lastHashRef = useRef('')
  // The transcript is ChatMessageList's virtualized mount: it owns the scroller
  // and, in startAtBottom mode, the stick-to-bottom follow (the same
  // FollowController semantics as ChatPane and the main chat — re-pin on growth
  // AND collapse, released only by a genuine user scroll up). A top-anchored
  // embed opens at the top and is never pinned; it keeps its own contract
  // below — a deliberate smooth scroll to each NEW MESSAGE regardless of
  // position.
  const listRef = useRef<VirtualTranscriptHandle | null>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)
  const scrollToBottom = useCallback(() => { listRef.current?.scrollToBottom() }, [])

  // The bounded window: newest `limit` rows. "Load earlier" widens it by a
  // page. The widening is remembered against the slot it was made for, so a
  // new slot is back at one page on its very first read — no effect-timed
  // reset that would let one wide read of the new slot slip out first.
  const [widened, setWidened] = useState<{ slot: string; limit: number } | null>(null)
  const limit = widened?.slot === slotKey ? widened.limit : EMBED_PAGE_LIMIT

  const { data: slotData, refetch, isPlaceholderData, isError } = useQuery({
    queryKey: ['app-sdk-embed', slotKey, limit],
    queryFn: () => api.get<ChatSlotData>(
      '/api/chat/slots/' + encodeURIComponent(slotKey) + '?limit=' + limit,
    ),
    // A wider page replaces the narrower one on arrival; until then the rows
    // already on screen stay put instead of blinking through an empty list.
    // Same slot only: a slot change must not paint the previous slot's rows
    // under the new slot's header while its first read is in flight.
    placeholderData: (prev, prevQuery) =>
      prevQuery && (prevQuery.queryKey as unknown[])[1] === slotKey ? prev : undefined,
    refetchInterval: (query) => {
      const running = query.state.data?.running ?? false
      return running ? 1000 : 5000
    },
  })

  // The last SETTLED page for this slot. A placeholder covers the in-flight
  // window of a widen, but a REJECTED widen leaves the wider key with no data
  // at all — and the transcript must not blank on a failed history fetch. The
  // settled page stays on screen and the bar shows the failure with a retry.
  const settledRef = useRef<{ slot: string; data: ChatSlotData } | null>(null)
  if (slotData && !isPlaceholderData) settledRef.current = { slot: slotKey, data: slotData }
  const settled = settledRef.current?.slot === slotKey ? settledRef.current.data : undefined
  const shown = slotData ?? settled

  const messages = shown?.messages ?? EMPTY_MESSAGES
  const running = shown?.running ?? false
  const title = shown?.title ?? ''
  // The widen failed: the wider read errored and the rows on screen are still
  // the narrower page. An ambient poll error on a settled page (nothing being
  // widened) is not the bar's to report.
  const widenFailed = isError && limit > EMBED_PAGE_LIMIT && slotData == null && settled != null
  // No page at all and the read failed: the transcript is unavailable, which is
  // not the same thing as an empty session — say so, with the retry.
  const loadFailed = isError && shown == null
  const widening = isPlaceholderData && !isError
  const canWiden = ((shown?.has_more ?? false) && limit < EMBED_PAGE_LIMIT_MAX) || widenFailed
  const widen = useCallback(() => setWidened((w) => {
    const current = w?.slot === slotKey ? w.limit : EMBED_PAGE_LIMIT
    return { slot: slotKey, limit: Math.min(current + EMBED_PAGE_LIMIT, EMBED_PAGE_LIMIT_MAX) }
  }), [slotKey])
  // Retry re-issues the read at the limit that failed rather than widening again.
  const retryWiden = useCallback(() => { void refetch() }, [refetch])

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
  const { draft, setDraft, picked, toggleOption, composition, submitOnEnter } =
    useComposerDraft({ followUpOptions })

  /** What recent sends left behind on the transcript's tail, if anything:
   *  an `error` row for a send that never went out (the same role ChatPage and
   *  ChatPane append into their slot), or a `notice` row for one whose
   *  delivery could not be confirmed. This embed owns no store, and the slot
   *  detail it polls has no record of a message the server never took, so the
   *  rows live here -- and a receipt that lands after this instance unmounted
   *  is parked by slot (`embedSendRecovery`) for the next instance to show.
   *  One record PER SEND, oldest first: a drain can apply several at once, and
   *  each must keep its own id so its own proof of delivery can retire it
   *  (below). Cleared when the next COMPOSER submit starts -- that submit
   *  consumes the draft the tails' restores wrote into, so the user has
   *  decided what to do with the handed-back text. An override send (a
   *  follow-up chip's own text) consumes nothing: its tails stay, so a late
   *  send's proof can still retire its notice and take the restored copy
   *  back instead of stranding it in the composer for a duplicate resend. */
  const [sendTails, setSendTails] = useState<SendTail[]>([])
  const shownMessages = useMemo<ChatMessage[]>(
    () => sendTails.length === 0 ? messages : [...messages, ...sendTails.map(t => ({ role: t.role, content: t.content, cls: '' }))],
    [messages, sendTails],
  )
  /** Latest rendered draft, for the async send path to read at settle time
   *  instead of a value closed over at render. */
  const draftRef = useRef(draft)
  draftRef.current = draft
  // An "unconfirmed" notice answers the question "did it go out?". Proof is a
  // USER row for THIS send anywhere in the polled window, recognised by
  // identity ONLY: a row carrying this send's `meta.sendId` (or naming it in
  // a merged row's `meta.sendIds`). No positional gate: the poll is BOUNDED
  // (newest `EMBED_PAGE_LIMIT` rows, widened by "load earlier"), so the
  // window's length neither grows with a delivered send once the transcript
  // is past the limit nor stays put when a page is prepended -- only the id
  // is a stable signal, and it is sufficient because nothing else can carry
  // it. The server keeps the id on both paths a send can take -- persisted
  // directly on a dispatched send's row, carried through the queue entry onto the drained
  // row when the slot was busy (#8853) -- so a same-text resend or injection
  // can never be mistaken for it, and there is no text-matching fallback to
  // false-retire on. Only that proof retires the notice and takes back the
  // text it handed back -- and only if the composer still holds exactly what
  // the restore produced; anything typed or edited since stays. Transcript
  // growth from anything else (a prior turn still streaming, a cron or
  // sub-agent injecting text into the slot, a same-text row under another id,
  // an older backend that drops `meta`) is NOT proof: the send may still be
  // undelivered, so the notice and the restored text both stay -- withdrawing
  // the text on unproven delivery would re-create the silent loss this surface
  // is being fixed for. An `error` tail is never retired here: nothing
  // arriving later makes a refused send less refused. Each notice is judged
  // against ITS OWN id and retired on its own -- two late sends drained
  // together stay two records, so proof of the first cannot be masked by the
  // second's recovery having landed after it, and taking the first's text back
  // rebuilds the composer around the second's (`retireSendTail`).
  useEffect(() => {
    const proven = sendTails.filter(t =>
      t.role === 'notice'
      && messages.some(m => isOwnUserRow(m, t.sendId)))
    if (proven.length === 0) return
    let tails = sendTails
    let draft = draftRef.current
    for (const t of proven) {
      const r = retireSendTail(tails, t.sendId, draft, mergeIntoInputDraft)
      tails = r.tails
      if (r.draft != null) draft = r.draft
    }
    setSendTails(tails)
    if (draft !== draftRef.current) {
      draftRef.current = draft
      setDraft(draft)
    }
  }, [messages, sendTails, setDraft])

  // startAtBottom follow is owned by the virtualizer behind ChatMessageList.
  // Non-startAtBottom embeds keep the message-arrival smooth scroll: it fires
  // on NEW MESSAGES only (not on content growth) and deliberately scrolls
  // regardless of position — a top-anchored embed announcing each reply.
  // Keyed on the TAIL row's identity, not the row count: a "load earlier"
  // press prepends a page, which must not read as a new reply and yank the
  // reader away from the history they just asked for. The send-tail rows (a
  // failed or unconfirmed send, above) count as new messages here: they are
  // the one row that must never land below the fold unannounced, or a failed
  // send looks sent again.
  const tail = messages[messages.length - 1]
  const msgHash = `${(tail?.meta?.mid as string | undefined) ?? tail?.ts ?? ''}:${tail?.content?.length ?? 0}`
    + ':' + sendTails.map(t => t.role + t.content.length).join(',')
  useEffect(() => {
    if (startAtBottom) return
    if (msgHash === lastHashRef.current) return
    lastHashRef.current = msgHash
    listRef.current?.scrollToBottom('smooth')
  }, [msgHash, startAtBottom])

  /** Hand an undelivered message back to the composer. The user may have typed
   *  more since the send cleared it; `mergeRecoveredDraft` owns the rule for
   *  keeping both, for every recovery site in the app. Its join is a paragraph
   *  break, which suits every other composer -- but THIS one is a single-line
   *  `<input>`, and a browser silently drops newlines assigned to an input's
   *  value, gluing "first" and "second" into "firstsecond". So the break is
   *  flattened to one space before it reaches the field. Returns the composer
   *  value before and after, so an "unconfirmed" restore can be undone later
   *  if delivery is proven and the user has not touched it since. */
  const restoreIntoComposer = useCallback((text: string): { before: string; after: string } => {
    const before = draftRef.current
    const after = mergeIntoInputDraft(before, text)
    // Advance the ref eagerly: two restores in one tick (a drain of several
    // parked receipts) must each merge on top of the previous one, and the
    // render that would refresh the ref has not happened yet.
    draftRef.current = after
    setDraft(after)
    return { before, after }
  }, [setDraft])

  /** Show a receipt's outcome on THIS embed: its own tail record appended
   *  (never replacing another send's), and the handed-back text merged into
   *  the composer (with the before/after an unconfirmed restore needs so
   *  proven delivery can take it back). One function for the live path and
   *  for a receipt drained after a remount, so the two cannot render the same
   *  outcome differently. */
  const applyRecovery = useCallback((rec: PendingEmbedRecovery) => {
    const restore = rec.restoreText == null ? undefined : { text: rec.restoreText, ...restoreIntoComposer(rec.restoreText) }
    setSendTails(prev => [...prev.filter(t => t.sendId !== rec.tail.sendId), { ...rec.tail, restore }])
  }, [restoreIntoComposer])

  /** Whether this instance is still mounted, and which slot it currently
   *  shows. A receipt is asynchronous: by the time it lands the host may have
   *  unmounted the embed (switched specs, closed a drawer) or re-propped it to
   *  another slot. Writing the recovery into a dead instance would lose the
   *  only copy of a refused send -- the server never took it, so no poll will
   *  ever show it -- and drop the "unconfirmed" warning for a late one. Such a
   *  receipt is parked by slot in `embedSendRecovery` and drained by the next
   *  embed mounted for that slot (below). */
  const mountedRef = useRef(true)
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])
  useEffect(() => {
    // Drain anything a previous instance parked for this slot -- every record,
    // oldest first, so two overlapping sends that both failed each get their
    // text back -- and keep listening: a receipt can land AFTER this instance
    // mounted (the user came back within the deadline) but be addressed by the
    // instance that sent it, which by then is unmounted and can only park it.
    // The subscription is keyed to the slot it was made for, and this instance
    // can be re-propped to another slot (or unmounted) before this effect's
    // cleanup runs: `slotKeyRef` advances during render, the unsubscribe only
    // on the passive flush. A receipt landing in that gap would fire the
    // still-live listener and drain slot A's only copy into the composer now
    // showing slot B, deleting it from A. So the drain re-checks, at call
    // time, that it is still the instance for THIS slot; otherwise the record
    // stays parked for the embed that actually shows the slot.
    const drain = () => {
      if (!mountedRef.current || slotKeyRef.current !== slotKey) return
      for (const rec of drainEmbedRecovery({ app: appName, slot: slotKey })) applyRecovery(rec)
    }
    drain()
    return subscribeEmbedRecovery({ app: appName, slot: slotKey }, drain)
  }, [appName, slotKey, applyRecovery])

  const wire = useMemo(() => appApiSendWire(api, agent), [api, agent])

  // Receipt semantics live in the chat-core transport: `sendTurn` owns the
  // abort deadline and the shared classification, reached here through the
  // app-sdk wire so the host app's `allowedApiPaths` grant still gates the
  // POST. This embed only decides how to REACT per status:
  // - `refused` / `transport-error`: nothing was sent. Say so on the
  //   transcript and hand the text back. The old path reported neither -- it
  //   swallowed the SSE stream's parse error as success and never consumed
  //   `sendMutation.isError`, so a failed send looked sent and the text was
  //   gone. (With the app-sdk wire, a rejected fetch is reported as
  //   `response-late`, not `transport-error`: one POST with no response cannot
  //   tell "never left" from "accepted, then the connection dropped", and the
  //   second means a resend runs the turn twice. The branch stays for the
  //   contract's sake; the wire does not currently produce it.)
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
    mutationFn: async ({ msg, draftText, override, sendId, slot }: { msg: string; draftText: string; override: boolean; sendId: string; slot: string }) => {
      // Where the outcome goes: onto this embed if it is still mounted and still
      // showing the slot the send belonged to; otherwise parked by that slot for
      // the next embed that shows it. Only a composer submit has text to hand
      // back -- a chip send never consumed the draft.
      const deliver = (tail: PendingEmbedRecovery['tail']) => {
        const rec: PendingEmbedRecovery = { tail, restoreText: override ? undefined : draftText }
        if (mountedRef.current && slotKeyRef.current === slot) applyRecovery(rec)
        else stashEmbedRecovery({ app: appName, slot }, rec)
      }
      const fail = (reason?: string) => {
        // FRAMED, not bare: a raw backend reason ("slot agent mismatch") reads
        // as the agent erroring mid-work, not as "your request never went
        // out" -- and this surface has no optimistic bubble to anchor it.
        // Same core-owned framing keys ChatPane uses: when the send consumed
        // the draft, the row also says the text is back in the composer (the
        // `_restored` variant) -- a pre-filled composer with no word about it
        // reads as a guess ("I didn't type it, it was just there"); a chip send
        // restored nothing, so its row does not claim to. Without a reason the
        // only remaining cause is the transport itself (the wire names every
        // server-side refusal), so that row states the cause too instead of a
        // bare "Send failed".
        deliver({
          role: 'error',
          content: (reason
            ? (override
              ? i18nT('pages.chatPage.send_failed_with_error', { error: reason })
              // The `_restored` template supplies its own sentence end, and a
              // reason that is itself a sentence (the permission denial's
              // human copy) would otherwise close twice ("messages.. Your").
              : i18nT('pages.chatPage.send_failed_with_error_restored', { error: reason.replace(/[.。।]+$/u, '') }))
            : i18nT('pages.chatPage.send_failed_connection')) as string,
          sendId,
        })
      }
      const unconfirmed = () => {
        deliver({
          role: 'notice',
          content: i18nT(override ? 'pages.chatPage.delivery_unconfirmed_option' : 'pages.chatPage.delivery_unconfirmed') as string,
          sendId,
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
      const receipt = await sendTurn({ message: msg, slot, meta: { sendId }, wire })
      // `transport-error` is not handled: the app-sdk wire never yields it (a raw
      // rejection is indeterminate -> `response-late`), so a clause for it here
      // would be dead code kept for symmetry.
      if (receipt.status === 'refused') fail(receipt.reason)
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
   *  throw away a draft the user has not sent yet — and, for the same reason, only
   *  a composer submit clears the send tails: their restores live in that draft,
   *  and an override send that wiped them would leave a late-accepted send's
   *  restored text with no proof path left to retire it. */
  const send = useCallback((override?: string) => {
    const draftText = override ?? draft
    const msg = draftText.trim()
    if (!msg || sendMutation.isPending) return
    if (override == null) {
      setSendTails([])
      setDraft('')
    }
    // A fresh id and the slot the send belongs to: the yardstick for "did the
    // poll show THIS send", and the address a receipt is delivered to if this
    // embed is gone by then.
    sendMutation.mutate({ msg, draftText, override: override != null, sendId: mintSendId(), slot: slotKey })
  }, [draft, setDraft, sendMutation, slotKey])

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

      {/* canTrust: this embed's approve routes through the slot approve
          endpoint (above), which records standing trust — the one mount
          allowed to offer the tier (#5434). */}
      <ChatMessageList
        ref={listRef}
        messages={shownMessages}
        running={running}
        onApprove={approve}
        onApproveBatch={approveBatch}
        canTrust
        transcript={{
          sessionId: `embed:${slotKey}`,
          followOutput: !!startAtBottom,
          initialPlacement: startAtBottom ? 'bottom' : 'top',
          onAtBottomChange: setIsAtBottom,
          scrollerStyle: { paddingTop: 16, paddingBottom: 16, minHeight: 0 },
          // No hand-off: the embed's composer draft is unsaved local state that
          // the hand-off's navigation to the main chat would discard.
          earlier: { hasMore: canWiden, loading: widening, failed: widenFailed, onLoad: widenFailed ? retryWiden : widen, handOff: false },
          aboveRows: loadFailed ? (
            <div className="mx-4 my-3 flex items-start gap-2">
              <ErrorNotice className="flex-1" testId="chat-embed-load-error" message={i18nT('components.chatPane.history_load_failed')} />
              <button
                type="button"
                className="text-[12px] text-accent underline bg-transparent border-none cursor-pointer hover:text-accent-hover"
                onClick={() => { void refetch() }}
              >
                {i18nT('components.chatPane.retry')}
              </button>
            </div>
          ) : messages.length === 0 && !running ? (
            <div className="text-center text-muted text-[13px] py-10">{i18nT('appSdk.chatEmbed.session_ready_type_a_message_to_start')}</div>
          ) : undefined,
        }}
      />
      {/* The send-tail row is announced visually by the new-message scroll;
          a screen-reader user would otherwise only notice the button
          re-enabling. A polite live region carries the same text. */}
      {/* Announce the NEWEST outcome only: re-reading every live tail would
          replay the first failure each time a second one lands. */}
      <div role="status" aria-live="polite" className="sr-only">{sendTails.length ? sendTails[sendTails.length - 1].content : ''}</div>

      {startAtBottom && (
        <div className="relative">
          <JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={scrollToBottom} />
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
